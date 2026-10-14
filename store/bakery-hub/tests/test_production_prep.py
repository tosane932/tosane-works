"""仕込み計算、Admin分離、DB保存、発注電話リンク。"""
import datetime
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
    assert plan.select_one('input[type="range"][step="1"]')
    assert plan.select_one('input[name="quantity"][type="number"]')
    assert plan.select_one('button[data-step="-1"]') and plan.select_one('button[data-step="1"]')
    assert plan.select_one('a[href="/material-orders"]')
    assert plan.select_one('input[name="csrf_token"]')
    assert plan.select_one('meta[name="viewport"]')
