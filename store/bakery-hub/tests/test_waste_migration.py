"""The waste migration adds only its records and preserves legacy sales."""

from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text

from models import db
from tests.test_migrations import MIGRATIONS_DIR


def test_waste_upgrade_and_downgrade_preserve_older_data(tmp_path):
    app = Flask("waste_migration_test")
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'waste.sqlite'}",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="b6d2e9f4a107")
        db.session.execute(text("INSERT INTO products (dataset_id, year, month, name, price) "
                                "SELECT id, 2026, 10, '合成商品', 280 FROM datasets WHERE system_key='admin'"))
        db.session.execute(text("INSERT INTO daily_sales (product_id, date, quantity) "
                                "SELECT id, '2026-10-23', 7 FROM products WHERE name='合成商品'"))
        db.session.commit()
        before = set(inspect(db.engine).get_table_names())
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        inspector = inspect(db.engine)
        assert set(inspector.get_table_names()) - before == {"waste_records"}
        assert any(item["name"] == "uq_products_id_dataset_waste"
                   for item in inspector.get_unique_constraints("products"))
        assert db.session.execute(text("SELECT quantity FROM daily_sales")).scalar_one() == 7
        assert db.session.execute(text("SELECT count(*) FROM waste_records")).scalar_one() == 0
        downgrade(directory=str(MIGRATIONS_DIR), revision="b6d2e9f4a107")
        assert set(inspect(db.engine).get_table_names()) == before
        assert db.session.execute(text("SELECT quantity FROM daily_sales")).scalar_one() == 7
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        assert "waste_records" in inspect(db.engine).get_table_names()
