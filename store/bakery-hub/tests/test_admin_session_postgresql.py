"""Real COMMIT rejection through HTTP; only an opt-in disposable loopback DB."""
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine

from models import db
from postgresql_test_utils import get_isolated_postgresql_test_url


TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="isolated PostgreSQL URL is not configured",
)
APP_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = APP_ROOT / "migrations"


@pytest.fixture()
def isolated_admin_http_database():
    """Create/drop only this test's generated schema; never reset public."""
    schema = "admin_session_test_" + uuid.uuid4().hex
    control = create_engine(TEST_URL, hide_parameters=True)
    try:
        with control.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        url = control.url.update_query_dict({"options": "-csearch_path=" + schema})
        app = Flask("isolated_admin_http_migration")
        app.config.update(
            SQLALCHEMY_DATABASE_URI=url,
            SQLALCHEMY_ENGINE_OPTIONS={"hide_parameters": True},
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(app)
        Migrate(app, db, directory=str(MIGRATIONS))
        with app.app_context():
            try:
                upgrade(directory=str(MIGRATIONS), revision="head")
            finally:
                db.session.remove()
                db.engine.dispose()
        yield url, schema
    finally:
        with control.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        control.dispose()


def test_http_logout_real_commit_failure_is_atomic(isolated_admin_http_database):
    """Deferred FK fails in DBAPI COMMIT, after both revocations were flushed."""
    url, schema = isolated_admin_http_database
    # The application is imported in a separate process, so conftest's SQLite
    # singleton cannot silently substitute for the intended PostgreSQL engine.
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT",
    }}
    env.update(
        DATABASE_URL=url.render_as_string(hide_password=False),
        TEST_POSTGRESQL_DATABASE_URL=url.render_as_string(hide_password=False),
        ALLOW_POSTGRESQL_TEST_DATABASE_RESET="1",
        TEST_ADMIN_SESSION_SCHEMA=schema,
        FLASK_SKIP_DOTENV="1",
    )
    result = subprocess.run(
        [sys.executable, str(APP_ROOT / "tests" / "admin_session_pg_worker.py")],
        cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=90,
        check=False,
    )
    # No raw stderr, DSN, cookies, or tokens in failure messages.
    stages = re.findall(r"PHASE4_FAILURE:[a-z_]+:[A-Za-z_]+", result.stdout)
    assert result.returncode == 0, stages or ["isolated HTTP worker failed"]
    assert result.stdout.strip() == "PHASE4_REAL_COMMIT_ROLLBACK_PASSED"
