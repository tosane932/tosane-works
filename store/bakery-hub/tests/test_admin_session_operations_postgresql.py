"""Phase 5: independent real workers and real dump/restore on disposable PG."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import uuid

from bs4 import BeautifulSoup
from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
import pytest
import requests
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from admin_sessions import active_admin_session, issue_admin_session, revoke_admin_session
from models import AdminAuthSession, Dataset, RegisterCutover, RegisterProduct, db, utc_now
from register_offline_service import prepare_offline
from register_service import TOKYO
from tests.test_admin_session_postgresql import isolated_admin_http_database
from tests.test_admin_session_migration import seed_business_rows, snapshot
from postgresql_test_utils import get_isolated_postgresql_test_url

URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL URL is not configured")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = str(ROOT / "migrations")


@contextmanager
def bound_app(url):
    app = Flask("phase5-synthetic")
    app.config.update(SQLALCHEMY_DATABASE_URI=url, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={"hide_parameters": True},
                      ADMIN_SESSION_MAX_AGE_SECONDS=43200)
    db.init_app(app)
    Migrate(app, db, directory=MIGRATIONS)
    with app.app_context():
        try:
            yield app
        finally:
            db.session.remove()
            db.engine.dispose()


def worker_environment(url):
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}
    env.update(DATABASE_URL=url.render_as_string(hide_password=False),
               TEST_POSTGRESQL_DATABASE_URL=url.render_as_string(hide_password=False),
               ALLOW_POSTGRESQL_TEST_DATABASE_RESET="1", FLASK_SKIP_DOTENV="1",
               SECRET_KEY=secrets.token_urlsafe(32), ADMIN_USERNAME="phase5-admin")
    return env


class Workers:
    def __init__(self, url, directory):
        self.env = worker_environment(url)
        self.password = secrets.token_urlsafe(32)
        self.env["ADMIN_PASSWORD_HASH"] = generate_password_hash(self.password)
        self.log = directory / "synthetic-gunicorn.log"
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.base = f"http://127.0.0.1:{self.listener.getsockname()[1]}"

    def __enter__(self):
        self.process = subprocess.Popen([
            sys.executable, "-m", "gunicorn", "--workers", "2", "--bind",
            f"fd://{self.listener.fileno()}", "--pythonpath", "tests",
            "--timeout", "20", "--graceful-timeout", "5", "--access-logfile", "/dev/null",
            "--error-logfile", str(self.log), "phase5_wsgi:app",
        ], cwd=ROOT, env=self.env, pass_fds=[self.listener.fileno()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.listener.close()
        try:
            self.wait_ready()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def wait_ready(self):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            assert self.process.poll() is None, "synthetic Gunicorn stopped"
            try:
                if requests.get(self.base + "/__phase5/health", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.05)
        raise AssertionError("synthetic Gunicorn readiness timed out")

    def probes(self, cookies, status, previous=None):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            def one(_):
                response = requests.get(self.base + "/shop-tools/memo", cookies=cookies,
                                        allow_redirects=False, timeout=5)
                assert response.status_code == status
                return int(response.headers["X-Phase5-Pid"])
            with ThreadPoolExecutor(max_workers=4) as executor:
                pids = set(executor.map(one, range(8)))
            if len(pids) == 2 and (previous is None or pids.isdisjoint(previous)):
                return pids
            time.sleep(0.05)
        raise AssertionError("both independent Gunicorn workers were not observed")

    def csrf(self, client, path):
        response = client.get(self.base + path)
        assert response.status_code == 200
        return BeautifulSoup(response.text, "html.parser").select_one('input[name="csrf_token"]')["value"]

    def login(self, client):
        response = client.post(self.base + "/login", data={
            "username": self.env["ADMIN_USERNAME"], "password": self.password,
            "csrf_token": self.csrf(client, "/login"),
        }, allow_redirects=False)
        assert response.status_code == 302

    def __exit__(self, *_):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def test_two_workers_restart_expiry_and_database_failure(isolated_admin_http_database, tmp_path):
    url, _ = isolated_admin_http_database
    with bound_app(url):
        owner = Dataset.query.filter_by(system_key="admin").one()
        day = utc_now().astimezone(TOKYO).date()
        db.session.add_all([RegisterProduct(dataset_id=owner.id, name="合成パン", price_yen=120),
                            RegisterCutover(dataset_id=owner.id, first_register_date=day)])
        db.session.commit()
        for _ in range(2):
            prepare_offline(owner, str(uuid.uuid4()))
    engine = create_engine(url, hide_parameters=True)
    try:
        with Workers(url, tmp_path) as workers, requests.Session() as a, requests.Session() as b:
            workers.login(a)
            workers.login(b)
            old_a, old_b = a.cookies.get_dict(), b.cookies.get_dict()
            pids = workers.probes(old_a, 200)
            assert workers.probes(old_b, 200) == pids
            response = a.post(workers.base + "/logout", data={
                "csrf_token": workers.csrf(a, "/shop-tools/memo")}, allow_redirects=False)
            assert response.status_code == 302
            assert int(response.headers["X-Phase5-Pid"]) in pids
            assert workers.probes(old_a, 302) == pids
            assert workers.probes(old_b, 200) == pids
            with engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM register_offline_permits WHERE revoked_at IS NULL")).scalar_one() == 0
            workers.process.send_signal(signal.SIGHUP)
            new_pids = workers.probes(old_b, 200, previous=pids)
            assert workers.probes(old_a, 302) == new_pids
            # Change synthetic timestamps; do not sleep twelve hours or touch real records.
            now = utc_now()
            with engine.begin() as connection:
                connection.execute(text("UPDATE admin_auth_sessions SET issued_at=:issued, expires_at=:expiry WHERE revoked_at IS NULL"),
                                   {"issued": now - datetime.timedelta(hours=13),
                                    "expiry": now - datetime.timedelta(hours=1)})
            assert workers.probes(old_b, 302) == new_pids
            workers.login(b)
            current = b.cookies.get_dict()
            assert workers.probes(current, 200) == new_pids
            assert workers.probes(old_b, 302) == new_pids
            # Real PG lookup failure, not a Python Mock; business/auth rows stay intact.
            with engine.begin() as connection:
                connection.exec_driver_sql("ALTER TABLE admin_auth_sessions RENAME TO phase5_auth_unavailable")
            try:
                assert workers.probes(current, 503) == new_pids
            finally:
                with engine.begin() as connection:
                    connection.exec_driver_sql("ALTER TABLE phase5_auth_unavailable RENAME TO admin_auth_sessions")
            assert workers.probes(current, 200) == new_pids
    finally:
        engine.dispose()


def test_real_startup_process_refuses_old_schema_and_preserves_rows(isolated_admin_http_database):
    url, _ = isolated_admin_http_database
    with bound_app(url):
        downgrade(directory=MIGRATIONS, revision="c8d3e7f1a204")
        seed_business_rows()
        before = snapshot()
        def guard():
            return subprocess.run([sys.executable, "startup_guard.py"], cwd=ROOT,
                                  env=worker_environment(url), capture_output=True, timeout=30)
        assert guard().returncode == 1
        assert snapshot() == before
        upgrade(directory=MIGRATIONS, revision="head")
        assert guard().returncode == 0
        assert guard().returncode == 0
        assert snapshot() == before


@pytest.mark.parametrize("invalid", ["hash", "expiry", "revocation"])
def test_pg_session_constraints_index_and_timezone(isolated_admin_http_database, invalid):
    url, _ = isolated_admin_http_database
    with bound_app(url):
        now = utc_now()
        data = dict(token_hash="a" * 64, issued_at=now,
                    expires_at=now + datetime.timedelta(hours=12), revoked_at=None)
        if invalid == "hash": data["token_hash"] = "short"
        elif invalid == "expiry": data["expires_at"] = now
        else: data["revoked_at"] = now - datetime.timedelta(seconds=1)
        db.session.add(AdminAuthSession(**data))
        with pytest.raises(IntegrityError) as error:
            db.session.commit()
        assert error.value.orig.pgcode == "23514"
        db.session.rollback()
        inspector = inspect(db.engine)
        assert inspector.get_pk_constraint("admin_auth_sessions")["constrained_columns"] == ["token_hash"]
        assert any(i["name"] == "ix_admin_auth_sessions_expiry" and i["column_names"] == ["expires_at"]
                   for i in inspector.get_indexes("admin_auth_sessions"))
        assert all(c["type"].timezone for c in inspector.get_columns("admin_auth_sessions")
                   if c["name"].endswith("_at"))
        token = issue_admin_session()
        db.session.commit()
        db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
        row = db.session.execute(select(AdminAuthSession)).scalar_one()
        assert row.expires_at.utcoffset() == datetime.timedelta(hours=9)
        assert row.expires_at - row.issued_at == datetime.timedelta(hours=12)
        assert active_admin_session(token)


def test_dump_restore_retains_revocation_and_exposes_historical_snapshot_risk(isolated_admin_http_database, tmp_path):
    container = os.environ.get("PHASE5_TEST_CONTAINER_ID", "")
    if not container:
        pytest.skip("dedicated CI PostgreSQL container clients are required")
    assert re.fullmatch(r"[0-9a-f]{12,64}", container)
    url, schema = isolated_admin_http_database
    with bound_app(url):
        seed_business_rows()
        revoked, later_revoked = issue_admin_session(), issue_admin_session()
        revoke_admin_session(revoked)
        db.session.commit()
        business = snapshot()
        auth = db.session.execute(text("SELECT * FROM admin_auth_sessions ORDER BY token_hash")).all()
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT="5432", PGUSER=url.username,
               PGPASSWORD=url.password or "")
    def client(tool, arguments, data=None):
        result = subprocess.run(["docker", "exec", "-i", "--env", "PGHOST", "--env", "PGPORT",
                                 "--env", "PGUSER", "--env", "PGPASSWORD", container, tool, *arguments],
                                env=env, input=data, capture_output=True, timeout=60)
        assert result.returncode == 0, "synthetic PostgreSQL client failed"
        return result.stdout
    control = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    target = "sales_data_app_test_restore_" + uuid.uuid4().hex
    assert re.fullmatch(r"sales_data_app_test_restore_[0-9a-f]{32}", target)
    created = False
    try:
        with control.connect() as connection:
            identity = str(connection.execute(text("SELECT system_identifier FROM pg_control_system()")).scalar_one())
        assert client("psql", ["-XAt", "--dbname", "sales_data_app_test", "-c",
                               "SELECT system_identifier FROM pg_control_system()"]).decode().strip() == identity
        archive = client("pg_dump", ["--format=custom", "--no-owner", "--no-acl", "--schema", schema,
                                     "--dbname", "sales_data_app_test"])
        assert archive.startswith(b"PGDMP")
        # Revoke after the snapshot: restoring it must not be mistaken for current auth state.
        with bound_app(url):
            revoke_admin_session(later_revoked)
            db.session.commit()
            assert not active_admin_session(later_revoked)
        with control.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{target}"')
        created = True
        client("pg_restore", ["--no-owner", "--no-acl", "--exit-on-error", "--dbname", target], archive)
        with bound_app(url.set(database=target)):
            assert snapshot() == business
            assert db.session.execute(text("SELECT * FROM admin_auth_sessions ORDER BY token_hash")).all() == auth
            assert not active_admin_session(revoked)
            assert active_admin_session(later_revoked)  # Historical state really reappears.
            db.session.execute(update(AdminAuthSession).where(AdminAuthSession.revoked_at.is_(None)).values(
                revoked_at=db.func.greatest(AdminAuthSession.issued_at, utc_now())))
            db.session.commit()
            assert not active_admin_session(revoked) and not active_admin_session(later_revoked)
            assert snapshot() == business
    finally:
        if created:
            with control.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{target}" WITH (FORCE)')
        control.dispose()
