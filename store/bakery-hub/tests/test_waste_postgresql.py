"""Synthetic PostgreSQL verifies the migration and dataset/duplicate fences."""

import datetime
from pathlib import Path
import uuid

import pytest
from flask import Flask
from flask_migrate import Migrate, upgrade
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from models import db
from postgresql_test_utils import get_isolated_postgresql_test_url


URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL URL is not configured")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def test_postgresql_waste_migration_and_dataset_boundaries():
    engine = create_engine(URL, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        app = Flask("waste_pg_migration")
        app.config.update(SQLALCHEMY_DATABASE_URI=URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        Migrate(app, db, directory=str(MIGRATIONS))
        with app.app_context():
            upgrade(directory=str(MIGRATIONS), revision="head")
            assert "waste_records" in inspect(db.engine).get_table_names()
            assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "c8d3e7f1a204"

        with engine.begin() as connection:
            admin = connection.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
            guest = uuid.uuid4()
            connection.execute(text("INSERT INTO datasets (id, kind, absolute_expires_at) "
                                    "VALUES (:id, 'guest', now() + interval '2 hours')"), {"id": guest})
            register = connection.execute(text("INSERT INTO register_products (dataset_id, name, price_yen) "
                                               "VALUES (:id, '架空パン', 100) RETURNING id"), {"id": admin}).scalar_one()
            legacy = connection.execute(text("INSERT INTO products (dataset_id, year, month, name, price) "
                                             "VALUES (:id, 2026, 10, '架空旧商品', 100) RETURNING id"),
                                        {"id": admin}).scalar_one()
            connection.execute(text("INSERT INTO waste_records "
                                    "(dataset_id, business_date, register_product_id, product_label, "
                                    "made_quantity, discarded_quantity) "
                                    "VALUES (:id, :day, :product, '架空パン', NULL, 0)"),
                               {"id": admin, "day": datetime.date(2026, 10, 23), "product": register})

        def reject(sql, parameters):
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(text(sql), parameters)

        insert = ("INSERT INTO waste_records (dataset_id, business_date, register_product_id, "
                  "product_label, made_quantity) VALUES (:id, '2026-10-23', :product, '架空パン', :amount)")
        reject(insert, {"id": guest, "product": register, "amount": 1})
        reject(insert, {"id": admin, "product": register, "amount": 1})
        reject(insert, {"id": admin, "product": register, "amount": -1})
        reject("INSERT INTO waste_records (dataset_id, business_date, legacy_product_id, "
               "product_label, discarded_quantity) VALUES (:id, '2026-10-23', :product, '旧商品', 1)",
               {"id": guest, "product": legacy})
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO waste_records "
                                    "(dataset_id, business_date, legacy_product_id, product_label, discarded_quantity) "
                                    "VALUES (:id, '2026-10-23', :product, '旧商品', 2)"),
                               {"id": admin, "product": legacy})
            values = connection.execute(text("SELECT made_quantity, discarded_quantity FROM waste_records "
                                             "WHERE register_product_id=:product"), {"product": register}).one()
            assert values == (None, 0)
            assert connection.execute(text("SELECT count(*) FROM waste_records")).scalar_one() == 2
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        engine.dispose()
