"""Disposable PostgreSQL 18 startup checks; never use an app DATABASE_URL."""

import os
from pathlib import Path

import pytest
from flask import Flask
from flask_migrate import Migrate, upgrade
from sqlalchemy import create_engine, text

from models import db
from postgresql_test_utils import get_isolated_postgresql_test_url
from startup_guard import check_schema


URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(
    not URL or os.environ.get("STARTUP_GUARD_PG18_SYNTHETIC") != "1",
    reason="dedicated disposable PostgreSQL 18 required",
)
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_explicit_migration_then_repeated_read_only_web_preflight():
    engine = create_engine(URL)
    app = Flask("startup_pg18_synthetic")
    app.config.update(SQLALCHEMY_DATABASE_URI=URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS))
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        with pytest.raises(RuntimeError, match="not initialized"):
            check_schema(engine)
        with app.app_context():
            upgrade(directory=str(MIGRATIONS), revision="head")
        # The old seed would add seven legacy products and 49 sales to this
        # new-product-only state. Normal startup must preserve it exactly.
        with engine.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM datasets WHERE system_key='admin'")).scalar_one() == 1
            connection.execute(text("INSERT INTO register_products (dataset_id,name,price_yen) "
                                    "SELECT id,'synthetic bread',280 FROM datasets WHERE system_key='admin'"))
            connection.execute(text("INSERT INTO prep_recipes (dataset_id,product_key,display_name,base_quantity) "
                                    "SELECT id,'synthetic-key','synthetic recipe',10 FROM datasets WHERE system_key='admin'"))
            connection.execute(text("INSERT INTO prep_ingredients (dataset_id,recipe_id,name,unit,amount) "
                                    "SELECT dataset_id,id,'synthetic flour','g',100 FROM prep_recipes"))
        with engine.connect() as connection:
            before = tuple(connection.execute(text(
                f"SELECT count(*) FROM {table}"
            )).scalar_one() for table in ("products", "daily_sales", "register_products",
                                         "prep_recipes", "prep_ingredients"))
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        for _ in range(3):
            check_schema(engine)
        with engine.connect() as connection:
            after = tuple(connection.execute(text(
                f"SELECT count(*) FROM {table}"
            )).scalar_one() for table in ("products", "daily_sales", "register_products",
                                         "prep_recipes", "prep_ingredients"))
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == revision
        assert before == after == (0, 0, 1, 1, 1)
    finally:
        with app.app_context():
            db.session.remove()
        engine.dispose()
