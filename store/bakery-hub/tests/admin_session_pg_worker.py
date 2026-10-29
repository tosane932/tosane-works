"""Subprocess implementation for the opt-in PostgreSQL HTTP regression test.

Reconstructed from the G580 experiment log, with real CSRF, offline grants,
an independent login, separate-connection observations, and DBAPI error proof.
No real credentials, cookie values, or exception details are printed.
"""
import logging
import os
from pathlib import Path
import re
import secrets
import sys
from html.parser import HTMLParser
from urllib.parse import urlparse
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CsrfParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "csrf_token":
            self.token = attrs.get("value")


def run():
    from sqlalchemy import event, select, text
    from werkzeug.security import generate_password_hash
    from postgresql_test_utils import get_isolated_postgresql_test_url
    from sqlalchemy.engine import make_url

    global stage
    stage = "guard"
    allowed = get_isolated_postgresql_test_url()
    assert allowed is not None and allowed == os.environ["DATABASE_URL"]
    schema = os.environ["TEST_ADMIN_SESSION_SCHEMA"]
    assert re.fullmatch(r"admin_session_test_[0-9a-f]{32}", schema)
    assert make_url(allowed).query.get("options") == "-csearch_path=" + schema
    os.environ["SECRET_KEY"] = secrets.token_urlsafe(32)
    logging.disable(logging.CRITICAL)

    import app as app_module
    from admin_sessions import ADMIN_SESSION_TOKEN_KEY, token_hash
    from models import (AdminAuthSession, Dataset, RegisterCutover,
                        RegisterOfflinePermit, RegisterProduct, db)
    from register_offline_service import prepare_offline
    from register_service import TOKYO, utc_now

    app = app_module.app
    password = secrets.token_urlsafe(32)
    app.config.update(
        TESTING=True, SESSION_COOKIE_SECURE=False, WTF_CSRF_ENABLED=True,
        ADMIN_USERNAME="isolated-phase4-admin",
        ADMIN_PASSWORD_HASH=generate_password_hash(password),
        REGISTER_V3_OFFLINE_MODE_ENABLED=True,
    )

    stage = "schema"
    with app.app_context():
        engine = db.engine
        assert engine.dialect.name == "postgresql"
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT current_database()").scalar_one() == "sales_data_app_test"
            assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
            connection.exec_driver_sql("CREATE TABLE phase4_fault_parent (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE phase4_fault_child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES phase4_fault_parent(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
        owner = db.session.execute(select(Dataset).filter_by(system_key="admin")).scalar_one()
        day = utc_now().astimezone(TOKYO).date()
        db.session.add_all([
            RegisterProduct(dataset_id=owner.id, name="架空パン", price_yen=120),
            RegisterCutover(dataset_id=owner.id, first_register_date=day),
        ])
        db.session.commit()
        grant_ids = []
        for _ in range(2):
            grant, _, _, _ = prepare_offline(owner, str(uuid.uuid4()))
            grant_ids.append(grant.id)

    def csrf(client, path):
        page = client.get(path)
        assert page.status_code == 200
        parser = CsrfParser()
        parser.feed(page.get_data(as_text=True))
        assert parser.token
        return parser.token

    def login(client):
        response = client.post("/login", data={
            "username": app.config["ADMIN_USERNAME"], "password": password,
            "csrf_token": csrf(client, "/login"),
        })
        assert response.status_code == 302
        with client.session_transaction() as saved:
            return token_hash(saved[ADMIN_SESSION_TOKEN_KEY])

    stage = "login"
    first, second = app.test_client(), app.test_client()
    first_hash, second_hash = login(first), login(second)
    cookie_name = app.config["SESSION_COOKIE_NAME"]
    captured_cookie = first.get_cookie(cookie_name).value

    def observed(connection):
        auth = dict(connection.execute(select(
            AdminAuthSession.token_hash, AdminAuthSession.revoked_at,
        )).all())
        permits = dict(connection.execute(select(
            RegisterOfflinePermit.id, RegisterOfflinePermit.revoked_at,
        )).all())
        assert set(auth) == {first_hash, second_hash}
        assert set(permits) == set(grant_ids)
        return auth, permits

    def assert_unrevoked():
        with engine.connect() as connection:
            auth, permits = observed(connection)
            assert all(value is None for value in [*auth.values(), *permits.values()])
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM phase4_fault_child").scalar_one() == 0

    stage = "csrf"
    assert first.post("/logout").status_code == 400
    assert_unrevoked()
    logout_csrf = csrf(first, "/shop-tools/memo")
    proof = {"inserted": False, "commit_attempted": False, "sqlstate": None}

    def deferred_failure(session):
        session.flush()
        auth, permits = observed(session.connection())
        assert auth[first_hash] is not None and auth[second_hash] is None
        assert all(value is not None for value in permits.values())
        # Another actual connection still sees the committed pre-logout state.
        assert_unrevoked()
        session.execute(text("INSERT INTO phase4_fault_child VALUES (1, 9999)"))
        proof["inserted"] = True

    def commit_attempted(connection):
        del connection
        proof["commit_attempted"] = True

    def commit_error(context):
        if context.statement is None:
            proof["sqlstate"] = getattr(context.original_exception, "pgcode", None)

    stage = "real_commit"
    with app.app_context():
        session_class = type(db.session())
    event.listen(session_class, "before_commit", deferred_failure)
    event.listen(engine, "commit", commit_attempted)
    event.listen(engine, "handle_error", commit_error)
    try:
        failed = first.post("/logout", data={"csrf_token": logout_csrf})
    finally:
        event.remove(session_class, "before_commit", deferred_failure)
        event.remove(engine, "commit", commit_attempted)
        event.remove(engine, "handle_error", commit_error)
    assert failed.status_code == 503
    assert proof == {"inserted": True, "commit_attempted": True, "sqlstate": "23503"}

    stage = "rollback"
    assert_unrevoked()
    assert first.get("/shop-tools/memo").status_code == 200
    assert second.get("/shop-tools/memo").status_code == 200

    stage = "retry"
    success = first.post("/logout", data={"csrf_token": csrf(first, "/shop-tools/memo")})
    assert success.status_code == 302
    with engine.connect() as connection:
        auth, permits = observed(connection)
        assert auth[first_hash] is not None and auth[second_hash] is None
        assert all(value is not None for value in permits.values())
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM phase4_fault_child").scalar_one() == 0
    assert second.get("/shop-tools/memo").status_code == 200

    stage = "cookie_replay"
    replay = app.test_client()
    replay.set_cookie(cookie_name, captured_cookie)
    for path in ("/shop-tools/memo", "/api/dashboard-data"):
        response = replay.get(path)
        assert response.status_code == 302
        assert urlparse(response.headers["Location"]).path == "/login"
    with app.app_context():
        db.session.remove()
        engine.dispose()


if __name__ == "__main__":
    stage = "initialization"
    try:
        run()
    except Exception as error:
        print(f"PHASE4_FAILURE:{stage}:{type(error).__name__}")
        raise SystemExit(2) from None
    print("PHASE4_REAL_COMMIT_ROLLBACK_PASSED")
