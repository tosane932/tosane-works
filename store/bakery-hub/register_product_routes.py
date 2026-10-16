"""v3商品管理の試験画面。Dataset IDは認証主体からのみ取得する。"""

import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.exc import SQLAlchemyError

from models import Product, RegisterProduct, db
from product_images import PRODUCT_IMAGE_OPTIONS, _safe_image_path
from register_products import (
    CustomProductLimitReached, ProductSubmissionConflict,
    copy_legacy_product, create_custom_product, ensure_standard_products,
    update_register_product,
)


def _preview_enabled():
    return bool(current_app.config.get("REGISTER_V3_PRODUCTS_PREVIEW_ENABLED")) and bool(
        current_app.config.get("TESTING") or current_app.config.get("LOCAL_DEVELOPMENT"))


def _write_enabled():
    return _preview_enabled() and bool(current_app.config.get("REGISTER_V3_PRODUCTS_WRITE_ENABLED"))


def _signature(action, owner_id, product):
    return URLSafeSerializer(current_app.secret_key, salt="v3-register-products-" + action).dumps(
        [str(owner_id), product.id, product.name,
         product.price_yen if action == "edit" else product.price]
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


def create_register_product_blueprint(*, access_required, resolve_dataset):
    blueprint = Blueprint("register_products", __name__)

    @blueprint.before_request
    def gate():
        if not _preview_enabled():
            abort(404)
        if request.method == "POST" and not _write_enabled():
            abort(403)
        if request.method == "POST":
            request.max_content_length = 16_384

    @blueprint.get("/register/products")
    @access_required
    def list_products():
        owner = resolve_dataset()
        try:
            ensure_standard_products(owner)
            products = (RegisterProduct.query.filter_by(dataset_id=owner.id)
                        .order_by(RegisterProduct.display_order, RegisterProduct.id).all())
            legacy = (Product.query.filter_by(dataset_id=owner.id)
                      .order_by(Product.year.desc(), Product.month.desc(), Product.id.desc()).all())
        except SQLAlchemyError:
            db.session.rollback()
            abort(503)
        image_dir = (Path(current_app.static_folder) / "register_images").resolve()
        image_urls = {key: url_for("static", filename=f"register_images/{key}.webp")
                      for key in PRODUCT_IMAGE_OPTIONS
                      if _safe_image_path(image_dir, f"{key}.webp") is not None}
        return render_template("register_products.html", products=products,
                               legacy=legacy, image_urls=image_urls,
                               request_key=str(uuid.uuid4()),
                               write_enabled=_write_enabled(),
                               notice=request.args.get("notice"))

    @blueprint.post("/register/products/new")
    @access_required
    def create():
        owner = resolve_dataset()
        if not request.form.get("request_key"):
            return "送信キーが必要です。", 400
        try:
            create_custom_product(owner, name=request.form.get("name"),
                                  price_yen=request.form.get("price_yen"),
                                  request_key=request.form.get("request_key"))
        except (CustomProductLimitReached, ValueError) as error:
            return str(error), 409 if isinstance(error, (CustomProductLimitReached,
                                                         ProductSubmissionConflict)) else 400
        except PermissionError:
            abort(403)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register product create failed")
            abort(503)
        return redirect(url_for("register_products.list_products", notice="created"), code=303)

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
                                   write_enabled=_write_enabled())
        original_name, original_price = _original(
            "edit", owner.id, product_id, request.form.get("signed_original"))
        try:
            update_register_product(owner, product_id, name=request.form.get("name"),
                                    price_yen=request.form.get("price_yen"),
                                    expected_name=original_name,
                                    expected_price_yen=original_price)
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
        return redirect(url_for("register_products.list_products", notice="updated"), code=303)

    @blueprint.route("/register/products/legacy/<int:product_id>/copy", methods=["GET", "POST"])
    @access_required
    def copy(product_id):
        owner = resolve_dataset()
        product = Product.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if product is None:
            abort(404)
        eligible = product.is_active and 0 <= product.price <= 500
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
        return redirect(url_for("register_products.list_products", notice="copied"), code=303)

    return blueprint
