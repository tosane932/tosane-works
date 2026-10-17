"""切替前監査はDBを変更せず、認可主体と商品IDの境界を維持する。"""

import datetime
import uuid
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy import event

from models import (DailySales, Dataset, PrepPlan, PrepRecipe, Product, RegisterCutover,
                    RegisterPrepLink, RegisterProduct, RegisterTransaction,
                    RegisterTransactionLine, db)
from register_cutover_audit import audit_cutover, planned_business_day
from register_products import ensure_standard_products
from tests.test_register_image_prep_reference import guest, register


DAY = datetime.date(2026, 10, 17)


def _old(owner, date, price=100, name="同名"):
    product = Product(dataset_id=owner.id, year=date.year, month=date.month,
                      name=name, price=price)
    db.session.add(product)
    db.session.flush()
    db.session.add(DailySales(product_id=product.id, date=date, quantity=2))
    db.session.commit()
    return product


def _custom(owner, name="同名", price=120):
    product = RegisterProduct(dataset_id=owner.id, name=name, price_yen=price)
    db.session.add(product)
    db.session.commit()
    return product


def _audit(owner, day=None):
    return audit_cutover(owner, planned_date=day, today=DAY - datetime.timedelta(days=1))


def test_audit_and_planned_day_read_only(flask_app, authenticated_client, admin_dataset):
    flask_app.config["REGISTER_V3_READINESS_ENABLED"] = True
    old = _old(admin_dataset, DAY - datetime.timedelta(days=1), 501)
    ensure_standard_products(admin_dataset)
    recipe = PrepRecipe(dataset_id=admin_dataset.id, product_key=f"product:{old.id}",
                        display_name="旧商品", base_quantity=10)
    db.session.add(recipe)
    db.session.commit()
    statements = []
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.split()[0].upper())
    event.listen(db.engine, "before_cursor_execute", observe)
    try:
        result = _audit(admin_dataset, DAY)
        response = authenticated_client.get("/register/readiness?date=2026-10-17")
    finally:
        event.remove(db.engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    assert result["counts"]["old_products"] == 1
    assert result["counts"]["old_sales"] == 1
    assert result["counts"]["register_products"] == 18
    assert result["counts"]["transactions"] == 0
    assert result["old_last_day"] == DAY - datetime.timedelta(days=1)
    assert result["expensive_old"][0]["id"] == old.id
    assert result["status"] == "review"
    assert not result["device_queue_known"]
    assert set(statements) <= {"SELECT", "PRAGMA"}
    assert RegisterCutover.query.count() == 0
    assert "端末内の未同期会計" in response.get_data(as_text=True)
    assert planned_business_day("2026-10-17") == DAY
    for invalid in ("2026-02-30", "2026-10-17anything", "1999-01-01"):
        assert authenticated_client.get("/register/readiness?date=" + invalid).status_code == 400
    assert RegisterCutover.query.count() == 0


def test_audit_blockers_overlap_boundaries_and_mismatched_lines(flask_app, admin_dataset):
    old = _old(admin_dataset, DAY)
    register_product = _custom(admin_dataset)
    register(admin_dataset, DAY, (register_product, "以前の名前", 120, 1))
    result = _audit(admin_dataset, DAY)
    assert result["status"] == "blocked" and result["overlap_days"] == [DAY]
    assert result["old_after"] == 1
    assert result["header_line_mismatch"] == 0
    assert result["unmapped_old"][0]["id"] == old.id
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                   first_register_date=DAY + datetime.timedelta(days=1)))
    db.session.commit()
    result = _audit(admin_dataset, DAY)
    assert result["status"] == "blocked"
    assert result["new_before"] == 1
    assert any("異なります" in reason for reason in result["blockers"])
    line = RegisterTransactionLine.query.one()
    line.quantity = 2
    line.line_total_yen = 240
    db.session.commit()
    assert _audit(admin_dataset, DAY)["header_line_mismatch"] == 1
    assert RegisterTransaction.query.one().total_yen == 120
    assert RegisterTransactionLine.query.one().product_name_snapshot == "以前の名前"


def test_new_only_without_cutover_is_blocked_and_existing_cutover_can_be_reviewed(
        flask_app, admin_dataset):
    product = _custom(admin_dataset, price=0)
    register(admin_dataset, DAY, (product, "無償", 0, 1))
    assert _audit(admin_dataset)["status"] == "blocked"
    assert _audit(admin_dataset, DAY)["status"] == "blocked"
    assert any("過去の営業日" in reason for reason in audit_cutover(
        admin_dataset, planned_date=DAY - datetime.timedelta(days=2), today=DAY)["blockers"])
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=DAY))
    db.session.commit()
    result = _audit(admin_dataset, DAY)
    assert result["counts"]["transactions"] == result["counts"]["lines"] == 1
    assert result["status"] == "blocked"  # 有償品なしでは切替を許可しない。


def test_readiness_gate_auth_and_guest_boundary(flask_app, client, authenticated_client,
                                                 admin_dataset):
    assert authenticated_client.get("/register/readiness").status_code == 404
    flask_app.config["REGISTER_V3_READINESS_ENABLED"] = True
    g.pop("_login_user", None)
    assert client.get("/register/readiness").status_code != 200
    owner, guest_client = guest(flask_app)
    _old(owner, DAY, name="Guestだけ")
    assert guest_client.get("/register/readiness").status_code in (302, 403)
    assert guest_client.get("/prep/register-links").status_code in (302, 403)
    g.pop("_login_user", None)
    assert "Guestだけ" not in authenticated_client.get(
        "/register/readiness?date=2026-10-18").get_data(as_text=True)
    g.pop("_login_user", None)
    assert authenticated_client.get("/register/demo").status_code == 404
    g.pop("_login_user", None)
    assert authenticated_client.get(
        f"/api/register/transactions/{uuid.uuid4()}/status").status_code == 403


def test_explicit_recipe_link_same_dataset_no_name_match_and_no_plan_mutation(
        flask_app, authenticated_client, admin_dataset, csrf_post):
    flask_app.config.update(REGISTER_V3_READINESS_ENABLED=True,
                            REGISTER_V3_AGGREGATION_ENABLED=True)
    old = _old(admin_dataset, DAY - datetime.timedelta(days=1))
    first = _custom(admin_dataset)
    second = _custom(admin_dataset)
    assert _audit(admin_dataset, DAY)["duplicate_names"][0]["ids"] == [first.id, second.id]
    recipe = PrepRecipe(dataset_id=admin_dataset.id, product_key=f"product:{old.id}",
                        display_name="同名", base_quantity=10)
    db.session.add(recipe)
    db.session.commit()
    plan = PrepPlan(dataset_id=admin_dataset.id, recipe_id=recipe.id,
                    business_date=DAY, quantity=11)
    db.session.add_all([plan, RegisterCutover(dataset_id=admin_dataset.id,
                                               first_register_date=DAY)])
    db.session.commit()
    register(admin_dataset, DAY, (first, "古い名前", 120, 2),
             (second, "同名", 120, 3))
    path = "/prep/register-links"
    response = authenticated_client.get(path)
    assert response.status_code == 200
    html = BeautifulSoup(response.data, "html.parser")
    assert f"ID {recipe.id}" in html.get_text()
    assert len(html.select('option[value=""]')) == 1
    payload = {"recipe_id": str(recipe.id), "product_id": str(first.id),
               "expected_product_id": "", "confirmed": "yes"}
    assert csrf_post(authenticated_client, path, payload).status_code == 303
    assert RegisterPrepLink.query.one().register_product_id == first.id
    shown = authenticated_client.get("/prep?date=2026-10-17").get_data(as_text=True)
    assert "2個" in shown and f"レジ商品ID {second.id}" in shown
    assert "3個" in shown
    assert csrf_post(authenticated_client, path, payload).status_code == 409
    assert csrf_post(authenticated_client, path, {**payload, "product_id": str(second.id),
                                                  "expected_product_id": str(first.id)}).status_code == 303
    assert RegisterPrepLink.query.one().register_product_id == second.id
    assert (plan.quantity, recipe.product_key, recipe.display_name) == (
        11, f"product:{old.id}", "同名")
    assert DailySales.query.one().quantity == 2
    assert RegisterTransactionLine.query.first().product_name_snapshot == "古い名前"


def test_mapping_rejects_other_dataset_template_and_unconfirmed(flask_app, authenticated_client,
                                                                  admin_dataset, csrf_post):
    flask_app.config["REGISTER_V3_READINESS_ENABLED"] = True
    other, _ = guest(flask_app)
    foreign = _custom(other)
    local = _custom(admin_dataset)
    recipe = PrepRecipe(dataset_id=admin_dataset.id, product_key="product:1",
                        display_name="ローカル", base_quantity=10)
    template = PrepRecipe(dataset_id=admin_dataset.id, product_key="template:croissant",
                          display_name="標準", base_quantity=10)
    db.session.add_all([recipe, template])
    db.session.commit()
    path = "/prep/register-links"
    base = {"recipe_id": str(recipe.id), "expected_product_id": "", "confirmed": "yes"}
    assert csrf_post(authenticated_client, path, {**base, "product_id": str(foreign.id)}).status_code == 400
    assert csrf_post(authenticated_client, path, {**base, "product_id": str(local.id),
                                                  "confirmed": ""}).status_code == 400
    assert csrf_post(authenticated_client, path, {**base, "product_id": str(local.id),
                                                  "recipe_id": str(template.id)}).status_code == 400
    assert RegisterPrepLink.query.count() == 0
