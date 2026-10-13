"""Staff and shift migration round trip on an isolated SQLite database."""
from pathlib import Path

from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text

from models import db

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
PREVIOUS = "b7f2a1c9d603"
HEAD = "c8e6a1b9d4f2"


def test_staff_shift_migration_preserves_existing_tables_and_rows(tmp_path):
    app = Flask("staff_shift_migration_test")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'staff_shifts.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS), revision=PREVIOUS)
        admin_id = db.session.execute(text(
            "SELECT id FROM datasets WHERE system_key = 'admin'"
        )).scalar_one()
        db.session.execute(text(
            "INSERT INTO products (id, dataset_id, year, month, name, price, is_active) "
            "VALUES (9001, :dataset_id, 2026, 10, '既存商品', 200, 1)"
        ), {"dataset_id": admin_id})
        db.session.execute(text(
            "INSERT INTO daily_sales (id, product_id, date, quantity) "
            "VALUES (9002, 9001, '2026-10-14', 3)"
        ))
        db.session.execute(text(
            "INSERT INTO shop_schedules (id, dataset_id, title, starts_at) "
            "VALUES (9003, :dataset_id, '既存予定', '2026-10-13 20:00:00')"
        ), {"dataset_id": admin_id})
        db.session.commit()
        before = {
            table: db.session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in ("datasets", "products", "daily_sales", "shop_schedules")
        }

        upgrade(directory=str(MIGRATIONS), revision=HEAD)
        inspector = inspect(db.engine)
        assert {"shop_staff", "shop_shifts"} <= set(inspector.get_table_names())
        staff_columns = {row["name"] for row in inspector.get_columns("shop_staff")}
        shift_columns = {row["name"] for row in inspector.get_columns("shop_shifts")}
        assert {"dataset_id", "display_name", "role", "is_active", "submission_key"} <= staff_columns
        assert {"dataset_id", "staff_id", "business_date", "status", "starts_at", "ends_at"} <= shift_columns
        assert any(set(row["column_names"]) == {"dataset_id", "staff_id", "business_date"}
                   for row in inspector.get_unique_constraints("shop_shifts"))
        assert any(set(row["constrained_columns"]) == {"staff_id", "dataset_id"}
                   for row in inspector.get_foreign_keys("shop_shifts"))
        assert all(db.session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == count
                   for table, count in before.items())

        downgrade(directory=str(MIGRATIONS), revision=PREVIOUS)
        assert {"shop_staff", "shop_shifts"}.isdisjoint(inspect(db.engine).get_table_names())
        assert all(db.session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == count
                   for table, count in before.items())
        assert db.session.execute(text("SELECT quantity FROM daily_sales WHERE id = 9002")).scalar_one() == 3
