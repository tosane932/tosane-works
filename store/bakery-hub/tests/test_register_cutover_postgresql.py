"""使い捨てPostgreSQLで切替と旧POSTの排他・履歴制約を検証する。"""

import concurrent.futures
import datetime
from pathlib import Path
import threading

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from legacy_sales_cutover import assert_legacy_sales_writable
from models import (DailySales, Dataset, Product, RegisterCutover,
                    RegisterCutoverApproval, RegisterPriceHistory, RegisterProduct,
                    RegisterRecoveryCase, RegisterRecoveryEvent, RegisterRecoveryLine,
                    RegisterTransaction, db)
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_cutover_service import CutoverBlocked, confirm_cutover
from register_products import create_custom_product, ensure_standard_products, update_register_product
from register_recovery_service import RecoveryConflict, preserve_case, review_case
from tests.test_register_recovery import receipt
from sqlalchemy import event


TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="isolated PostgreSQL test DB is not configured")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
DAY = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()


@pytest.fixture()
def pg_app():
    reset = create_engine(TEST_URL, pool_pre_ping=True)
    try:
        with reset.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        reset.dispose()
    app = Flask("cutover_postgresql")
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        owner_id = Dataset.query.filter_by(system_key="admin").one().id
    yield app, owner_id
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def args():
    return dict(date=DAY.isoformat(), approved_by="admin", reason="全端末と日次売上を確認",
                device_inventory="レジ端末A 0件、端末B 0件", devices_checked=True,
                backup_checked=True)


def test_parallel_cutover_is_idempotent_and_approval_is_single(pg_app):
    app, owner_id = pg_app
    with app.app_context():
        ensure_standard_products(db.session.get(Dataset, owner_id))
    barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            owner = db.session.get(Dataset, owner_id)
            barrier.wait(timeout=15)
            try:
                _, created = confirm_cutover(owner, **args())
                return created
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(worker) for _ in range(2)]]
    assert sorted(results) == [False, True]
    with app.app_context():
        assert RegisterCutover.query.count() == RegisterCutoverApproval.query.count() == 1


def test_legacy_writer_commits_first_and_cutover_rechecks_under_lock(pg_app):
    app, owner_id = pg_app
    with app.app_context():
        ensure_standard_products(db.session.get(Dataset, owner_id))
        item = Product(dataset_id=owner_id, year=DAY.year, month=DAY.month,
                       name="旧商品", price=200)
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    locked = threading.Event()
    release = threading.Event()

    def writer():
        with app.app_context():
            try:
                assert_legacy_sales_writable(db.session.get(Dataset, owner_id), DAY)
                locked.set()
                assert release.wait(15)
                db.session.add(DailySales(product_id=item_id, date=DAY, quantity=2))
                db.session.commit()
            finally:
                db.session.remove()

    def cutover():
        with app.app_context():
            try:
                return confirm_cutover(db.session.get(Dataset, owner_id), **args())
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer)
        assert locked.wait(15)
        second = pool.submit(cutover)
        assert not second.done()
        release.set()
        first.result(timeout=30)
        with pytest.raises(CutoverBlocked, match="旧売上"):
            second.result(timeout=30)
    with app.app_context():
        assert RegisterCutover.query.count() == RegisterCutoverApproval.query.count() == 0
        assert DailySales.query.count() == 1


def test_price_history_composite_fk_and_append_only(pg_app):
    app, owner_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset, owner_id)
        item = create_custom_product(owner, name="商品", price_yen=120)
        update_register_product(owner, item.id, name="商品", price_yen=150,
                                expected_name="商品", expected_price_yen=120)
        rows = RegisterPriceHistory.query.filter_by(dataset_id=owner_id).order_by(RegisterPriceHistory.id).all()
        assert [(r.event, r.old_price_yen, r.new_price_yen) for r in rows] == [
            ("created", None, 120), ("changed", 120, 150)]
        with pytest.raises(SQLAlchemyError):
            db.session.execute(text("DELETE FROM register_price_history WHERE id=:id"), {"id": rows[0].id})
        db.session.rollback()
        guest = Dataset(kind="guest", absolute_expires_at=datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(hours=1))
        db.session.add(guest)
        db.session.commit()
        with pytest.raises(IntegrityError):
            db.session.add(RegisterPriceHistory(dataset_id=guest.id,
                                                register_product_id=item.id,
                                                event="created", new_price_yen=120))
            db.session.commit()
        db.session.rollback()
        assert RegisterPriceHistory.query.count() == 2


def test_concurrent_preservation_same_key_fk_and_immutable_events(pg_app):
    app, owner_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset, owner_id)
        item = create_custom_product(owner, name="復旧商品", price_yen=120)
        foreign_item = create_custom_product(owner, name="越境検証商品", price_yen=120)
        original = receipt(item, day=DAY)
        product_id = foreign_item.id
    barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            barrier.wait(timeout=15)
            try:
                _, created = preserve_case(db.session.get(Dataset, owner_id), original,
                                          actor="admin", device_status="needs_review")
                return created
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(worker) for _ in range(2)]]
    assert sorted(results) == [False, True]
    with app.app_context():
        case = RegisterRecoveryCase.query.one()
        assert RegisterRecoveryEvent.query.count() == RegisterRecoveryLine.query.count() == 1
        assert RegisterTransaction.query.count() == 0
        with pytest.raises(SQLAlchemyError):
            db.session.execute(text("UPDATE register_recovery_events SET reason='変更'"))
        db.session.rollback()
        with pytest.raises(SQLAlchemyError):
            db.session.execute(text("UPDATE register_recovery_cases SET snapshot_json='{}'"))
        db.session.rollback()
        guest = Dataset(kind="guest", absolute_expires_at=datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(hours=1))
        db.session.add(guest)
        db.session.commit()
        with pytest.raises(IntegrityError):
            db.session.add(RegisterRecoveryLine(case_id=case.id, dataset_id=guest.id,
                register_product_id=product_id, product_name_snapshot="誤った商品", quantity=1,
                unit_price_yen=120, line_total_yen=120))
            db.session.commit()
        db.session.rollback()
        assert RegisterRecoveryLine.query.count() == 1


def test_concurrent_review_is_single_event_and_rollback_atomic(pg_app):
    app, owner_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset, owner_id)
        item = create_custom_product(owner, name="復旧商品", price_yen=120)
        case, _ = preserve_case(owner, receipt(item, day=DAY), actor="admin")
        case_id = case.id
    barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            barrier.wait(timeout=15)
            try:
                review_case(db.session.get(Dataset, owner_id), case_id, actor="admin",
                    target="investigating", reason="現金日報との照合を開始した", confirmed=True)
                return "saved"
            except RecoveryConflict:
                return "conflict"
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(worker) for _ in range(2)]]
    assert sorted(results) == ["conflict", "saved"]
    with app.app_context():
        assert RegisterRecoveryEvent.query.count() == 2
        def fail(_mapper, _connection, _target):
            if _target.to_status == "held":
                raise RuntimeError("audit failed")
        event.listen(RegisterRecoveryEvent, "before_insert", fail)
        try:
            with pytest.raises(RuntimeError, match="audit failed"):
                review_case(db.session.get(Dataset, owner_id), case_id, actor="admin",
                    target="held", reason="追加証拠の調査を継続する", confirmed=True)
        finally:
            event.remove(RegisterRecoveryEvent, "before_insert", fail)
        assert RegisterRecoveryCase.query.one().status == "investigating"
        assert RegisterRecoveryEvent.query.count() == 2
