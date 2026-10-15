"""仕込み計算、Admin分離、DB保存、発注電話リンク。"""
import datetime
import json
from pathlib import Path
import subprocess
import uuid
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from material_orders import phone_from_memo
from models import Dataset, MaterialOrderItem, PrepIngredient, PrepPlan, PrepRecipe, Product, db
from production_prep import combined_ingredients, compare_stock, ingredient_amount, required_ingredients, stock_amount, whole_number


def _token(client, path):
    g.pop("_login_user", None)
    page = client.get(path)
    assert page.status_code == 200
    return BeautifulSoup(page.data, "html.parser").select_one('input[name="csrf_token"]')["value"]


def _create(client, source="template:croissant", **extra):
    path = "/prep/recipes/new?source=" + source
    data = {"csrf_token": _token(client, "/prep"), "source": source,
            "base_quantity": "10", "note": "こねる", "ingredient_name": ["強力粉", "バター"],
            "ingredient_amount": ["500", "25"], "ingredient_unit": ["g", "g"], **extra}
    return client.post("/prep/recipes/new", data=data)


def _guest(flask_app):
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(kind="guest", created_at=now, last_activity_at=now,
                      absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(dataset)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{dataset.id}"
        session["_fresh"] = True
    return client, dataset


def test_recipe_create_edit_and_month_independence(authenticated_client, admin_dataset):
    response = _create(authenticated_client, ingredient_name=[" 強力粉 ", "バター"])
    assert response.status_code == 303
    recipe = PrepRecipe.query.one()
    assert recipe.product_key == "template:croissant"
    assert recipe.ingredients[0].name == "強力粉"
    assert _create(authenticated_client).status_code == 303
    assert PrepRecipe.query.count() == 1
    edit_path = f"/prep/recipes/{recipe.id}/edit"
    response = authenticated_client.post(edit_path, data={
        "csrf_token": _token(authenticated_client, edit_path), "base_quantity": "5",
        "note": "<script>alert(1)</script>", "ingredient_name": ["バター", "砂糖"],
        "ingredient_amount": ["12.5", "1"], "ingredient_unit": ["g", "g"]})
    assert response.status_code == 303
    db.session.refresh(recipe)
    assert recipe.base_quantity == 5
    assert {(i.name, i.amount) for i in recipe.ingredients} == {("バター", Decimal("12.5")), ("砂糖", Decimal("1"))}
    html = authenticated_client.get(f"/prep/recipes/{recipe.id}").get_data(as_text=True)
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    db.session.add(Product(dataset_id=admin_dataset.id, year=2027, month=1, name="クロワッサン", price=100))
    db.session.commit()
    assert authenticated_client.get(f"/prep/recipes/{recipe.id}").status_code == 200


def test_decimal_exact_combine_stock_and_invalid_inputs():
    class Item:
        def __init__(self, name, unit, amount):
            self.name, self.unit, self.amount = name, unit, Decimal(amount)
    class Recipe:
        def __init__(self, name, ingredients):
            self.display_name, self.ingredients, self.base_quantity = name, ingredients, 10
    first = Recipe("A", [Item(" 強力粉 ", "g", "25"), Item("バター", "g", "25")])
    second = Recipe("B", [Item("強力粉", "g", "25"), Item("強力粉", "ml", "2")])
    assert required_ingredients(first, 1)[0][2] == Decimal("2.500000")
    assert required_ingredients(first, 3)[0][2] == Decimal("7.500000")
    assert required_ingredients(first, 0)[0][2] == 0
    combined = combined_ingredients([(first, 3), (second, 1)])
    assert {(name, unit): amount for name, unit, amount, _ in combined}[("強力粉", "g")] == Decimal("10")
    assert len(combined) == 3
    assert compare_stock(Decimal("2.5"), "") == ("未確認", None)
    assert compare_stock(Decimal("2.5"), "0") == ("不足", Decimal("2.5"))
    assert compare_stock(Decimal("2.5"), "2.5") == ("余裕", Decimal("0"))
    assert compare_stock(Decimal("2.5"), "3") == ("余裕", Decimal("0.5"))
    for value in ("-1", "1e6", "NaN", "1.0001", "1000001", "01"):
        with pytest.raises(ValueError): ingredient_amount(value)
    for value in ("-1", "1e3", "1001", "1.1", ""):
        with pytest.raises(ValueError): whole_number(value)
    for value in ("-1", "NaN", "1e4", "1.1234567"):
        with pytest.raises(ValueError): stock_amount(value)


def test_recipe_validation_preserves_inputs_and_atomic_edit(authenticated_client, admin_dataset, monkeypatch):
    assert _create(authenticated_client, ingredient_name=["同じ", " 同じ "]).status_code == 400
    assert PrepRecipe.query.count() == 0
    assert _create(authenticated_client, base_quantity="0").status_code == 400
    assert _create(authenticated_client, ingredient_amount=["2.5", "oops"]).status_code == 400
    assert _create(authenticated_client).status_code == 303
    recipe = PrepRecipe.query.one()
    path = f"/prep/recipes/{recipe.id}/edit"
    before = [(item.name, item.amount) for item in recipe.ingredients]
    response = authenticated_client.post(path, data={"csrf_token": _token(authenticated_client, path),
        "base_quantity": "12", "note": "保持", "ingredient_name": ["新しい"],
        "ingredient_amount": ["2.5"], "ingredient_unit": ["bad"]})
    assert response.status_code == 400
    assert "新しい" in response.get_data(as_text=True)
    db.session.refresh(recipe)
    assert recipe.base_quantity == 10
    assert [(item.name, item.amount) for item in recipe.ingredients] == before


def test_plan_save_upsert_remove_and_no_inventory_change(authenticated_client, admin_dataset):
    assert _create(authenticated_client).status_code == 303
    recipe = PrepRecipe.query.one()
    day = "2026-10-15"
    form = {"recipe_id": str(recipe.id), "business_date": day, "quantity": "20"}
    assert authenticated_client.post("/prep/plans", data={**form, "csrf_token": _token(authenticated_client, "/prep/plans")}).status_code == 303
    assert authenticated_client.post("/prep/plans", data={**form, "quantity": "3", "csrf_token": _token(authenticated_client, "/prep/plans")}).status_code == 303
    plan = PrepPlan.query.one()
    assert plan.quantity == 3
    page = authenticated_client.get("/prep/plans?date=" + day).get_data(as_text=True)
    assert "7.5" in page and "材料発注リストへ" in page
    assert MaterialOrderItem.query.count() == 0
    assert authenticated_client.post(f"/prep/plans/{plan.id}/remove", data={"csrf_token": _token(authenticated_client, "/prep/plans")}).status_code == 303
    assert PrepPlan.query.count() == 0 and PrepRecipe.query.count() == 1


def test_admin_only_dataset_fk_csrf_and_other_ids(authenticated_client, client, flask_app, admin_dataset):
    g.pop("_login_user", None)
    assert client.get("/prep").status_code == 302
    guest, other = _guest(flask_app)
    g.pop("_login_user", None)
    assert guest.get("/prep").status_code == 403
    assert guest.get("/prep/recipes/1").status_code == 403
    assert guest.get("/prep/plans").status_code == 403
    assert _create(authenticated_client).status_code == 303
    recipe = PrepRecipe.query.one()
    assert authenticated_client.post("/prep/plans", data={"recipe_id": str(recipe.id), "business_date": "2026-10-15", "quantity": "1"}).status_code == 400
    assert authenticated_client.get("/prep/recipes/9999").status_code == 404
    other_recipe = PrepRecipe(dataset_id=other.id, product_key="other", display_name="秘密", base_quantity=10)
    db.session.add(other_recipe)
    db.session.commit()
    assert authenticated_client.get(f"/prep/recipes/{other_recipe.id}").status_code == 404
    assert any(
        fk["constrained_columns"] == ["recipe_id", "dataset_id"]
        and fk["referred_columns"] == ["id", "dataset_id"]
        for fk in inspect(db.engine).get_foreign_keys("prep_plans")
    )


def test_phone_from_memo_and_order_page(authenticated_client, admin_dataset):
    assert phone_from_memo("発注先：A\n電話：082-123-4567") == "tel:0821234567"
    assert phone_from_memo("電話：javascript:alert(1)") is None
    assert phone_from_memo("電話：082-123-4567<script>") is None
    assert phone_from_memo("電話：123") is None
    db.session.add(MaterialOrderItem(dataset_id=admin_dataset.id, name="バター", memo="電話：082-123-4567"))
    db.session.commit()
    page = authenticated_client.get("/material-orders").get_data(as_text=True)
    assert 'href="tel:0821234567"' in page


def test_original_products_same_name_are_not_merged(authenticated_client, admin_dataset):
    first = Product(dataset_id=admin_dataset.id, year=2026, month=10, name="同じ名前", price=180)
    second = Product(dataset_id=admin_dataset.id, year=2026, month=11, name="同じ名前", price=200)
    db.session.add_all([first, second]); db.session.commit()
    assert _create(authenticated_client, source=f"product:{first.id}").status_code == 303
    assert _create(authenticated_client, source=f"product:{second.id}").status_code == 303
    assert PrepRecipe.query.count() == 2
    assert {recipe.product_key for recipe in PrepRecipe.query.all()} == {f"product:{first.id}", f"product:{second.id}"}


def test_zero_day_plan_and_recipe_change_recalculate(authenticated_client, admin_dataset):
    assert _create(authenticated_client).status_code == 303
    recipe = PrepRecipe.query.one()
    path = "/prep/plans"
    data = {"csrf_token": _token(authenticated_client, path), "business_date": "2026-10-15",
            "recipe_id": str(recipe.id), "quantity": "0"}
    assert authenticated_client.post(path, data=data).status_code == 303
    assert PrepPlan.query.one().quantity == 0
    assert "必要 0" not in authenticated_client.get(path + "?date=2026-10-15").get_data(as_text=True)
    data["quantity"] = "3"
    assert authenticated_client.post(path, data=data).status_code == 303
    before = authenticated_client.get(path + "?date=2026-10-15").get_data(as_text=True)
    assert "必要 <output>7.5</output>g" in before
    recipe.ingredients[1].amount = Decimal("50")
    db.session.commit()
    after = authenticated_client.get(path + "?date=2026-10-15").get_data(as_text=True)
    assert "必要 <output>15</output>g" in after
    assert PrepPlan.query.one().quantity == 3


def test_plan_validation_and_rollback(authenticated_client, admin_dataset, monkeypatch):
    assert _create(authenticated_client).status_code == 303
    recipe = PrepRecipe.query.one()
    path = "/prep/plans"
    token = _token(authenticated_client, path)
    base = {"csrf_token": token, "business_date": "2026-10-15", "recipe_id": str(recipe.id)}
    for invalid in ("-1", "1e3", "1001", "1.5"):
        assert authenticated_client.post(path, data={**base, "quantity": invalid}).status_code == 400
    assert authenticated_client.post(path, data={**base, "business_date": "2026-02-30", "quantity": "1"}).status_code == 400
    assert PrepPlan.query.count() == 0
    original_commit = db.session.commit
    def broken_commit():
        raise SQLAlchemyError("forced failure")
    monkeypatch.setattr(db.session, "commit", broken_commit)
    assert authenticated_client.post(path, data={**base, "quantity": "1"}).status_code == 500
    monkeypatch.setattr(db.session, "commit", original_commit)
    assert PrepPlan.query.count() == 0
    assert PrepRecipe.query.count() == 1


def test_prep_html_controls_and_mobile_layout(authenticated_client, admin_dataset):
    assert _create(authenticated_client).status_code == 303
    index = BeautifulSoup(authenticated_client.get("/prep").data, "html.parser")
    assert index.select_one('a[data-page="prep"][aria-current="page"]')
    plan = BeautifulSoup(authenticated_client.get("/prep/plans").data, "html.parser")
    assert not plan.select('input[type="range"]')
    assert "スライダーで調整" not in plan.get_text()
    number = plan.select_one('input[name^="quantity_"][type="number"]')
    assert number and number["inputmode"] == "numeric"
    assert number["step"] == "1" and number["min"] == "0" and number["max"] == "1000"
    assert plan.select_one('button[data-step="-10"]') and plan.select_one('button[data-step="10"]')
    assert "−10" in plan.get_text() and "＋10" in plan.get_text()
    assert plan.select_one('a[href="/material-orders"]')
    assert plan.select_one('input[name="csrf_token"]')
    assert plan.select_one('meta[name="viewport"]')


def _bulk(client, day, quantities):
    return client.post("/prep/plans/bulk", data={
        "csrf_token": _token(client, "/prep?date=" + day),
        "business_date": day,
        "recipe_id": [str(recipe_id) for recipe_id in quantities],
        **{f"quantity_{recipe_id}": str(quantity) for recipe_id, quantity in quantities.items()},
    })


def test_three_step_entry_recipe_route_and_selection(authenticated_client, admin_dataset):
    assert _create(authenticated_client).status_code == 303
    assert _create(authenticated_client, source="template:melonpan").status_code == 303
    recipes = PrepRecipe.query.order_by(PrepRecipe.id).all()
    today = authenticated_client.get("/prep")
    assert today.status_code == 200
    soup = BeautifulSoup(today.data, "html.parser")
    assert [h.get_text(strip=True) for h in soup.select(".prep-step h2")] == [
        "① 今日作るパンを選ぶ", "② 作る個数を決める", "③ 必要な材料を見る",
    ]
    assert soup.select_one('a[href="/prep/recipes"]')
    assert soup.select_one('form[action="/prep/plans/bulk"]')
    assert soup.select_one('#prep-stock-details:not([open])')
    assert soup.select_one('#prep-breakdown:not([open])')
    assert not soup.select('details.prep-slider, input[type="range"]')
    assert len(soup.select('.prep-choice input[name="recipe_id"]')) == 2
    assert all(not checkbox.has_attr("checked") for checkbox in soup.select(".prep-choice input"))
    recipe_page = BeautifulSoup(authenticated_client.get("/prep/recipes").data, "html.parser")
    assert recipe_page.select_one("h1").get_text(strip=True) == "レシピ管理"
    assert len(recipe_page.select('a[href^="/prep/recipes/"]')) >= 2
    saved = _bulk(authenticated_client, "2026-10-15", {recipes[0].id: 0, recipes[1].id: 100})
    assert saved.status_code == 303 and saved.location.endswith("/prep?date=2026-10-15")
    assert _bulk(authenticated_client, "2026-10-15", {recipes[0].id: 0, recipes[1].id: 100}).status_code == 303
    assert PrepPlan.query.count() == 2
    restored = BeautifulSoup(authenticated_client.get("/prep?date=2026-10-15").data, "html.parser")
    assert len(restored.select('.prep-choice input[checked]')) == 2
    assert {field["value"] for field in restored.select('input[name^="quantity_"]')} == {"0", "100"}
    another_day = BeautifulSoup(authenticated_client.get("/prep?date=2026-10-16").data, "html.parser")
    assert not another_day.select('.prep-choice input[checked]')
    assert authenticated_client.get("/prep?date=2026-02-30").status_code == 400


def test_quantity_buttons_adjust_from_current_value_and_reject_invalid_input():
    script = Path(__file__).resolve().parents[1] / "static" / "production_prep.js"
    values = ["400", "400", "25", "25", "5", "995", "1000", "0", "", "-1", "1.5", "1e3", "oops"]
    steps = [10, -10, 10, -10, -10, 10, 10, -10, 10, 10, 10, 10, 10]
    code = """
const {stepQuantity} = require(process.argv[1]);
const values = JSON.parse(process.argv[2]);
const steps = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(values.map((value, i) => stepQuantity(value, steps[i]))));
"""
    result = subprocess.run(
        ["node", "-e", code, str(script), json.dumps(values), json.dumps(steps)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [410, 390, 35, 15, 0, 1000, 1000, 0,
                                         None, None, None, None, None]


def test_direct_one_unit_quantity_and_odd_quantity_keep_recipe_ratio(authenticated_client, admin_dataset):
    assert _create(authenticated_client).status_code == 303
    recipe = PrepRecipe.query.one()
    day = "2026-10-15"
    assert _bulk(authenticated_client, day, {recipe.id: 1}).status_code == 303
    page = authenticated_client.get("/prep?date=" + day).get_data(as_text=True)
    assert 'value="1"' in page
    assert "必要 <output>50</output>g" in page
    assert "必要 <output>2.5</output>g" in page
    assert _bulk(authenticated_client, day, {recipe.id: 25}).status_code == 303
    assert PrepPlan.query.one().quantity == 25
    page = authenticated_client.get("/prep?date=" + day).get_data(as_text=True)
    assert "必要 <output>1250</output>g" in page
    assert "必要 <output>62.5</output>g" in page


def test_bulk_atomic_update_no_implicit_deletion_and_rollback(authenticated_client, admin_dataset, monkeypatch):
    assert _create(authenticated_client).status_code == 303
    assert _create(authenticated_client, source="template:melonpan").status_code == 303
    first, second = PrepRecipe.query.order_by(PrepRecipe.id).all()
    day = "2026-10-15"
    assert _bulk(authenticated_client, day, {first.id: 1, second.id: 1000}).status_code == 303
    assert _bulk(authenticated_client, day, {first.id: 20}).status_code == 303
    assert {(plan.recipe_id, plan.quantity) for plan in PrepPlan.query.all()} == {(first.id, 20), (second.id, 1000)}
    assert _bulk(authenticated_client, "2026-10-16", {first.id: 0}).status_code == 303
    assert PrepPlan.query.count() == 3
    bad = {"csrf_token": _token(authenticated_client, "/prep"), "business_date": day,
           "recipe_id": [str(first.id), str(second.id)],
           f"quantity_{first.id}": "8", f"quantity_{second.id}": "1001"}
    for invalid in ("1001", "-1", "1.5", "1e3", "oops", ""):
        bad[f"quantity_{second.id}"] = invalid
        response = authenticated_client.post("/prep/plans/bulk", data=bad)
        assert response.status_code == 400
        if invalid == "1001":
            form = BeautifulSoup(response.data, "html.parser")
            assert form.select_one(f'input[name="quantity_{first.id}"]')["value"] == "8"
            assert form.select_one(f'input[name="quantity_{second.id}"]')["value"] == "1001"
    bad[f"quantity_{second.id}"] = ["2", "3"]
    assert authenticated_client.post("/prep/plans/bulk", data=bad).status_code == 400
    assert PrepPlan.query.filter_by(business_date=datetime.date.fromisoformat(day), recipe_id=first.id).one().quantity == 20
    original_commit = db.session.commit
    monkeypatch.setattr(db.session, "commit", lambda: (_ for _ in ()).throw(SQLAlchemyError("forced")))
    assert _bulk(authenticated_client, day, {first.id: 3, second.id: 4}).status_code == 500
    monkeypatch.setattr(db.session, "commit", original_commit)
    assert {(p.recipe_id, p.quantity) for p in PrepPlan.query.filter_by(business_date=datetime.date.fromisoformat(day))} == {(first.id, 20), (second.id, 1000)}
    assert MaterialOrderItem.query.count() == 0


def test_bulk_dataset_auth_csrf_xss_and_duplicate_ids(authenticated_client, client, flask_app, admin_dataset):
    assert _create(authenticated_client).status_code == 303
    own = PrepRecipe.query.one()
    guest, other = _guest(flask_app)
    alien = PrepRecipe(dataset_id=other.id, product_key="product:999", display_name="<script>秘密</script>", base_quantity=10)
    db.session.add(alien); db.session.commit()
    day = "2026-10-15"
    data = {"business_date": day, "recipe_id": [str(own.id), str(alien.id)],
            f"quantity_{own.id}": "2", f"quantity_{alien.id}": "3"}
    assert authenticated_client.post("/prep/plans/bulk", data=data).status_code == 400
    data["csrf_token"] = _token(authenticated_client, "/prep")
    assert authenticated_client.post("/prep/plans/bulk", data=data).status_code == 404
    assert PrepPlan.query.count() == 0
    data["recipe_id"] = [str(own.id), str(own.id)]
    assert authenticated_client.post("/prep/plans/bulk", data=data).status_code == 400
    g.pop("_login_user", None)
    assert guest.get("/prep/recipes").status_code == 403
    assert guest.post("/prep/plans/bulk", data=data).status_code in (400, 403)
    g.pop("_login_user", None)
    assert client.get("/prep").status_code == 302
    assert client.post("/prep/plans/bulk", data=data).status_code in (302, 400)
    own.display_name = "<script>危険</script>"
    db.session.commit()
    g.pop("_login_user", None)
    html = authenticated_client.get("/prep").get_data(as_text=True)
    assert "&lt;script&gt;危険&lt;/script&gt;" in html
    assert "<script>危険</script>" not in html
    assert "秘密" not in html
