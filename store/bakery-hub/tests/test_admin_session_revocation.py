"""Private regression coverage; use only synthetic credentials and never log tokens."""

from urllib.parse import urlparse
import uuid

import pytest
from flask import g
from itsdangerous import TimestampSigner
from werkzeug.security import generate_password_hash
import datetime
from unittest.mock import Mock
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

import app as app_module
import admin_sessions
from admin_sessions import ADMIN_SESSION_TOKEN_KEY, token_hash
from models import AdminAuthSession, db


def _login(client, csrf_token, credentials):
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    token = csrf_token(client, "/login")
    response = client.post("/login", data={
        "username": credentials.username,
        "password": credentials.password,
        "csrf_token": token,
    })
    assert response.status_code == 302


def _logout(client, csrf_token):
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    token = csrf_token(client, "/shop-tools/memo")
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302


def _request(client, path="/shop-tools/memo"):
    g.pop("_login_user", None)
    return client.get(path)


def _assert_anonymous(response):
    assert response.status_code == 302
    assert urlparse(response.headers["Location"]).path == "/login"


@pytest.mark.parametrize("path", ["/shop-tools/memo", "/api/dashboard-data"])
def test_logout_rejects_captured_admin_cookie(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config, path,
):
    _login(client, csrf_token, admin_auth_config)
    assert _request(client, path).status_code == 200
    cookie_name = flask_app.config["SESSION_COOKIE_NAME"]
    captured = client.get_cookie(cookie_name).value
    _logout(client, csrf_token)
    _assert_anonymous(_request(client, path))
    replay = flask_app.test_client()
    replay.set_cookie(cookie_name, captured)
    _assert_anonymous(_request(replay, path))


def test_normal_login_logout_and_relogin_still_work(
    client, admin_dataset, csrf_token, admin_auth_config,
):
    _login(client, csrf_token, admin_auth_config)
    assert _request(client).status_code == 200
    _logout(client, csrf_token)
    _assert_anonymous(_request(client))
    _login(client, csrf_token, admin_auth_config)
    assert _request(client).status_code == 200


def test_logout_on_device_a_does_not_end_device_b_login(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config,
):
    other = flask_app.test_client()
    _login(client, csrf_token, admin_auth_config)
    _login(other, csrf_token, admin_auth_config)
    assert _request(other).status_code == 200
    _logout(client, csrf_token)
    assert _request(other).status_code == 200


def test_signed_cookie_expiry_is_enforced(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config, monkeypatch,
):
    issued = TimestampSigner.get_timestamp(None)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _self: issued)
    _login(client, csrf_token, admin_auth_config)
    assert _request(client).status_code == 200
    expiry = int(flask_app.permanent_session_lifetime.total_seconds())
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _self: issued + expiry + 1)
    _assert_anonymous(_request(client))


def test_password_hash_rotation_rejects_existing_admin_cookie(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config, monkeypatch,
):
    _login(client, csrf_token, admin_auth_config)
    assert _request(client).status_code == 200
    monkeypatch.setitem(flask_app.config, "ADMIN_PASSWORD_HASH",
                        generate_password_hash("synthetic-rotated-password"))
    _assert_anonymous(_request(client))


def test_existing_offline_logout_revokes_all_dataset_grants_but_not_other_login(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config,
):
    from models import db
    from register_offline_service import prepare_offline
    from tests.test_register_offline import setup_grant

    _, first, _, _, _ = setup_grant(admin_dataset)
    second, _, _, _ = prepare_offline(admin_dataset, str(uuid.uuid4()))
    other = flask_app.test_client()
    _login(client, csrf_token, admin_auth_config)
    _login(other, csrf_token, admin_auth_config)
    flask_app.config.update(REGISTER_V3_OFFLINE_MODE_ENABLED=True)
    _logout(client, csrf_token)
    db.session.refresh(first)
    db.session.refresh(second)
    assert first.revoked_at is not None and second.revoked_at is not None
    assert AdminAuthSession.query.filter(AdminAuthSession.revoked_at.is_not(None)).count() == 1
    _assert_anonymous(_request(client))
    assert _request(other).status_code == 200


def test_only_digest_is_persisted_with_twelve_hour_absolute_expiry(
    flask_app, client, csrf_token, admin_auth_config,
):
    _login(client, csrf_token, admin_auth_config)
    with client.session_transaction() as saved:
        token = saved[ADMIN_SESSION_TOKEN_KEY]
    row = AdminAuthSession.query.one()
    assert len(token) == 43 and len(row.token_hash) == 64
    assert row.token_hash == token_hash(token) and row.token_hash != token
    assert row.expires_at - row.issued_at == datetime.timedelta(hours=12)
    assert row.revoked_at is None


@pytest.mark.parametrize("offset,allowed", [(-1, True), (0, False), (1, False)])
def test_absolute_expiry_boundary(
    client, admin_dataset, csrf_token, admin_auth_config, monkeypatch, offset, allowed,
):
    issued = datetime.datetime(2026, 10, 28, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(admin_sessions, "utc_now", lambda: issued)
    _login(client, csrf_token, admin_auth_config)
    monkeypatch.setattr(admin_sessions, "utc_now", lambda: issued + datetime.timedelta(hours=12, seconds=offset))
    response = _request(client)
    if allowed:
        assert response.status_code == 200
        row = AdminAuthSession.query.one()
        assert row.expires_at - row.issued_at == datetime.timedelta(hours=12)
    else:
        _assert_anonymous(response)


@pytest.mark.parametrize("token", [None, "", "malformed", "A" * 43, ["not-a-token"]])
def test_legacy_malformed_or_unknown_admin_session_is_rejected(
    client, admin_dataset, csrf_token, admin_auth_config, token,
):
    _login(client, csrf_token, admin_auth_config)
    with client.session_transaction() as saved:
        if token is None:
            saved.pop(ADMIN_SESSION_TOKEN_KEY)
        else:
            saved[ADMIN_SESSION_TOKEN_KEY] = token
    _assert_anonymous(_request(client))


def test_relogin_rotates_current_login_without_ending_independent_login(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config,
):
    _login(client, csrf_token, admin_auth_config)
    name = flask_app.config["SESSION_COOKIE_NAME"]
    captured = client.get_cookie(name).value
    other = flask_app.test_client()
    _login(other, csrf_token, admin_auth_config)
    _login(client, csrf_token, admin_auth_config)
    assert _request(client).status_code == _request(other).status_code == 200
    replay = flask_app.test_client()
    replay.set_cookie(name, captured)
    _assert_anonymous(_request(replay))
    assert AdminAuthSession.query.filter(AdminAuthSession.revoked_at.is_not(None)).count() == 1
    assert AdminAuthSession.query.filter_by(revoked_at=None).count() == 2


def test_authentication_db_failure_is_closed_and_does_not_log_error_detail(
    client, admin_dataset, csrf_token, admin_auth_config, monkeypatch, caplog,
):
    _login(client, csrf_token, admin_auth_config)
    with monkeypatch.context() as patch:
        patch.setattr(app_module, "active_admin_session", Mock(side_effect=SQLAlchemyError("private error marker")))
        assert _request(client).status_code == 503
    assert "private error marker" not in caplog.text
    assert _request(client).status_code == 200


@pytest.mark.parametrize("operation", ["flush", "commit"])
def test_failed_login_record_does_not_authenticate(
    client, csrf_token, admin_auth_config, monkeypatch, operation,
):
    g.pop("csrf_token", None)
    token = csrf_token(client, "/login")
    with monkeypatch.context() as patch:
        patch.setattr(db.session, operation, Mock(side_effect=SQLAlchemyError("synthetic failure")))
        response = client.post("/login", data={"username": admin_auth_config.username,
            "password": admin_auth_config.password, "csrf_token": token})
    assert response.status_code == 503
    with client.session_transaction() as saved:
        assert "_user_id" not in saved and ADMIN_SESSION_TOKEN_KEY not in saved
    assert AdminAuthSession.query.count() == 0


@pytest.mark.parametrize("failure", ["session_update", "commit"])
def test_logout_failure_rolls_back_session_and_offline_grants_together(
    flask_app, client, admin_dataset, csrf_token, admin_auth_config, monkeypatch, failure,
):
    from tests.test_register_offline import setup_grant
    _, grant, _, _, _ = setup_grant(admin_dataset)
    _login(client, csrf_token, admin_auth_config)
    flask_app.config["REGISTER_V3_OFFLINE_MODE_ENABLED"] = True
    g.pop("csrf_token", None)
    token = csrf_token(client, "/shop-tools/memo")
    with monkeypatch.context() as patch:
        if failure == "session_update":
            patch.setattr(app_module, "revoke_admin_session", Mock(side_effect=SQLAlchemyError("synthetic failure")))
        else:
            patch.setattr(db.session, "commit", Mock(side_effect=SQLAlchemyError("synthetic failure")))
        response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 503
    db.session.refresh(grant)
    assert grant.revoked_at is None
    assert AdminAuthSession.query.one().revoked_at is None
    assert _request(client).status_code == 200


def test_missing_csrf_does_not_revoke_session(client, admin_dataset, csrf_token, admin_auth_config):
    _login(client, csrf_token, admin_auth_config)
    assert client.post("/logout").status_code == 400
    assert AdminAuthSession.query.one().revoked_at is None
    assert _request(client).status_code == 200


@pytest.mark.parametrize("change", ["hash", "expiry", "revocation"])
def test_database_constraints_reject_invalid_session_rows(flask_app, change):
    now = datetime.datetime.now(datetime.timezone.utc)
    data = dict(token_hash="a" * 64, issued_at=now,
                expires_at=now + datetime.timedelta(hours=12), revoked_at=None)
    if change == "hash": data["token_hash"] = "short"
    elif change == "expiry": data["expires_at"] = now
    else: data["revoked_at"] = now - datetime.timedelta(seconds=1)
    db.session.add(AdminAuthSession(**data))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert AdminAuthSession.query.count() == 0
