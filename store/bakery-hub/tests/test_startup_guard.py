"""Normal startup only reads schema state, including on repeated starts."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from startup_guard import check_schema


ROOT = Path(__file__).resolve().parents[1]


def test_container_startup_never_runs_migration_or_seed():
    command = (ROOT / "Dockerfile").read_text().split("CMD ", 1)[1]
    assert "python startup_guard.py && exec gunicorn" in command
    assert "flask db upgrade" not in command
    assert "seed_demo.py" not in command


def test_empty_database_refused_without_creating_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with pytest.raises(RuntimeError, match="not initialized"):
        check_schema(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").all() == []


def test_stale_or_falsely_stamped_database_refused(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32))")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('stale')")
    with pytest.raises(RuntimeError, match="revision"):
        check_schema(engine)


def test_migrated_database_repeated_checks_do_not_modify_business_rows(tmp_path):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    engine = create_engine(f"sqlite:///{tmp_path / 'ready.db'}")
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32))")
        connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": head})
        for table in ("datasets", "products", "daily_sales"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id integer primary key, marker text)")
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES (1, 'original')")
    for _ in range(3):
        check_schema(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head
        for table in ("datasets", "products", "daily_sales"):
            assert connection.exec_driver_sql(f"SELECT * FROM {table}").all() == [(1, "original")]


def test_main_fails_closed_without_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'missing.db'}")
    # The production entrypoint invokes a fresh process. Its failure is verified
    # here without displaying any driver error or connection string.
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "startup_guard.py"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Startup blocked" in result.stderr
