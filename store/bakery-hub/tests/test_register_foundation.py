"""v3レジのDB制約。保存route公開前でもDB境界を検証する。"""

import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app as app_module
from models import (
    DailySales, Dataset, Product, RegisterCutover, RegisterProduct,
    RegisterTransaction, RegisterTransactionLine, db,
)


TODAY = datetime.date(2026, 10, 16)
NOW = datetime.datetime(2026, 10, 16, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def sqlite_foreign_keys(flask_app):
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def dataset(kind="guest", *, expired=False):
    row = Dataset(
        kind=kind, system_key="admin" if kind == "admin" else None,
        created_at=NOW - datetime.timedelta(hours=3),
        last_activity_at=NOW - datetime.timedelta(minutes=5),
        absolute_expires_at=(NOW - datetime.timedelta(hours=1) if expired
                             else NOW + datetime.timedelta(hours=1)) if kind == "guest" else None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def product(owner, **overrides):
    values = dict(dataset_id=owner.id, name="クロワッサン", price_yen=200)
    values.update(overrides)
    return RegisterProduct(**values)


def transaction(owner, **overrides):
    values = dict(
        dataset_id=owner.id, submission_key=uuid.uuid4(),
        request_digest="a" * 64, completed_at=NOW, business_date=TODAY,
        kind="sale", total_yen=200, received_yen=500, change_yen=300,
    )
    values.update(overrides)
    return RegisterTransaction(**values)


def line(sale, item, **overrides):
    values = dict(
        dataset_id=sale.dataset_id, transaction_id=sale.id,
        register_product_id=item.id, product_name_snapshot=item.name,
        unit_price_yen=item.price_yen, quantity=1, line_total_yen=item.price_yen,
    )
    values.update(overrides)
    return RegisterTransactionLine(**values)


def rejected(row):
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("price", [501, -1, 0.5])
def test_product_price_rejects_out_of_range_or_fractional(flask_app, price):
    rejected(product(dataset(), price_yen=price))


def test_product_zero_and_five_hundred_are_valid_schema_values(flask_app):
    owner = dataset()
    db.session.add_all([product(owner, name="無料候補", price_yen=0),
                        product(owner, name="上限", price_yen=500)])
    db.session.commit()
    assert sorted(row.price_yen for row in RegisterProduct.query.all()) == [0, 500]


@pytest.mark.parametrize("changes", [
    {"name": "   "}, {"name": "a" * 101}, {"template_key": " "},
    {"image_key": " "}, {"display_order": -1},
])
def test_product_invalid_metadata_is_rejected(flask_app, changes):
    rejected(product(dataset(), **changes))


def test_template_key_is_unique_per_dataset_and_optional_for_custom_items(flask_app):
    owner, other = dataset(), dataset()
    db.session.add_all([product(owner, template_key="croissant"),
                        product(other, template_key="croissant"),
                        product(owner, name="独自1"), product(owner, name="独自2")])
    db.session.commit()
    rejected(product(owner, template_key="croissant"))
    assert RegisterProduct.query.count() == 4


@pytest.mark.parametrize("changes", [
    {"kind": "void"}, {"kind": "sale", "total_yen": -1},
    {"kind": "sale", "received_yen": 100, "change_yen": 0},
    {"kind": "sale", "change_yen": 99},
    {"kind": "refund", "total_yen": 200, "received_yen": None, "change_yen": None},
    {"kind": "refund", "total_yen": -200},
    {"request_digest": "short"}, {"total_yen": 1000000001},
])
def test_transaction_kind_and_settlement_checks(flask_app, changes):
    rejected(transaction(dataset(), **changes))


def test_zero_total_and_refund_can_be_represented_without_deciding_sale_policy(flask_app):
    owner = dataset()
    db.session.add_all([
        transaction(owner, total_yen=0, received_yen=0, change_yen=0),
        transaction(owner, kind="refund", total_yen=-200,
                    received_yen=None, change_yen=None),
    ])
    db.session.commit()
    assert RegisterTransaction.query.count() == 2


def test_submission_key_is_unique_within_dataset_only(flask_app):
    a, b = dataset(), dataset()
    key = uuid.uuid4()
    db.session.add_all([transaction(a, submission_key=key),
                        transaction(b, submission_key=key)])
    db.session.commit()
    rejected(transaction(a, submission_key=key))
    assert RegisterTransaction.query.count() == 2


@pytest.mark.parametrize("quantity,amount", [
    (51, 10200), (-51, -10200), (0, 0), (1.5, 300), (1, 199),
])
def test_line_quantity_and_amount_checks(flask_app, quantity, amount):
    owner = dataset()
    item = product(owner)
    sale = transaction(owner)
    db.session.add_all([item, sale])
    db.session.commit()
    rejected(line(sale, item, quantity=quantity, line_total_yen=amount))


def test_line_snapshot_price_cannot_exceed_new_register_limit(flask_app):
    owner = dataset()
    item, sale = product(owner), transaction(owner)
    db.session.add_all([item, sale])
    db.session.commit()
    rejected(line(sale, item, unit_price_yen=501, line_total_yen=501))
    rejected(line(sale, item, unit_price_yen=-1, line_total_yen=-1))


def test_multiple_lines_refund_bounds_and_snapshot_survive_product_change(flask_app):
    owner = dataset()
    first = product(owner)
    second = product(owner, name="メロンパン", price_yen=0)
    sale = transaction(owner, total_yen=10000, received_yen=10000, change_yen=0)
    refund = transaction(owner, kind="refund", total_yen=-10000,
                         received_yen=None, change_yen=None)
    db.session.add_all([first, second, sale, refund])
    db.session.commit()
    db.session.add_all([line(sale, first, quantity=50, line_total_yen=10000),
                        line(sale, second, unit_price_yen=0, quantity=1, line_total_yen=0),
                        line(refund, first, quantity=-50, line_total_yen=-10000)])
    db.session.commit()
    first.name = "新しい名前"
    first.price_yen = 500
    db.session.commit()
    saved = RegisterTransactionLine.query.filter_by(transaction_id=sale.id, register_product_id=first.id).one()
    assert (saved.product_name_snapshot, saved.unit_price_yen, saved.line_total_yen) == ("クロワッサン", 200, 10000)
    rejected(line(sale, first))
    with pytest.raises(IntegrityError):
        db.session.delete(first)
        db.session.commit()
    db.session.rollback()
    assert RegisterTransactionLine.query.count() == 3


def test_composite_foreign_keys_reject_cross_dataset_and_missing_parents(flask_app):
    admin, guest_a, guest_b = dataset("admin"), dataset(), dataset()
    products = [product(owner) for owner in (admin, guest_a, guest_b)]
    sales = [transaction(owner) for owner in (admin, guest_a, guest_b)]
    db.session.add_all(products + sales)
    db.session.commit()
    for owner_index, product_index in ((0, 1), (1, 2), (2, 0)):
        rejected(line(sales[owner_index], products[product_index]))
    rejected(line(sales[0], products[0], transaction_id=uuid.uuid4()))
    rejected(line(sales[0], products[0], register_product_id=999999))
    rejected(line(sales[0], products[0], dataset_id=guest_a.id))
    rejected(line(sales[0], products[0], dataset_id=None))
    assert RegisterTransactionLine.query.count() == 0


def test_guest_cleanup_removes_only_expired_register_data(flask_app):
    expired, active, admin = dataset(expired=True), dataset(), dataset("admin")
    expired_id = expired.id
    saved_ids = {}
    for owner in (expired, active, admin):
        item, sale = product(owner), transaction(owner)
        cutover = RegisterCutover(dataset_id=owner.id, first_register_date=TODAY)
        db.session.add_all([item, sale, cutover])
        db.session.commit()
        detail = line(sale, item)
        db.session.add(detail)
        db.session.commit()
        saved_ids[owner.id] = (item.id, sale.id, detail.id)

    assert app_module._cleanup_expired_guest_datasets(now=NOW) == 1
    db.session.commit()
    assert db.session.get(Dataset, expired_id) is None
    for owner in (active, admin):
        item_id, sale_id, detail_id = saved_ids[owner.id]
        assert db.session.get(RegisterProduct, item_id) is not None
        assert db.session.get(RegisterTransaction, sale_id) is not None
        assert db.session.get(RegisterTransactionLine, detail_id) is not None
        assert db.session.get(RegisterCutover, owner.id) is not None
    assert RegisterTransaction.query.count() == 2
    assert Product.query.count() == DailySales.query.count() == 0
