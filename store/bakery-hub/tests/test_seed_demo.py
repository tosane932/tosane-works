import datetime

import pytest

import seed_demo as seed_demo_module
from models import DailySales, Dataset, Product, db
from product_images import PRODUCT_IMAGE_OPTIONS
from seed_demo import DEMO_PRODUCTS, DEMO_QUANTITIES, seed_demo_data


def _create_dataset_records(
    dataset,
    *,
    name,
    price,
    is_active,
    sale_date,
    quantity,
):
    product = Product(
        dataset=dataset,
        year=sale_date.year,
        month=sale_date.month,
        name=name,
        price=price,
        is_active=is_active,
    )
    db.session.add(product)
    db.session.flush()
    sale = DailySales(
        product_id=product.id,
        date=sale_date,
        quantity=quantity,
    )
    db.session.add(sale)
    db.session.commit()
    return product, sale


def _dataset_snapshot(dataset_id):
    products = [
        (
            product.id,
            product.name,
            product.price,
            product.is_active,
            product.dataset_id,
        )
        for product in (
            Product.query
            .filter_by(dataset_id=dataset_id)
            .order_by(Product.id)
            .all()
        )
    ]
    sales = [
        (
            sale.id,
            sale.quantity,
            sale.date,
            sale.product_id,
        )
        for sale in (
            DailySales.query
            .join(Product, DailySales.product_id == Product.id)
            .filter(Product.dataset_id == dataset_id)
            .order_by(DailySales.id)
            .all()
        )
    ]
    return products, sales


def _create_guest_dataset():
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    db.session.add(guest_dataset)
    db.session.flush()
    return guest_dataset


def test_seed_demo_data_inserts_demo_data(flask_app, admin_dataset):
    result = seed_demo_data(
        reference_date=datetime.date(2026, 8, 13)
    )

    assert result is True
    assert Product.query.count() == 7
    assert DailySales.query.count() == 49

    products = Product.query.all()
    assert [(product.name, product.price) for product in products] == DEMO_PRODUCTS
    assert [name for name, _ in DEMO_PRODUCTS] == [
        "食パン", "クロワッサン", "メロンパン", "あんぱん",
        "クリームパン", "塩パン", "くるみパン",
    ]
    assert len(DEMO_QUANTITIES) == 7
    assert all(len(row) == len(DEMO_PRODUCTS) for row in DEMO_QUANTITIES)
    assert "カレーパン" not in [product.name for product in products]
    assert [sale.quantity for sale in DailySales.query.order_by(DailySales.id)] == [
        quantity for row in DEMO_QUANTITIES for quantity in row
    ]

    assert all(product.year == 2026 for product in products)
    assert all(product.month == 8 for product in products)
    assert all(product.is_active for product in products)
    assert all(
        product.dataset_id == admin_dataset.id
        for product in products
    )


def test_seed_demo_data_uses_business_today_by_default(
    flask_app,
    admin_dataset,
    monkeypatch,
):
    frozen_today = datetime.date(2027, 1, 1)
    monkeypatch.setattr(
        seed_demo_module,
        "business_today",
        lambda: frozen_today,
    )

    result = seed_demo_data()

    products = Product.query.all()
    assert result is True
    assert products
    assert all(product.year == 2027 for product in products)
    assert all(product.month == 1 for product in products)


def test_seed_demo_data_does_not_duplicate_existing_data(
    flask_app,
    admin_dataset,
):
    first_result = seed_demo_data(
        reference_date=datetime.date(2026, 8, 13)
    )

    second_result = seed_demo_data(
        reference_date=datetime.date(2026, 8, 13)
    )

    assert first_result is True
    assert second_result is False

    assert Product.query.count() == 7
    assert DailySales.query.count() == 49


def test_seed_demo_data_requires_admin_dataset(flask_app):
    with pytest.raises(RuntimeError, match="Admin Dataset is missing"):
        seed_demo_data(reference_date=datetime.date(2026, 8, 13))

    assert Product.query.count() == 0
    assert DailySales.query.count() == 0


def test_admin_seed_runs_when_only_guest_dataset_has_data(
    flask_app,
    admin_dataset,
):
    guest_dataset = _create_guest_dataset()
    _create_dataset_records(
        guest_dataset,
        name="ゲスト既存商品",
        price=987,
        is_active=True,
        sale_date=datetime.date(2026, 8, 1),
        quantity=654,
    )

    result = seed_demo_data(
        reference_date=datetime.date(2026, 8, 13)
    )

    admin_products, admin_sales = _dataset_snapshot(admin_dataset.id)
    assert result is True
    assert len(admin_products) == len(DEMO_PRODUCTS)
    assert len(admin_sales) == len(DEMO_PRODUCTS) * len(DEMO_QUANTITIES)


def test_admin_seed_does_not_modify_or_delete_guest_dataset_data(
    flask_app,
    admin_dataset,
):
    guest_dataset = _create_guest_dataset()
    _create_dataset_records(
        guest_dataset,
        name="変更禁止ゲスト商品",
        price=4321,
        is_active=False,
        sale_date=datetime.date(2026, 7, 9),
        quantity=765432,
    )
    guest_before = _dataset_snapshot(guest_dataset.id)

    result = seed_demo_data(
        reference_date=datetime.date(2026, 8, 13)
    )

    guest_after = _dataset_snapshot(guest_dataset.id)
    admin_products, admin_sales = _dataset_snapshot(admin_dataset.id)
    assert guest_after == guest_before
    assert result is True
    assert len(admin_products) == len(DEMO_PRODUCTS)
    assert len(admin_sales) == len(DEMO_PRODUCTS) * len(DEMO_QUANTITIES)


def test_admin_seed_skips_existing_admin_data_without_affecting_guest_data(
    flask_app,
    admin_dataset,
):
    admin_product, admin_sale = _create_dataset_records(
        admin_dataset,
        name="既存管理者商品",
        price=321,
        is_active=True,
        sale_date=datetime.date(2026, 8, 2),
        quantity=12,
    )
    guest_dataset = _create_guest_dataset()
    _create_dataset_records(
        guest_dataset,
        name="重複確認ゲスト商品",
        price=654,
        is_active=False,
        sale_date=datetime.date(2026, 6, 3),
        quantity=34,
    )
    admin_before = _dataset_snapshot(admin_dataset.id)
    guest_before = _dataset_snapshot(guest_dataset.id)

    result = seed_demo_data(
        reference_date=datetime.date(2026, 8, 13)
    )

    assert result is False
    assert _dataset_snapshot(admin_dataset.id) == admin_before
    assert _dataset_snapshot(guest_dataset.id) == guest_before
    assert db.session.get(Product, admin_product.id) is not None
    assert db.session.get(DailySales, admin_sale.id) is not None

def test_image_template_curry_bread_remains_available():
    curry_bread = PRODUCT_IMAGE_OPTIONS["curry_bread"]
    assert curry_bread["label"] == "カレーパン"
    assert curry_bread["default_price"] == 220
    assert curry_bread["filename"] == "curry_bread.png"


def test_seed_preserves_existing_unpictured_curry_bread_and_sales(
    flask_app, admin_dataset,
):
    existing, sale = _create_dataset_records(
        admin_dataset,
        name="カレーパン",
        price=280,
        is_active=True,
        sale_date=datetime.date(2026, 8, 2),
        quantity=14,
    )
    before = _dataset_snapshot(admin_dataset.id)

    assert seed_demo_data(reference_date=datetime.date(2026, 8, 13)) is False
    assert _dataset_snapshot(admin_dataset.id) == before
    assert db.session.get(Product, existing.id) is not None
    assert db.session.get(DailySales, sale.id) is not None
