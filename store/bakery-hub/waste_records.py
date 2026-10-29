"""Admin-only actual production and disposal records, independent of plans/sales."""

import logging
import re

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import Product, RegisterProduct, WasteRecord, db
from production_prep import parse_business_date


logger = logging.getLogger(__name__)
MAX_QUANTITY = 100000
PRODUCT_KEY = re.compile(r"(register|legacy):([1-9]\d{0,10})\Z")


def optional_quantity(value):
    if value == "":
        return None
    if not re.fullmatch(r"(?:0|[1-9]\d{0,8})", value):
        raise ValueError("個数は0以上の整数で入力してください。")
    number = int(value)
    if number > MAX_QUANTITY:
        raise ValueError(f"個数は{MAX_QUANTITY}個以下で入力してください。")
    return number


def create_waste_blueprint(*, access_required, resolve_dataset, business_today):
    blueprint = Blueprint("waste_records", __name__)

    def admin_dataset():
        current = resolve_dataset()
        if current.kind != "admin":
            abort(403)
        return current

    def product_for(current, key):
        match = PRODUCT_KEY.fullmatch(key)
        if match is None:
            abort(404)
        kind, identifier = match[1], int(match[2])
        if kind == "register":
            item = RegisterProduct.query.filter_by(dataset_id=current.id, id=identifier).one_or_none()
            if item is None:
                abort(404)
            return "register_product_id", item.id, f"継続商品｜{item.name}"
        item = Product.query.filter_by(dataset_id=current.id, id=identifier).one_or_none()
        if item is None:
            abort(404)
        return "legacy_product_id", item.id, f"旧商品｜{item.year}年{item.month}月｜{item.name}"

    def show(current, day, *, error=None, status=200, draft=None, result=None,
             overage=False):
        try:
            registered = RegisterProduct.query.filter_by(dataset_id=current.id).order_by(
                RegisterProduct.is_active.desc(), RegisterProduct.id).all()
            legacy = Product.query.filter_by(dataset_id=current.id).order_by(
                Product.year.desc(), Product.month.desc(), Product.id).all()
            records = WasteRecord.query.filter_by(dataset_id=current.id, business_date=day).order_by(
                WasteRecord.id).all()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to read waste records")
            return "製造実績・廃棄ロスを表示できませんでした。", 500
        keys = {f"register:{row.register_product_id}" if row.register_product_id is not None
                else f"legacy:{row.legacy_product_id}" for row in records}
        new_draft = {} if draft and draft.get("product_key") in keys else (draft or {})
        return render_template("waste_records.html", day=day, registered=registered,
                               legacy=legacy, records=records, draft=draft or {}, new_draft=new_draft,
                               error=error, result=result, overage=overage,
                               max_quantity=MAX_QUANTITY), status

    def return_to_record(day, record_id, message):
        session["waste_record_result"] = {"day": day.isoformat(), "id": record_id,
                                          "message": message}
        return redirect(url_for(".index", date=day.isoformat(),
                                _anchor=f"waste-record-{record_id}"), code=303)

    @blueprint.get("/prep/waste")
    @access_required
    def index():
        current = admin_dataset()
        try:
            day = parse_business_date(request.args["date"]) if "date" in request.args else business_today()
        except ValueError as error:
            return str(error), 400
        result = session.pop("waste_record_result", None)
        if not isinstance(result, dict) or result.get("day") != day.isoformat():
            result = None
        return show(current, day, result=result)

    @blueprint.post("/prep/waste")
    @access_required
    def save():
        current = admin_dataset()
        try:
            day = parse_business_date(request.form.get("business_date"))
        except ValueError as error:
            return str(error), 400
        key = request.form.get("product_key", "")
        field, identifier, label = product_for(current, key)
        draft = {"product_key": key,
                 "made_quantity": request.form.get("made_quantity", ""),
                 "discarded_quantity": request.form.get("discarded_quantity", ""),
                 "note": request.form.get("note", "")}
        try:
            made = optional_quantity(draft["made_quantity"])
            discarded = optional_quantity(draft["discarded_quantity"])
            if made is None and discarded is None:
                raise ValueError("実製造数か廃棄数の少なくとも一方を入力してください。")
            if len(draft["note"]) > 500:
                raise ValueError("メモは500文字以内で入力してください。")
        except ValueError as error:
            return show(current, day, error=str(error), status=400, draft=draft)

        try:
            # The Dataset row serializes competing creates on PostgreSQL. The
            # unique key remains the final defense on both PostgreSQL/SQLite.
            db.session.query(type(current)).filter_by(id=current.id).with_for_update().one()
            filters = {"dataset_id": current.id, "business_date": day, field: identifier}
            record = WasteRecord.query.filter_by(**filters).with_for_update().one_or_none()
            if record is not None and (record.made_quantity, record.discarded_quantity, record.note) == (
                    made, discarded, draft["note"]):
                record_id = record.id
                db.session.rollback()  # Release the Dataset lock without writing or touching updated_at.
                return return_to_record(day, record_id, "変更はありません。保存済みの内容を表示しています。")
            if made is not None and discarded is not None and discarded > made and (
                    request.form.get("overage_confirm") != "confirmed"):
                db.session.rollback()
                return show(current, day, error="廃棄数が実製造数を超えています。持ち越し等の理由を確認し、確認欄を選んでください。",
                            status=400, draft=draft, overage=True)
            if record is None:
                record = WasteRecord(**filters, product_label=label)
                db.session.add(record)
            record.made_quantity = made
            record.discarded_quantity = discarded
            record.note = draft["note"]
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return show(current, day, error="同じ商品・日付の記録が先に保存されました。再読み込みして確認してください。",
                        status=409, draft=draft)
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save waste record")
            return show(current, day, error="保存できませんでした。再度お試しください。",
                        status=500, draft=draft)

        return return_to_record(day, record.id, "製造実績・廃棄数を保存しました。")

    return blueprint
