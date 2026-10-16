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
                    RegisterCutoverApproval, RegisterPriceHistory, RegisterProduct, db)
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_cutover_service import CutoverBlocked, confirm_cutover
from register_products import create_custom_product, update_register_product


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
