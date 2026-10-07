"""Regression contracts for the verified Phase 16 survivor gaps."""
import datetime
import uuid
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

import app as app_module
from models import DailySales, Dataset, Product, db

TODAY = datetime.date(2026, 8, 10)


def _snapshot():
    db.session.expire_all()
    return (
        [(p.id, p.dataset_id, p.year, p.month, p.name, p.price, p.is_active)
         for p in Product.query.order_by(Product.id).all()],
        [(s.id, s.product_id, s.date, s.quantity)
         for s in DailySales.query.order_by(DailySales.id).all()],
    )


def _datasets(admin_dataset, reverse=False):
    # Authorization must be independent of the ordering of valid Dataset UUIDs.
    ids = [uuid.UUID(value) for value in [
        "10000000-0000-4000-8000-000000000001",
        "80000000-0000-4000-8000-000000000002",
        "f0000000-0000-4000-8000-000000000003",
    ]]
    if reverse:
        ids.reverse()
    admin_dataset.id = ids[0]
    now = datetime.datetime.now(datetime.timezone.utc)
    guests = [Dataset(
        id=value, kind="guest", system_key=None, created_at=now,
        last_activity_at=now, absolute_expires_at=now + datetime.timedelta(hours=2),
    ) for value in ids[1:]]
    db.session.add_all(guests)
    db.session.commit()
    return {"admin": admin_dataset, "guest-a": guests[0], "guest-b": guests[1]}


def _product(dataset, name, *, year=2026, month=8, active=True):
    product = Product(dataset=dataset, name=name, price=250,
                      year=year, month=month, is_active=active)
    db.session.add(product)
    db.session.flush()
    return product


def _sale(product, date, quantity):
    db.session.add(DailySales(product_id=product.id, date=date, quantity=quantity))


@pytest.fixture()
def phase17_records(flask_app, admin_dataset, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    datasets = _datasets(admin_dataset)
    products = {}
    for label, dataset in datasets.items():
        product = _product(dataset, label)
        _sale(product, TODAY, 9)
        products[label] = product
    db.session.commit()
    return SimpleNamespace(datasets=datasets, products=products)


@pytest.mark.parametrize("value", ["１２", "٠١"], ids=["fullwidth", "arabic"])
def test_product_post_rejects_unicode_price_without_changes(
    authenticated_client, phase17_records, csrf_post, value,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/", {
        "year": "2026", "month": "8", "product_id": [""],
        "prod_name": ["新規商品"], "prod_price": [value],
    })
    assert response.status_code == 400
    assert _snapshot() == before


@pytest.mark.parametrize("value", ["１２", "٠١"], ids=["fullwidth", "arabic"])
def test_sales_post_rejects_unicode_quantity_without_changes(
    authenticated_client, phase17_records, csrf_post, value,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/input", {
        "date": TODAY.isoformat(),
        "product_id": [str(phase17_records.products["admin"].id)],
        "quantity": [value],
    })
    assert response.status_code == 400
    assert _snapshot() == before


@pytest.mark.parametrize("year,month", [(2026, 0), (2026, 13), (1999, 8), (2101, 8)])
def test_new_product_post_rejects_year_month_bounds_without_changes(
    authenticated_client, phase17_records, csrf_post, year, month,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/", {
        "year": str(year), "month": str(month), "product_id": [""],
        "prod_name": ["新規商品"], "prod_price": ["100"],
    })
    assert response.status_code == 400
    assert _snapshot() == before


def test_new_december_product_is_saved_with_correct_period(
    authenticated_client, phase17_records, csrf_post,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/", {
        "year": "2026", "month": "12", "product_id": [""],
        "prod_name": ["12月商品"], "prod_price": ["250"],
    })
    assert response.status_code == 200
    saved = Product.query.filter_by(dataset_id=phase17_records.datasets["admin"].id,
                                    year=2026, month=12).one()
    assert (saved.name, saved.price, saved.is_active) == ("12月商品", 250, True)
    after = _snapshot()
    assert [p for p in after[0] if p[0] != saved.id] == before[0]
    assert after[1] == before[1]


def test_existing_product_rejects_future_year_with_same_month(
    authenticated_client, phase17_records, csrf_post,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/", {
        "year": "2027", "month": "8",
        "product_id": [str(phase17_records.products["admin"].id)],
        "prod_name": ["変更されない商品"], "prod_price": ["100"],
    })
    assert response.status_code == 400
    assert _snapshot() == before


def test_product_id_array_cannot_have_an_extra_row(
    authenticated_client, phase17_records, csrf_post,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/", {
        "year": "2026", "month": "8",
        "product_id": [str(phase17_records.products["admin"].id), ""],
        "prod_name": ["変更されない商品"], "prod_price": ["100"],
    })
    assert response.status_code == 400
    assert _snapshot() == before


def test_sales_success_is_visible_after_persisting_all_changes(
    authenticated_client, phase17_records, csrf_post,
):
    before = _snapshot()
    product = phase17_records.products["admin"]
    response = csrf_post(authenticated_client, "/input", {
        "date": TODAY.isoformat(), "product_id": [str(product.id)], "quantity": ["17"],
    })
    assert response.status_code == 200
    after = _snapshot()
    assert DailySales.query.filter_by(product_id=product.id, date=TODAY).one().quantity == 17
    assert after[0] == before[0]
    assert [s for s in after[1] if s[1] != product.id] == [s for s in before[1] if s[1] != product.id]
    message = BeautifulSoup(response.get_data(as_text=True), "html.parser").select_one(".success-msg")
    assert message is not None
    assert "本日の売上個数を更新しました！" in message.get_text()


@pytest.mark.parametrize("year", [2025, 2027])
def test_sales_rejects_other_year_with_same_product_month(
    authenticated_client, phase17_records, csrf_post, year,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/input", {
        "date": f"{year}-08-10", "product_id": [str(phase17_records.products["admin"].id)],
        "quantity": ["17"],
    })
    assert response.status_code == 400
    assert _snapshot() == before


def test_fallback_updates_existing_sale_atomically_without_duplicates(
    authenticated_client, phase17_records, csrf_post, monkeypatch,
):
    # Stub only the route's dialect selection; ORM persistence remains SQLite.
    monkeypatch.setattr(db.session, "get_bind", lambda *a, **k: SimpleNamespace(dialect=SimpleNamespace(name="fallback-test")))
    before = _snapshot()
    product = phase17_records.products["admin"]
    response = csrf_post(authenticated_client, "/input", {
        "date": TODAY.isoformat(), "product_id": [str(product.id)], "quantity": ["17"],
    })
    assert response.status_code == 200
    after = _snapshot()
    assert len(after[1]) == len(before[1])
    assert DailySales.query.filter_by(product_id=product.id, date=TODAY).one().quantity == 17
    assert after[0] == before[0]
    assert [s for s in after[1] if s[1] != product.id] == [s for s in before[1] if s[1] != product.id]


def test_guest_exactly_thirty_sales_are_all_saved(
    flask_app, phase17_records, csrf_post,
):
    guest = phase17_records.datasets["guest-a"]
    products = [phase17_records.products["guest-a"]]
    products += [_product(guest, f"Guest商品{n}") for n in range(29)]
    db.session.commit()
    before = _snapshot()
    with flask_app.app_context():
        client = flask_app.test_client()
        with client.session_transaction() as state:
            state["_user_id"] = f"guest:{guest.id}"
            state["_fresh"] = True
        response = csrf_post(client, "/input", {
            "date": TODAY.isoformat(), "product_id": [str(p.id) for p in products],
            "quantity": [str(n + 1) for n in range(30)],
        })
    assert response.status_code == 200
    after = _snapshot()
    owned = {p.id for p in products}
    assert {s.product_id: s.quantity for s in DailySales.query.filter(DailySales.product_id.in_(owned)).all()} == {p.id: n + 1 for n, p in enumerate(products)}
    assert after[0] == before[0]
    assert [s for s in after[1] if s[1] not in owned] == [s for s in before[1] if s[1] not in owned]
    assert len(after[1]) == 32


def test_current_products_respects_explicit_period_dataset_and_active(
    phase17_records,
):
    dataset = phase17_records.datasets["admin"]
    selected = _product(dataset, "指定年月", year=2027, month=1)
    _product(dataset, "指定年月の非active", year=2027, month=1, active=False)
    _product(dataset, "指定年の別月", year=2027, month=2)
    _product(dataset, "同月の別年", year=2026, month=1)
    _product(phase17_records.datasets["guest-a"], "別Datasetの同年月", year=2027, month=1)
    db.session.commit()
    before = _snapshot()
    actual = app_module._get_current_products(dataset, datetime.date(2027, 1, 10))
    assert {p.id for p in actual} == {selected.id}
    assert _snapshot() == before


@pytest.mark.parametrize("reverse", [False, True], ids=["uuid-ascending", "uuid-descending"])
@pytest.mark.parametrize("viewer", ["admin", "guest-a", "guest-b"])
def test_today_sales_map_has_exact_date_and_dataset_without_overwrite_masking(
    flask_app, admin_dataset, monkeypatch, reverse, viewer,
):
    monkeypatch.setattr(app_module, "business_today", lambda: TODAY)
    datasets = _datasets(admin_dataset, reverse)
    expected = {}
    for label, dataset in datasets.items():
        today_product = _product(dataset, f"{label}当日")
        _sale(today_product, TODAY, 11)
        expected[label] = {today_product.id: 11}
        past = _product(dataset, f"{label}過去のみ")
        future = _product(dataset, f"{label}未来のみ")
        _product(dataset, f"{label}売上なし")
        _sale(past, TODAY - datetime.timedelta(days=1), 211)
        _sale(future, TODAY + datetime.timedelta(days=1), 311)
    db.session.commit()
    before = _snapshot()
    actual = app_module._get_today_sales_map(TODAY, datasets[viewer])
    assert actual == expected[viewer]
    assert _snapshot() == before


@pytest.mark.parametrize("reverse", [False, True], ids=["uuid-ascending", "uuid-descending"])
@pytest.mark.parametrize("viewer", ["admin", "guest-a", "guest-b"])
def test_registered_sales_months_exclude_other_datasets_and_future_year(
    flask_app, admin_dataset, reverse, viewer,
):
    datasets = _datasets(admin_dataset, reverse)
    months = {"admin": 3, "guest-a": 7, "guest-b": 11}
    for label, dataset in datasets.items():
        for year, month in [(2026, months[label]), (2025, 1), (2027, 12)]:
            product = _product(dataset, f"{label}-{year}", year=year, month=month)
            _sale(product, datetime.date(year, month, 10), 5)
    db.session.commit()
    before = _snapshot()
    actual = app_module._get_dashboard_sales_months(datasets[viewer], 2026)
    assert actual == [months[viewer]]
    assert _snapshot() == before
