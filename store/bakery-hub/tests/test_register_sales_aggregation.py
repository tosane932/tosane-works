"""新旧売上の排他的な読み取り契約。既存公開APIは切り替えない。"""

import datetime
import uuid
from decimal import Decimal

import pytest

import app as app_module
from models import (DailySales, Dataset, Product, RegisterCutover, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, db)
from register_sales_aggregation import (SalesAggregationConsistencyError,
                                        SalesCutoverIntegrityError, aggregate_period,
                                        aggregate_sales, sales_year_months)


def day(number):
    return datetime.date(2026, 10, number)


def legacy(owner, date, name, price, quantity):
    product = Product(dataset_id=owner.id, year=date.year, month=date.month,
                      name=name, price=price)
    db.session.add(product)
    db.session.flush()
    db.session.add(DailySales(product_id=product.id, date=date, quantity=quantity))
    db.session.commit()
    return product


def register(owner, date, *lines, kind="sale"):
    total = sum(price * quantity for product, name, price, quantity in lines)
    transaction = RegisterTransaction(
        dataset_id=owner.id, submission_key=uuid.uuid4(), request_digest="a" * 64,
        business_date=date, kind=kind, total_yen=total,
        received_yen=max(total, 0) if kind == "sale" else None,
        change_yen=0 if kind == "sale" else None,
    )
    db.session.add(transaction)
    db.session.flush()
    for product, name, price, quantity in lines:
        db.session.add(RegisterTransactionLine(
            transaction_id=transaction.id, dataset_id=owner.id,
            register_product_id=product.id, product_name_snapshot=name,
            unit_price_yen=price, quantity=quantity, line_total_yen=price * quantity,
        ))
    db.session.commit()
    return transaction


def switch(owner, date):
    db.session.add(RegisterCutover(dataset_id=owner.id, first_register_date=date))
    db.session.commit()


def item(owner, name="クロワッサン", price=120):
    product = RegisterProduct(dataset_id=owner.id, name=name, price_yen=price)
    db.session.add(product)
    db.session.commit()
    return product


def test_legacy_only_and_price_limitation(flask_app, admin_dataset):
    old = legacy(admin_dataset, day(15), "クロワッサン", 100, 3)
    result = aggregate_sales(admin_dataset, day(14), day(17))
    assert result["total_yen"] == 300 and result["has_record"]
    assert result["transaction_count"] is None and result["average_yen"] is None
    assert result["products"][0]["key"] == f"legacy:{old.id}"
    old.price = 110
    db.session.commit()
    assert aggregate_sales(admin_dataset, day(15), day(15))["total_yen"] == 330


def test_cutover_period_headers_refund_snapshots_and_zero(flask_app, admin_dataset):
    old = legacy(admin_dataset, day(15), "クロワッサン", 100, 2)
    switch(admin_dataset, day(16))
    first = item(admin_dataset)
    second = item(admin_dataset, "塩パン", 140)
    register(admin_dataset, day(16), (first, "クロワッサン", 120, 2),
             (second, "塩パン", 140, 3))  # 660、明細は2行でも1会計
    register(admin_dataset, day(16), (first, "クロワッサン", 120, 6))  # 720
    register(admin_dataset, day(16), (first, "クロワッサン", 110, -2), kind="refund")
    register(admin_dataset, day(16), (second, "塩パン", 140, 6))  # 840
    first.name, first.price_yen = "新しい商品名", 150
    db.session.commit()
    result = aggregate_sales(admin_dataset, day(15), day(16))
    assert (result["total_yen"], result["legacy_total_yen"],
            result["register_total_yen"]) == (2200, 200, 2000)
    assert (result["transaction_count"], result["sale_count"], result["refund_count"],
            result["average_yen"], result["average_display_yen"]) == (4, 3, 1, Decimal(500), 500)
    assert [(x["date"], x["total_yen"], x["transaction_count"]) for x in result["days"]] == [
        (day(15), 200, 0), (day(16), 2000, 4)]
    products = {x["key"]: x for x in result["products"]}
    assert len(products) == 3 and products[f"legacy:{old.id}"]["quantity"] == 2
    assert products[f"register:{first.id}"]["quantity"] == 6
    assert products[f"register:{first.id}"]["revenue_yen"] == 740
    assert products[f"register:{first.id}"]["name"] == "クロワッサン"
    assert products[f"register:{second.id}"]["revenue_yen"] == 1260
    assert sales_year_months(admin_dataset, day(14), day(17)) == {
        "years": [2026], "year_months": [(2026, 10)]}


def test_zero_net_is_record_and_missing_comparison_is_distinct(flask_app, admin_dataset):
    switch(admin_dataset, day(16))
    product = item(admin_dataset, price=500)
    empty = aggregate_sales(admin_dataset, day(16), day(16))
    assert empty["total_yen"] == 0 and not empty["has_record"]
    assert empty["transaction_count"] == 0 and empty["average_yen"] is None
    register(admin_dataset, day(17), (product, "クロワッサン", 500, 1))
    register(admin_dataset, day(17), (product, "クロワッサン", 500, -1), kind="refund")
    zero = aggregate_sales(admin_dataset, day(17), day(17))
    assert zero["total_yen"] == 0 and zero["has_record"]
    assert zero["transaction_count"] == 2 and zero["average_yen"] == 0
    result = aggregate_period(admin_dataset, "day", 0, day(17))
    assert result["previous_yen"] is None and result["comparison_rate_percent"] is None
    register(admin_dataset, day(16), (product, "クロワッサン", 500, 1))
    register(admin_dataset, day(16), (product, "クロワッサン", 500, -1), kind="refund")
    result = aggregate_period(admin_dataset, "day", 0, day(17))
    assert result["previous_yen"] == 0 and result["difference_yen"] == 0
    assert result["comparison_rate_percent"] is None


def test_snapshot_name_changes_and_average_rounding(flask_app, admin_dataset):
    switch(admin_dataset, day(16))
    product = item(admin_dataset, "旧名", 100)
    register(admin_dataset, day(16), (product, "旧名", 100, 1))
    product.name, product.price_yen = "新名", 101
    db.session.commit()
    register(admin_dataset, day(17), (product, "新名", 101, 2))
    result = aggregate_sales(admin_dataset, day(16), day(17))
    assert result["total_yen"] == 302
    assert result["transaction_count"] == 2
    assert result["average_yen"] == Decimal(151)
    assert result["products"][0]["key"] == f"register:{product.id}"
    assert result["products"][0]["snapshots"] == [
        {"name": "新名", "quantity": 2, "revenue_yen": 202},
        {"name": "旧名", "quantity": 1, "revenue_yen": 100},
    ]
    register(admin_dataset, day(17), (product, "新名", 101, -1), kind="refund")
    result = aggregate_sales(admin_dataset, day(16), day(17))
    assert result["total_yen"] == 201 and result["transaction_count"] == 3
    assert result["average_yen"] == Decimal(67)
    assert result["products"][0]["quantity"] == 2


def test_average_half_up_and_negative_previous(flask_app, admin_dataset):
    switch(admin_dataset, day(15))
    product = item(admin_dataset, price=1)
    register(admin_dataset, day(15), (product, "パン", 1, -1), kind="refund")
    register(admin_dataset, day(16), (product, "パン", 1, 1))
    register(admin_dataset, day(16), (product, "パン", 1, 2))
    result = aggregate_sales(admin_dataset, day(16), day(16))
    assert result["average_yen"] == Decimal("1.5")
    assert result["average_display_yen"] == 2
    comparison = aggregate_period(admin_dataset, "day", 0, day(16))
    assert comparison["previous_yen"] == -1
    assert comparison["difference_yen"] == 4
    assert comparison["comparison_rate_percent"] is None


@pytest.mark.parametrize("old_date,new_date,old_bad,new_bad", [
    (day(16), None, 1, 0), (None, day(15), 0, 1), (day(16), day(15), 1, 1),
])
def test_cutover_anomalies_raise_not_silent(flask_app, admin_dataset,
                                            old_date, new_date, old_bad, new_bad):
    switch(admin_dataset, day(16))
    if old_date:
        legacy(admin_dataset, old_date, "異常", 100, 1)
    if new_date:
        register(admin_dataset, new_date, (item(admin_dataset), "異常", 120, 1))
    with pytest.raises(SalesCutoverIntegrityError) as error:
        aggregate_sales(admin_dataset, day(15), day(16))
    assert (error.value.legacy_count, error.value.register_count) == (old_bad, new_bad)


def test_no_cutover_rejects_register_record(flask_app, admin_dataset):
    register(admin_dataset, day(16), (item(admin_dataset), "パン", 120, 1))
    with pytest.raises(SalesCutoverIntegrityError):
        aggregate_sales(admin_dataset, day(15), day(16))


def test_header_line_mismatch_fails_closed(flask_app, admin_dataset):
    switch(admin_dataset, day(16))
    product = item(admin_dataset)
    transaction = register(admin_dataset, day(16), (product, "パン", 120, 1))
    # DBは明細とヘッダーの合計一致を保証しない。読み取りでも異常を正常値にしない。
    transaction.total_yen = 121
    transaction.received_yen = 121
    db.session.commit()
    with pytest.raises(SalesAggregationConsistencyError):
        aggregate_sales(admin_dataset, day(16), day(16))


def test_dataset_isolation_and_expired_guest(flask_app, admin_dataset):
    now = datetime.datetime.now(datetime.timezone.utc)
    guests = [Dataset(kind="guest", created_at=now - datetime.timedelta(hours=1),
                      last_activity_at=now, absolute_expires_at=now + datetime.timedelta(hours=1))
              for _ in range(2)]
    db.session.add_all(guests)
    db.session.commit()
    for owner, price in [(admin_dataset, 100), (guests[0], 200), (guests[1], 300)]:
        switch(owner, day(16))
        register(owner, day(16), (item(owner, price=price), "パン", price, 1))
    assert [aggregate_sales(owner, day(16), day(16))["total_yen"]
            for owner in (admin_dataset, *guests)] == [100, 200, 300]
    guests[0].last_activity_at = now - datetime.timedelta(hours=1)
    db.session.commit()
    with pytest.raises(PermissionError):
        aggregate_sales(guests[0], day(16), day(16))
    assert aggregate_sales(guests[1], day(16), day(16))["total_yen"] == 300


def test_period_comparison_and_legacy_dashboard_untouched(authenticated_client,
                                                             admin_dataset, monkeypatch):
    legacy(admin_dataset, day(7), "前週", 100, 2)
    legacy(admin_dataset, day(14), "現在", 100, 3)
    switch(admin_dataset, day(15))
    register(admin_dataset, day(15), (item(admin_dataset), "レジ", 120, 2))
    result = aggregate_period(admin_dataset, "week", 0, day(16))
    assert result["current"]["total_yen"] == 540
    assert result["previous_yen"] == 200 and result["difference_yen"] == 340
    assert result["comparison_rate_percent"] == Decimal("170.0")
    monkeypatch.setattr(app_module, "business_today", lambda: day(16))
    # Phase 3Aでは既存HTTP経路を差し替えない。
    assert authenticated_client.get("/api/sales-period-summary?period=week").get_json()["total_sales"] == 300


@pytest.mark.parametrize("period,today,expected", [
    ("week", datetime.date(2026, 1, 1), datetime.date(2025, 12, 22)),
    ("month", datetime.date(2024, 3, 1), datetime.date(2024, 2, 1)),
    ("year", datetime.date(2025, 3, 1), datetime.date(2024, 1, 1)),
])
def test_period_bounds_previous_and_leap(flask_app, admin_dataset, period, today, expected):
    result = aggregate_period(admin_dataset, period, 0, today)
    assert result["previous"]["start"] == expected
    assert result["previous"]["has_record"] is False


def test_invalid_dates_are_rejected(flask_app, admin_dataset):
    for start, end in [(day(17), day(16)), (datetime.datetime.now(), day(17)),
                       ("2026-10-16", day(17))]:
        with pytest.raises(ValueError):
            aggregate_sales(admin_dataset, start, end)
