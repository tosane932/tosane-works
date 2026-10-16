"""公開前のレジ画面と会計同期API。Datasetは常に認証主体から解決。"""

import uuid

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

from models import RegisterCutover, RegisterProduct, RegisterTransaction, RegisterTransactionLine, db
from register_images import register_image_choices
from register_products import _standard_rows, ensure_standard_products
from register_service import (BusinessDayChanged, GuestTransactionLimitReached,
                              PriceChanged, SubmissionConflict,
                              save_register_transaction)


REGISTER_BODY_LIMIT = 16_384
LOCAL_SCOPE_SESSION_KEY = "register_local_scope"


def _production_owner_allowed(owner):
    if not current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED"):
        return True
    return owner.kind == "admin" and current_user.is_authenticated and current_user.is_admin


def _production_cutover_ready(owner):
    if not current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED"):
        return True
    from register_service import TOKYO, utc_now
    cutover = db.session.get(RegisterCutover, owner.id)
    return cutover is not None and utc_now().astimezone(TOKYO).date() >= cutover.first_register_date


def create_register_blueprint(*, access_required, admin_required, resolve_dataset):
    blueprint = Blueprint("register", __name__)

    @blueprint.before_request
    def api_session_state():
        # 分類にだけsigned sessionを使う。Datasetの認可・保存には使わない。
        if request.endpoint in {"register.create_transaction", "register.transaction_status"} and not current_user.is_authenticated:
            if str(session.get("_user_id", "")).startswith("guest:"):
                return jsonify(error="Guestの利用期限が終了しました。", code="guest_expired"), 403
            return jsonify(error="ログインの確認が必要です。", code="auth_required"), 401

    @blueprint.get("/register/preview")
    @access_required
    def preview():
        if not current_app.config["REGISTER_V3_PREVIEW_ENABLED"]:
            abort(404)
        owner = resolve_dataset()
        if not _production_owner_allowed(owner):
            abort(403)
        if not _production_cutover_ready(owner):
            abort(404)
        try:
            ensure_standard_products(owner)
            products = (RegisterProduct.query.filter_by(dataset_id=owner.id, is_active=True)
                        .order_by(RegisterProduct.display_order, RegisterProduct.id).all())
            image_urls = {key: choice["url"] for key, choice in
                          register_image_choices(current_app.static_folder,
                                                 is_guest=owner.kind == "guest").items()}
        except (SQLAlchemyError, ValueError, OSError):
            db.session.rollback()
            abort(503)
        if owner.kind == "guest" and LOCAL_SCOPE_SESSION_KEY not in session:
            session[LOCAL_SCOPE_SESSION_KEY] = uuid.uuid4().hex
        rows = [{"id": product.id, "name": product.name, "price": product.price_yen,
                 "image": image_urls.get(product.image_key),
                 "standard": product.template_key is not None}
                for product in products]
        rows.sort(key=lambda row: not row["standard"])
        save_enabled = bool(current_app.config["REGISTER_V3_SAVE_ENABLED"])
        return render_template(
            "register.html", products=rows, save_enabled=save_enabled,
            local_scope=(f"{owner.id}:admin" if owner.kind == "admin" else
                         f"{owner.id}:{session[LOCAL_SCOPE_SESSION_KEY]}"),
            production=bool(current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED")),
        )

    @blueprint.get("/register/demo")
    @admin_required
    def demo():
        if not current_app.config["REGISTER_V3_PUBLIC_DEMO_ENABLED"]:
            abort(404)
        # 定義と静的WebPだけで構築。Datasetの取得・商品登録・session変更は行わない。
        try:
            definitions = _standard_rows(current_app.static_folder)
            image_urls = {key: choice["url"] for key, choice in
                          register_image_choices(current_app.static_folder, is_guest=False).items()}
        except (ValueError, OSError):
            abort(503)
        rows = [{"id": index, "name": row["name"], "price": row["price_yen"],
                 "image": image_urls.get(row["image_key"]), "standard": True}
                for index, row in enumerate(definitions, 1)]
        response = current_app.make_response(render_template(
            "register.html", products=rows, demo=True, save_enabled=False,
        ))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/register/transactions")
    @access_required
    def create_transaction():
        if not (current_app.config["REGISTER_V3_PREVIEW_ENABLED"]
                and current_app.config["REGISTER_V3_SAVE_ENABLED"]):
            return jsonify(error="レジ会計の保存は現在利用できません。", code="disabled"), 403
        owner = resolve_dataset()
        if not _production_owner_allowed(owner) or not _production_cutover_ready(owner):
            return jsonify(error="正式レジを利用できません。", code="disabled"), 403
        if not request.is_json:
            return jsonify(error="JSON形式の会計データが必要です。", code="invalid"), 400
        data = request.get_json(silent=True)
        required = {"submission_key", "kind", "items", "received_yen",
                    "refund_confirmed", "displayed_prices", "client_business_date"}
        if not isinstance(data, dict) or set(data) != required:
            return jsonify(error="会計データの項目が不正です。", code="invalid"), 400
        try:
            saved = save_register_transaction(
                owner, submission_key=data["submission_key"], kind=data["kind"],
                items=data["items"], received_yen=data["received_yen"],
                refund_confirmed=data["refund_confirmed"],
                displayed_prices=data["displayed_prices"],
                checkout_business_date=data["client_business_date"],
            )
        except PriceChanged as error:
            return jsonify(error=str(error), code="price_changed",
                           differences=error.differences), 409
        except BusinessDayChanged as error:
            return jsonify(error=str(error), code="business_day_changed"), 409
        except SubmissionConflict as error:
            return jsonify(error=str(error), code="key_conflict"), 409
        except PermissionError as error:
            if owner.kind == "guest" and str(error) == "期限切れのGuestです。":
                return jsonify(error=str(error), code="guest_expired"), 403
            return jsonify(error=str(error), code="forbidden"), 403
        except GuestTransactionLimitReached as error:
            return jsonify(error=str(error), code="forbidden"), 403
        except ValueError as error:
            return jsonify(error=str(error), code="invalid"), 400
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register transaction failed")
            return jsonify(error="保存できませんでした。後で同じ会計キーで再送してください。",
                           code="temporary"), 503
        return jsonify(submission_key=str(saved.submission_key),
                       transaction_id=str(saved.id), total_yen=saved.total_yen,
                       business_date=saved.business_date.isoformat(),
                       kind=saved.kind), 200

    @blueprint.get("/api/register/transactions/<uuid:submission_key>/status")
    @access_required
    def transaction_status(submission_key):
        if not (current_app.config["REGISTER_V3_PREVIEW_ENABLED"] and
                (current_app.config["REGISTER_V3_SAVE_ENABLED"] or
                 current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED"))):
            return jsonify(error="保存状況の照会は現在利用できません。", code="disabled"), 403
        owner = resolve_dataset()
        if not _production_owner_allowed(owner) or not _production_cutover_ready(owner):
            return jsonify(error="保存状況を照会できません。", code="disabled"), 403
        try:
            saved = RegisterTransaction.query.filter_by(
                dataset_id=owner.id, submission_key=submission_key).one_or_none()
            if saved is None:
                response = jsonify(status="not_found")
            else:
                lines = (RegisterTransactionLine.query.filter_by(
                    dataset_id=owner.id, transaction_id=saved.id)
                    .order_by(RegisterTransactionLine.register_product_id).all())
                response = jsonify(status="saved", submission_key=str(saved.submission_key),
                                   transaction_id=str(saved.id), kind=saved.kind,
                                   business_date=saved.business_date.isoformat(),
                                   total_yen=saved.total_yen, received_yen=saved.received_yen,
                                   items=[[line.register_product_id, line.quantity] for line in lines],
                                   displayed_prices=[[line.register_product_id, line.unit_price_yen]
                                                     for line in lines])
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register status lookup failed")
            return jsonify(error="保存状況を照会できません。", code="temporary"), 503
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint
