"""隔離PostgreSQLでDatasetロックとテンプレート一意制約の同時実行を検証。"""

import concurrent.futures
import datetime
from pathlib import Path
import threading
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, text

from models import Dataset, RegisterProduct, db
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_products import (CustomProductLimitReached, ProductSubmissionConflict,
                               create_custom_product, ensure_standard_products)


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
    app = Flask("register_products_postgresql")
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      static_folder=str(MIGRATIONS_DIR.parent / "static"))
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        now = datetime.datetime.now(datetime.timezone.utc)
        guest = Dataset(kind="guest", created_at=now - datetime.timedelta(minutes=5),
                        last_activity_at=now, absolute_expires_at=now + datetime.timedelta(hours=2))
        db.session.add(guest)
        db.session.commit()
        guest_id = guest.id
    yield app, guest_id
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def _race(app, dataset_id, action):
    barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            dataset = db.session.get(Dataset, dataset_id)
            barrier.wait(timeout=15)
            try:
                return action(dataset)
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        return [future.result(timeout=25) for future in futures]


def test_parallel_standard_seeding_is_idempotent_and_preserves_prices(pg_app):
    app, guest_id = pg_app
    assert sorted(_race(app, guest_id, ensure_standard_products)) == [0, 18]
    with app.app_context():
        products = RegisterProduct.query.filter_by(dataset_id=guest_id).all()
        assert len(products) == 18
        melon = next(p for p in products if p.template_key == "melonpan")
        melon.price_yen, melon.image_key, melon.is_active = 150, None, False
        db.session.commit()
        assert ensure_standard_products(db.session.get(Dataset, guest_id)) == 0
        db.session.refresh(melon)
        assert (melon.price_yen, melon.image_key, melon.is_active) == (150, None, False)


def test_parallel_guest_custom_registration_never_exceeds_thirty(pg_app):
    app, guest_id = pg_app
    with app.app_context():
        guest = db.session.get(Dataset, guest_id)
        ensure_standard_products(guest)
        for i in range(29):
            create_custom_product(guest, name=f"独自{i}", price_yen=200)

    def try_create(dataset):
        try:
            create_custom_product(dataset, name="同時追加", price_yen=300)
            return "saved"
        except CustomProductLimitReached:
            return "full"

    assert sorted(_race(app, guest_id, try_create)) == ["full", "saved"]
    with app.app_context():
        assert RegisterProduct.query.filter_by(dataset_id=guest_id, template_key=None).count() == 30
        assert RegisterProduct.query.filter_by(dataset_id=guest_id).count() == 48


def test_parallel_same_submission_key_creates_one_product_and_detects_conflict(pg_app):
    app, guest_id = pg_app
    key = str(uuid.uuid4())
    ids = _race(app, guest_id, lambda dataset: create_custom_product(
        dataset, name="同時商品", price_yen=500, request_key=key).id)
    assert ids[0] == ids[1]
    with app.app_context():
        assert RegisterProduct.query.filter_by(dataset_id=guest_id, template_key=None).count() == 1
        with pytest.raises(ProductSubmissionConflict):
            create_custom_product(db.session.get(Dataset, guest_id), name="別商品",
                                  price_yen=500, request_key=key)
        assert RegisterProduct.query.filter_by(dataset_id=guest_id, template_key=None).count() == 1


def test_guest_seed_and_cutover_share_transaction(pg_app):
    from models import RegisterCutover
    app, guest_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset, guest_id)
        day = datetime.date.today()
        db.session.add(RegisterCutover(dataset_id=guest_id, first_register_date=day))
        assert ensure_standard_products(owner, commit=False) > 0
        db.session.rollback()
        assert db.session.get(RegisterCutover, guest_id) is None
        assert RegisterProduct.query.filter_by(dataset_id=guest_id).count() == 0
        db.session.add(RegisterCutover(dataset_id=guest_id, first_register_date=day))
        ensure_standard_products(owner, commit=False)
        db.session.commit()
        assert db.session.get(RegisterCutover, guest_id).first_register_date == day
        assert ensure_standard_products(owner) == 0
