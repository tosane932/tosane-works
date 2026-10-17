"""隔離PostgreSQLで同時POST、Datasetロック、保存失敗を確認。"""

import concurrent.futures
import datetime
from pathlib import Path
import threading
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, text

import app as app_module
from models import (Dataset, RegisterCutover, RegisterProduct, RegisterTransaction,
                    RegisterTransactionLine, db)
from postgresql_test_utils import get_isolated_postgresql_test_url
import register_service as service
from register_products import update_register_product


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
    app = Flask("register_service_postgresql")
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        now = datetime.datetime.now(datetime.timezone.utc)
        guest = Dataset(kind="guest", created_at=now - datetime.timedelta(minutes=5),
                        last_activity_at=now, absolute_expires_at=now + datetime.timedelta(hours=2))
        db.session.add(guest)
        db.session.flush()
        db.session.add(RegisterCutover(dataset_id=guest.id,
                                       first_register_date=datetime.date(2020, 1, 1)))
        item = RegisterProduct(dataset_id=guest.id, name="クロワッサン", price_yen=120)
        db.session.add(item)
        db.session.commit()
        guest_id, item_id = guest.id, item.id
    yield app, guest_id, item_id
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def _race(app, owner_id, action):
    barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            owner = db.session.get(Dataset, owner_id)
            barrier.wait(timeout=15)
            try:
                return action(owner)
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        return [future.result(timeout=30) for future in futures]


def test_concurrent_retry_commits_one_and_keeps_snapshot(pg_app):
    app, owner_id, product_id = pg_app
    key = uuid.uuid4()

    def save(owner):
        value = service.save_register_transaction(
            owner, submission_key=key, kind="sale", items=[(product_id, 2)],
            received_yen=500)
        return value.id, value.total_yen

    first, second = _race(app, owner_id, save)
    assert first == second and first[1] == 240
    with app.app_context():
        assert RegisterTransaction.query.count() == 1
        assert RegisterTransactionLine.query.one().unit_price_yen == 120


def test_concurrent_guest_cap_and_different_keys(pg_app, monkeypatch):
    app, owner_id, product_id = pg_app
    monkeypatch.setattr(service, "GUEST_TRANSACTION_LIMIT", 1)

    def save(owner):
        try:
            service.save_register_transaction(
                owner, submission_key=uuid.uuid4(), kind="sale",
                items=[(product_id, 1)], received_yen=120)
            return "saved"
        except service.GuestTransactionLimitReached:
            return "full"

    assert sorted(_race(app, owner_id, save)) == ["full", "saved"]
    with app.app_context():
        assert RegisterTransaction.query.count() == 1
        assert RegisterTransactionLine.query.count() == 1


def test_failed_commit_rolls_back_and_next_request_succeeds(pg_app, monkeypatch):
    app, owner_id, product_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset, owner_id)
        commit = db.session.commit

        def fail():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(db.session, "commit", fail)
        with pytest.raises(RuntimeError, match="commit failed"):
            service.save_register_transaction(
                owner, submission_key=uuid.uuid4(), kind="sale",
                items=[(product_id, 1)], received_yen=120)
        monkeypatch.setattr(db.session, "commit", commit)
        assert RegisterTransaction.query.count() == 0
        service.save_register_transaction(
            owner, submission_key=uuid.uuid4(), kind="sale",
            items=[(product_id, 1)], received_yen=120)
        assert RegisterTransaction.query.count() == 1


def test_price_change_is_checked_under_lock_but_saved_key_replays(pg_app):
    app, owner_id, product_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset, owner_id)
        day = service.utc_now().astimezone(service.TOKYO).date().isoformat()
        key = uuid.uuid4()
        kwargs = dict(kind="sale", items=[(product_id, 1)], received_yen=200,
                      displayed_prices=[(product_id, 120)], checkout_business_date=day)
        first = service.save_register_transaction(owner, submission_key=key, **kwargs)
        update_register_product(owner, product_id, name="クロワッサン", price_yen=140)
        replay = service.save_register_transaction(owner, submission_key=key, **kwargs)
        assert replay.id == first.id and replay.total_yen == 120
        with pytest.raises(service.PriceChanged):
            service.save_register_transaction(owner, submission_key=uuid.uuid4(), **kwargs)
        assert RegisterTransaction.query.count() == 1


def test_guest_cleanup_serializes_with_checkout(pg_app, monkeypatch):
    app, owner_id, product_id = pg_app
    locked = threading.Event()
    resume = threading.Event()
    actual_now = service.utc_now

    def pause_after_lock():
        locked.set()
        assert resume.wait(timeout=15)
        return actual_now()

    monkeypatch.setattr(service, "utc_now", pause_after_lock)

    def save():
        with app.app_context():
            owner = db.session.get(Dataset, owner_id)
            try:
                return service.save_register_transaction(
                    owner, submission_key=uuid.uuid4(), kind="sale",
                    items=[(product_id, 1)], received_yen=120).id
            finally:
                db.session.remove()

    def cleanup():
        with app.app_context():
            try:
                count = app_module._cleanup_expired_guest_datasets(
                    now=actual_now() + datetime.timedelta(minutes=31))
                db.session.commit()
                return count
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(save)
        assert locked.wait(timeout=15)
        cleaned = pool.submit(cleanup)
        resume.set()
        assert saved.result(timeout=30) is not None
        assert cleaned.result(timeout=30) == 1
    with app.app_context():
        assert db.session.get(Dataset, owner_id) is None
        assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 0


def test_concurrent_offline_next_day_same_key_and_audit(pg_app, monkeypatch):
    from zoneinfo import ZoneInfo
    from models import RegisterOfflinePermit
    import register_offline_service as offline
    from tests.test_register_recovery import receipt

    app, _, _ = pg_app
    app.secret_key = "isolated-test-only"
    with app.app_context():
        admin = Dataset(kind="admin", system_key="offline-admin")
        db.session.add(admin)
        db.session.flush()
        item = RegisterProduct(dataset_id=admin.id, name="パン", price_yen=120)
        day = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("Asia/Tokyo")).date()
        db.session.add_all([item, RegisterCutover(dataset_id=admin.id,
                                                  first_register_date=day)])
        db.session.commit()
        admin_id, product_id = admin.id, item.id
        grant, token, proof, _ = offline.prepare_offline(admin, str(uuid.uuid4()), next_day=True)
        grant_id = grant.id
        original = receipt(item, day=day)
        original["catalog_proof"] = proof
        original["completed_at_local"] = grant.issued_at.isoformat()
        monkeypatch.setattr(offline, "utc_now", lambda: datetime.datetime.combine(
            day + datetime.timedelta(days=1), datetime.time(8),
            tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(datetime.timezone.utc))

    def save(owner):
        result = offline.save_offline_transaction(owner, original, token)
        return result.id

    assert len(set(_race(app, admin_id, save))) == 1
    with app.app_context():
        tx = RegisterTransaction.query.filter_by(dataset_id=admin_id).one()
        assert tx.business_date == day and tx.save_method == "offline"
        assert tx.offline_permit_id == grant_id
        assert RegisterTransactionLine.query.filter_by(transaction_id=tx.id).count() == 1
        assert RegisterOfflinePermit.query.filter_by(dataset_id=admin_id).count() == 1
        assert RegisterTransaction.query.filter_by(dataset_id=admin_id).count() == 1
