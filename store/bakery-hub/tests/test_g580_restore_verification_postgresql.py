"""PR限定: 架空のPostgreSQL 18実スキーマで復元照合SQLを検証する。"""

import datetime
import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import psycopg2
import pytest

from models import (DailySales, Dataset, PrepIngredient, PrepPlan, PrepRecipe, Product,
                    RegisterPrepLink, RegisterPriceHistory, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, ShopShift, ShopStaff, db)


pytestmark = pytest.mark.skipif(
    os.environ.get("ENCRYPTED_BACKUP_SYNTHETIC_TEST") != "1",
    reason="two disposable PostgreSQL 18 CI servers are required",
)
ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "docs/g580-restore-verification.sql"


def _psql(service, service_file, pass_file):
    env = dict(os.environ, PGSERVICEFILE=str(service_file), PGPASSFILE=str(pass_file))
    env.pop("PGPASSWORD", None)
    return subprocess.run(
        ["psql", "-X", "-A", "-v", "ON_ERROR_STOP=1",
         "--dbname", f"service={service}", "--file", str(SQL)],
        capture_output=True, text=True, env=env,
    )


def test_real_migration_schema_and_read_only_verification(tmp_path):
    # CIの専用localhost:5433、使い捨てDB、明示的なテストフラグ以外は拒否。
    assert os.environ.get("ENCRYPTED_BACKUP_SYNTHETIC_TEST") == "1"
    admin = psycopg2.connect(host="127.0.0.1", port=5433, dbname="restore_test",
                             user="ci_test_user", password="ci_test_password")
    admin.autocommit = True
    app = None
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT current_setting('server_version_num')::int / 10000")
            assert cursor.fetchone()[0] == 18
            cursor.execute("CREATE DATABASE isolated_restore")
        app = Flask("phase3c_synthetic", static_folder=str(ROOT / "static"))
        app.config.update(
            SQLALCHEMY_DATABASE_URI=(
                "postgresql+psycopg2://ci_test_user:ci_test_password"
                "@127.0.0.1:5433/isolated_restore"
            ),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(app)
        Migrate(app, db, directory=str(ROOT / "migrations"))
        with app.app_context():
            upgrade(directory=str(ROOT / "migrations"), revision="head")
            owner = Dataset.query.filter_by(system_key="admin").one()
            guest = Dataset(kind="guest", absolute_expires_at=(
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)))
            db.session.add(guest)
            db.session.flush()
            old = Product(dataset_id=owner.id, year=2026, month=10,
                          name="synthetic private bread", price=280)
            db.session.add(old)
            db.session.flush()
            db.session.add(DailySales(product_id=old.id, date=datetime.date(2026, 10, 21), quantity=2))
            db.session.add(Product(dataset_id=guest.id, year=2026, month=10,
                                   name="synthetic guest bread", price=120))
            item = RegisterProduct(dataset_id=owner.id, name="synthetic private item",
                                   price_yen=280, photo_webp=b"synthetic-photo-bytes")
            db.session.add(item)
            db.session.flush()
            db.session.add(RegisterPriceHistory(dataset_id=owner.id, register_product_id=item.id,
                                                event="created", new_price_yen=280))
            recipe = PrepRecipe(dataset_id=owner.id, product_key="synthetic-recipe",
                                display_name="synthetic", base_quantity=10)
            db.session.add(recipe)
            db.session.flush()
            db.session.add(PrepIngredient(dataset_id=owner.id, recipe_id=recipe.id,
                                          name="synthetic flour", unit="g", amount=100))
            db.session.add(PrepPlan(dataset_id=owner.id, recipe_id=recipe.id,
                                    business_date=datetime.date(2026, 10, 21), quantity=2))
            db.session.add(RegisterPrepLink(dataset_id=owner.id, recipe_id=recipe.id,
                                            register_product_id=item.id))
            staff = ShopStaff(dataset_id=owner.id, display_name="synthetic private staff", role="sales")
            db.session.add(staff)
            db.session.flush()
            db.session.add(ShopShift(dataset_id=owner.id, staff_id=staff.id,
                                     business_date=datetime.date(2026, 10, 21), status="off"))
            sale = RegisterTransaction(dataset_id=owner.id, submission_key=uuid.uuid4(),
                                       request_digest="a" * 64, business_date=datetime.date(2026, 10, 21),
                                       kind="sale", total_yen=280, received_yen=300, change_yen=20)
            refund = RegisterTransaction(dataset_id=owner.id, submission_key=uuid.uuid4(),
                                         request_digest="b" * 64, business_date=datetime.date(2026, 10, 21),
                                         kind="refund", total_yen=-280)
            db.session.add_all([sale, refund])
            db.session.flush()
            db.session.add(RegisterTransactionLine(transaction_id=sale.id, dataset_id=owner.id,
                register_product_id=item.id, product_name_snapshot="synthetic", unit_price_yen=280,
                quantity=1, line_total_yen=280))
            db.session.commit()
            db.session.remove()
            db.engine.dispose()

        service = tmp_path / "pg_service.conf"
        service.write_text("[approved_restore_target]\nhost=127.0.0.1\nport=5433\n"
                           "dbname=isolated_restore\nuser=ci_test_user\nsslmode=disable\n"
                           "[wrong_target]\nhost=127.0.0.1\nport=5433\n"
                           "dbname=restore_test\nuser=ci_test_user\nsslmode=disable\n")
        password = tmp_path / "pgpass"
        password.write_text("127.0.0.1:5433:*:ci_test_user:ci_test_password\n")
        service.chmod(0o600)
        password.chmod(0o600)

        result = _psql("approved_restore_target", service, password)
        assert result.returncode == 0, result.stderr
        counts = dict(re.findall(r"^public\.([a-z_]+)\|(\d+)$", result.stdout, re.MULTILINE))
        assert len(counts) == 27
        assert counts["daily_sales"] == "1"
        assert counts["register_transactions"] == "2"
        assert counts["register_transaction_lines"] == "1"
        assert counts["prep_ingredients"] == counts["prep_plans"] == counts["shop_shifts"] == "1"
        assert "admin|1" in result.stdout and "guest|1" in result.stdout
        photo = b"synthetic-photo-bytes"
        photo_digest = hashlib.sha256(hashlib.sha256(photo).hexdigest().encode()).hexdigest()
        assert f"1|{len(photo)}|{photo_digest}" in result.stdout
        dated_digest = hashlib.sha256(b"2026-10-21:1:2:560").hexdigest()
        assert f"1|1|{dated_digest}" in result.stdout
        register_digest = hashlib.sha256(
            b"2026-10-21:refund:1:-280\n2026-10-21:sale:1:280").hexdigest()
        assert f"2|2|{register_digest}" in result.stdout
        assert "synthetic private" not in result.stdout
        orphan_columns = ("orphan_transaction_lines", "orphan_transaction_products",
                          "orphan_price_history", "orphan_recipe_links", "orphan_ingredients",
                          "orphan_plans", "orphan_recovery_lines", "orphan_recovery_events",
                          "orphan_shifts")
        assert "|".join(orphan_columns) in result.stdout
        assert "|".join("0" for _ in orphan_columns) in result.stdout
        assert "alembic_rows" in result.stdout
        assert re.search(r"^[pufc]\|\d+\|0$", result.stdout, re.MULTILINE)
        assert "invalid|not_ready" in result.stdout
        assert "internal|enabled|triggers" in result.stdout
        assert "schemaname|sequencename|last_value" in result.stdout

        wrong = _psql("wrong_target", service, password)
        assert wrong.returncode != 0
        assert "public.daily_sales" not in wrong.stdout

        # 27表の安全ゲートを保ちつつ、FKを持たない合成ビューで動的孤児検出を試す。
        isolated = psycopg2.connect(host="127.0.0.1", port=5433, dbname="isolated_restore",
                                    user="ci_test_user", password="ci_test_password")
        try:
            with isolated.cursor() as cursor:
                cursor.execute("CREATE VIEW public.orphan_probe AS SELECT %s::uuid AS dataset_id",
                               (str(uuid.uuid4()),))
            isolated.commit()
        finally:
            isolated.close()
        orphan = _psql("approved_restore_target", service, password)
        assert orphan.returncode == 0, orphan.stderr
        assert re.search(r"^public\.orphan_probe\|1$", orphan.stdout, re.MULTILINE)
    finally:
        if app is not None:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        with admin.cursor() as cursor:
            cursor.execute("DROP DATABASE IF EXISTS isolated_restore WITH (FORCE)")
        admin.close()
