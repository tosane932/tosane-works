"""正式切替は明示承認・Datasetロック・再監査を要求する。"""

import datetime
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy import event

from models import (DailySales, Product, RegisterCutover, RegisterCutoverApproval,
                    RegisterPriceHistory, RegisterProduct, db)
from register_cutover_service import CutoverBlocked, confirm_cutover
from register_products import create_custom_product, ensure_standard_products, update_register_product
from tests.test_register_routes import guest


def today():
    return datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()


def confirmation(**overrides):
    values = dict(date=today().isoformat(), approved_by="admin", reason="店長が営業日境界を確認",
                  device_inventory="端末A 0件、端末B 0件", devices_checked=True,
                  backup_checked=True)
    values.update(overrides)
    return values


def test_price_history_records_only_observed_period_and_is_atomic(flask_app, admin_dataset):
    existing = RegisterProduct(dataset_id=admin_dataset.id, name="導入以前の商品", price_yen=180)
    db.session.add(existing)
    db.session.commit()
    ensure_standard_products(admin_dataset)
    assert RegisterPriceHistory.query.filter_by(register_product_id=existing.id).count() == 0
    assert RegisterPriceHistory.query.filter_by(dataset_id=admin_dataset.id, event="created").count() == 17 + 1
    item = create_custom_product(admin_dataset, name="新商品", price_yen=200)
    rows = RegisterPriceHistory.query.filter_by(register_product_id=item.id).all()
    assert [(r.event, r.old_price_yen, r.new_price_yen) for r in rows] == [("created", None, 200)]
    update_register_product(admin_dataset, item.id, name="新商品", price_yen=250,
                            expected_name="新商品", expected_price_yen=200)
    update_register_product(admin_dataset, item.id, name="改名", price_yen=250,
                            expected_name="新商品", expected_price_yen=250)
    rows = RegisterPriceHistory.query.filter_by(register_product_id=item.id).order_by(RegisterPriceHistory.id).all()
    assert [(r.event, r.old_price_yen, r.new_price_yen) for r in rows] == [
        ("created", None, 200), ("changed", 200, 250)]
    assert db.session.get(RegisterProduct, item.id).price_yen == 250
    with pytest.raises(ValueError):
        update_register_product(admin_dataset, item.id, name="破棄", price_yen=100001)
    assert RegisterPriceHistory.query.filter_by(register_product_id=item.id).count() == 2


def test_cutover_requires_admin_evidence_and_rechecks_locked_rows(flask_app, admin_dataset):
    with pytest.raises(ValueError):
        confirm_cutover(admin_dataset, **confirmation(devices_checked=False))
    old = Product(dataset_id=admin_dataset.id, year=today().year, month=today().month,
                  name="旧商品", price=150)
    db.session.add(old)
    db.session.flush()
    db.session.add(DailySales(product_id=old.id, date=today(), quantity=1))
    db.session.commit()
    with pytest.raises(CutoverBlocked, match="旧売上"):
        confirm_cutover(admin_dataset, **confirmation())
    assert RegisterCutover.query.count() == RegisterCutoverApproval.query.count() == 0
    target = today() + datetime.timedelta(days=1)
    ensure_standard_products(admin_dataset)
    review = {"recipe_mapping": "旧商品のレシピ未対応を確認し、仕込み側で別途照合する"}
    cutover, created = confirm_cutover(admin_dataset, **confirmation(date=target.isoformat(),
                                                                     review_decisions=review))
    assert created and cutover.first_register_date == target
    approval = db.session.get(RegisterCutoverApproval, admin_dataset.id)
    assert approval.approved_by == "admin" and approval.reason == "店長が営業日境界を確認"
    assert approval.device_inventory == "端末A 0件、端末B 0件"
    same, created = confirm_cutover(admin_dataset, **confirmation(date=target.isoformat(),
                                                                   review_decisions=review))
    assert same.dataset_id == cutover.dataset_id and not created
    with pytest.raises(CutoverBlocked, match="上書き"):
        confirm_cutover(admin_dataset, **confirmation(date=today().isoformat()))
    assert RegisterCutoverApproval.query.count() == 1


def test_cutover_rolls_back_header_if_approval_write_fails(flask_app, admin_dataset):
    ensure_standard_products(admin_dataset)
    def reject(_mapper, _connection, _target):
        raise RuntimeError("approval storage failed")
    event.listen(RegisterCutoverApproval, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="approval storage failed"):
            confirm_cutover(admin_dataset, **confirmation())
    finally:
        event.remove(RegisterCutoverApproval, "before_insert", reject)
    assert RegisterCutover.query.count() == RegisterCutoverApproval.query.count() == 0


def test_cutover_post_csrf_flag_and_guest_boundary(flask_app, authenticated_client,
                                                   admin_dataset, csrf_token):
    path = "/register/readiness/confirm"
    payload = {"date": today().isoformat(), "reason": "全売上記録を確認しました",
               "device_inventory": "管理用端末 0件", "devices_checked": "yes",
               "backup_checked": "yes"}
    assert authenticated_client.post(path, data=payload).status_code == 400  # CSRF
    token = csrf_token(authenticated_client, "/login")
    assert authenticated_client.post(path, data=dict(payload, csrf_token=token)).status_code == 404
    flask_app.config.update(REGISTER_V3_READINESS_ENABLED=True,
                            REGISTER_V3_CUTOVER_ENABLED=True,
                            REGISTER_V3_RECOVERY_ENABLED=True)
    ensure_standard_products(admin_dataset)
    assert authenticated_client.post(path, data=payload).status_code == 400
    owner, visitor = guest(flask_app)
    g.pop("_login_user", None)
    assert visitor.get("/register/readiness").status_code in (302, 403)
    assert visitor.post(path, data=dict(payload, csrf_token=csrf_token(visitor, "/login"))).status_code in (302, 400, 403)
    assert db.session.get(RegisterCutover, owner.id) is None
    g.pop("_login_user", None)
    result = authenticated_client.post(path, data=dict(payload, csrf_token=token))
    assert result.status_code == 303
    assert db.session.get(RegisterCutover, admin_dataset.id).first_register_date == today()


def test_production_flag_requires_admin_and_cutover_and_keeps_demo(flask_app,
        authenticated_client, admin_dataset):
    flask_app.config.update(REGISTER_V3_PRODUCTION_ENABLED=True,
                            REGISTER_V3_PREVIEW_ENABLED=True,
                            REGISTER_V3_SAVE_ENABLED=True,
                            REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=True,
                            REGISTER_V3_PRODUCTS_WRITE_ENABLED=True,
                            REGISTER_V3_PUBLIC_DEMO_ENABLED=True)
    assert authenticated_client.get("/register/preview").status_code == 404
    assert authenticated_client.get("/register/demo").status_code == 200
    owner, visitor = guest(flask_app)
    g.pop("_login_user", None)
    assert visitor.get("/register/preview").status_code == 403
    assert visitor.get("/register/products").status_code == 403
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=today()))
    db.session.commit()
    g.pop("_login_user", None)
    response = authenticated_client.get("/home")
    nav = BeautifulSoup(response.data, "html.parser")
    assert nav.select_one('a[data-page="register"]')["href"] == "/register/preview"
    assert nav.select_one('a[data-page="sales"]') is None
    assert authenticated_client.get("/input").status_code == 200
    page = authenticated_client.get("/register/preview")
    assert page.status_code == 200
    assert "正式レジの会計" in page.get_data(as_text=True)
    assert authenticated_client.get("/register/products").status_code == 303
    assert authenticated_client.get("/products").status_code == 200
    flask_app.config["REGISTER_V3_SAVE_ENABLED"] = False
    assert authenticated_client.get("/register/preview").status_code == 200
    assert authenticated_client.get("/api/register/transactions/00000000-0000-0000-0000-000000000001/status").status_code == 200
    assert authenticated_client.get("/register/demo").status_code == 200
