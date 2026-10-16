"""明示的に有効化した集計画面と旧入力POSTの切替条件。"""

import datetime
import uuid
from unittest.mock import Mock

import pytest
from flask import g

import app as app_module
from conftest import post_ai
from models import (DailySales, Dataset, Product, RegisterCutover, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, db)
from legacy_sales_cutover import LegacySalesClosed
from seed_demo import seed_demo_data


OLD = datetime.date(2026, 10, 15)
TODAY = datetime.date(2026, 10, 16)


def _legacy(owner, date=OLD, qty=2):
    product = Product(dataset_id=owner.id, year=date.year, month=date.month,
                      name="クロワッサン", price=100, is_active=True)
    db.session.add(product)
    db.session.flush()
    db.session.add(DailySales(product_id=product.id, date=date, quantity=qty))
    db.session.commit()
    return product


def _register(owner, qty=2, price=120, *, kind="sale", product=None):
    if product is None:
        product = RegisterProduct(dataset_id=owner.id, name="クロワッサン", price_yen=price)
        db.session.add(product)
        db.session.flush()
    amount = price * qty
    record = RegisterTransaction(dataset_id=owner.id, submission_key=uuid.uuid4(),
        request_digest="a" * 64, business_date=TODAY, kind=kind, total_yen=amount,
        received_yen=amount if kind == "sale" else None,
        change_yen=0 if kind == "sale" else None)
    db.session.add(record)
    db.session.flush()
    db.session.add(RegisterTransactionLine(transaction_id=record.id,
        dataset_id=owner.id, register_product_id=product.id,
        product_name_snapshot="クロワッサン", unit_price_yen=price,
        quantity=qty, line_total_yen=amount))
    db.session.commit()
    return product, record


@pytest.fixture
def unified(flask_app, admin_dataset, monkeypatch):
    flask_app.config["REGISTER_V3_AGGREGATION_ENABLED"] = True
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    return admin_dataset


def test_flag_off_keeps_old_dashboard_and_home(authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    _legacy(admin_dataset, TODAY, 1)
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                   first_register_date=TODAY + datetime.timedelta(days=1)))
    db.session.commit()
    assert authenticated_client.get("/api/dashboard-data").get_json()["today_sales"] == 100
    assert "¥100" in authenticated_client.get("/home").get_data(as_text=True)


def test_cutover_dashboard_home_api_and_refund(authenticated_client, unified):
    old = _legacy(unified)
    db.session.add(RegisterCutover(dataset_id=unified.id, first_register_date=TODAY))
    db.session.commit()
    product, _ = _register(unified, qty=3)
    _register(unified, qty=-1, kind="refund", product=product)
    response = authenticated_client.get("/dashboard?year=2026&month=10")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "¥440" in html  # 旧200 + 差引レジ240
    assert "レジ導入後の会計：2件" in html
    assert "販売1件・返金1件" in html
    assert "¥120" in html  # レジ平均240 / 2、旧200は分子に含まない
    assert "¥240" in authenticated_client.get("/home").get_data(as_text=True)
    payload = authenticated_client.get("/api/dashboard-data?year=2026&month=10").get_json()
    assert payload["today_sales"] == 240 and payload["transaction_count"] == 2
    assert {tuple(row) for row in payload["ranked_sales"]} == {
        (f"クロワッサン（旧商品 #{old.id}）", 2),
        (f"クロワッサン（レジ商品 #{product.id}）", 2),
    }
    period = authenticated_client.get("/api/sales-period-summary?period=week").get_json()
    assert period["total_sales"] == 440
    assert period["transaction_count"] == 2
    assert period["average_display_yen"] == 120
    assert period["chart_values"][3:5] == [200, 240]
    assert len(period["rankings"]) == 2
    assert {row["revenue"] for row in period["rankings"]} == {200, 240}


def test_zero_net_and_missing_home_are_distinct(authenticated_client, unified):
    db.session.add(RegisterCutover(dataset_id=unified.id, first_register_date=TODAY))
    db.session.commit()
    assert "未入力" in authenticated_client.get("/home").get_data(as_text=True)
    assert "売上記録なし" in authenticated_client.get("/dashboard").get_data(as_text=True)
    product, _ = _register(unified, qty=1)
    _register(unified, qty=-1, kind="refund", product=product)
    html = authenticated_client.get("/home").get_data(as_text=True)
    assert "¥0" in html and "未入力" not in html
    assert "売上記録あり" in authenticated_client.get("/dashboard").get_data(as_text=True)
    payload = authenticated_client.get("/api/sales-period-summary?period=day").get_json()
    assert payload["has_record"] is True and payload["total_sales"] == 0
    assert payload["transaction_count"] == 2 and payload["average_display_yen"] == 0


@pytest.mark.parametrize("period,old_index,new_index", [
    ("week", 3, 4), ("month", 14, 15), ("year", 9, 9),
])
def test_period_api_day_week_month_year_keep_calendar_buckets(
        authenticated_client, unified, period, old_index, new_index):
    _legacy(unified)
    db.session.add(RegisterCutover(dataset_id=unified.id, first_register_date=TODAY))
    db.session.commit()
    _register(unified, qty=-1, kind="refund")
    payload = authenticated_client.get(
        f"/api/sales-period-summary?period={period}").get_json()
    assert payload["total_sales"] == 80  # 旧200 + 返金-120
    assert payload["chart_values"][old_index] == (80 if period == "year" else 200)
    if period != "year":
        assert payload["chart_values"][new_index] == -120
    assert payload["transaction_count"] == payload["refund_count"] == 1
    assert payload["average_display_yen"] == -120


def test_integrity_error_returns_503_without_sales(authenticated_client, unified, caplog):
    db.session.add(RegisterCutover(dataset_id=unified.id, first_register_date=TODAY))
    db.session.commit()
    product, record = _register(unified)
    record.total_yen = 241
    record.received_yen = 241
    db.session.commit()
    response = authenticated_client.get("/api/dashboard-data")
    assert response.status_code == 503
    assert response.get_json()["error"] == "sales_integrity_error"
    assert "today_sales" not in response.get_json()
    assert authenticated_client.get("/dashboard").status_code == 503
    assert authenticated_client.get("/home").status_code == 503
    assert authenticated_client.get("/api/sales-period-summary").status_code == 503
    ai_response = post_ai(authenticated_client, "/api/ai-advice")
    assert ai_response.status_code == 503
    assert ai_response.get_json()["error"] == "sales_integrity_error"
    assert "SalesAggregationConsistencyError" in caplog.text
    assert str(unified.id) not in caplog.text
    assert product.id


def test_cutover_violation_returns_safe_html_and_json(authenticated_client, unified, caplog):
    _legacy(unified, TODAY)
    db.session.add(RegisterCutover(dataset_id=unified.id, first_register_date=TODAY))
    db.session.commit()
    response = authenticated_client.get("/dashboard")
    assert response.status_code == 503
    assert "売上データを確認できませんでした" in response.get_data(as_text=True)
    response = authenticated_client.get("/api/sales-period-summary?period=day")
    assert response.status_code == 503
    assert set(response.get_json()) == {"error", "message", "incident_id"}
    assert response.get_json()["error"] == "sales_integrity_error"
    assert "SalesCutoverIntegrityError" in caplog.text
    assert "old=1" in caplog.text
    assert str(unified.id) not in caplog.text


def test_legacy_post_closed_at_cutover_even_when_flag_off(authenticated_client,
                                                           admin_dataset, csrf_post, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    product = _legacy(admin_dataset, TODAY, 3)
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=TODAY))
    db.session.commit()
    response = csrf_post(authenticated_client, "/input", {
        "date": TODAY.isoformat(), "product_id": [str(product.id)], "quantity": ["99"]})
    assert response.status_code == 409
    assert DailySales.query.filter_by(product_id=product.id).one().quantity == 3
    assert "保存できません" in authenticated_client.get("/input").get_data(as_text=True)
    old = _legacy(admin_dataset, OLD, 1)
    response = csrf_post(authenticated_client, "/input", {
        "date": OLD.isoformat(), "product_id": [str(old.id)], "quantity": ["4"]})
    assert response.status_code == 200
    assert DailySales.query.filter_by(product_id=old.id).one().quantity == 4


def test_uncutover_legacy_post_and_seed_cutover_guard(authenticated_client,
                                                      admin_dataset, csrf_post, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    product = Product(dataset_id=admin_dataset.id, year=2026, month=10,
                      name="販売", price=100, is_active=True)
    db.session.add(product)
    db.session.commit()
    response = csrf_post(authenticated_client, "/input", {
        "date": TODAY.isoformat(), "product_id": [str(product.id)], "quantity": ["3"]})
    assert response.status_code == 200
    assert DailySales.query.filter_by(product_id=product.id).one().quantity == 3
    # seed用の別Datasetでは書込前に日付を検証する。既存商品があれば処理をskipするため、
    # ここでは管理者の旧行を消さず、helperの境界自体を別テストで保証する。
    from legacy_sales_cutover import assert_legacy_sales_writable
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=TODAY))
    db.session.commit()
    with pytest.raises(LegacySalesClosed):
        assert_legacy_sales_writable(admin_dataset, TODAY)
    db.session.rollback()
    assert DailySales.query.filter_by(product_id=product.id).one().quantity == 3


def test_guest_datasets_do_not_mix_when_unified(flask_app, unified):
    now = datetime.datetime.now(datetime.timezone.utc)
    guests = [Dataset(kind="guest", created_at=now, last_activity_at=now,
                      absolute_expires_at=now + datetime.timedelta(hours=2))
              for _ in range(2)]
    db.session.add_all(guests)
    db.session.commit()
    for owner, quantity in [(unified, 1), (guests[0], 2), (guests[1], 3)]:
        db.session.add(RegisterCutover(dataset_id=owner.id, first_register_date=TODAY))
        db.session.commit()
        _register(owner, qty=quantity)
    for owner, expected in [(guests[0], 240), (guests[1], 360)]:
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = f"guest:{owner.id}"
            session["_fresh"] = True
        # flask_app fixtureの長寿命app context内ではFlask-Loginのgキャッシュを明示的に消す。
        g.pop("_login_user", None)
        assert client.get("/api/dashboard-data").get_json()["today_sales"] == expected
        assert f"¥{expected}" in client.get("/home").get_data(as_text=True)
    assert db.session.query(RegisterTransaction).count() == 3


def test_demo_seed_rejects_cutover_days(flask_app, admin_dataset):
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                   first_register_date=datetime.date(2026, 10, 1)))
    db.session.commit()
    with pytest.raises(LegacySalesClosed):
        seed_demo_data(reference_date=TODAY)
    assert Product.query.filter_by(dataset_id=admin_dataset.id).count() == 0
    assert DailySales.query.count() == 0


def test_aggregation_flag_rejected_outside_test_or_local(flask_app):
    flask_app.config.update(REGISTER_V3_AGGREGATION_ENABLED=True,
                            TESTING=False, LOCAL_DEVELOPMENT=False)
    assert app_module._unified_sales_enabled() is False


def test_guest_ai_uses_signed_net_and_preserves_usage(flask_app, unified, monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = Dataset(kind="guest", created_at=now, last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(guest)
    db.session.flush()
    db.session.add(RegisterCutover(dataset_id=guest.id, first_register_date=TODAY))
    db.session.commit()
    product, _ = _register(guest, qty=2)
    _register(guest, qty=-1, kind="refund", product=product)
    fake = Mock()
    fake.models.generate_content.return_value.text = "助言"
    monkeypatch.setattr(app_module.genai, "Client", lambda **kwargs: fake)
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-key")
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{guest.id}"
        session["_fresh"] = True
    # 認証sessionは既存のGuest用fixture形式と一致することを確認する。
    response = post_ai(client, "/api/ai-advice")
    assert response.status_code == 200
    assert "差引1個、差引120円" in fake.models.generate_content.call_args.kwargs["contents"]
    assert db.session.get(Dataset, guest.id).guest_ai_usage_count == 1
