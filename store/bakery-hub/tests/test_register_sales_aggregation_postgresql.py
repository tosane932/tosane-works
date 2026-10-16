"""隔離PostgreSQLで新旧排他・複数明細・返金の集計契約を確認。"""

import datetime
from pathlib import Path
import threading
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, text

from models import (DailySales, Dataset, PrepRecipe, Product, RegisterCutover, RegisterPrepLink, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, db)
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_sales_aggregation import (SalesCutoverIntegrityError, aggregate_sales,
                                        sales_read_snapshot)
from legacy_sales_cutover import LegacySalesClosed, assert_legacy_sales_writable
from register_products import _lock_dataset
from prep_sales_reference import prep_sales_reference
from register_cutover_audit import audit_cutover
from sqlalchemy.exc import IntegrityError
from types import SimpleNamespace


TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="隔離PostgreSQLの設定なし")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture()
def pg_app():
    reset = create_engine(TEST_URL, pool_pre_ping=True)
    try:
        with reset.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        reset.dispose()
    app = Flask("register_sales_aggregation_postgresql")
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        yield app
        db.session.remove()
        db.engine.dispose()


def test_cutover_refund_and_two_lines_do_not_multiply_header(pg_app):
    with pg_app.app_context():
        day_before, cutover = datetime.date(2026, 10, 15), datetime.date(2026, 10, 16)
        # 既存migrationが管理者Datasetを初期生成するので、その行を使う。
        owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
        old = Product(dataset_id=owner.id, year=2026, month=10, name="パン", price=100)
        first = RegisterProduct(dataset_id=owner.id, name="パン", price_yen=120)
        second = RegisterProduct(dataset_id=owner.id, name="塩パン", price_yen=140)
        db.session.add_all([old, first, second, RegisterCutover(
            dataset_id=owner.id, first_register_date=cutover)])
        db.session.flush()
        db.session.add(DailySales(product_id=old.id, date=day_before, quantity=2))
        for kind, amount, lines in [
            ("sale", 380, [(first, 120, 2), (second, 140, 1)]),
            ("refund", -120, [(first, 120, -1)]),
        ]:
            header = RegisterTransaction(dataset_id=owner.id, business_date=cutover,
                submission_key=uuid.uuid4(), request_digest="a" * 64, kind=kind,
                total_yen=amount, received_yen=amount if kind == "sale" else None,
                change_yen=0 if kind == "sale" else None)
            db.session.add(header)
            db.session.flush()
            for product, unit, quantity in lines:
                db.session.add(RegisterTransactionLine(
                    transaction_id=header.id, dataset_id=owner.id,
                    register_product_id=product.id, product_name_snapshot=product.name,
                    unit_price_yen=unit, quantity=quantity, line_total_yen=unit * quantity))
        db.session.commit()
        result = aggregate_sales(owner, day_before, cutover)
        assert (result["total_yen"], result["register_total_yen"],
                result["transaction_count"], result["refund_count"]) == (460, 260, 2, 1)
        assert result["products"][1]["quantity"] == 1
        db.session.add(DailySales(product_id=old.id, date=cutover, quantity=1))
        db.session.commit()
        with pytest.raises(SalesCutoverIntegrityError) as error:
            aggregate_sales(owner, day_before, cutover)
        assert error.value.legacy_count == 1


def test_snapshot_does_not_mix_concurrent_checkout_headers_and_lines(pg_app):
    day = datetime.date(2026, 10, 16)
    with pg_app.app_context():
        owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
        db.session.add(RegisterCutover(dataset_id=owner.id, first_register_date=day))
        product = RegisterProduct(dataset_id=owner.id, name="パン", price_yen=120)
        db.session.add(product)
        db.session.commit()
        owner_id, product_id = owner.id, product.id

    finished = threading.Event()
    writer_errors = []

    def writer():
        with pg_app.app_context():
            try:
                header = RegisterTransaction(dataset_id=owner_id, business_date=day,
                    submission_key=uuid.uuid4(), request_digest="a" * 64, kind="sale",
                    total_yen=240, received_yen=240, change_yen=0)
                db.session.add(header)
                db.session.flush()
                db.session.add(RegisterTransactionLine(transaction_id=header.id,
                    dataset_id=owner_id, register_product_id=product_id,
                    product_name_snapshot="パン", unit_price_yen=120,
                    quantity=2, line_total_yen=240))
                db.session.commit()
            except Exception as exc:
                writer_errors.append(exc)
            finally:
                finished.set()

    with pg_app.app_context():
        owner = db.session.get(Dataset, owner_id)
        with sales_read_snapshot() as snapshot:
            assert snapshot.execute(text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
            assert snapshot.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
            assert aggregate_sales(owner, day, day, read_session=snapshot)["total_yen"] == 0
            thread = threading.Thread(target=writer)
            thread.start()
            assert finished.wait(timeout=10), "読み取り中に会計保存が妨げられた"
            assert aggregate_sales(owner, day, day, read_session=snapshot)["total_yen"] == 0
        thread.join(timeout=10)
        assert not writer_errors
        with sales_read_snapshot() as snapshot:
            result = aggregate_sales(owner, day, day, read_session=snapshot)
        assert result["total_yen"] == 240
        assert result["transaction_count"] == 1
        assert result["products"][0]["revenue_yen"] == 240


def test_prep_reference_uses_same_snapshot_as_concurrent_sale(pg_app):
    day = datetime.date(2026, 10, 16)
    with pg_app.app_context():
        owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
        product = RegisterProduct(dataset_id=owner.id, template_key="croissant",
                                  name="クロワッサン", price_yen=120)
        db.session.add_all([product, RegisterCutover(dataset_id=owner.id,
                                                     first_register_date=day)])
        db.session.commit()
        owner_id, product_id = owner.id, product.id
    recipe = SimpleNamespace(id=7, product_key="template:croissant")
    with pg_app.app_context():
        owner = db.session.get(Dataset, owner_id)
        with sales_read_snapshot() as snapshot:
            before = prep_sales_reference(owner, [recipe], day, read_session=snapshot)
            assert before["references"][7]["status"] == "unrecorded"
            with db.engine.begin() as conn:
                tx_id = uuid.uuid4()
                conn.execute(RegisterTransaction.__table__.insert().values(
                    id=tx_id, dataset_id=owner_id, submission_key=uuid.uuid4(),
                    request_digest="a" * 64, business_date=day, kind="sale",
                    total_yen=120, received_yen=120, change_yen=0))
                conn.execute(RegisterTransactionLine.__table__.insert().values(
                    transaction_id=tx_id, dataset_id=owner_id,
                    register_product_id=product_id, product_name_snapshot="クロワッサン",
                    unit_price_yen=120, quantity=1, line_total_yen=120))
            during = prep_sales_reference(owner, [recipe], day, read_session=snapshot)
            assert during["references"][7]["status"] == "unrecorded"
        with sales_read_snapshot() as snapshot:
            after = prep_sales_reference(owner, [recipe], day, read_session=snapshot)
        assert after["references"][7] == {"status": "recorded", "quantity": 1}


def test_cutover_lock_serializes_legacy_writer(pg_app):
    day = datetime.date(2026, 10, 16)
    with pg_app.app_context():
        owner_id = Dataset.query.filter_by(kind="admin", system_key="admin").one().id
    locked = threading.Event()
    release = threading.Event()
    writer_errors = []

    def cutover_writer():
        with pg_app.app_context():
            try:
                owner = db.session.get(Dataset, owner_id)
                _lock_dataset(owner)
                db.session.add(RegisterCutover(dataset_id=owner_id, first_register_date=day))
                db.session.flush()
                locked.set()
                assert release.wait(timeout=10)
                db.session.commit()
            except Exception as exc:
                writer_errors.append(exc)
                locked.set()

    thread = threading.Thread(target=cutover_writer)
    thread.start()
    assert locked.wait(timeout=10)

    def release_cutover():
        release.set()

    timer = threading.Timer(0.2, release_cutover)
    timer.start()
    try:
        with pg_app.app_context():
            owner = db.session.get(Dataset, owner_id)
            with pytest.raises(LegacySalesClosed):
                assert_legacy_sales_writable(owner, day)
            db.session.rollback()
            assert DailySales.query.count() == 0
    finally:
        release.set()
        thread.join(timeout=10)
        timer.join(timeout=10)
    assert not thread.is_alive()
    assert not writer_errors


def test_readonly_audit_snapshot_and_prep_link_composite_constraints(pg_app):
    day = datetime.date(2026, 10, 16)
    with pg_app.app_context():
        owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
        guest = Dataset(kind="guest", created_at=datetime.datetime.now(datetime.timezone.utc),
                        last_activity_at=datetime.datetime.now(datetime.timezone.utc),
                        absolute_expires_at=datetime.datetime.now(datetime.timezone.utc)
                        + datetime.timedelta(hours=2))
        product = RegisterProduct(dataset_id=owner.id, name="独自", price_yen=130)
        db.session.add_all([guest, product])
        db.session.flush()
        local = PrepRecipe(dataset_id=owner.id, product_key="product:100",
                           display_name="独自", base_quantity=10)
        foreign = PrepRecipe(dataset_id=guest.id, product_key="product:100",
                             display_name="独自", base_quantity=10)
        db.session.add_all([local, foreign])
        db.session.commit()
        with sales_read_snapshot() as snapshot:
            assert snapshot.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
            result = audit_cutover(owner, planned_date=day, read_session=snapshot, today=day)
            assert result["status"] == "review"
            assert result["counts"]["register_products"] == 1
        db.session.add(RegisterPrepLink(dataset_id=owner.id, recipe_id=foreign.id,
                                         register_product_id=product.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        db.session.add(RegisterPrepLink(dataset_id=owner.id, recipe_id=local.id,
                                         register_product_id=product.id))
        db.session.commit()
        assert RegisterPrepLink.query.count() == 1
        assert audit_cutover(owner, planned_date=day, today=day)["unmapped_custom"] == []
