"""All-day CHECK changes preserve existing timed shifts on SQLite."""
from pathlib import Path

import pytest
from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import text

from models import db

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
OLD = "c8e6a1b9d4f2"
NEW = "d5a9f1b7c204"


def test_all_day_migration_preserves_legacy_and_guards_downgrade(tmp_path):
    app = Flask("all_day_migration")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'all_day.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS), revision=OLD)
        admin = db.session.execute(text("SELECT id FROM datasets WHERE system_key = 'admin'")).scalar_one()
        db.session.execute(text(
            "INSERT INTO shop_staff (id, dataset_id, display_name, role, submission_key) "
            "VALUES (8701, :admin, '既存スタッフ', 'production', 'legacy-migration-key')"
        ), {"admin": admin})
        db.session.execute(text(
            "INSERT INTO shop_shifts (id, dataset_id, staff_id, business_date, status, starts_at, ends_at) "
            "VALUES (8702, :admin, 8701, '2026-10-14', 'work', "
            "'2026-10-14 13:00:00', '2026-10-14 17:00:00')"
        ), {"admin": admin})
        db.session.commit()

        upgrade(directory=str(MIGRATIONS), revision=NEW)
        assert db.session.execute(text(
            "SELECT starts_at, ends_at FROM shop_shifts WHERE id=8702"
        )).one() == ("2026-10-14 13:00:00", "2026-10-14 17:00:00")
        db.session.execute(text(
            "INSERT INTO shop_shifts (id, dataset_id, staff_id, business_date, status) "
            "VALUES (8703, :admin, 8701, '2026-10-15', 'all_day')"
        ), {"admin": admin})
        db.session.commit()
        with pytest.raises(SystemExit):
            downgrade(directory=str(MIGRATIONS), revision=OLD)
        db.session.rollback()
        assert db.session.execute(text("SELECT count(*) FROM shop_shifts")).scalar_one() == 2

        db.session.execute(text("DELETE FROM shop_shifts WHERE id=8703"))
        db.session.commit()
        downgrade(directory=str(MIGRATIONS), revision=OLD)
        assert db.session.execute(text("SELECT count(*) FROM shop_shifts WHERE id=8702")).scalar_one() == 1
        with pytest.raises(Exception):
            db.session.execute(text(
                "INSERT INTO shop_shifts (id, dataset_id, staff_id, business_date, status) "
                "VALUES (8704, :admin, 8701, '2026-10-16', 'all_day')"
            ), {"admin": admin})
        db.session.rollback()
