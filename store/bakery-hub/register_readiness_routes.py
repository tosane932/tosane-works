"""正式切替を行わないAdmin専用の事前監査と独自商品対応。"""

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import PrepRecipe, RegisterPrepLink, RegisterProduct, db
from register_cutover_audit import audit_cutover, planned_business_day
from register_cutover_service import CutoverBlocked, confirm_cutover
from register_prep_mapping import MappingConflict, set_recipe_link
from register_sales_aggregation import sales_read_snapshot


def create_register_readiness_blueprint(*, admin_required, resolve_dataset):
    blueprint = Blueprint("register_readiness", __name__)

    @blueprint.before_request
    def gate():
        if not current_app.config["REGISTER_V3_READINESS_ENABLED"]:
            abort(404)

    @blueprint.get("/register/readiness")
    @admin_required
    def audit():
        owner = resolve_dataset()
        raw_day = request.args.get("date", "")
        try:
            day = planned_business_day(raw_day) if raw_day else None
            with sales_read_snapshot() as session:
                result = audit_cutover(owner, planned_date=day, read_session=session)
        except ValueError as error:
            return str(error), 400
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register readiness audit failed")
            abort(503)
        response = current_app.make_response(render_template(
            "register_readiness.html", audit=result, requested_date=raw_day))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/register/readiness/confirm")
    @admin_required
    def confirm():
        if not (current_app.config.get("REGISTER_V3_CUTOVER_ENABLED")
                and current_app.config.get("REGISTER_V3_RECOVERY_ENABLED")):
            abort(404)
        owner = resolve_dataset()
        try:
            cutover, created = confirm_cutover(
                owner, date=request.form.get("date"),
                approved_by=current_app.config.get("ADMIN_USERNAME") or "admin",
                reason=request.form.get("reason"),
                device_inventory=request.form.get("device_inventory"),
                devices_checked=request.form.get("devices_checked") == "yes",
                backup_checked=request.form.get("backup_checked") == "yes",
                review_decisions={code: request.form.get(f"review_{code}", "")
                                  for code in ("inactive_products", "duplicate_names",
                                               "expensive_old", "recipe_mapping")
                                  if f"review_{code}" in request.form},
            )
        except CutoverBlocked as error:
            return str(error), 409
        except ValueError as error:
            return str(error), 400
        except PermissionError:
            abort(403)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register cutover confirmation failed")
            abort(503)
        return redirect(url_for(".audit", date=cutover.first_register_date.isoformat(),
                                saved="yes" if created else "already"), code=303)

    @blueprint.route("/prep/register-links", methods=["GET", "POST"])
    @admin_required
    def links():
        owner = resolve_dataset()
        if request.method == "POST":
            if request.form.get("confirmed") != "yes":
                return "商品とレシピの対応を確認してください。", 400
            try:
                recipe_id = int(request.form["recipe_id"])
                raw_product = request.form["product_id"]
                product_id = int(raw_product) if raw_product else None
                raw_expected = request.form["expected_product_id"]
                expected = int(raw_expected) if raw_expected else None
                set_recipe_link(owner, recipe_id, product_id, expected_product_id=expected)
            except (ValueError, KeyError) as error:
                return str(error), 409 if isinstance(error, MappingConflict) else 400
            except PermissionError:
                abort(403)
            except IntegrityError:
                db.session.rollback()
                return "対応が同時に変更されました。読み直してください。", 409
            except SQLAlchemyError:
                db.session.rollback()
                current_app.logger.exception("Register prep link failed")
                abort(503)
            return redirect(url_for(".links"), code=303)
        try:
            recipes = PrepRecipe.query.filter_by(dataset_id=owner.id).order_by(PrepRecipe.id).all()
            products = (RegisterProduct.query.filter_by(dataset_id=owner.id, template_key=None)
                        .order_by(RegisterProduct.id).all())
            mapped = {link.recipe_id: link.register_product_id for link in
                      RegisterPrepLink.query.filter_by(dataset_id=owner.id).all()}
        except SQLAlchemyError:
            db.session.rollback()
            abort(503)
        response = current_app.make_response(render_template(
            "register_prep_links.html", recipes=recipes, products=products, mapped=mapped))
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint
