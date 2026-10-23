"""Refuse normal web startup when the database is not at this code's schema head.

This check does not initialize or migrate a database. Maintenance commands are
deliberately separate from the normal container entrypoint.
"""

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from models import db


def check_schema(engine, migrations_dir=None):
    migrations_dir = Path(migrations_dir or Path(__file__).resolve().parent / "migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations_dir))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError("Expected exactly one application migration head")

    with engine.connect() as connection:
        # A transaction read-only guard on PostgreSQL; the SQLite equivalent is
        # covered by absence of any DDL/DML here. Never run a repair operation.
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        if not inspect(connection).has_table("alembic_version"):
            raise RuntimeError("Database schema is not initialized")
        revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if revisions != heads:
            raise RuntimeError("Database revision does not match application migration head")
        # A manually stamped partial DB is still unsafe: inspect every model's
        # mapped table and column. This is a structural gate, not an exhaustive
        # comparison of every constraint or its data.
        schema = inspect(connection)
        actual = set(schema.get_table_names())
        expected = db.metadata.tables
        if not set(expected).issubset(actual):
            raise RuntimeError("Required application tables are missing")
        for name, table in expected.items():
            columns = {column["name"] for column in schema.get_columns(name)}
            if not set(table.columns.keys()).issubset(columns):
                raise RuntimeError("Required application columns are missing")


def main():
    try:
        from app import app
        with app.app_context():
            check_schema(db.engine)
    except Exception:
        # Driver exceptions can contain connection strings; never log the detail.
        logging.error("Startup blocked: database schema check failed; inspect revision in maintenance mode")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
