"""Admin-only staff directory and planned shifts. No attendance is recorded here."""
import calendar
import datetime
import logging
import re
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import ShopShift, ShopStaff, db
from shop_schedules import JST, as_jst, parse_day

logger = logging.getLogger(__name__)
ROLES = {
    "production": "製造",
    "sales": "販売",
    "both": "兼任",
    "other": "その他",
}
STATUS_LABELS = {"work": "出勤予定", "all_day": "終日（時刻未指定）", "off": "休み"}
TIME_RE = re.compile(r"[0-9]{2}:[0-9]{2}\Z")
MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}\Z")
TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
MAX_SHIFT = datetime.timedelta(hours=12)


def planned_work_for_day(dataset, day):
    """Phase 3用。認可済みAdmin DatasetのJST業務日だけを取得する。"""
    if dataset.kind != "admin" or dataset.system_key != "admin":
        raise ValueError("Admin Datasetが必要です。")
    return (
        ShopShift.query.filter(
            ShopShift.dataset_id == dataset.id, ShopShift.business_date == day,
            ShopShift.status.in_(("work", "all_day")),
        )
        .order_by(ShopShift.starts_at, ShopShift.id).all()
    )


def parse_month(value):
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise ValueError("月を確認してください。")
    try:
        day = parse_day(value + "-01")
    except ValueError as exc:
        raise ValueError("月を確認してください。") from exc
    return day


def parse_clock(value):
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValueError("時刻は時:分で入力してください。")
    try:
        return datetime.time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("存在する時刻を入力してください。") from exc


def form_datetime(day, clock):
    return datetime.datetime.combine(day, clock, JST).astimezone(datetime.timezone.utc)


def shift_payload(values, row=None):
    day = parse_day(values["business_date"])
    status = values["status"]
    if status not in STATUS_LABELS:
        raise ValueError("勤務予定の区分を選んでください。")
    note = values["note"].strip()
    if len(note) > 500:
        raise ValueError("備考は500文字以内で入力してください。")
    if status in ("off", "all_day"):
        if values["start_time"] or values["end_time"] or values["end_next_day"]:
            raise ValueError("休み・終日には勤務時刻を入力しないでください。")
        return day, status, None, None, note
    start = parse_clock(values["start_time"])
    end = parse_clock(values["end_time"])
    if values["end_next_day"]:
        raise ValueError("新しい予定は同じ日内で登録してください。")
    # Preserve a legacy overnight or long shift if only its note changes.
    if row is not None and row.status == "work" and row.business_date == day:
        old_start, old_end = as_jst(row.starts_at), as_jst(row.ends_at)
        if old_start.strftime("%H:%M") == values["start_time"] and old_end.strftime("%H:%M") == values["end_time"]:
            return day, status, row.starts_at, row.ends_at, note
    starts_at = form_datetime(day, start)
    ends_at = form_datetime(day, end)
    duration = ends_at - starts_at
    if not datetime.timedelta(0) < duration <= MAX_SHIFT:
        raise ValueError("終了は開始後、同じ日の12時間以内にしてください。")
    return day, status, starts_at, ends_at, note


def create_shop_staff_shifts_blueprint(*, access_required, resolve_dataset):
    bp = Blueprint("shop_staff_shifts", __name__)

    def admin_dataset():
        dataset = resolve_dataset()
        if dataset.kind != "admin" or dataset.system_key != "admin":
            abort(403)
        return dataset

    def owned_staff(staff_id, dataset_id, *, lock=False):
        query = ShopStaff.query.filter_by(id=staff_id, dataset_id=dataset_id)
        if lock:
            query = query.with_for_update()
        return query.first_or_404()

    def owned_shift(shift_id, dataset_id, *, lock=False):
        query = ShopShift.query.filter_by(id=shift_id, dataset_id=dataset_id)
        if lock:
            query = query.with_for_update()
        return query.first_or_404()

    @bp.get("/staff-shifts/staff/<int:staff_id>")
    @access_required
    def staff_detail(staff_id):
        dataset = admin_dataset()
        return render_template("shop_staff_detail.html", staff=owned_staff(staff_id, dataset.id), roles=ROLES)

    @bp.get("/staff-shifts/staff/<int:staff_id>/status")
    @access_required
    def confirm_staff_status(staff_id):
        dataset = admin_dataset()
        row = owned_staff(staff_id, dataset.id)
        target = request.args.get("is_active")
        if target not in ("0", "1") or (target == "1") == row.is_active:
            abort(400)
        return render_template("shop_staff_shift_confirm.html", kind="staff", row=row, target=target)

    @bp.get("/staff-shifts/shifts/<int:shift_id>")
    @access_required
    def shift_detail(shift_id):
        dataset = admin_dataset()
        return render_template("shop_shift_detail.html", shift=owned_shift(shift_id, dataset.id),
                               as_jst=as_jst, status_labels=STATUS_LABELS)

    @bp.get("/staff-shifts/shifts/<int:shift_id>/cancel")
    @access_required
    def confirm_cancel_shift(shift_id):
        dataset = admin_dataset()
        return render_template("shop_staff_shift_confirm.html", kind="shift",
                               row=owned_shift(shift_id, dataset.id))

    def staff_values(row=None):
        if request.method == "POST":
            return {field: request.form.get(field, "") for field in ("display_name", "role", "hired_on", "submission_key")}
        if row is None:
            return {"display_name": "", "role": "production", "hired_on": "", "submission_key": secrets.token_hex(16)}
        return {
            "display_name": row.display_name,
            "role": row.role,
            "hired_on": row.hired_on.isoformat() if row.hired_on else "",
            "submission_key": "",
        }

    def validate_staff(values, *, editing):
        name = values["display_name"].strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("表示名は1〜80文字で入力してください。")
        if values["role"] not in ROLES:
            raise ValueError("担当を選んでください。")
        hired = parse_day(values["hired_on"]) if values["hired_on"] else None
        if not editing and not TOKEN_RE.fullmatch(values["submission_key"]):
            raise ValueError("登録フォームを開き直してください。")
        return name, values["role"], hired

    @bp.get("/staff-shifts")
    @access_required
    def index():
        dataset = admin_dataset()
        today = datetime.datetime.now(JST).date()
        try:
            first = parse_month(request.args.get("month", today.strftime("%Y-%m")))
            selected = parse_day(request.args.get("date", today.isoformat() if first.month == today.month and first.year == today.year else first.isoformat()))
        except ValueError:
            abort(400)
        if (selected.year, selected.month) != (first.year, first.month):
            abort(400)
        last_day = calendar.monthrange(first.year, first.month)[1]
        last = first.replace(day=last_day)
        shifts = (
            ShopShift.query.filter(
                ShopShift.dataset_id == dataset.id,
                ShopShift.business_date >= first,
                ShopShift.business_date <= last,
            ).order_by(ShopShift.business_date, ShopShift.starts_at, ShopShift.id).all()
        )
        by_day = {}
        for shift in shifts:
            by_day.setdefault(shift.business_date, []).append(shift)
        staff = ShopStaff.query.filter_by(dataset_id=dataset.id).order_by(
            ShopStaff.is_active.desc(), ShopStaff.id
        ).all()
        prev_month = first - datetime.timedelta(days=1)
        next_month = last + datetime.timedelta(days=1)
        nearby = [selected + datetime.timedelta(days=offset) for offset in range(-3, 4)]
        return render_template(
            "shop_staff_shifts.html", first=first, selected=selected, today=today,
            weeks=calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month),
            by_day=by_day, staff=staff, roles=ROLES, status_labels=STATUS_LABELS,
            as_jst=as_jst, prev_month=prev_month, next_month=next_month,
            nearby=nearby, prev_day=selected - datetime.timedelta(days=1),
            next_day=selected + datetime.timedelta(days=1),
        )

    @bp.route("/staff-shifts/staff/new", methods=["GET", "POST"])
    @access_required
    def new_staff():
        dataset = admin_dataset()
        values = staff_values()
        error = None
        status_code = 200
        if request.method == "POST":
            try:
                name, role, hired = validate_staff(values, editing=False)
                db.session.add(ShopStaff(
                    dataset_id=dataset.id, display_name=name, role=role,
                    hired_on=hired, submission_key=values["submission_key"],
                ))
                db.session.commit()
            except ValueError as exc:
                error, status_code = str(exc), 400
            except IntegrityError:
                db.session.rollback()
                error, status_code = "この登録はすでに送信されています。一覧を確認してください。", 409
            except SQLAlchemyError:
                db.session.rollback()
                logger.exception("Failed to create staff.")
                abort(503)
            else:
                flash("スタッフを登録しました。", "success")
                return redirect(url_for(".index") + "#staff", code=303)
        return render_template("shop_staff_form.html", values=values, error=error, editing=False, roles=ROLES), status_code

    @bp.route("/staff-shifts/staff/<int:staff_id>/edit", methods=["GET", "POST"])
    @access_required
    def edit_staff(staff_id):
        dataset = admin_dataset()
        row = owned_staff(staff_id, dataset.id, lock=request.method == "POST")
        values = staff_values(row)
        error = None
        if request.method == "POST":
            try:
                name, role, hired = validate_staff(values, editing=True)
                row.display_name, row.role, row.hired_on = name, role, hired
                db.session.commit()
            except ValueError as exc:
                error = str(exc)
            except SQLAlchemyError:
                db.session.rollback()
                logger.exception("Failed to edit staff.")
                abort(503)
            else:
                flash("スタッフ情報を更新しました。", "success")
                return redirect(url_for(".staff_detail", staff_id=row.id), code=303)
        return render_template("shop_staff_form.html", values=values, error=error, editing=True, roles=ROLES, staff=row), 400 if error else 200

    @bp.post("/staff-shifts/staff/<int:staff_id>/status")
    @access_required
    def change_staff_status(staff_id):
        dataset = admin_dataset()
        row = owned_staff(staff_id, dataset.id, lock=True)
        requested = request.form.get("is_active")
        if requested not in ("0", "1"):
            abort(400)
        row.is_active = requested == "1"
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to change staff status.")
            abort(503)
        flash("在籍状態を更新しました。過去のシフトは保持します。", "success")
        return redirect(url_for(".staff_detail", staff_id=row.id), code=303)

    def shift_values(row=None):
        if request.method == "POST":
            return {field: request.form.get(field, "") for field in (
                "business_date", "staff_id", "status", "start_time", "end_time",
                "end_next_day", "note",
            )}
        if row is None:
            return {
                "business_date": request.args.get("date", datetime.datetime.now(JST).date().isoformat()),
                "staff_id": "", "status": "work", "start_time": "", "end_time": "",
                "end_next_day": "", "note": "",
            }
        start = as_jst(row.starts_at) if row.starts_at else None
        end = as_jst(row.ends_at) if row.ends_at else None
        return {
            "business_date": row.business_date.isoformat(),
            "staff_id": str(row.staff_id), "status": row.status,
            "start_time": start.strftime("%H:%M") if start else "",
            "end_time": end.strftime("%H:%M") if end else "",
            "end_next_day": "1" if end and end.date() > row.business_date else "",
            "note": row.note or "",
        }

    def save_shift(dataset, row, values):
        try:
            raw_staff_id = values["staff_id"]
            if not re.fullmatch(r"[0-9]{1,10}", raw_staff_id):
                raise ValueError("スタッフを選んでください。")
            staff = owned_staff(int(raw_staff_id), dataset.id, lock=True)
            if not staff.is_active and (row is None or row.staff_id != staff.id):
                raise ValueError("退職済みスタッフの新規シフトは登録できません。")
            day, status, starts_at, ends_at, note = shift_payload(
                values, row=row if row is not None and row.staff_id == staff.id else None
            )
            if status == "work":
                overlap = ShopShift.query.filter(
                    ShopShift.dataset_id == dataset.id,
                    ShopShift.staff_id == staff.id,
                    ShopShift.status == "work",
                    ShopShift.starts_at < ends_at,
                    ShopShift.ends_at > starts_at,
                )
                if row is not None:
                    overlap = overlap.filter(ShopShift.id != row.id)
                if overlap.first() is not None:
                    raise ValueError("このスタッフの勤務予定時刻が、別のシフトと重複しています。")
            if row is None:
                row = ShopShift(dataset_id=dataset.id)
            row.staff_id, row.business_date, row.status = staff.id, day, status
            row.starts_at, row.ends_at, row.note = starts_at, ends_at, note or None
            db.session.add(row)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            return str(exc), 400, None
        except IntegrityError:
            db.session.rollback()
            return "同じ日の予定が登録済みです。一覧から編集してください。", 409, None
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save shift.")
            abort(503)
        return None, 200, day

    @bp.route("/staff-shifts/shifts/new", methods=["GET", "POST"])
    @access_required
    def new_shift():
        dataset = admin_dataset()
        values = shift_values()
        if request.method == "GET":
            try:
                parse_day(values["business_date"])
            except ValueError:
                abort(400)
        error = None
        status_code = 200
        if request.method == "POST":
            error, status_code, day = save_shift(dataset, None, values)
            if error is None:
                flash("勤務予定を保存しました。", "success")
                return redirect(url_for(".index", month=day.strftime("%Y-%m"), date=day.isoformat()) + "#day-shifts", code=303)
        staff = ShopStaff.query.filter_by(dataset_id=dataset.id, is_active=True).order_by(ShopStaff.id).all()
        return render_template("shop_shift_form.html", values=values, error=error, editing=False, staff=staff, status_labels=STATUS_LABELS), status_code

    @bp.route("/staff-shifts/shifts/<int:shift_id>/edit", methods=["GET", "POST"])
    @access_required
    def edit_shift(shift_id):
        dataset = admin_dataset()
        row = owned_shift(shift_id, dataset.id, lock=request.method == "POST")
        original = row.business_date
        values = shift_values(row)
        error = None
        status_code = 200
        if request.method == "POST":
            error, status_code, day = save_shift(dataset, row, values)
            if error is None:
                flash("勤務予定を更新しました。", "success")
                return redirect(url_for(".index", month=original.strftime("%Y-%m"), date=original.isoformat()) + "#day-shifts", code=303)
        staff = ShopStaff.query.filter_by(dataset_id=dataset.id, is_active=True).order_by(ShopStaff.id).all()
        if row.staff not in staff:
            staff.append(row.staff)
        legacy_interval = row.status == "work" and (
            as_jst(row.ends_at).date() > row.business_date or row.ends_at - row.starts_at > MAX_SHIFT
        )
        return render_template("shop_shift_form.html", values=values, error=error, editing=True,
                               staff=staff, shift=row, legacy_interval=legacy_interval,
                               status_labels=STATUS_LABELS), status_code

    @bp.post("/staff-shifts/shifts/<int:shift_id>/cancel")
    @access_required
    def cancel_shift(shift_id):
        dataset = admin_dataset()
        row = owned_shift(shift_id, dataset.id, lock=True)
        day = row.business_date
        try:
            db.session.delete(row)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to cancel shift.")
            abort(503)
        flash("勤務予定を取り消しました。スタッフ情報は保持します。", "success")
        return redirect(url_for(".index", month=day.strftime("%Y-%m"), date=day.isoformat()) + "#day-shifts", code=303)

    return bp

