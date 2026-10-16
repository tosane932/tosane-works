import os
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from flask import g
from werkzeug.security import generate_password_hash


TEST_DATABASE_URI = "sqlite:///:memory:"
TEST_SECRET_KEY = "test-secret-key"
TEST_ADMIN_USERNAME = "test-admin"
TEST_ADMIN_PASSWORD = "test-password"
TEST_ADMIN_PASSWORD_HASH = generate_password_hash(TEST_ADMIN_PASSWORD)
os.environ["DATABASE_URL"] = TEST_DATABASE_URI

import app as app_module
from models import Dataset, db


class _CSRFTokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "csrf_token":
            self.token = attributes.get("value")


def post_ai(test_client, path, **kwargs):
    """AI POSTにも通常画面で発行された本物のCSRF tokenを送る。"""
    # fixtureの長寿命app contextで別clientのtokenを再利用しない。
    g.pop("csrf_token", None)
    response = test_client.get("/login")
    assert response.status_code == 200
    parser = _CSRFTokenParser()
    parser.feed(response.get_data(as_text=True))
    assert parser.token
    headers = dict(kwargs.pop("headers", {}))
    headers["X-CSRFToken"] = parser.token
    return test_client.post(path, headers=headers, **kwargs)


@pytest.fixture()
def flask_app():
    app_module.app.config.update(
        TESTING=True,
        SESSION_COOKIE_SECURE=False,
        SECRET_KEY=TEST_SECRET_KEY,
        ADMIN_USERNAME=TEST_ADMIN_USERNAME,
        ADMIN_PASSWORD_HASH=TEST_ADMIN_PASSWORD_HASH,
        ADMIN_LOGIN_RATE_LIMIT_MAX_FAILURES=5,
        ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS=15 * 60,
        GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS=100,
        GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS=60,
        GUEST_CREATION_RATE_LIMIT_TEST_CLIENT_IP="127.0.0.1",
        GUEST_ACTIVE_DATASET_LIMIT=10,
        REGISTER_V3_PREVIEW_ENABLED=False,
        REGISTER_V3_SAVE_ENABLED=False,
        REGISTER_V3_AGGREGATION_ENABLED=False,
        REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=False,
        REGISTER_V3_PRODUCTS_WRITE_ENABLED=False,
    )

    assert app_module.app.config["SQLALCHEMY_DATABASE_URI"] == TEST_DATABASE_URI

    with app_module.app.app_context():
        assert db.engine.url.drivername == "sqlite"
        assert db.engine.url.database == ":memory:"

        db.create_all()
        yield app_module.app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture()
def admin_dataset(flask_app):
    dataset = Dataset(
        kind="admin",
        system_key="admin",
        absolute_expires_at=None,
    )
    db.session.add(dataset)
    db.session.commit()
    return dataset


@pytest.fixture()
def admin_auth_config(flask_app):
    return SimpleNamespace(
        username=TEST_ADMIN_USERNAME,
        password=TEST_ADMIN_PASSWORD,
    )


@pytest.fixture()
def csrf_token():
    def get_csrf_token(test_client, path):
        response = test_client.get(path)
        assert response.status_code == 200

        parser = _CSRFTokenParser()
        parser.feed(response.get_data(as_text=True))
        assert parser.token
        return parser.token

    return get_csrf_token


@pytest.fixture()
def csrf_post(csrf_token):
    def post(test_client, path, data, **kwargs):
        payload = data.copy()
        payload["csrf_token"] = csrf_token(test_client, path)
        return test_client.post(path, data=payload, **kwargs)

    return post


@pytest.fixture()
def authenticated_client(flask_app, csrf_token):
    test_client = flask_app.test_client()
    login_csrf_token = csrf_token(test_client, "/login")
    response = test_client.post(
        "/login",
        data={
            "username": TEST_ADMIN_USERNAME,
            "password": TEST_ADMIN_PASSWORD,
            "csrf_token": login_csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    return test_client
