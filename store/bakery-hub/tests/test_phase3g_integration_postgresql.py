"""Disposable audit: migrate, encrypt, restore offline, then start the web image.

Only run in the dedicated CI with two synthetic PostgreSQL 18 services.
This audit test is not part of any production PR.
"""

from argparse import Namespace
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

from flask import Flask
from flask_migrate import Migrate, upgrade
import psycopg2
import pytest
from sqlalchemy import create_engine, text

import production_backup_guard as guard
from models import db
from postgresql_test_utils import get_isolated_postgresql_test_url
from startup_guard import check_schema
from test_production_backup_offline import approved


URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(
    not URL or os.environ.get("PHASE3G_SYNTHETIC_INTEGRATION") != "1",
    reason="two disposable PostgreSQL 18 services and an audit web image required",
)


def connect(port):
    return psycopg2.connect(host="127.0.0.1", port=port, dbname=(
        "sales_data_app_test" if port == 5432 else "restore_test"),
        user="ci_test_user", password="ci_test_password")


def snapshot(port):
    with closing(connect(port)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
        names = tuple(row[0] for row in cursor.fetchall())
        counts = []
        for name in names:
            cursor.execute(f'SELECT count(*) FROM "{name}"')
            counts.append((name, cursor.fetchone()[0]))
        cursor.execute("SELECT version_num FROM alembic_version")
        revision = cursor.fetchone()[0]
        cursor.execute("SELECT name,price,year,month FROM products ORDER BY id")
        products = cursor.fetchall()
        cursor.execute("SELECT date,quantity FROM daily_sales ORDER BY id")
        sales = cursor.fetchall()
        cursor.execute("SELECT name,price_yen,photo_webp FROM register_products ORDER BY id")
        register = [(row[0], row[1], hashlib.sha256(row[2]).hexdigest()) for row in cursor.fetchall()]
        cursor.execute("SELECT display_name,base_quantity FROM prep_recipes ORDER BY id")
        recipes = cursor.fetchall()
        cursor.execute("SELECT name,unit,amount FROM prep_ingredients ORDER BY id")
        ingredients = cursor.fetchall()
        cursor.execute("SELECT conname,contype FROM pg_constraint c JOIN pg_namespace n "
                       "ON n.oid=c.connamespace WHERE n.nspname='public' ORDER BY 1,2")
        constraints = cursor.fetchall()
        cursor.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY 1")
        sequences = cursor.fetchall()
        return names, tuple(counts), revision, products, sales, register, recipes, ingredients, constraints, sequences


def test_migrated_backup_offline_restore_then_repeated_web_start(monkeypatch, tmp_path):
    assert guard.client_major("pg_dump") == guard.client_major("pg_restore") == 18
    # Each endpoint belongs only to the disposable CI services. Previous tests
    # may have left synthetic tables; reset these explicit test databases only.
    for port in (5432, 5433):
        with closing(connect(port)) as connection, connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
    app = Flask("phase3g_synthetic")
    app.config.update(SQLALCHEMY_DATABASE_URI=URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    Migrate(app, db, directory=str(migrations))
    with app.app_context():
        upgrade(directory=str(migrations), revision="head")
    engine = create_engine(URL)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO products(dataset_id,year,month,name,price) "
                                "SELECT id,2026,10,'synthetic roll',280 FROM datasets WHERE system_key='admin'"))
        connection.execute(text("INSERT INTO daily_sales(product_id,date,quantity) "
                                "SELECT id,'2026-10-22',3 FROM products"))
        connection.execute(text("INSERT INTO register_products(dataset_id,name,price_yen,photo_webp) "
                                "SELECT id,'synthetic loaf',350,decode('aabbcc','hex') "
                                "FROM datasets WHERE system_key='admin'"))
        connection.execute(text("INSERT INTO prep_recipes(dataset_id,product_key,display_name,base_quantity) "
                                "SELECT id,'synthetic-loaf','synthetic recipe',10 "
                                "FROM datasets WHERE system_key='admin'"))
        connection.execute(text("INSERT INTO prep_ingredients(dataset_id,recipe_id,name,unit,amount) "
                                "SELECT dataset_id,id,'synthetic flour','g',100 FROM prep_recipes"))
    check_schema(engine)
    engine.dispose()
    expected = snapshot(5432)
    assert len(expected[0]) >= 27
    assert dict(expected[1])["daily_sales"] == 1

    service = tmp_path / "service"
    service.write_text("[ci_source]\nhost=127.0.0.1\nport=5432\n"
                       "dbname=sales_data_app_test\nuser=ci_test_user\nsslmode=disable\n"
                       "[ci_target]\nhost=127.0.0.1\nport=5433\n"
                       "dbname=restore_test\nuser=ci_test_user\nsslmode=disable\n")
    password = tmp_path / "pass"
    password.write_text("127.0.0.1:5432:sales_data_app_test:ci_test_user:ci_test_password\n"
                        "127.0.0.1:5433:restore_test:ci_test_user:ci_test_password\n")
    service.chmod(0o600)
    password.chmod(0o600)
    monkeypatch.setenv("PGSERVICEFILE", str(service))
    monkeypatch.setenv("PGPASSFILE", str(password))
    monkeypatch.delenv("PGPASSWORD", raising=False)
    folder = tmp_path / "BakeryHub-Backup" / "database"
    folder.mkdir(parents=True, mode=0o700)
    folder.parent.chmod(0o700)
    monkeypatch.setattr(guard, "BACKUP_DIR", folder)
    key = tmp_path / "key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    recipient = tmp_path / "recipient"
    with recipient.open("wb") as output:
        subprocess.run(["age-keygen", "-y", str(key)], check=True,
                       stdout=output, stderr=subprocess.DEVNULL)
    recipient.chmod(0o600)
    source_identity = guard.identity("ci_source")
    target_identity = guard.identity("ci_target")
    path, size, digest = guard.encrypted_backup("ci_source", "sales_data_app_test",
                                               "sales_data_app_test", True, recipient)
    assert guard.verify_encrypted_archive(path, key, digest) == (size, digest)
    assert not list(tmp_path.rglob("*.dump"))
    with pytest.raises(RuntimeError, match="not initialized"):
        target_engine = create_engine(URL.replace(":5432/", ":5433/").replace(
            "/sales_data_app_test", "/restore_test"))
        try:
            check_schema(target_engine)
        finally:
            target_engine.dispose()

    # The source libpq service is removed; a mistaken source preflight is fatal.
    service.write_text("[ci_target]\nhost=127.0.0.1\nport=5433\n"
                       "dbname=restore_test\nuser=ci_test_user\nsslmode=disable\n")
    password.write_text("127.0.0.1:5433:restore_test:ci_test_user:ci_test_password\n")
    monkeypatch.setattr(guard, "preflight", lambda *a: pytest.fail("source contacted"))
    monkeypatch.setattr(guard, "source_preflight", lambda *a: pytest.fail("source contacted"))
    approvals = tmp_path / "approvals"
    approvals.mkdir(mode=0o700)
    record = approved() | dict(backup_id=path.name, source_service="ci_source",
        source_database="sales_data_app_test", source_host="127.0.0.1",
        source_address=source_identity[1], source_port=source_identity[2],
        target_service="ci_target", target_database="restore_test", target_host="127.0.0.1",
        target_address=target_identity[1], target_port=target_identity[2],
        target_connect_port=5433, encrypted_size=size, encrypted_sha256=digest)
    approval = approvals / "approved.json"
    approval.write_text(json.dumps(record))
    approval.chmod(0o600)
    args = Namespace(approval_record=str(approval), identity_file=str(key),
                     confirm_source="sales_data_app_test", confirm_target="restore_test",
                     confirm_backup_id=path.name, path=str(path))
    with pytest.raises(ValueError, match="SHA-256"):
        record["encrypted_sha256"] = "b" * 64
        approval.write_text(json.dumps(record))
        guard.restore_encrypted_offline_approved(args)
    record["encrypted_sha256"] = digest
    approval.write_text(json.dumps(record))
    assert guard.restore_encrypted_offline_approved(args) == size
    assert snapshot(5433) == expected
    restored = create_engine(URL.replace(":5432/", ":5433/").replace(
        "/sales_data_app_test", "/restore_test"))
    try:
        check_schema(restored)
        for _ in range(2):
            container = subprocess.check_output(["docker", "run", "-d", "--network", "host",
                "-e", "DATABASE_URL=" + str(restored.url), "-e", "SECRET_KEY=synthetic-only",
                "bakeryhub-phase3g-synthetic"], text=True).strip()
            try:
                for attempt in range(30):
                    try:
                        with urlopen("http://127.0.0.1:5000/login", timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(1)
                else:
                    pytest.fail("synthetic web container failed to become ready")
            finally:
                subprocess.run(["docker", "stop", container], check=True, stdout=subprocess.DEVNULL)
            assert snapshot(5433) == expected
        with pytest.raises(ValueError, match="not empty"):
            guard.restore_encrypted_offline_approved(args)
    finally:
        restored.dispose()
    assert not list(tmp_path.rglob("*.dump"))
