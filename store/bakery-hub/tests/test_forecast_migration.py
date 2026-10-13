"""予報テーブルだけを追加・削除するmigrationの往復。"""
from pathlib import Path

from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text

from models import db

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
PREVIOUS = "d9e4b1c7a630"
HEAD = "b7f2a1c9d603"


def test_forecast_migration_upgrade_downgrade_preserves_existing_data(tmp_path):
    application = Flask("forecast_migration_test")
    application.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'forecast.sqlite'}",
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(application)
    Migrate(application, db, directory=str(MIGRATIONS))
    with application.app_context():
        upgrade(directory=str(MIGRATIONS), revision=PREVIOUS)
        before = db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one()
        original_tables = set(inspect(db.engine).get_table_names())
        upgrade(directory=str(MIGRATIONS), revision=HEAD)
        assert set(inspect(db.engine).get_table_names()) - original_tables == {"daily_forecasts"}
        columns = {col["name"] for col in inspect(db.engine).get_columns("daily_forecasts")}
        assert {"dataset_id", "business_date", "weather_code", "forecast_valid_at", "fetched_at", "latitude",
                "longitude", "location_name", "source"} <= columns
        assert db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one() == before
        downgrade(directory=str(MIGRATIONS), revision=PREVIOUS)
        assert set(inspect(db.engine).get_table_names()) == original_tables
        assert db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one() == before
