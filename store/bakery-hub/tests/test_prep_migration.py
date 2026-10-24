"""新規仕込みテーブルだけを増減し、旧業務データを保持する。"""
import datetime
import uuid

from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text

from models import db
from tests.test_migrations import MIGRATIONS_DIR


def test_prep_upgrade_downgrade_preserves_existing_tables(tmp_path):
    app = Flask("prep_migration_test")
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'prep.sqlite'}",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="d5a9f1b7c204")
        before = set(inspect(db.engine).get_table_names())
        identifier = db.session.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
        db.session.execute(text("INSERT INTO products (dataset_id,year,month,name,price,is_active) VALUES (:id,2026,10,'従来の商品',280,1)"), {"id": identifier})
        db.session.commit()
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        after = set(inspect(db.engine).get_table_names())
        added_tables = {
            "prep_recipes", "prep_ingredients", "prep_plans",
            "register_products", "register_cutovers", "register_transactions",
            "register_transaction_lines", "register_prep_links",
            "register_price_history", "register_cutover_approvals",
                "register_recovery_cases", "register_recovery_lines", "register_recovery_events",
                "register_offline_permits",
            "waste_records",
        }
        assert after - before == added_tables
        assert db.session.execute(text("SELECT name FROM products WHERE name='従来の商品'")).scalar() == "従来の商品"
        db.session.execute(text("INSERT INTO prep_recipes (dataset_id, product_key,display_name,base_quantity) VALUES (:id,'template:croissant','クロワッサン',10)"), {"id": identifier})
        db.session.commit()
        downgrade(directory=str(MIGRATIONS_DIR), revision="d5a9f1b7c204")
        assert set(inspect(db.engine).get_table_names()) == before
        assert db.session.execute(text("SELECT name FROM products WHERE name='従来の商品'")).scalar() == "従来の商品"
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        assert added_tables.issubset(set(inspect(db.engine).get_table_names()))
