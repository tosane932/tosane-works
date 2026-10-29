import datetime
import uuid
from unittest.mock import Mock

import pytest
from flask import g
from flask_login import current_user, login_user
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import Conflict, InternalServerError, ServiceUnavailable

import app as app_module
from models import Dataset, db


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _create_guest_dataset():
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    db.session.add(dataset)
    db.session.commit()
    return dataset


def _login_valid_admin(flask_app):
    from conftest import establish_test_admin_session
    establish_test_admin_session(app_module.session)
    app_module.session[
        app_module.ADMIN_AUTH_FINGERPRINT_SESSION_KEY
    ] = app_module._get_admin_auth_fingerprint(
        flask_app.config["ADMIN_PASSWORD_HASH"]
    )
    admin_user = app_module.load_user("admin")
    assert admin_user is not None
    login_user(admin_user)
    return admin_user


def test_start_guest_session_creates_server_owned_guest_dataset(flask_app):
    before = datetime.datetime.now(datetime.timezone.utc)

    with flask_app.test_request_context("/"):
        guest_dataset = app_module.start_guest_session()

    after = datetime.datetime.now(datetime.timezone.utc)
    created_at = _as_utc(guest_dataset.created_at)
    last_activity_at = _as_utc(guest_dataset.last_activity_at)
    absolute_expires_at = _as_utc(guest_dataset.absolute_expires_at)

    assert Dataset.query.filter_by(kind="guest").count() == 1
    assert isinstance(guest_dataset.id, uuid.UUID)
    assert guest_dataset.kind == "guest"
    assert guest_dataset.system_key is None
    assert before <= created_at <= after
    assert last_activity_at == created_at
    assert absolute_expires_at > created_at
    assert datetime.timedelta(hours=2) - datetime.timedelta(seconds=5) <= (
        absolute_expires_at - created_at
    ) <= datetime.timedelta(hours=2) + datetime.timedelta(seconds=5)


def test_start_guest_session_issues_authenticated_guest_identity(flask_app):
    with flask_app.test_request_context("/"):
        guest_dataset = app_module.start_guest_session()

        assert current_user.is_authenticated is True
        assert current_user.is_guest is True
        assert current_user.is_admin is False
        assert current_user.get_id() == f"guest:{guest_dataset.id}"


def test_started_guest_resolves_the_created_dataset(flask_app):
    with flask_app.test_request_context("/"):
        guest_dataset = app_module.start_guest_session()

        resolved_dataset = app_module.require_current_dataset()

    assert resolved_dataset.id == guest_dataset.id


def test_rate_limit_and_guest_dataset_commits_happen_before_login(
    flask_app,
    monkeypatch,
):
    events = []
    real_commit = db.session.commit
    real_login_user = app_module.login_user

    def recording_commit():
        events.append("commit")
        return real_commit()

    def recording_login_user(user):
        events.append("login_user")
        return real_login_user(user)

    monkeypatch.setattr(db.session, "commit", recording_commit)
    monkeypatch.setattr(app_module, "login_user", recording_login_user)

    with flask_app.test_request_context("/"):
        app_module.start_guest_session()

    assert events == ["commit", "commit", "login_user"]


def test_database_failure_rolls_back_without_guest_login(
    flask_app,
    monkeypatch,
):
    real_rollback = db.session.rollback
    rollback = Mock(wraps=real_rollback)
    login = Mock()
    log_exception = Mock()
    monkeypatch.setattr(
        db.session,
        "commit",
        Mock(side_effect=SQLAlchemyError("test commit failure")),
    )
    monkeypatch.setattr(db.session, "rollback", rollback)
    monkeypatch.setattr(app_module, "login_user", login)
    monkeypatch.setattr(app_module.logger, "exception", log_exception)

    with flask_app.test_request_context("/"):
        with pytest.raises(ServiceUnavailable):
            app_module.start_guest_session()

        assert current_user.is_authenticated is False
        assert "_user_id" not in app_module.session

    assert Dataset.query.filter_by(kind="guest").count() == 0
    rollback.assert_called_once_with()
    login.assert_not_called()
    log_exception.assert_called_once()


def test_authenticated_admin_cannot_start_guest_session(
    flask_app,
    admin_dataset,
):
    admin_fingerprint = app_module._get_admin_auth_fingerprint(
        flask_app.config["ADMIN_PASSWORD_HASH"]
    )
    guest_count_before = Dataset.query.filter_by(kind="guest").count()

    with flask_app.test_request_context("/"):
        _login_valid_admin(flask_app)

        with pytest.raises(Conflict):
            app_module.start_guest_session()

        assert current_user.get_id() == "admin"
        assert current_user.is_admin is True
        assert (
            app_module.session[
                app_module.ADMIN_AUTH_FINGERPRINT_SESSION_KEY
            ]
            == admin_fingerprint
        )

    assert Dataset.query.filter_by(kind="guest").count() == guest_count_before
    assert Dataset.query.filter_by(id=admin_dataset.id).one() is not None


def test_authenticated_guest_cannot_replace_its_identity(flask_app):
    guest_dataset = _create_guest_dataset()
    guest_count_before = Dataset.query.filter_by(kind="guest").count()

    with flask_app.test_request_context("/"):
        guest_user = app_module.load_user(f"guest:{guest_dataset.id}")
        assert guest_user is not None
        login_user(guest_user)

        with pytest.raises(Conflict):
            app_module.start_guest_session()

        assert current_user.get_id() == f"guest:{guest_dataset.id}"

    assert Dataset.query.filter_by(kind="guest").count() == guest_count_before


def test_untrusted_session_values_do_not_control_guest_identity(flask_app):
    requested_dataset_id = uuid.uuid4()

    with flask_app.test_request_context("/"):
        app_module.session["role"] = "guest"
        app_module.session["dataset_id"] = str(requested_dataset_id)
        app_module.session["is_admin"] = False

        guest_dataset = app_module.start_guest_session()

        assert guest_dataset.id != requested_dataset_id
        assert current_user.get_id() == f"guest:{guest_dataset.id}"


def test_guest_creation_does_not_modify_admin_dataset(
    flask_app,
    admin_dataset,
):
    admin_snapshot = (
        admin_dataset.id,
        admin_dataset.kind,
        admin_dataset.system_key,
        admin_dataset.created_at,
        admin_dataset.last_activity_at,
        admin_dataset.absolute_expires_at,
    )

    with flask_app.test_request_context("/"):
        guest_dataset = app_module.start_guest_session()

    db.session.refresh(admin_dataset)
    assert (
        admin_dataset.id,
        admin_dataset.kind,
        admin_dataset.system_key,
        admin_dataset.created_at,
        admin_dataset.last_activity_at,
        admin_dataset.absolute_expires_at,
    ) == admin_snapshot
    assert guest_dataset.id != admin_dataset.id
    assert guest_dataset.kind == "guest"


def test_independent_anonymous_sessions_get_different_datasets(flask_app):
    with flask_app.test_request_context("/"):
        guest_a_dataset = app_module.start_guest_session()
        guest_a_id = current_user.get_id()

    g.pop("_login_user", None)
    with flask_app.test_request_context("/"):
        guest_b_dataset = app_module.start_guest_session()
        guest_b_id = current_user.get_id()

    assert guest_a_dataset.id != guest_b_dataset.id
    assert guest_a_id == f"guest:{guest_a_dataset.id}"
    assert guest_b_id == f"guest:{guest_b_dataset.id}"
    assert guest_a_id != guest_b_id


def test_start_guest_session_accepts_no_identity_arguments():
    with pytest.raises(TypeError):
        app_module.start_guest_session(dataset_id=uuid.uuid4())


def test_login_user_false_fails_without_issuing_identity(
    flask_app,
    monkeypatch,
):
    login = Mock(return_value=False)
    log_error = Mock()
    monkeypatch.setattr(app_module, "login_user", login)
    monkeypatch.setattr(app_module.logger, "error", log_error)

    with flask_app.test_request_context("/"):
        with pytest.raises(InternalServerError):
            app_module.start_guest_session()

        assert current_user.is_authenticated is False
        assert "_user_id" not in app_module.session

    assert Dataset.query.filter_by(kind="guest").count() == 1
    login.assert_called_once()
    log_error.assert_called_once()
