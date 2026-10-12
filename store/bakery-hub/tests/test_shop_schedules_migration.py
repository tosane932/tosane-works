"""新しい予定テーブルの往復migrationと既存Datasetの保全。"""
from pathlib import Path

from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text

from models import db

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
PREVIOUS = "a6b9c2d4e8f0"
HEAD = "d9e4b1c7a630"


def test_schedule_migration_adds_only_its_table_and_downgrades(tmp_path):
    application = Flask("schedule_migration_test")
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'schedule.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(application)
    Migrate(application, db, directory=str(MIGRATIONS))
    with application.app_context():
        upgrade(directory=str(MIGRATIONS), revision=PREVIOUS)
        before = db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one()
        assert "shop_schedules" not in inspect(db.engine).get_table_names()

        upgrade(directory=str(MIGRATIONS), revision=HEAD)
        columns = {column["name"] for column in inspect(db.engine).get_columns("shop_schedules")}
        assert {"dataset_id", "title", "starts_at", "ends_at", "note", "created_at", "updated_at"} <= columns
        assert db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one() == before

        downgrade(directory=str(MIGRATIONS), revision=PREVIOUS)
        assert "shop_schedules" not in inspect(db.engine).get_table_names()
        assert db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one() == before
