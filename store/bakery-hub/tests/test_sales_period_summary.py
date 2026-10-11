"""期間別売上ダッシュボードの境界値・比較・Dataset分離。"""
import datetime

import pytest

import app as app_module
from models import DailySales, Dataset, Product, db
from sales_period_summary import period_bounds


@pytest.fixture
def period_records(flask_app, admin_dataset, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: datetime.date(2026, 10, 14))
    product = Product(dataset=admin_dataset, year=2026, month=10, name="クロワッサン", price=200)
    second = Product(dataset=admin_dataset, year=2026, month=10, name="塩パン", price=120)
    previous = Product(dataset=admin_dataset, year=2026, month=9, name="旧商品", price=300)
    db.session.add_all([product, second, previous])
    db.session.flush()
    db.session.add_all([
        DailySales(product_id=product.id, date=datetime.date(2026, 10, 12), quantity=3),
        DailySales(product_id=product.id, date=datetime.date(2026, 10, 14), quantity=5),
        DailySales(product_id=second.id, date=datetime.date(2026, 10, 13), quantity=10),
        DailySales(product_id=product.id, date=datetime.date(2026, 10, 5), quantity=2),
        DailySales(product_id=previous.id, date=datetime.date(2026, 9, 30), quantity=6),
    ])
    db.session.commit()


def test_week_to_date_compares_equal_number_of_days(authenticated_client, period_records):
    response = authenticated_client.get("/api/sales-period-summary?period=week&offset=0")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["total_sales"] == 2800
    assert payload["previous_sales"] == 400
    assert payload["difference"] == 2400
    assert payload["comparison_rate"] == 600.0
    assert payload["chart_values"] == [600, 1200, 1000, None, None, None, None]
    assert payload["rankings"] == [
        {"name": "塩パン", "quantity": 10, "revenue": 1200},
        {"name": "クロワッサン", "quantity": 8, "revenue": 1600},
    ]


def test_previous_week_full_range(authenticated_client, period_records):
    payload = authenticated_client.get(
        "/api/sales-period-summary?period=week&offset=-1"
    ).get_json()
    assert payload["total_sales"] == 400
    assert payload["chart_values"] == [400, 0, 0, 0, 0, 0, 0]


@pytest.mark.parametrize("query", [
    "period=unknown", "period=week&offset=1", "period=week&offset=-105",
    "period=week&offset=no", "period=month&offset=1",
])
def test_invalid_summary_filter_returns_400(authenticated_client, period_records, query):
    assert authenticated_client.get("/api/sales-period-summary?" + query).status_code == 400


def test_calendar_bounds_cross_year_and_leap_day():
    assert period_bounds("week", -1, datetime.date(2026, 1, 1)) == (
        datetime.date(2025, 12, 22), datetime.date(2025, 12, 28)
    )
    assert period_bounds("month", -1, datetime.date(2026, 1, 1)) == (
        datetime.date(2025, 12, 1), datetime.date(2025, 12, 31)
    )
    assert period_bounds("month", 0, datetime.date(2024, 2, 29)) == (
        datetime.date(2024, 2, 1), datetime.date(2024, 2, 29)
    )


def test_day_and_year_summary(authenticated_client, period_records):
    day = authenticated_client.get(
        "/api/sales-period-summary?period=day"
    ).get_json()
    assert day["total_sales"] == 1000
    assert day["previous_sales"] == 1200
    year = authenticated_client.get(
        "/api/sales-period-summary?period=year"
    ).get_json()
    assert year["total_sales"] == 5000
    assert len(year["chart_values"]) == 12
    assert year["chart_values"][10:] == [None, None]


def test_dashboard_contains_period_controls(authenticated_client, period_records):
    html = authenticated_client.get("/dashboard").get_data(as_text=True)
    for value in ('data-sales-period="day"', 'data-sales-period="week"',
                  'data-sales-period="month"', 'data-sales-period="year"'):
        assert value in html
    assert 'periodSalesChart' in html
    assert 'dashboard-legacy-details' in html


def test_dataset_isolation_for_period_summary(authenticated_client, period_records, admin_dataset):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = Dataset(
        kind="guest", system_key=None, created_at=now, last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    outsider = Product(dataset=guest, year=2026, month=10, name="ゲスト限定", price=999)
    db.session.add_all([guest, outsider])
    db.session.flush()
    db.session.add(DailySales(
        product_id=outsider.id, date=datetime.date(2026, 10, 14), quantity=100
    ))
    db.session.commit()
    payload = authenticated_client.get("/api/sales-period-summary?period=week").get_json()
    assert payload["total_sales"] == 2800
    assert all(item["name"] != "ゲスト限定" for item in payload["rankings"])
