"""使い捨てPostgreSQLだけで、実スキーマのバックアップと隔離復元を確認する。"""

import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from models import DailySales, Dataset, Product, RegisterPriceHistory, RegisterProduct, db
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_products import ensure_standard_products


TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="隔離PostgreSQLの設定なし")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _snapshot(engine):
    """復元先の全表件数、売上、版、制約を元DBと照合する。"""
    tables = inspect(engine).get_table_names(schema="public")
    with engine.connect() as connection:
        counts = {name: connection.execute(text(
            f'SELECT count(*) FROM "{name}"'
        )).scalar_one() for name in tables}
        revenue = connection.execute(text(
            "SELECT coalesce(sum(s.quantity * p.price), 0) "
            "FROM daily_sales s JOIN products p ON p.id = s.product_id"
        )).scalar_one()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        products = connection.execute(text(
            "SELECT template_key, name, price_yen, image_key, is_active "
            "FROM register_products ORDER BY template_key"
        )).all()
        # PostgreSQL may rewrite semantically identical CHECK text on restore.
        # Compare every name/type and exact FK/UNIQUE/PK definitions, then probe
        # CHECK, UNIQUE and FK rejection on the restored database below.
        constraints = connection.execute(text(
            "SELECT c.conrelid::regclass::text, c.conname, c.contype, "
            "CASE WHEN c.contype = 'c' THEN NULL ELSE pg_get_constraintdef(c.oid) END "
            "FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' ORDER BY 1, 2"
        )).all()
    return counts, revenue, revision, products, constraints


def _rejects(engine, sql, code):
    with engine.connect() as connection:
        with pytest.raises(IntegrityError) as failure:
            connection.execute(text(sql))
        assert failure.value.orig.pgcode == code


def test_real_schema_dump_and_isolated_restore_preserve_rows_and_constraints():
    # get_isolated_postgresql_test_url requires localhost, this exact disposable DB
    # name, and explicit reset opt-in. Never take DATABASE_URL from an app service.
    for tool in ("pg_dump", "pg_restore"):
        assert shutil.which(tool), f"{tool} が必要です"
    source_base = create_engine(TEST_URL)
    base_url = source_base.url
    source_base.dispose()
    suffix = uuid.uuid4().hex[:12]
    source_name = f"sales_data_app_test_backup_source_{suffix}"
    restore_name = f"sales_data_app_test_backup_restore_{suffix}"
    assert all(re.fullmatch(r"sales_data_app_test_backup_(source|restore)_[0-9a-f]{12}", name)
               for name in (source_name, restore_name))
    admin = create_engine(TEST_URL, isolation_level="AUTOCOMMIT")
    created = []
    engines = []
    app = None
    try:
        with admin.connect() as connection:
            for name in (source_name, restore_name):
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        source_url = base_url.set(database=source_name)
        restore_url = base_url.set(database=restore_name)
        app = Flask("isolated_backup_restore", static_folder=str(MIGRATIONS_DIR.parent / "static"))
        app.config.update(SQLALCHEMY_DATABASE_URI=source_url.render_as_string(hide_password=False),
                          SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        Migrate(app, db, directory=str(MIGRATIONS_DIR))
        with app.app_context():
            upgrade(directory=str(MIGRATIONS_DIR), revision="head")
            owner = Dataset.query.filter_by(system_key="admin").one()
            old = Product(dataset_id=owner.id, year=2026, month=10, name="隔離用旧商品", price=280)
            db.session.add(old)
            db.session.flush()
            db.session.add(DailySales(product_id=old.id, date=datetime.date(2026, 10, 15), quantity=3))
            db.session.commit()
            assert ensure_standard_products(owner) == 18
            assert ensure_standard_products(owner) == 0
            assert RegisterProduct.query.filter_by(dataset_id=owner.id).count() == 18
            assert RegisterPriceHistory.query.filter_by(dataset_id=owner.id).count() == 18
            assert Product.query.count() == DailySales.query.count() == 1
            db.session.remove()
            db.engine.dispose()

        source = create_engine(source_url)
        restored = create_engine(restore_url)
        engines.extend((source, restored))
        before = _snapshot(source)
        assert before[0]["register_products"] == 18
        assert before[0]["daily_sales"] == 1
        assert before[1] == 840

        client_env = dict(os.environ)
        client_env.update(PGHOST=base_url.host, PGPORT=str(base_url.port or 5432),
                          PGUSER=base_url.username, PGPASSWORD=base_url.password or "")
        with tempfile.TemporaryDirectory(prefix="isolated-pg-backup-") as directory:
            archive = Path(directory) / "test-only.dump"
            previous_umask = os.umask(0o077)
            try:
                subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl",
                                "--dbname", source_name, "--file", str(archive)],
                               env=client_env, check=True, capture_output=True)
            finally:
                os.umask(previous_umask)
            assert archive.stat().st_size > 0
            assert archive.stat().st_mode & 0o077 == 0
            subprocess.run(["pg_restore", "--no-owner", "--no-acl", "--exit-on-error",
                            "--dbname", restore_name, str(archive)],
                           env=client_env, check=True, capture_output=True)
        assert _snapshot(restored) == before
        _rejects(restored, "INSERT INTO register_products "
                 "(dataset_id, template_key, name, price_yen, is_active, display_order) "
                 "SELECT id, 'illegal_price', 'bad', 501, true, 0 "
                 "FROM datasets WHERE system_key = 'admin'", "23514")
        _rejects(restored, "INSERT INTO register_products "
                 "(dataset_id, template_key, name, price_yen, is_active, display_order) "
                 "SELECT dataset_id, template_key, name, price_yen, is_active, display_order "
                 "FROM register_products LIMIT 1", "23505")
        _rejects(restored, "INSERT INTO daily_sales (product_id, date, quantity) "
                 "VALUES (-123, '2026-10-18', 1)", "23503")
    finally:
        for engine in engines:
            engine.dispose()
        if app is not None:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        with admin.connect() as connection:
            for name in reversed(created):
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
