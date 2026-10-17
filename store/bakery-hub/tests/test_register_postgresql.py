"""Opt-inの隔離PostgreSQLだけでv3制約とmigrationを検証する。"""

import datetime
from pathlib import Path
import uuid

from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from models import Dataset, RegisterProduct, RegisterTransaction, RegisterTransactionLine, db
from postgresql_test_utils import get_isolated_postgresql_test_url


TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="isolated PostgreSQL test DB is not configured")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
NEW_HEAD = "a3c9d7e5f102"
OLD_HEAD = "e2a7c4d9b105"
TABLES = {"register_products", "register_cutovers", "register_transactions",
          "register_transaction_lines", "register_offline_permits"}
NOW = datetime.datetime(2026, 10, 16, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def migrated_postgresql():
    reset_engine = create_engine(TEST_URL, pool_pre_ping=True)
    try:
        with reset_engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        reset_engine.dispose()

    app = Flask("register_postgresql_test")
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        yield app
        db.session.remove()
        db.engine.dispose()


def rejected(row):
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_postgresql_register_constraints_and_populated_downgrade(migrated_postgresql):
    inspector = inspect(db.engine)
    assert TABLES <= set(inspector.get_table_names())
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD
    refs = {tuple(fk["constrained_columns"]): (fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys("register_transaction_lines")}
    assert refs == {
        ("transaction_id", "dataset_id"): ("register_transactions", ("id", "dataset_id")),
        ("register_product_id", "dataset_id"): ("register_products", ("id", "dataset_id")),
    }
    offline_refs = {tuple(fk["constrained_columns"]): fk["referred_table"]
                    for fk in inspector.get_foreign_keys("register_transactions")}
    assert offline_refs[("offline_permit_id", "dataset_id")] == "register_offline_permits"

    admin = Dataset.query.filter_by(kind="admin", system_key="admin").one()
    guest = Dataset(kind="guest", created_at=NOW, last_activity_at=NOW,
                    absolute_expires_at=NOW + datetime.timedelta(hours=2))
    db.session.add(guest)
    db.session.commit()
    item = RegisterProduct(dataset_id=admin.id, template_key="bread", name="食パン", price_yen=500)
    guest_item = RegisterProduct(dataset_id=guest.id, template_key="bread", name="食パン", price_yen=500)
    db.session.add_all([item, guest_item])
    db.session.commit()
    rejected(RegisterProduct(dataset_id=admin.id, name="高額", price_yen=501))
    rejected(RegisterProduct(dataset_id=admin.id, name="別の商品", price_yen=100, template_key="bread"))

    sale = RegisterTransaction(dataset_id=admin.id, submission_key=uuid.uuid4(), request_digest="a" * 64,
                               completed_at=NOW, business_date=NOW.date(), kind="sale", total_yen=500,
                               received_yen=500, change_yen=0)
    db.session.add(sale)
    db.session.commit()
    rejected(RegisterTransaction(dataset_id=admin.id, submission_key=sale.submission_key,
                                 request_digest="b" * 64, completed_at=NOW, business_date=NOW.date(),
                                 kind="sale", total_yen=500, received_yen=500, change_yen=0))
    rejected(RegisterTransaction(dataset_id=guest.id, submission_key=uuid.uuid4(),
                                 request_digest="a" * 64, completed_at=NOW, business_date=NOW.date(),
                                 kind="invalid", total_yen=0, received_yen=0, change_yen=0))

    def detail(product_id, quantity=1, amount=500):
        return RegisterTransactionLine(dataset_id=admin.id, transaction_id=sale.id,
                                       register_product_id=product_id, product_name_snapshot="食パン",
                                       unit_price_yen=500, quantity=quantity, line_total_yen=amount)

    rejected(detail(guest_item.id))  # Composite FK, not just the product PK.
    rejected(detail(item.id, quantity=51, amount=25500))
    rejected(detail(item.id, quantity=1, amount=499))
    db.session.add(detail(item.id))
    db.session.commit()
    assert RegisterTransactionLine.query.count() == 1
    with pytest.raises(SystemExit) as error:
        downgrade(directory=str(MIGRATIONS_DIR), revision=OLD_HEAD)
    assert error.value.code == 1
    db.session.rollback()
    assert TABLES <= set(inspect(db.engine).get_table_names())
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD


def test_postgresql_empty_register_tables_downgrade_safely(migrated_postgresql):
    assert all(db.session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
               for table in TABLES)
    # PostgreSQL holds ACCESS SHARE locks until this read transaction ends;
    # leave no reader open while Alembic drops the tables on another connection.
    db.session.rollback()
    downgrade(directory=str(MIGRATIONS_DIR), revision=OLD_HEAD)
    assert TABLES.isdisjoint(set(inspect(db.engine).get_table_names()))
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == OLD_HEAD
