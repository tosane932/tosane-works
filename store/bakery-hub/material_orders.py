import logging
import re

from flask import Blueprint, abort, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from models import Dataset, MaterialOrderItem, db, utc_now


logger = logging.getLogger(__name__)

MATERIAL_ORDER_ITEM_LIMIT = 100
MATERIAL_NAME_MAX_LENGTH = 100
MATERIAL_QUANTITY_TEXT_MAX_LENGTH = 30
MATERIAL_MEMO_MAX_LENGTH = 300


def phone_from_memo(memo):
    """明示的な「電話:」行の国内番号だけをtelリンクにする。"""
    if not memo:
        return None
    for line in memo.splitlines():
        matched = re.fullmatch(r"\s*電話\s*[:：]\s*([0-9\-() ]+)\s*", line)
        if not matched:
            continue
        value = matched.group(1)
        digits = re.sub(r"[\-() ]", "", value)
        if re.fullmatch(r"0[0-9]{9,10}", digits):
            return "tel:" + digits
    return None


def _normalize_optional_text(value):
    normalized_value = value.strip()
    return normalized_value or None


def _validate_create_form(form):
    raw_values = {
        "name": form.get("name", ""),
        "quantity_text": form.get("quantity_text", ""),
        "memo": form.get("memo", ""),
    }
    name = raw_values["name"].strip()
    quantity_text = _normalize_optional_text(raw_values["quantity_text"])
    memo = _normalize_optional_text(raw_values["memo"])

    if not name:
        return None, raw_values, "材料名を入力してください。"
    if len(name) > MATERIAL_NAME_MAX_LENGTH:
        return (
            None,
            raw_values,
            "材料名は100文字以内で入力してください。",
        )
    if (
        quantity_text is not None
        and len(quantity_text) > MATERIAL_QUANTITY_TEXT_MAX_LENGTH
    ):
        return (
            None,
            raw_values,
            "数量・単位は30文字以内で入力してください。",
        )
    if memo is not None and len(memo) > MATERIAL_MEMO_MAX_LENGTH:
        return None, raw_values, "メモは300文字以内で入力してください。"

    return {
        "name": name,
        "quantity_text": quantity_text,
        "memo": memo,
    }, raw_values, None


def create_material_orders_blueprint(*, access_required, resolve_dataset):
    blueprint = Blueprint("material_orders", __name__)

    def render_list(current_dataset, *, form_values=None, error=None, status=200):
        try:
            items = (
                MaterialOrderItem.query
                .filter_by(dataset_id=current_dataset.id)
                .order_by(
                    MaterialOrderItem.is_completed.asc(),
                    MaterialOrderItem.created_at.desc(),
                    MaterialOrderItem.id.desc(),
                )
                .all()
            )
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to load material order items.")
            return "材料発注リストを表示できませんでした。", 500

        incomplete_items = [item for item in items if not item.is_completed]
        completed_items = [item for item in items if item.is_completed]
        return (
            render_template(
                "material_orders.html",
                incomplete_items=incomplete_items,
                completed_items=completed_items,
                phone_links={item.id: phone_from_memo(item.memo) for item in items},
                form_values=form_values
                or {"name": "", "quantity_text": "", "memo": ""},
                error=error,
                active_tool="orders",
            ),
            status,
        )

    @blueprint.get("/material-orders")
    @access_required
    def list_items():
        current_dataset = resolve_dataset()
        return render_list(current_dataset)

    @blueprint.post("/material-orders")
    @access_required
    def create_item():
        current_dataset = resolve_dataset()
        validated_values, raw_values, validation_error = (
            _validate_create_form(request.form)
        )
        if validation_error is not None:
            return render_list(
                current_dataset,
                form_values=raw_values,
                error=validation_error,
                status=400,
            )

        try:
            locked_dataset = (
                Dataset.query
                .filter_by(
                    id=current_dataset.id,
                    kind=current_dataset.kind,
                    system_key=current_dataset.system_key,
                )
                .populate_existing()
                .with_for_update()
                .one_or_none()
            )
            if locked_dataset is None:
                db.session.rollback()
                abort(403)

            current_item_count = MaterialOrderItem.query.filter_by(
                dataset_id=locked_dataset.id,
            ).count()
            if current_item_count >= MATERIAL_ORDER_ITEM_LIMIT:
                db.session.rollback()
                return render_list(
                    current_dataset,
                    form_values=raw_values,
                    error="材料発注リストは100件まで登録できます。",
                    status=400,
                )

            db.session.add(
                MaterialOrderItem(
                    dataset_id=locked_dataset.id,
                    **validated_values,
                )
            )
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to create a material order item.")
            return render_list(
                current_dataset,
                form_values=raw_values,
                error="材料を追加できませんでした。",
                status=500,
            )

        return redirect(url_for("material_orders.list_items"), code=303)

    @blueprint.post("/material-orders/<int:item_id>/completion")
    @access_required
    def set_completion(item_id):
        current_dataset = resolve_dataset()
        desired_state = request.form.get("completed")
        if desired_state not in {"0", "1"}:
            return "完了状態が正しくありません。", 400

        try:
            item = MaterialOrderItem.query.filter_by(
                id=item_id,
                dataset_id=current_dataset.id,
            ).populate_existing().with_for_update().one_or_none()
            if item is None:
                abort(404)

            should_be_completed = desired_state == "1"
            if should_be_completed and not item.is_completed:
                item.is_completed = True
                item.completed_at = utc_now()
            elif not should_be_completed and item.is_completed:
                item.is_completed = False
                item.completed_at = None

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to update a material order item.")
            return "材料の完了状態を更新できませんでした。", 500

        return redirect(url_for("material_orders.list_items"), code=303)

    @blueprint.post("/material-orders/<int:item_id>/delete")
    @access_required
    def delete_item(item_id):
        current_dataset = resolve_dataset()

        try:
            item = MaterialOrderItem.query.filter_by(
                id=item_id,
                dataset_id=current_dataset.id,
            ).populate_existing().with_for_update().one_or_none()
            if item is None:
                abort(404)

            db.session.delete(item)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to delete a material order item.")
            return "材料を削除できませんでした。", 500

        return redirect(url_for("material_orders.list_items"), code=303)

    return blueprint
