"""Business regression contracts selected from canonical Phase 18 survivors."""
import datetime
import uuid
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from google.genai import errors

import app as app_module
import config
from conftest import post_ai
from models import DailySales, Dataset, Product, db
from prompts import build_sales_prompt
from test_guest_ai_limit import _create_guest_dataset, _guest_client, _mock_gemini, _usage_count

TODAY = datetime.date(2026, 8, 10)


def _sale(dataset, name, date, quantity):
    product = Product(dataset=dataset, name=name, price=250,
                      year=date.year, month=date.month, is_active=True)
    db.session.add(product)
    db.session.flush()
    db.session.add(DailySales(product_id=product.id, date=date, quantity=quantity))
    db.session.commit()
    return product


@pytest.mark.parametrize("month", [1, 12], ids=["january", "december"])
def test_dashboard_valid_calendar_end_months_keep_period_and_scoped_sales(
    authenticated_client, admin_dataset, month,
):
    _sale(admin_dataset, "対象商品", datetime.date(2026, month, 1), 17)
    neighbor = 2 if month == 1 else 11
    _sale(admin_dataset, "別月商品", datetime.date(2026, neighbor, 1), 99)
    path = f"?year=2026&month={month}"
    response = authenticated_client.get("/api/dashboard-data" + path)
    payload = response.get_json(silent=True) or {}
    assert (response.status_code, payload.get("period_text"), payload.get("ranked_sales")) == (
        200, f"2026年{month}月", [["対象商品", 17]],
    )
    page = authenticated_client.get("/dashboard" + path)
    document = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    assert page.status_code == 200
    assert f"2026年{month}月" in document.get_text()
    assert document.select_one("#selectMonth option[selected]")["value"] == str(month)


@pytest.mark.parametrize("selected_year", [2025, 2026, 2027], ids=["before", "target", "after"])
def test_dashboard_selected_year_excludes_both_adjacent_years(
    authenticated_client, admin_dataset, selected_year,
):
    for year, quantity in [(2025, 11), (2026, 17), (2027, 23)]:
        _sale(admin_dataset, f"{year}年商品", datetime.date(year, 8, 1), quantity)
    foreign = _create_guest_dataset()
    _sale(foreign, "別Dataset商品", datetime.date(selected_year, 8, 1), 999)
    response = authenticated_client.get(f"/api/dashboard-data?year={selected_year}")
    payload = response.get_json(silent=True) or {}
    expected = {2025: 11, 2026: 17, 2027: 23}[selected_year]
    assert (response.status_code, payload.get("period_text"), payload.get("ranked_sales")) == (
        200, f"{selected_year}年・全月", [[f"{selected_year}年商品", expected]],
    )


@pytest.mark.parametrize("selected_month", [7, 8, 9], ids=["before", "target", "after"])
def test_dashboard_selected_month_excludes_both_adjacent_months(
    authenticated_client, admin_dataset, selected_month,
):
    for month, quantity in [(7, 11), (8, 17), (9, 23)]:
        _sale(admin_dataset, f"{month}月商品", datetime.date(2026, month, 1), quantity)
    foreign = _create_guest_dataset()
    _sale(foreign, "別Dataset商品", datetime.date(2026, selected_month, 1), 999)
    response = authenticated_client.get(f"/api/dashboard-data?year=2026&month={selected_month}")
    payload = response.get_json(silent=True) or {}
    expected = {7: 11, 8: 17, 9: 23}[selected_month]
    assert (response.status_code, payload.get("period_text"), payload.get("ranked_sales")) == (
        200, f"2026年{selected_month}月", [[f"{selected_month}月商品", expected]],
    )


@pytest.mark.parametrize("reverse", [False, True], ids=["uuid-ascending", "uuid-descending"])
@pytest.mark.parametrize("role", ["admin", "guest-a", "guest-b"])
def test_dashboard_year_options_follow_ownership_for_both_uuid_directions(
    flask_app, admin_dataset, authenticated_client, monkeypatch, reverse, role,
):
    # Legal UUID v4s, legitimate registration/sale periods and fresh principal context.
    ids = [uuid.UUID(value) for value in [
        "10000000-0000-4000-8000-000000000001",
        "80000000-0000-4000-8000-000000000002",
        "f0000000-0000-4000-8000-000000000003",
    ]]
    if reverse:
        ids.reverse()
    admin_dataset.id = ids[0]
    now = datetime.datetime.now(datetime.timezone.utc)
    guests = [Dataset(id=value, kind="guest", system_key=None, created_at=now,
                      last_activity_at=now, absolute_expires_at=now + datetime.timedelta(hours=2))
              for value in ids[1:]]
    db.session.add_all(guests)
    db.session.commit()
    datasets = dict(zip(["admin", "guest-a", "guest-b"], [admin_dataset, *guests]))
    years = dict(zip(datasets, [2022, 2023, 2024]))
    for actor, dataset in datasets.items():
        _sale(dataset, actor + "商品", datetime.date(years[actor], 8, 1), 7)
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    assert app_module._get_dashboard_sales_years(datasets[role], TODAY.year) == [years[role], TODAY.year]
    client = authenticated_client if role == "admin" else _guest_client(flask_app, datasets[role])
    with flask_app.app_context():
        page = client.get("/dashboard")
    document = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    assert page.status_code == 200
    values = {option["value"] for option in document.select("#selectYear option") if option["value"]}
    assert values == {str(years[role]), str(TODAY.year)}


@pytest.mark.parametrize("name,quantity", [("a", 1), ("パン", 0)], ids=["one-character", "zero-quantity"])
def test_guest_ai_valid_lower_bounds_reach_sdk_with_complete_prompt(
    flask_app, monkeypatch, name, quantity,
):
    guest = _create_guest_dataset()
    # ORM allows zero rows; this protects the documented legacy-summary boundary.
    _sale(guest, name, TODAY, quantity)
    generate = _mock_gemini(monkeypatch)
    ranked = list(app_module._get_sales_from_db(guest, TODAY.year, TODAY.month).items())
    assert ranked == [(name, quantity)]
    client = _guest_client(flask_app, guest)
    with flask_app.app_context():
        response = post_ai(client, f"/api/ai-advice?year={TODAY.year}&month={TODAY.month}")
    generate.assert_called_once_with(model=config.GEMINI_MODEL,
                                     contents=build_sales_prompt(f"{name}: {quantity}個"))
    assert _usage_count(guest.id) == 1
    assert response.status_code == 200


@pytest.mark.parametrize("name,quantity", [("", 1), ("パン", -1)], ids=["empty-name", "negative-quantity"])
def test_guest_ai_invalid_lower_bounds_precede_sdk_and_usage(
    flask_app, monkeypatch, name, quantity,
):
    guest = _create_guest_dataset()
    # Approved ORM has no empty-name/nonnegative-quantity CHECK; validate legacy data.
    _sale(guest, name, TODAY, quantity)
    generate = _mock_gemini(monkeypatch)
    reserve = Mock(wraps=app_module._reserve_guest_ai_usage)
    monkeypatch.setattr(app_module, "_reserve_guest_ai_usage", reserve)
    ranked = list(app_module._get_sales_from_db(guest, TODAY.year, TODAY.month).items())
    assert ranked == [(name, quantity)]
    client = _guest_client(flask_app, guest)
    with flask_app.app_context():
        response = post_ai(client, f"/api/ai-advice?year={TODAY.year}&month={TODAY.month}")
    assert (response.status_code, app_module.genai.Client.call_count, generate.call_count,
            reserve.call_count, _usage_count(guest.id)) == (400, 0, 0, 0, 0), (
        "Invalid legacy summary must be rejected before SDK/usage"
    )
    app_module.genai.Client.assert_not_called()
    generate.assert_not_called()
    reserve.assert_not_called()
    assert _usage_count(guest.id) == 0


RATE_LIMIT_MESSAGE = (
    "☕【AIが少し休憩中です】\n"
    "短時間に多くの分析を行ったため、AIの利用制限がかかりました。"
    "少し時間を置いてから、もう一度お試しください。"
)
UNAVAILABLE_MESSAGE = (
    "🥐【AIアシスタントが混み合っています】\n"
    "売上データは正常に保存・集計されています。"
    "少し時間を置いてから、もう一度お試しください。"
)


@pytest.mark.parametrize("family,marker", [
    (429, "code-only"), (429, "status-only"),
    (503, "code-only"), (503, "status-only"),
])
def test_ai_fallback_recognizes_code_and_status_independently(
    flask_app, admin_dataset, authenticated_client, monkeypatch, family, marker,
):
    if marker == "code-only":
        error_type = errors.ClientError if family == 429 else errors.ServerError
        failure = error_type(family, {"error": {"message": "test-code-only"}})
        assert ("RESOURCE_EXHAUSTED" if family == 429 else "UNAVAILABLE") not in str(failure)
    else:
        failure = RuntimeError("RESOURCE_EXHAUSTED" if family == 429 else "UNAVAILABLE")
        assert str(family) not in str(failure)
    generate = _mock_gemini(monkeypatch, side_effect=failure)
    _sale(admin_dataset, "パン", TODAY, 1)
    response = post_ai(authenticated_client, f"/api/ai-advice?year={TODAY.year}&month={TODAY.month}")
    assert response.status_code == 200
    actual = response.get_json()["ai_advice"]
    expected = RATE_LIMIT_MESSAGE if family == 429 else UNAVAILABLE_MESSAGE
    assert actual == expected
    generate.assert_called_once_with(model=config.GEMINI_MODEL,
                                     contents=build_sales_prompt("パン: 1個"))
    assert _usage_count(admin_dataset.id) == 0
