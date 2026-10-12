"""店舗全体の予定。Taskとは独立し、常に現在のDatasetへ限定する。"""
import datetime
import logging
import re
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from models import ShopSchedule, db

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
UTC = datetime.timezone.utc
LOCAL_MINUTE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\Z")
LOCAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def as_jst(value):
    """SQLiteで失われるUTCのtzinfoも補って、日本時刻へ変換する。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(JST)


def parse_local_minute(value):
    if not isinstance(value, str) or not LOCAL_MINUTE.fullmatch(value):
        raise ValueError("日時を分単位で入力してください。")
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("存在する日時を入力してください。") from exc
    if not 2000 <= parsed.year <= 2100:
        raise ValueError("年月日は2000年から2100年の範囲で入力してください。")
    return parsed.replace(tzinfo=JST).astimezone(UTC)


def parse_day(value):
    if not isinstance(value, str) or not LOCAL_DATE.fullmatch(value):
        raise ValueError("日付が正しくありません。")
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日付が正しくありません。") from exc
    if not 2000 <= day.year <= 2100:
        raise ValueError("日付が範囲外です。")
    return day


def daily_schedules(dataset_id, day):
    start = datetime.datetime.combine(day, datetime.time.min, JST).astimezone(UTC)
    end = start + datetime.timedelta(days=1)
    return ShopSchedule.query.filter(
        ShopSchedule.dataset_id == dataset_id,
        ShopSchedule.starts_at >= start,
        ShopSchedule.starts_at < end,
    ).order_by(ShopSchedule.starts_at, ShopSchedule.id).all()


def schedule_snapshot(rows):
    return [{
        "id": row.id,
        "title": row.title,
        "start": as_jst(row.starts_at).isoformat(timespec="minutes"),
        "end": as_jst(row.ends_at).isoformat(timespec="minutes") if row.ends_at else None,
        "url": url_for("shop_schedules.detail", schedule_id=row.id),
    } for row in rows]


def create_shop_schedules_blueprint(*, access_required, resolve_dataset):
    bp = Blueprint("shop_schedules", __name__)

    def owned_schedule(schedule_id, dataset_id):
        return ShopSchedule.query.filter_by(id=schedule_id, dataset_id=dataset_id).first_or_404()

    @bp.get("/schedule")
    @access_required
    def list_schedules():
        dataset = resolve_dataset()
        try:
            day = parse_day(request.args.get("date", datetime.datetime.now(JST).date().isoformat()))
        except ValueError:
            abort(400)
        return render_template("schedule_list.html", day=day, schedules=daily_schedules(dataset.id, day), as_jst=as_jst)

    @bp.get("/api/home/schedule")
    @access_required
    def today_json():
        dataset = resolve_dataset()
        day = datetime.datetime.now(JST).date()
        return jsonify({"date": day.isoformat(), "schedules": schedule_snapshot(daily_schedules(dataset.id, day))})

    @bp.get("/schedule/<int:schedule_id>")
    @access_required
    def detail(schedule_id):
        dataset = resolve_dataset()
        row = owned_schedule(schedule_id, dataset.id)
        return render_template("schedule_detail.html", schedule=row, as_jst=as_jst)

    def form_values(row=None):
        if request.method == "POST":
            return {field: request.form.get(field, "") for field in ("title", "starts_at", "ends_at", "note")}
        if row is None:
            return {"title": "", "starts_at": "", "ends_at": "", "note": ""}
        return {
            "title": row.title,
            "starts_at": as_jst(row.starts_at).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": as_jst(row.ends_at).strftime("%Y-%m-%dT%H:%M") if row.ends_at else "",
            "note": row.note or "",
        }

    def validate(values):
        title = values["title"].strip()
        note = values["note"].strip()
        if not title or len(title) > 100 or len(note) > 1000:
            raise ValueError("タイトルは1〜100文字、メモは1000文字以内で入力してください。")
        starts_at = parse_local_minute(values["starts_at"])
        ends_at = parse_local_minute(values["ends_at"]) if values["ends_at"] else None
        if ends_at is not None and ends_at <= starts_at:
            raise ValueError("終了日時は開始日時より後にしてください。")
        return title, note, starts_at, ends_at

    def save(row, values):
        try:
            title, note, starts_at, ends_at = validate(values)
        except ValueError as exc:
            return str(exc)
        row.title = title
        row.note = note or None
        row.starts_at = starts_at
        row.ends_at = ends_at
        try:
            db.session.add(row)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save a store schedule.")
            abort(503)
        return None

    @bp.route("/schedule/new", methods=["GET", "POST"])
    @access_required
    def create():
        dataset = resolve_dataset()
        values = form_values()
        error = None
        if request.method == "POST":
            row = ShopSchedule(dataset_id=dataset.id)
            error = save(row, values)
            if error is None:
                return redirect(url_for(".detail", schedule_id=row.id), code=303)
        return render_template("schedule_form.html", values=values, error=error, editing=False), 400 if error else 200

    @bp.route("/schedule/<int:schedule_id>/edit", methods=["GET", "POST"])
    @access_required
    def edit(schedule_id):
        dataset = resolve_dataset()
        row = owned_schedule(schedule_id, dataset.id)
        values = form_values(row)
        error = None
        if request.method == "POST":
            error = save(row, values)
            if error is None:
                return redirect(url_for(".detail", schedule_id=row.id), code=303)
        return render_template("schedule_form.html", values=values, error=error, editing=True, schedule=row), 400 if error else 200

    @bp.post("/schedule/<int:schedule_id>/delete")
    @access_required
    def delete(schedule_id):
        dataset = resolve_dataset()
        row = owned_schedule(schedule_id, dataset.id)
        day = as_jst(row.starts_at).date()
        try:
            db.session.delete(row)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to delete a store schedule.")
            abort(503)
        return redirect(url_for(".list_schedules", date=day.isoformat()), code=303)

    return bp
