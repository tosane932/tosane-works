"""Admin向け仕込み試算。数量はDecimalで扱い、在庫は保存しない。"""
import datetime
import logging
import re
from collections import defaultdict
from decimal import Decimal, ROUND_UP

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import Dataset, PrepIngredient, PrepPlan, PrepRecipe, Product, db
from product_images import available_product_images, available_product_image_assets
from prep_sales_reference import prep_sales_reference
from register_sales_aggregation import sales_read_snapshot


logger = logging.getLogger(__name__)
MAX_QUANTITY = 1000
MAX_INGREDIENTS = 30
UNITS = ("g", "ml", "個")
AMOUNT_PATTERN = re.compile(r"(?:0|[1-9]\d{0,6})(?:\.\d{1,3})?\Z")
STOCK_PATTERN = re.compile(r"(?:0|[1-9]\d{0,8})(?:\.\d{1,6})?\Z")


def whole_number(value, *, minimum=0, maximum=MAX_QUANTITY):
    if not re.fullmatch(r"(?:0|[1-9]\d{0,8})", str(value)):
        raise ValueError("個数は整数で入力してください。")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"個数は{minimum}～{maximum}個で入力してください。")
    return number


def ingredient_amount(value):
    if not AMOUNT_PATTERN.fullmatch(str(value)):
        raise ValueError("使用量は小数第3位までの数値で入力してください。")
    amount = Decimal(value)
    if not Decimal("0") < amount <= Decimal("1000000"):
        raise ValueError("使用量は0より大きく1000000以下にしてください。")
    return amount


def stock_amount(value):
    if value == "":
        return None
    if not STOCK_PATTERN.fullmatch(str(value)):
        raise ValueError("現在量は0以上、小数第6位までで入力してください。")
    return Decimal(value)


def quantity_text(amount):
    return format(amount, "f").rstrip("0").rstrip(".") if "." in format(amount, "f") else format(amount, "f")


def required_ingredients(recipe, quantity):
    """製造予定数に比例した量。小数第6位で不足しない方向に表示。"""
    count = whole_number(quantity)
    return [
        (item.name, item.unit, (item.amount * count / recipe.base_quantity).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        ))
        for item in recipe.ingredients
    ]


def combined_ingredients(recipe_quantities):
    """名前の前後空白のみ揃え、同名・同一単位だけを集計する。"""
    totals = defaultdict(lambda: Decimal("0"))
    breakdown = defaultdict(list)
    for recipe, quantity in recipe_quantities:
        for name, unit, amount in required_ingredients(recipe, quantity):
            key = (name.strip(), unit)
            totals[key] += amount
            breakdown[key].append((recipe.display_name, amount))
    return [(name, unit, amount, breakdown[(name, unit)]) for (name, unit), amount in sorted(totals.items())]


def compare_stock(required, current):
    stock = stock_amount(current)
    if stock is None:
        return "未確認", None
    missing = required - stock
    return ("不足", missing) if missing > 0 else ("余裕", -missing)


def validate_recipe(form):
    raw = {
        "base_quantity": form.get("base_quantity", ""), "note": form.get("note", ""),
        "names": form.getlist("ingredient_name"), "units": form.getlist("ingredient_unit"),
        "amounts": form.getlist("ingredient_amount"),
    }
    try:
        base = whole_number(raw["base_quantity"], minimum=1)
        if len(raw["note"]) > 2000:
            raise ValueError("仕込みメモは2000文字以内で入力してください。")
        if not (1 <= len(raw["names"]) <= MAX_INGREDIENTS) or not (len(raw["names"]) == len(raw["units"]) == len(raw["amounts"])):
            raise ValueError("材料は1～30件で入力してください。")
        ingredients = []
        seen = set()
        for name_raw, unit, amount_raw in zip(raw["names"], raw["units"], raw["amounts"]):
            name = name_raw.strip()
            if not name or len(name) > 100:
                raise ValueError("材料名は1～100文字で入力してください。")
            if unit not in UNITS:
                raise ValueError("材料の単位を選択してください。")
            if (name, unit) in seen:
                raise ValueError("同じ材料名・単位は1行にまとめてください。")
            seen.add((name, unit))
            ingredients.append((name, unit, ingredient_amount(amount_raw)))
        return (base, raw["note"], ingredients), raw, None
    except ValueError as error:
        return None, raw, str(error)


def parse_business_date(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        day = datetime.date.fromisoformat(value)
        if not 2000 <= day.year <= 2100:
            raise ValueError
        return day
    except (ValueError, TypeError):
        raise ValueError("対象日を正しく入力してください。") from None


def create_production_prep_blueprint(*, access_required, resolve_dataset, business_today):
    blueprint = Blueprint("production_prep", __name__)

    def dataset():
        selected = resolve_dataset()
        if selected.kind != "admin":
            abort(403)
        return selected

    def recipe_for(current, recipe_id, *, lock=False):
        query = PrepRecipe.query.filter_by(id=recipe_id, dataset_id=current.id)
        if lock:
            query = query.populate_existing().with_for_update()
        return query.one_or_none() or abort(404)

    def source_for(current, source):
        if source.startswith("template:"):
            key = source[len("template:"):]
            option = available_product_images(current_app.static_folder, is_guest=False).get(key)
            if option is None:
                abort(404)
            return option["label"], key
        if source.startswith("product:") and re.fullmatch(r"[1-9]\d{0,10}", source[8:]):
            product = Product.query.filter_by(id=int(source[8:]), dataset_id=current.id).one_or_none()
            if product is None:
                abort(404)
            assets = available_product_image_assets(current_app.static_folder, is_guest=False)
            return product.name, product.image_key if product.image_key in assets else None
        abort(404)

    @blueprint.get("/prep/recipes")
    @access_required
    def recipe_index():
        current = dataset()
        try:
            recipes = PrepRecipe.query.filter_by(dataset_id=current.id).order_by(PrepRecipe.id).all()
            products = Product.query.filter_by(dataset_id=current.id).order_by(Product.id.desc()).all()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to load prep catalog")
            return "仕込み管理を表示できませんでした。", 500
        return render_template("prep_index.html", recipes=recipes, products=products,
                               templates=available_product_images(current_app.static_folder, is_guest=False),
                               known_keys={recipe.product_key for recipe in recipes})

    def render_form(current, recipe=None, *, source=None, raw=None, error=None, status=200):
        if recipe is None:
            name, image_key = source_for(current, source)
        else:
            name, image_key = recipe.display_name, recipe.image_key
        if raw is None:
            raw = {"base_quantity": str(recipe.base_quantity) if recipe else "10",
                   "note": recipe.note if recipe else "",
                   "names": [item.name for item in recipe.ingredients] if recipe else [""],
                   "units": [item.unit for item in recipe.ingredients] if recipe else ["g"],
                   "amounts": [quantity_text(item.amount) for item in recipe.ingredients] if recipe else [""]}
        rows = list(zip(raw["names"], raw["units"], raw["amounts"]))
        return render_template("prep_recipe_form.html", recipe=recipe, source=source, name=name,
                               image_key=image_key, image_assets=available_product_image_assets(current_app.static_folder, is_guest=False),
                               rows=rows, raw=raw, units=UNITS, error=error), status

    @blueprint.route("/prep/recipes/new", methods=["GET", "POST"])
    @access_required
    def new_recipe():
        current = dataset()
        source = request.args.get("source", "") if request.method == "GET" else request.form.get("source", "")
        name, image_key = source_for(current, source)
        existing = PrepRecipe.query.filter_by(dataset_id=current.id, product_key=source).one_or_none()
        if existing:
            return redirect(url_for(".recipe_detail", recipe_id=existing.id), code=303)
        if request.method == "GET":
            return render_form(current, source=source)
        values, raw, error = validate_recipe(request.form)
        if error:
            return render_form(current, source=source, raw=raw, error=error, status=400)
        base, note, ingredients = values
        try:
            recipe = PrepRecipe(dataset_id=current.id, product_key=source, display_name=name,
                                image_key=image_key, base_quantity=base, note=note)
            recipe.ingredients = [PrepIngredient(dataset_id=current.id, name=n, unit=u, amount=a)
                                  for n, u, a in ingredients]
            db.session.add(recipe)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return render_form(current, source=source, raw=raw, error="同じ商品のレシピがすでに登録されています。", status=409)
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to create prep recipe")
            return render_form(current, source=source, raw=raw, error="レシピを保存できませんでした。", status=500)
        flash("レシピを保存しました。", "success")
        return redirect(url_for(".recipe_detail", recipe_id=recipe.id), code=303)

    @blueprint.get("/prep/recipes/<int:recipe_id>")
    @access_required
    def recipe_detail(recipe_id):
        current = dataset()
        recipe = recipe_for(current, recipe_id)
        return render_template("prep_recipe_detail.html", recipe=recipe,
                               image_assets=available_product_image_assets(current_app.static_folder, is_guest=False))

    @blueprint.route("/prep/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
    @access_required
    def edit_recipe(recipe_id):
        current = dataset()
        recipe = recipe_for(current, recipe_id)
        if request.method == "GET":
            return render_form(current, recipe)
        values, raw, error = validate_recipe(request.form)
        if error:
            return render_form(current, recipe, raw=raw, error=error, status=400)
        base, note, ingredients = values
        try:
            recipe = recipe_for(current, recipe_id, lock=True)
            recipe.base_quantity, recipe.note = base, note
            existing = {(item.name, item.unit): item for item in recipe.ingredients}
            incoming = {(n, u) for n, u, _ in ingredients}
            for key, item in existing.items():
                if key not in incoming:
                    db.session.delete(item)
            for name, unit, amount in ingredients:
                item = existing.get((name, unit))
                if item is None:
                    db.session.add(PrepIngredient(dataset_id=current.id, recipe=recipe,
                                                  name=name, unit=unit, amount=amount))
                else:
                    item.amount = amount
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to edit prep recipe")
            return render_form(current, recipe_for(current, recipe_id), raw=raw,
                               error="レシピを更新できませんでした。", status=500)
        flash("レシピを更新しました。", "success")
        return redirect(url_for(".recipe_detail", recipe_id=recipe_id), code=303)

    def show_plan(current, day, *, error=None, status=200, draft=None, selected=None):
        try:
            recipes = PrepRecipe.query.filter_by(dataset_id=current.id).order_by(PrepRecipe.id).all()
            plans = PrepPlan.query.filter_by(dataset_id=current.id, business_date=day).all()
            by_recipe = {plan.recipe_id: plan for plan in plans}
            totals = combined_ingredients((recipe, by_recipe[recipe.id].quantity)
                                          for recipe in recipes if recipe.id in by_recipe and by_recipe[recipe.id].quantity)
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to load prep plan")
            return "製造予定を表示できませんでした。", 500
        sales_reference = None
        if (current_app.config.get("REGISTER_V3_AGGREGATION_ENABLED") and
                (current_app.config.get("TESTING") or current_app.config.get("LOCAL_DEVELOPMENT"))):
            # 集計の全SELECTと商品マスタ照合を同じ読取スナップショットで行う。
            from app import _sales_scope
            _sales_scope(current, day, day)
            with sales_read_snapshot() as read_session:
                sales_reference = prep_sales_reference(current, recipes, day,
                                                       read_session=read_session)
        return render_template("prep_plans.html", recipes=recipes, by_recipe=by_recipe,
                               totals=totals, day=day, error=error, max_quantity=MAX_QUANTITY,
                               quantity_text=quantity_text, draft=draft or {}, selected=selected,
                               sales_reference=sales_reference), status

    def render_requested_day():
        current = dataset()
        try:
            day = parse_business_date(request.args["date"]) if "date" in request.args else business_today()
        except ValueError as error:
            return str(error), 400
        return show_plan(current, day)

    @blueprint.get("/prep/plans")
    @access_required
    def plans():
        return render_requested_day()

    @blueprint.get("/prep")
    @access_required
    def index():
        return render_requested_day()

    @blueprint.post("/prep/plans/bulk")
    @access_required
    def save_plans_bulk():
        current = dataset()
        raw_ids = request.form.getlist("recipe_id")
        try:
            day = parse_business_date(request.form.get("business_date", ""))
        except ValueError as error:
            return str(error), 400
        try:
            if not 1 <= len(raw_ids) <= 100:
                raise ValueError("保存するパンを1～100種類選んでください。")
            ids = [whole_number(value, minimum=1, maximum=999999999) for value in raw_ids]
            if len(set(ids)) != len(ids):
                raise ValueError("同じパンを重複して保存できません。")
            quantities = {}
            for recipe_id in ids:
                values = request.form.getlist(f"quantity_{recipe_id}")
                if len(values) != 1:
                    raise ValueError("パンごとの個数は1件ずつ入力してください。")
                quantities[recipe_id] = whole_number(values[0])
        except ValueError as error:
            # 直せる入力はその日付の画面で残す。表示対象はAdminのレシピに限定する。
            selected = set()
            draft = {}
            for raw_id in raw_ids[:100]:
                try:
                    recipe_id = whole_number(raw_id, minimum=1, maximum=999999999)
                except ValueError:
                    continue
                selected.add(recipe_id)
                draft[recipe_id] = request.form.get(f"quantity_{recipe_id}", "")
            return show_plan(current, day, error=str(error), status=400,
                             draft=draft, selected=selected)
        try:
            # PostgreSQLではDataset行をロックし、同じ店舗の日別一括保存を直列化する。
            # SQLiteでも既存の一意制約とIntegrityErrorで二重作成を拒否する。
            db.session.query(Dataset).filter_by(id=current.id, kind="admin").with_for_update().one()
            recipes = PrepRecipe.query.filter(PrepRecipe.dataset_id == current.id,
                                               PrepRecipe.id.in_(ids)).all()
            if len(recipes) != len(ids):
                db.session.rollback()
                abort(404)
            existing = {plan.recipe_id: plan for plan in PrepPlan.query.filter(
                PrepPlan.dataset_id == current.id, PrepPlan.business_date == day,
                PrepPlan.recipe_id.in_(ids)).with_for_update().all()}
            for recipe_id, quantity in quantities.items():
                if recipe_id in existing:
                    existing[recipe_id].quantity = quantity
                else:
                    db.session.add(PrepPlan(dataset_id=current.id, recipe_id=recipe_id,
                                            business_date=day, quantity=quantity))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return "同じ日の製造予定が同時に保存されました。日付を開き直してください。", 409
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save prep plans together")
            return "製造予定を保存できませんでした。", 500
        flash(f"{day.month}月{day.day}日の製造予定を保存しました。", "success")
        return redirect(url_for(".index", date=day.isoformat()), code=303)

    @blueprint.post("/prep/plans")
    @access_required
    def save_plan():
        current = dataset()
        try:
            day = parse_business_date(request.form.get("business_date", ""))
            quantity = whole_number(request.form.get("quantity", ""))
            recipe_id = whole_number(request.form.get("recipe_id", ""), minimum=1, maximum=999999999)
        except ValueError as error:
            return str(error), 400
        try:
            recipe_for(current, recipe_id)
            # 一意制約は同時POSTにも効く。存在する予定は追加せず更新する。
            plan = PrepPlan.query.filter_by(dataset_id=current.id, recipe_id=recipe_id,
                                            business_date=day).with_for_update().one_or_none()
            if plan is None:
                db.session.add(PrepPlan(dataset_id=current.id, recipe_id=recipe_id,
                                        business_date=day, quantity=quantity))
            else:
                plan.quantity = quantity
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return "同じ日の製造予定が同時に保存されました。日付を開き直してください。", 409
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save prep plan")
            return "製造予定を保存できませんでした。", 500
        flash("製造予定を保存しました。", "success")
        return redirect(url_for(".plans", date=day.isoformat()), code=303)

    @blueprint.post("/prep/plans/<int:plan_id>/remove")
    @access_required
    def remove_plan(plan_id):
        current = dataset()
        try:
            plan = PrepPlan.query.filter_by(id=plan_id, dataset_id=current.id).with_for_update().one_or_none()
            if plan is None:
                abort(404)
            day = plan.business_date
            db.session.delete(plan)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to remove prep plan")
            return "製造予定を外せませんでした。", 500
        flash("製造予定から外しました。", "success")
        return redirect(url_for(".plans", date=day.isoformat()), code=303)

    return blueprint
