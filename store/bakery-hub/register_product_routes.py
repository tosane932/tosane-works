"""v3商品管理の試験画面。Dataset IDは認証主体からのみ取得する。"""

import uuid
import hashlib
import io

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for, send_file
from flask_login import current_user
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.exc import SQLAlchemyError

from models import Product, RegisterProduct, db
from register_images import register_image_choices, product_image_url
from product_rules import BREAD_KINDS, PRODUCT_CLASSES, MAX_PRODUCT_PRICE_YEN
from catalog_repository import validate_image, MAX_UPLOAD_IMAGE_BYTES
from register_products import (
    CustomProductLimitReached, ProductSubmissionConflict,
    copy_legacy_product, create_custom_product, ensure_standard_products,
    update_register_product, update_register_product_image, set_product_active,
)


def _preview_enabled():
    return bool(current_app.config.get("REGISTER_V3_PRODUCTS_PREVIEW_ENABLED")) and bool(
        current_app.config.get("TESTING") or current_app.config.get("LOCAL_DEVELOPMENT")
        or current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED"))


def _write_enabled():
    return _preview_enabled() and bool(current_app.config.get("REGISTER_V3_PRODUCTS_WRITE_ENABLED"))


def _signature(action, owner_id, product):
    if action == "edit":
        return _revision_signature(owner_id, product)
    return URLSafeSerializer(current_app.secret_key, salt="v3-register-products-" + action).dumps(
        [str(owner_id), product.id, product.name,
         product.price if action == "copy" else product.price_yen]
    )


def _original(action, owner_id, product_id, token):
    try:
        value = URLSafeSerializer(current_app.secret_key,
                                  salt="v3-register-products-" + action).loads(token)
    except (BadSignature, TypeError):
        abort(400)
    if (not isinstance(value, list) or len(value) != 4 or
            value[:2] != [str(owner_id), product_id] or
            type(value[2]) is not str or type(value[3]) is not int):
        abort(400)
    return value[2], value[3]


def _image_signature(owner_id, product):
    return URLSafeSerializer(current_app.secret_key, salt="v3-register-product-image").dumps(
        [str(owner_id), product.id, product.name, product.price_yen, product.image_key, product.revision])


def _image_original(owner_id, product_id, token):
    try:
        value = URLSafeSerializer(current_app.secret_key,
                                  salt="v3-register-product-image").loads(token)
    except (BadSignature, TypeError):
        abort(400)
    if (not isinstance(value, list) or len(value) != 6 or
            value[:2] != [str(owner_id), product_id] or
            type(value[2]) is not str or type(value[3]) is not int or
            (value[4] is not None and type(value[4]) is not str) or type(value[5]) is not int):
        abort(400)
    return value[2], value[3], value[4], value[5]


def _revision_signature(owner_id, product):
    return URLSafeSerializer(current_app.secret_key, salt="unified-product-revision").dumps(
        [str(owner_id), product.id, product.revision])


def _revision_original(owner_id, product_id, token):
    try:
        value = URLSafeSerializer(current_app.secret_key, salt="unified-product-revision").loads(token)
    except (BadSignature, TypeError):
        abort(400)
    if (not isinstance(value, list) or len(value) != 3 or
            value[:2] != [str(owner_id), product_id] or type(value[2]) is not int or value[2] < 0):
        abort(400)
    return value[2]


def _form_context(owner, product=None):
    choices = register_image_choices(current_app.static_folder, is_guest=owner.kind == "guest")
    return dict(choices=choices, bread_kinds=BREAD_KINDS, product_classes=PRODUCT_CLASSES,
                max_price=MAX_PRODUCT_PRICE_YEN, can_upload=owner.kind == "admin",
                photo_url=product_image_url(product, choices) if product else None)


def _photo(owner):
    upload = request.files.get("photo")
    camera = request.files.get("camera_photo")
    if upload is not None and upload.filename and camera is not None and camera.filename:
        raise ValueError("写真は一つだけ選択してください。")
    if camera is not None and camera.filename:
        upload = camera
    if upload is None or not upload.filename:
        return None
    if owner.kind != "admin":
        abort(403)
    return validate_image(upload)[1]


def render_product_list(owner):
    """読取専用。初期候補の追加は明示的なPOSTに限定する。"""
    try:
        products = (RegisterProduct.query.filter_by(dataset_id=owner.id)
                    .order_by(RegisterProduct.display_order, RegisterProduct.id).all())
        legacy = (Product.query.filter_by(dataset_id=owner.id)
                  .order_by(Product.year.desc(), Product.month.desc(), Product.id.desc()).all())
        choices = register_image_choices(current_app.static_folder, is_guest=owner.kind == "guest")
        return render_template("register_products.html", products=products, legacy=legacy,
            product_urls={item.id: product_image_url(item, choices) for item in products},
            status_tokens={item.id: _revision_signature(owner.id, item) for item in products},
            write_enabled=_write_enabled(), bread_kinds=BREAD_KINDS, product_classes=PRODUCT_CLASSES,
            notice=request.args.get("notice"), request_key=str(uuid.uuid4()), max_price=MAX_PRODUCT_PRICE_YEN)
    except (SQLAlchemyError, ValueError, OSError):
        db.session.rollback()
        abort(503)


def create_register_product_blueprint(*, access_required, resolve_dataset):
    blueprint = Blueprint("register_products", __name__)

    @blueprint.before_request
    def gate():
        photo_access = (request.endpoint == "register_products.photo" and
                        current_app.config.get("REGISTER_V3_PREVIEW_ENABLED"))
        if not _preview_enabled() and not photo_access:
            abort(404)
        if current_app.config.get("REGISTER_V3_PRODUCTION_ENABLED") and (
                not current_user.is_authenticated or not current_user.is_admin):
            abort(403)
        if request.method == "POST" and not _write_enabled():
            abort(403)
        if request.method == "POST" and request.endpoint not in {"register_products.create", "register_products.edit"}:
            request.max_content_length = 16_384

    @blueprint.get("/register/products")
    @access_required
    def list_products():
        owner = resolve_dataset()
        if owner.kind == "admin":
            return redirect(url_for("product_catalog"), code=303)
        return render_product_list(owner)

    @blueprint.post("/register/products/initial-candidates")
    @access_required
    def initial_candidates():
        owner = resolve_dataset()
        if request.form.get("confirmed") != "yes":
            return "候補18商品の価格を確認してください。", 400
        try:
            ensure_standard_products(owner)
        except PermissionError:
            abort(403)
        except (SQLAlchemyError, ValueError, OSError):
            db.session.rollback()
            abort(503)
        return redirect(url_for("product_catalog") if owner.kind == "admin" else
                        url_for("register_products.list_products"), code=303)

    @blueprint.get("/register/products/<int:product_id>/photo")
    @access_required
    def photo(product_id):
        owner = resolve_dataset()
        product = RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if product is None or not product.photo_webp or owner.kind != "admin":
            abort(404)
        digest = hashlib.sha256(product.photo_webp).hexdigest()
        if request.args.get("v") != digest:
            abort(404)
        response = send_file(io.BytesIO(product.photo_webp), mimetype="image/webp", max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @blueprint.post("/register/products/<int:product_id>/status")
    @access_required
    def status(product_id):
        owner = resolve_dataset()
        revision = _revision_original(owner.id, product_id, request.form.get("signed_original"))
        if request.form.get("is_active") not in {"true", "false"}:
            return "販売状態を選択してください。", 400
        try:
            set_product_active(owner, product_id, is_active=request.form["is_active"] == "true",
                               expected_revision=revision)
        except ProductSubmissionConflict as error:
            return str(error), 409
        except PermissionError:
            abort(404)
        except ValueError as error:
            return str(error), 400
        except SQLAlchemyError:
            db.session.rollback()
            abort(503)
        return redirect(url_for("product_catalog") if owner.kind == "admin" else
                        url_for("register_products.list_products"), code=303)

    @blueprint.route("/register/products/new", methods=["GET", "POST"])
    @access_required
    def create():
        owner = resolve_dataset()
        if request.method == "GET":
            return render_template("register_product_form.html", mode="new", product=None,
                request_key=str(uuid.uuid4()), write_enabled=_write_enabled(), **_form_context(owner))
        if not request.form.get("request_key"):
            return "送信キーが必要です。", 400
        try:
            photo_webp = _photo(owner)
            product = create_custom_product(owner, name=request.form.get("name"),
                                  price_yen=request.form.get("price_yen"),
                                  request_key=request.form.get("request_key"),
                                  bread_kind=request.form.get("bread_kind", "other"),
                                  product_class=request.form.get("product_class", "regular"),
                                  image_key=None if photo_webp else (request.form.get("image_key") or None),
                                  photo_webp=photo_webp)
        except (CustomProductLimitReached, ValueError) as error:
            return str(error), 409 if isinstance(error, (CustomProductLimitReached,
                                                         ProductSubmissionConflict)) else 400
        except PermissionError:
            abort(403)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register product create failed")
            abort(503)
        return redirect(url_for("product_catalog" if owner.kind == "admin" else "register_products.list_products", notice="created", _anchor=f"product-{product.id}"), code=303)

    @blueprint.route("/register/products/<int:product_id>/edit", methods=["GET", "POST"])
    @access_required
    def edit(product_id):
        owner = resolve_dataset()
        product = RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if product is None:
            abort(404)
        if request.method == "GET":
            return render_template("register_product_form.html", mode="edit", product=product,
                                   signed_original=_signature("edit", owner.id, product),
                                   write_enabled=_write_enabled(), **_form_context(owner, product))
        revision = _revision_original(owner.id, product_id, request.form.get("signed_original"))
        try:
            update_register_product(owner, product_id, name=request.form.get("name"),
                                    price_yen=request.form.get("price_yen"),
                                    expected_revision=revision,
                                    bread_kind=request.form.get("bread_kind"),
                                    product_class=request.form.get("product_class"),
                                    image_key=request.form.get("image_key", "__keep__"),
                                    photo_webp=_photo(owner))
        except ProductSubmissionConflict as error:
            return str(error), 409
        except ValueError as error:
            return str(error), 400
        except PermissionError:
            abort(403)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register product edit failed")
            abort(503)
        return redirect(url_for("product_catalog" if owner.kind == "admin" else "register_products.list_products", notice="updated"), code=303)

    @blueprint.route("/register/products/<int:product_id>/image", methods=["GET", "POST"])
    @access_required
    def choose_image(product_id):
        owner = resolve_dataset()
        product = RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if product is None:
            abort(404)
        if request.method == "GET":
            try:
                choices = register_image_choices(current_app.static_folder,
                                                 is_guest=owner.kind == "guest")
            except (ValueError, SQLAlchemyError, OSError):
                db.session.rollback()
                abort(503)
            return render_template("register_product_images.html", product=product,
                                   choices=choices, write_enabled=_write_enabled(),
                                   signed_original=_image_signature(owner.id, product))
        if request.form.get("image_key") is None:
            return "画像を選択してください。", 400
        original_name, original_price, original_image, revision = _image_original(
            owner.id, product_id, request.form.get("signed_original"))
        try:
            update_register_product_image(
                owner, product_id, image_key=request.form["image_key"] or None,
                expected_name=original_name, expected_price_yen=original_price,
                expected_image_key=original_image, expected_revision=revision)
        except ProductSubmissionConflict as error:
            return str(error), 409
        except ValueError as error:
            return str(error), 400
        except PermissionError:
            abort(403)
        except (SQLAlchemyError, OSError):
            db.session.rollback()
            current_app.logger.exception("Register product image update failed")
            abort(503)
        return redirect(url_for("product_catalog" if owner.kind == "admin" else "register_products.list_products", notice="image"), code=303)

    @blueprint.route("/register/products/legacy/<int:product_id>/copy", methods=["GET", "POST"])
    @access_required
    def copy(product_id):
        owner = resolve_dataset()
        product = Product.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if product is None:
            abort(404)
        eligible = product.is_active and 0 <= product.price <= MAX_PRODUCT_PRICE_YEN
        if request.method == "GET":
            return render_template("register_product_form.html", mode="copy", product=product,
                                   signed_original=_signature("copy", owner.id, product),
                                   request_key=str(uuid.uuid4()), eligible=eligible,
                                   write_enabled=_write_enabled())
        if request.form.get("confirmed") != "yes":
            return "旧商品のコピーを確認してください。", 400
        if not request.form.get("request_key"):
            return "送信キーが必要です。", 400
        original_name, original_price = _original(
            "copy", owner.id, product_id, request.form.get("signed_original"))
        try:
            copy_legacy_product(owner, product_id, confirmed_name=original_name,
                                confirmed_price_yen=original_price,
                                request_key=request.form.get("request_key"))
        except (CustomProductLimitReached, ProductSubmissionConflict) as error:
            return str(error), 409
        except ValueError as error:
            return str(error), 400
        except PermissionError:
            abort(403)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register product copy failed")
            abort(503)
        return redirect(url_for("product_catalog" if owner.kind == "admin" else "register_products.list_products", notice="copied"), code=303)

    return blueprint
