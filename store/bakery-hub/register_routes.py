"""公開前のレジ画面と会計同期API。Datasetは常に認証主体から解決。"""

import datetime
import uuid

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session, send_from_directory
from flask_login import current_user
from flask_wtf.csrf import generate_csrf
from sqlalchemy.exc import SQLAlchemyError

from models import RegisterCutover, RegisterOfflinePermit, RegisterProduct, RegisterTransaction, RegisterTransactionLine, db
from guest_register import guest_register_enabled
from register_catalog_proof import issue_catalog_proof
from register_offline_service import (OfflineReviewRequired, prepare_offline,
                                      save_offline_transaction)
from register_images import register_image_choices, product_image_url
from register_products import _standard_rows, ensure_standard_products
from register_service import (BusinessDayChanged, GuestTransactionLimitReached,
                              PriceChanged, SubmissionConflict, RecoveryReviewRequired,
                              save_register_transaction)


REGISTER_BODY_LIMIT = 16_384
LOCAL_SCOPE_SESSION_KEY = "register_local_scope"


def _production_owner_allowed(owner):
    if guest_register_enabled(owner):
        return True
    if not current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED"):
        return True
    return owner.kind == "admin" and current_user.is_authenticated and current_user.is_admin


def _production_cutover_ready(owner):
    if not guest_register_enabled(owner) and not current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED"):
        return True
    from register_service import TOKYO, utc_now
    cutover = db.session.get(RegisterCutover, owner.id)
    return cutover is not None and utc_now().astimezone(TOKYO).date() >= cutover.first_register_date


def create_register_blueprint(*, access_required, admin_required, resolve_dataset):
    blueprint = Blueprint("register", __name__)

    @blueprint.before_request
    def api_session_state():
        # 分類にだけsigned sessionを使う。Datasetの認可・保存には使わない。
        if request.endpoint in {"register.create_transaction", "register.transaction_status",
                                "register.offline_sync"} and not current_user.is_authenticated:
            if str(session.get("_user_id", "")).startswith("guest:"):
                return jsonify(error="Guestの利用期限が終了しました。", code="guest_expired"), 403
            return jsonify(error="ログインの確認が必要です。", code="auth_required"), 401

    @blueprint.get("/register/preview")
    @access_required
    def preview():
        if not guest_register_enabled() and not current_app.config["REGISTER_V3_PREVIEW_ENABLED"]:
            abort(404)
        owner = resolve_dataset()
        if not _production_owner_allowed(owner):
            abort(403)
        if not _production_cutover_ready(owner):
            abort(404)
        try:
            if owner.kind == "guest" and not guest_register_enabled(owner):
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
                 "image": product_image_url(product, {key: {"url": value} for key, value in image_urls.items()}),
                 "standard": product.template_key is not None}
                for product in products]
        save_enabled = guest_register_enabled(owner) or bool(current_app.config["REGISTER_V3_SAVE_ENABLED"])
        offline_enabled = bool(owner.kind == "admin" and save_enabled and
                               current_app.config.get("REGISTER_V3_OFFLINE_MODE_ENABLED"))
        response = current_app.make_response(render_template(
            "register.html", products=rows, save_enabled=save_enabled,
            local_scope=(f"{owner.id}:admin" if owner.kind == "admin" else
                         f"{owner.id}:{session[LOCAL_SCOPE_SESSION_KEY]}"),
            guest_experience=guest_register_enabled(owner),
            production=owner.kind == "admin" and bool(current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED")),
            recovery_enabled=owner.kind == "admin" and bool(
                current_app.config.get("REGISTER_V3_RECOVERY_ENABLED")),
            offline_enabled=offline_enabled,
            offline_prepare_url="/api/register/offline/prepare" if offline_enabled else None,
            catalog_proof=issue_catalog_proof(owner, products) if offline_enabled else None,
            catalog_fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat() if offline_enabled else None,
        ))
        response.headers["Cache-Control"] = "no-store"
        return response

    def _offline_allowed(owner):
        return (owner.kind == "admin" and _production_owner_allowed(owner)
                and _production_cutover_ready(owner)
                and all(current_app.config.get(name) for name in (
                    "REGISTER_V3_PRODUCTION_ENABLED", "REGISTER_V3_PREVIEW_ENABLED",
                    "REGISTER_V3_SAVE_ENABLED", "REGISTER_V3_OFFLINE_MODE_ENABLED")))

    @blueprint.post("/api/register/offline/prepare")
    @admin_required
    def offline_prepare():
        owner = resolve_dataset()
        if not _offline_allowed(owner):
            abort(404)
        data = request.get_json(silent=True) if request.is_json else None
        if not isinstance(data, dict) or set(data) != {"device_id", "next_day"}:
            return jsonify(error="端末準備の形式が不正です。"), 400
        try:
            grant, token, proof, products = prepare_offline(
                owner, data["device_id"], next_day=data["next_day"])
            image_urls = {key: choice["url"] for key, choice in
                          register_image_choices(current_app.static_folder, is_guest=False).items()}
        except (ValueError, OfflineReviewRequired) as error:
            return jsonify(error=str(error)), 400
        except PermissionError:
            abort(403)
        except SQLAlchemyError:
            db.session.rollback()
            abort(503)
        response = jsonify(dataset_id=str(owner.id), grant_id=str(grant.id), device_id=grant.device_id,
                           scope=f"{owner.id}:admin", token=token, catalog_proof=proof,
                           issued_at=grant.issued_at.isoformat(),
                           expires_at=grant.expires_at.isoformat(),
                           business_date=grant.business_date.isoformat(),
                           next_business_date=(grant.next_business_date.isoformat()
                                               if grant.next_business_date else None),
                           products=[dict(id=p.id, name=p.name, price=p.price_yen,
                                          image=product_image_url(p, {key: {"url": value} for key, value in image_urls.items()}, embedded=True), image_key=p.image_key,
                                          is_active=p.is_active, standard=p.template_key is not None)
                                     for p in products])
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/api/register/offline/session")
    @admin_required
    def offline_session():
        owner = resolve_dataset()
        if not _offline_allowed(owner):
            abort(404)
        grant_id = request.args.get("grant_id", "")
        try:
            grant = db.session.get(RegisterOfflinePermit, uuid.UUID(grant_id))
        except ValueError:
            grant = None
        if grant is None or grant.dataset_id != owner.id or grant.revoked_at is not None:
            return jsonify(error="端末の許可が失効しています。"), 403
        response = jsonify(dataset_id=str(owner.id), csrf_token=generate_csrf())
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/register/offline")
    @admin_required
    def offline_shell():
        owner = resolve_dataset()
        if not _offline_allowed(owner):
            abort(404)
        # No session content, CSRF token, product, Dataset or credential in this shell.
        response = current_app.make_response(render_template(
            "register.html", products=[], offline_shell=True, save_enabled=False,
            production=True, recovery_enabled=False))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Register-Offline-Shell"] = "1"
        return response

    @blueprint.get("/register/offline-sw.js")
    @admin_required
    def offline_worker():
        owner = resolve_dataset()
        if not _offline_allowed(owner):
            abort(404)
        response = send_from_directory(current_app.static_folder, "register_offline_sw.js")
        response.headers["Service-Worker-Allowed"] = "/register/"
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/register/offline/sync")
    @admin_required
    def offline_sync():
        owner = resolve_dataset()
        if not _offline_allowed(owner):
            return jsonify(error="オフライン同期は無効です。", code="disabled"), 403
        data = request.get_json(silent=True) if request.is_json else None
        if not isinstance(data, dict) or set(data) != {"receipt", "grant_token"}:
            return jsonify(error="元会計または端末許可が不正です。", code="invalid"), 400
        try:
            saved = save_offline_transaction(owner, data["receipt"], data["grant_token"])
        except (OfflineReviewRequired, ValueError) as error:
            return jsonify(error=str(error), code="offline_review"), 409
        except PermissionError:
            return jsonify(error="Datasetの認可がありません。", code="forbidden"), 403
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Offline register sync failed")
            return jsonify(error="同じキーで後から照合してください。", code="temporary"), 503
        return jsonify(submission_key=str(saved.submission_key), transaction_id=str(saved.id),
                       business_date=saved.business_date.isoformat(), total_yen=saved.total_yen,
                       kind=saved.kind), 200

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
        if not guest_register_enabled() and not (current_app.config["REGISTER_V3_PREVIEW_ENABLED"]
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
        except RecoveryReviewRequired as error:
            return jsonify(error=str(error), code="recovery_review"), 409
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
        if not guest_register_enabled() and not (current_app.config["REGISTER_V3_PREVIEW_ENABLED"] and
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
