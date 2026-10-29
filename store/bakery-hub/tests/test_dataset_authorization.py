import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask_login import UserMixin, login_user
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import Forbidden, InternalServerError, ServiceUnavailable

import app as app_module
from models import Dataset, Product, db

class _AuthenticatedUnknownPrincipal(UserMixin):
    id = "test-authenticated-unknown"
    is_admin = False
    is_guest = False


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


def _login_restored_user(user_id):
    restored_user = app_module.load_user(user_id)
    assert restored_user is not None
    login_user(restored_user)
    return restored_user


def _login_valid_admin(flask_app):
    from conftest import establish_test_admin_session
    establish_test_admin_session(app_module.session)
    app_module.session[
        app_module.ADMIN_AUTH_FINGERPRINT_SESSION_KEY
    ] = app_module._get_admin_auth_fingerprint(
        flask_app.config["ADMIN_PASSWORD_HASH"]
    )
    return _login_restored_user("admin")


def test_admin_resolves_only_admin_dataset(flask_app, admin_dataset):
    with flask_app.test_request_context("/"):
        _login_valid_admin(flask_app)

        resolved_dataset = app_module.require_current_dataset()

    assert resolved_dataset.id == admin_dataset.id
    assert resolved_dataset.kind == "admin"
    assert resolved_dataset.system_key == "admin"


def test_guest_a_resolves_guest_a_dataset(flask_app):
    guest_a_dataset = _create_guest_dataset()

    with flask_app.test_request_context("/"):
        _login_restored_user(f"guest:{guest_a_dataset.id}")

        resolved_dataset = app_module.require_current_dataset()

    assert resolved_dataset.id == guest_a_dataset.id


def test_guest_b_resolves_guest_b_dataset(flask_app):
    guest_b_dataset = _create_guest_dataset()

    with flask_app.test_request_context("/"):
        _login_restored_user(f"guest:{guest_b_dataset.id}")

        resolved_dataset = app_module.require_current_dataset()

    assert resolved_dataset.id == guest_b_dataset.id


def test_expired_guest_dataset_is_forbidden(flask_app):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now - datetime.timedelta(hours=3),
        last_activity_at=now - datetime.timedelta(hours=3),
        absolute_expires_at=now - datetime.timedelta(hours=1),
    )
    db.session.add(guest_dataset)
    db.session.commit()

    with flask_app.test_request_context("/"):
        login_user(app_module.GuestUser(guest_dataset.id))

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_inactive_guest_dataset_is_forbidden(flask_app):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now - datetime.timedelta(hours=1),
        last_activity_at=now - datetime.timedelta(minutes=31),
        absolute_expires_at=now + datetime.timedelta(hours=1),
    )
    db.session.add(guest_dataset)
    db.session.commit()

    with flask_app.test_request_context("/"):
        login_user(app_module.GuestUser(guest_dataset.id))

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_guest_a_and_guest_b_resolve_different_datasets(flask_app):
    guest_a_dataset = _create_guest_dataset()
    guest_b_dataset = _create_guest_dataset()

    with flask_app.test_request_context("/"):
        _login_restored_user(f"guest:{guest_a_dataset.id}")
        resolved_guest_a = app_module.require_current_dataset()

    with flask_app.test_request_context("/"):
        _login_restored_user(f"guest:{guest_b_dataset.id}")
        resolved_guest_b = app_module.require_current_dataset()

    assert resolved_guest_a.id == guest_a_dataset.id
    assert resolved_guest_b.id == guest_b_dataset.id
    assert resolved_guest_a.id != resolved_guest_b.id


def test_guest_a_and_guest_b_only_see_their_own_products_via_route(
    flask_app,
):
    today = app_module.business_today()

    guest_a_dataset = _create_guest_dataset()
    guest_b_dataset = _create_guest_dataset()

    guest_a_product = Product(
        dataset=guest_a_dataset,
        year=today.year,
        month=today.month,
        name="GUEST_A_ONLY_PRODUCT",
        price=111,
        is_active=True,
    )
    guest_b_product = Product(
        dataset=guest_b_dataset,
        year=today.year,
        month=today.month,
        name="GUEST_B_ONLY_PRODUCT",
        price=222,
        is_active=True,
    )

    db.session.add_all([
        guest_a_product,
        guest_b_product,
    ])
    db.session.commit()

    guest_a_client = flask_app.test_client()
    with guest_a_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_a_dataset.id}"
        session_data["_fresh"] = True

    guest_b_client = flask_app.test_client()
    with guest_b_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_b_dataset.id}"
        session_data["_fresh"] = True

    with flask_app.app_context():
        guest_a_response = guest_a_client.get("/")

    with flask_app.app_context():
        guest_b_response = guest_b_client.get("/")

    guest_a_html = guest_a_response.get_data(as_text=True)
    guest_b_html = guest_b_response.get_data(as_text=True)

    assert guest_a_response.status_code == 200
    assert guest_b_response.status_code == 200

    assert "GUEST_A_ONLY_PRODUCT" in guest_a_html
    assert "GUEST_B_ONLY_PRODUCT" not in guest_a_html

    assert "GUEST_A_ONLY_PRODUCT" not in guest_b_html
    assert "GUEST_B_ONLY_PRODUCT" in guest_b_html


def test_guest_external_dataset_ids_cannot_change_resolved_dataset(
    flask_app,
    admin_dataset,
):
    guest_a_dataset = _create_guest_dataset()
    guest_b_dataset = _create_guest_dataset()
    path = f"/?dataset_id={guest_b_dataset.id}"

    with flask_app.test_request_context(
        path,
        method="POST",
        data={"dataset_id": str(admin_dataset.id)},
    ):
        app_module.session["dataset_id"] = str(guest_b_dataset.id)
        _login_restored_user(f"guest:{guest_a_dataset.id}")

        resolved_dataset = app_module.require_current_dataset()

    assert resolved_dataset.id == guest_a_dataset.id


def test_admin_external_guest_dataset_id_cannot_change_resolved_dataset(
    flask_app,
    admin_dataset,
):
    guest_dataset = _create_guest_dataset()
    path = f"/?dataset_id={guest_dataset.id}"

    with flask_app.test_request_context(
        path,
        method="POST",
        data={"dataset_id": str(guest_dataset.id)},
    ):
        app_module.session["dataset_id"] = str(guest_dataset.id)
        _login_valid_admin(flask_app)

        resolved_dataset = app_module.require_current_dataset()

    assert resolved_dataset.id == admin_dataset.id


def test_require_current_dataset_accepts_no_dataset_id_argument():
    with pytest.raises(TypeError):
        app_module.require_current_dataset(uuid.uuid4())


def test_anonymous_user_is_forbidden(flask_app):
    with flask_app.test_request_context("/"):
        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_authenticated_unknown_principal_is_forbidden(flask_app):
    with flask_app.test_request_context("/"):
        login_user(_AuthenticatedUnknownPrincipal())

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_nonexistent_guest_dataset_is_forbidden_without_admin_fallback(
    flask_app,
    admin_dataset,
):
    nonexistent_dataset_id = uuid.uuid4()

    with flask_app.test_request_context("/"):
        login_user(app_module.GuestUser(nonexistent_dataset_id))

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()

    assert admin_dataset.id != nonexistent_dataset_id


def test_deleted_guest_dataset_is_forbidden(flask_app):
    guest_dataset = _create_guest_dataset()

    with flask_app.test_request_context("/"):
        _login_restored_user(f"guest:{guest_dataset.id}")
        db.session.delete(guest_dataset)
        db.session.commit()

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_guest_identity_with_non_guest_kind_is_forbidden(
    flask_app,
    admin_dataset,
):
    with flask_app.test_request_context("/"):
        login_user(app_module.GuestUser(admin_dataset.id))

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_guest_dataset_with_invalid_system_key_is_forbidden(
    flask_app,
    monkeypatch,
):
    dataset_id = uuid.uuid4()
    invalid_dataset = SimpleNamespace(
        id=dataset_id,
        kind="guest",
        system_key="tampered",
    )

    class InvalidSystemKeyQuery:
        def filter_by(self, **kwargs):
            self.filters = kwargs
            return self

        def one_or_none(self):
            if self.filters.get("system_key", object()) is None:
                return None
            return invalid_dataset

    monkeypatch.setattr(Dataset, "query", InvalidSystemKeyQuery())

    with flask_app.test_request_context("/"):
        login_user(app_module.GuestUser(dataset_id))

        with pytest.raises(Forbidden):
            app_module.require_current_dataset()


def test_missing_admin_dataset_is_internal_server_error(flask_app):
    with flask_app.test_request_context("/"):
        _login_valid_admin(flask_app)

        with pytest.raises(InternalServerError):
            app_module.require_current_dataset()


def test_dataset_query_error_is_service_unavailable(
    flask_app,
    monkeypatch,
):
    class FailingQuery:
        def filter_by(self, **kwargs):
            raise SQLAlchemyError("test database error")

    rollback = Mock()
    monkeypatch.setattr(Dataset, "query", FailingQuery())
    monkeypatch.setattr(db.session, "rollback", rollback)

    with flask_app.test_request_context("/"):
        login_user(app_module.GuestUser(uuid.uuid4()))

        with pytest.raises(ServiceUnavailable):
            app_module.require_current_dataset()

    rollback.assert_called_once_with()


@pytest.mark.parametrize(
    ("principal", "expected_filters"),
    [
        (
            app_module.AdminUser(),
            {"kind": "admin", "system_key": "admin"},
        ),
        (
            app_module.GuestUser(uuid.UUID("550e8400-e29b-41d4-a716-446655440000")),
            {
                "id": uuid.UUID("550e8400-e29b-41d4-a716-446655440000"),
                "kind": "guest",
                "system_key": None,
            },
        ),
    ],
)
def test_dataset_query_uses_principal_scoped_filters(
    flask_app,
    monkeypatch,
    principal,
    expected_filters,
):
    received_filters = {}
    now = datetime.datetime.now(datetime.timezone.utc)

    class RecordingQuery:
        def filter_by(self, **kwargs):
            received_filters.update(kwargs)
            return self

        def one_or_none(self):
            return SimpleNamespace(
                id=expected_filters.get("id", uuid.uuid4()),
                last_activity_at=now,
                absolute_expires_at=now + datetime.timedelta(hours=2),
            )

    monkeypatch.setattr(Dataset, "query", RecordingQuery())

    with flask_app.test_request_context("/"):
        login_user(principal)

        app_module.require_current_dataset()

    assert received_filters == expected_filters
