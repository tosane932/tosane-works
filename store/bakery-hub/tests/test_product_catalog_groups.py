import datetime

from bs4 import BeautifulSoup

import app as app_module
from models import DailySales, Product, db


def _catalog(client):
    response = client.get('/products')
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), 'html.parser')


def _names(section):
    return [node.get_text(strip=True) for node in section.select('.product-catalog-name')]


def test_catalog_separates_this_month_from_latest_past_history(
    admin_dataset, authenticated_client, monkeypatch,
):
    monkeypatch.setattr(app_module, 'business_today', lambda: datetime.date(2026, 10, 7))
    rows = [
        Product(dataset=admin_dataset, year=2026, month=10, name='食パン', price=200),
        Product(dataset=admin_dataset, year=2026, month=9, name='食パン', price=190),
        Product(dataset=admin_dataset, year=2025, month=12, name='塩パン', price=160),
        Product(dataset=admin_dataset, year=2026, month=9, name='塩パン', price=180),
    ]
    db.session.add_all(rows)
    db.session.flush()
    db.session.add(DailySales(
        product_id=rows[2].id, date=datetime.date(2025, 12, 10), quantity=2,
    ))
    db.session.commit()
    before = [(p.id, p.price, p.image_key, p.is_active) for p in Product.query.order_by(Product.id)]
    sales_before = [(s.id, s.quantity) for s in DailySales.query.all()]

    doc = _catalog(authenticated_client)
    current = doc.select_one('#current-month-products')
    past = doc.select_one('#past-products')
    assert '今月登録の商品（2026年10月）' in current.get_text()
    assert _names(current) == ['食パン']
    assert _names(past) == ['塩パン']
    assert '180円' in past.get_text() and '最終登録' in past.get_text()
    assert '2026年9月' in past.get_text()
    assert '登録中' not in past.get_text()
    assert [(p.id, p.price, p.image_key, p.is_active) for p in Product.query.order_by(Product.id)] == before
    assert [(s.id, s.quantity) for s in DailySales.query.all()] == sales_before


def test_inactive_current_product_shows_prior_active_history(
    admin_dataset, authenticated_client, monkeypatch,
):
    monkeypatch.setattr(app_module, 'business_today', lambda: datetime.date(2026, 10, 7))
    db.session.add_all([
        Product(dataset=admin_dataset, year=2026, month=10, name='塩パン', price=190, is_active=False),
        Product(dataset=admin_dataset, year=2026, month=9, name='塩パン', price=180, is_active=True),
    ])
    db.session.commit()
    doc = _catalog(authenticated_client)
    assert _names(doc.select_one('#current-month-products')) == []
    past = doc.select_one('#past-products')
    assert _names(past) == ['塩パン']
    assert '180円' in past.get_text()
    assert '2026年9月' in past.get_text()


def test_current_inactive_and_future_records_are_not_labeled_past(
    admin_dataset, authenticated_client, monkeypatch,
):
    monkeypatch.setattr(app_module, 'business_today', lambda: datetime.date(2026, 10, 7))
    db.session.add_all([
        Product(dataset=admin_dataset, year=2026, month=10,
                name='今月解除', price=200, is_active=False),
        Product(dataset=admin_dataset, year=2026, month=11,
                name='来月予定', price=240, is_active=True),
    ])
    db.session.commit()
    doc = _catalog(authenticated_client)
    assert _names(doc.select_one('#current-month-products')) == []
    assert _names(doc.select_one('#past-products')) == []
