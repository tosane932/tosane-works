import datetime
import re
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import SQLAlchemyError

import app as app_module
from models import Dataset, GuestCreationRateLimit, db


def _create_guest_dataset(*, active=True):
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now - datetime.timedelta(hours=1),
        last_activity_at=(
            now - datetime.timedelta(minutes=5)
            if active
            else now - datetime.timedelta(minutes=31)
        ),
        absolute_expires_at=now + datetime.timedelta(hours=1),
    )
    db.session.add(dataset)
    db.session.flush()
    return dataset


def _document(response):
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _guest_form(response):
    return _document(response).select_one(
        'form#guest-demo-form[action="/guest/start"]'
    )


def _guest_csrf_token(client):
    response = client.get("/login")
    assert response.status_code == 200
    form = _guest_form(response)
    assert form is not None
    csrf_input = form.select_one('input[name="csrf_token"]')
    assert csrf_input is not None
    assert csrf_input.get("value")
    return csrf_input["value"]


def _admin_login_csrf_token(client):
    response = client.get("/login")
    assert response.status_code == 200

    document = _document(response)
    csrf_input = document.select_one(
        'form#admin-login-form input[name="csrf_token"]'
    )

    assert csrf_input is not None
    assert csrf_input.get("value")
    return csrf_input["value"]


def _tamper_csrf_token(token):
    replacement = "A" if token[0] != "A" else "B"
    return replacement + token[1:]


def test_login_page_contains_separate_csrf_protected_guest_start_form(client):
    response = client.get("/login")
    document = _document(response)
    admin_form = document.select_one("form#admin-login-form")
    guest_form = document.select_one("form#guest-demo-form")

    assert response.status_code == 200
    assert document.select_one("#guest-demo-heading").get_text(strip=True) == (
        "ゲストデモ"
    )
    assert admin_form is not None
    assert guest_form is not None
    assert guest_form.get("method", "").lower() == "post"
    assert guest_form.get("action") == "/guest/start"
    assert guest_form.select_one('input[name="csrf_token"]') is not None
    assert admin_form.find_parent("form") is None
    assert guest_form.find_parent("form") is None
    assert document.select("form form") == []


def test_login_page_is_tosane_works_portfolio_entry(client):
    response = client.get("/login")
    document = _document(response)

    assert response.status_code == 200
    assert document.title.get_text(strip=True) == (
        "Tosane Works | Web Application Portfolio"
    )
    assert document.select_one(".login-system-header h1").get_text(
        " ", strip=True
    ) == "Tosane Works 現場の課題をWebアプリとして形に"
    assert "sales_data_app" not in document.get_text(" ", strip=True)

    expected_projects = {
        "store-category-heading": (
            "Bakery Hub",
            "portfolio-category-store",
        ),
        "information-category-heading": (
            "Puoppo",
            "portfolio-category-information",
        ),
        "logistics-category-heading": (
            "Driver Personality Test",
            "portfolio-category-logistics",
        ),
    }
    for heading_id, (project_name, category_class) in expected_projects.items():
        category = document.select_one(
            f'.portfolio-category-card[aria-labelledby="{heading_id}"]'
        )
        assert category is not None
        assert category_class in category.get("class", [])
        assert category.select_one("h3").get_text(strip=True) == project_name

    bakery_links = document.select_one(
        '.portfolio-project-links[aria-label="Bakery Hubのリンク"]'
    )
    assert bakery_links.select_one('a[href="#guest-demo-heading"]') is None
    assert [link.get_text(" ", strip=True) for link in bakery_links.select("a")] == [
        "📖 詳細README"
    ]

    external_links = {
        "https://github.com/tosane932/tosane-works/blob/main/store/bakery-hub/README.md",
        "https://puoppo.onrender.com/",
        "https://github.com/tosane932/tosane-works/blob/main/information/puoppo/README.md",
        "https://tosane932.github.io/tosane-works/logistics/driver-personality-test/",
        "https://github.com/tosane932/tosane-works/blob/main/logistics/driver-personality-test/README.md",
    }
    for href in external_links:
        link = document.select_one(f'a[href="{href}"]')
        assert link is not None
        assert link.get("target") == "_blank"
        assert set(link.get("rel", [])) == {"noopener", "noreferrer"}

    assert [
        heading.name
        for heading in document.select(
            "main h2, main h3, main h4"
        )
    ] == ["h2", "h3", "h4", "h4", "h2", "h3", "h2", "h3"]


def test_logged_in_guest_sees_return_link_instead_of_new_guest_start(
    flask_app,
):
    dataset = _create_guest_dataset(active=True)
    db.session.commit()
    original_expiry = dataset.absolute_expires_at

    guest_client = flask_app.test_client()
    with guest_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{dataset.id}"
        session_data["_fresh"] = True

    response = guest_client.get("/login")
    document = _document(response)

    return_link = document.select_one("#guest-demo-return")

    assert response.status_code == 200
    assert return_link is not None
    assert return_link.get("href") == "/home"
    assert return_link.get_text(strip=True) == "ゲストデモに戻る"
    assert "ゲストデモを利用中です。" in document.get_text(
        " ", strip=True
    )

    assert document.select_one("#guest-demo-form") is None
    assert document.select_one("#guest-demo-start") is None

    # 管理者へ切り替える導線は残す
    assert document.select_one("#admin-login-form") is not None

    home_response = guest_client.get(return_link["href"], follow_redirects=False)
    assert home_response.status_code == 200
    assert "利用中：ゲストデモ" in home_response.get_data(as_text=True)
    with guest_client.session_transaction() as session_data:
        assert session_data["_user_id"] == f"guest:{dataset.id}"
    assert Dataset.query.filter_by(kind="guest").count() == 1
    assert db.session.get(Dataset, dataset.id).absolute_expires_at == original_expiry


def test_login_capacity_counts_only_active_guest_and_enables_start_button(
    client,
    admin_dataset,
):
    active_guest = _create_guest_dataset(active=True)
    expired_guest = _create_guest_dataset(active=False)
    db.session.commit()

    response = client.get("/login")
    document = _document(response)
    button = document.select_one("#guest-demo-start")

    assert response.status_code == 200
    assert "現在のゲストデモ利用数：1 / 10" in document.get_text(
        " ", strip=True
    )
    assert button is not None
    assert not button.has_attr("disabled")
    assert admin_dataset.kind == "admin"
    assert active_guest.kind == "guest"
    assert expired_guest.kind == "guest"


def test_login_full_capacity_disables_button_with_visible_explanation(
    client,
):
    for _ in range(10):
        _create_guest_dataset(active=True)
    db.session.commit()

    response = client.get("/login")
    document = _document(response)
    button = document.select_one("#guest-demo-start")
    explanation = document.select_one("#guest-demo-unavailable-message")

    assert response.status_code == 200
    assert "現在のゲストデモ利用数：10 / 10" in document.get_text(
        " ", strip=True
    )
    assert explanation is not None
    assert "ただいま満員です" in explanation.get_text(" ", strip=True)
    assert button is not None
    assert button.has_attr("disabled")
    assert button.get("aria-describedby") == explanation.get("id")


def test_guest_disabled_button_uses_not_allowed_cursor():
    stylesheet = Path("static/style.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.guest-demo-button:disabled\s*\{[^}]*cursor:\s*not-allowed;",
        stylesheet,
        flags=re.DOTALL,
    )


@pytest.mark.parametrize("failure_kind", ["database", "configuration"])
def test_capacity_failure_disables_guest_only_and_keeps_admin_login(
    client,
    flask_app,
    monkeypatch,
    failure_kind,
):
    internal_marker = "sensitive-capacity-internal-detail"
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    if failure_kind == "database":
        monkeypatch.setattr(
            app_module,
            "_get_active_guest_dataset_count",
            Mock(side_effect=SQLAlchemyError(internal_marker)),
        )
    else:
        flask_app.config["GUEST_ACTIVE_DATASET_LIMIT"] = internal_marker

    response = client.get("/login")
    document = _document(response)
    response_text = response.get_data(as_text=True)
    guest_button = document.select_one("#guest-demo-start")

    assert response.status_code == 200
    assert document.select_one("form#admin-login-form") is not None
    assert document.select_one('input[name="username"]') is not None
    assert document.select_one('input[name="password"]') is not None
    assert "現在のゲストデモ利用状況を取得できません" in response_text
    assert guest_button is not None
    assert guest_button.has_attr("disabled")
    assert internal_marker not in response_text
    rollback.assert_called_once_with()


def test_login_capacity_display_does_not_run_cleanup(client, monkeypatch):
    cleanup = Mock(side_effect=AssertionError("GET must not run cleanup"))
    monkeypatch.setattr(
        app_module,
        "_cleanup_expired_guest_datasets",
        cleanup,
    )

    response = client.get("/login")

    assert response.status_code == 200
    cleanup.assert_not_called()


def test_guest_start_get_is_method_not_allowed(client):
    response = client.get("/guest/start")

    assert response.status_code == 405


@pytest.mark.parametrize("csrf_kind", ["missing", "invalid"])
def test_guest_start_csrf_rejection_has_no_side_effects(
    client,
    monkeypatch,
    csrf_kind,
):
    valid_token = _guest_csrf_token(client)
    reserve = Mock(side_effect=AssertionError("rate limit must not run"))
    cleanup = Mock(side_effect=AssertionError("cleanup must not run"))
    login = Mock(side_effect=AssertionError("login_user must not run"))
    monkeypatch.setattr(
        app_module,
        "_reserve_guest_creation_attempt",
        reserve,
    )
    monkeypatch.setattr(
        app_module,
        "_cleanup_expired_guest_datasets",
        cleanup,
    )
    monkeypatch.setattr(app_module, "login_user", login)
    payload = {}
    if csrf_kind == "invalid":
        payload["csrf_token"] = _tamper_csrf_token(valid_token)

    response = client.post("/guest/start", data=payload)

    assert response.status_code == 400
    assert Dataset.query.filter_by(kind="guest").count() == 0
    assert GuestCreationRateLimit.query.count() == 0
    reserve.assert_not_called()
    cleanup.assert_not_called()
    login.assert_not_called()


def test_guest_start_success_creates_identity_and_redirects_with_303(client):
    response = client.post(
        "/guest/start",
        data={"csrf_token": _guest_csrf_token(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert urlparse(response.headers["Location"]).path == "/home"
    created_guest = Dataset.query.filter_by(kind="guest").one()
    with client.session_transaction() as session_data:
        assert session_data["_user_id"] == f"guest:{created_guest.id}"


def test_guest_start_rechecks_capacity_and_rejects_full_without_login(
    client,
    monkeypatch,
):
    csrf_token = _guest_csrf_token(client)
    for _ in range(10):
        _create_guest_dataset(active=True)
    db.session.commit()
    login = Mock()
    count = Mock(wraps=app_module._get_active_guest_dataset_count)
    monkeypatch.setattr(app_module, "login_user", login)
    monkeypatch.setattr(
        app_module,
        "_get_active_guest_dataset_count",
        count,
    )

    response = client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert "ただいまゲストデモは満員です" in response.get_data(
        as_text=True
    )
    assert Dataset.query.filter_by(kind="guest").count() == 10
    assert count.call_count == 1
    login.assert_not_called()


def test_guest_start_rate_limit_returns_429_without_dataset_or_second_query(
    client,
    monkeypatch,
):
    csrf_token = _guest_csrf_token(client)
    reserve = Mock(return_value=False)
    count = Mock(side_effect=AssertionError("error UI must not query capacity"))
    cleanup = Mock(side_effect=AssertionError("cleanup must not run"))
    login = Mock()
    monkeypatch.setattr(
        app_module,
        "_reserve_guest_creation_attempt",
        reserve,
    )
    monkeypatch.setattr(
        app_module,
        "_get_active_guest_dataset_count",
        count,
    )
    monkeypatch.setattr(
        app_module,
        "_cleanup_expired_guest_datasets",
        cleanup,
    )
    monkeypatch.setattr(app_module, "login_user", login)

    response = client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
    )

    assert response.status_code == 429
    assert "短時間にゲストデモの開始操作が繰り返されました" in (
        response.get_data(as_text=True)
    )
    assert Dataset.query.filter_by(kind="guest").count() == 0
    count.assert_not_called()
    cleanup.assert_not_called()
    login.assert_not_called()


def test_admin_login_form_on_guest_start_error_submits_to_login_endpoint(
    client,
    admin_auth_config,
    monkeypatch,
):
    csrf_token = _guest_csrf_token(client)
    monkeypatch.setattr(
        app_module,
        "_reserve_guest_creation_attempt",
        Mock(return_value=False),
    )

    error_response = client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
    )
    error_document = _document(error_response)
    admin_form = error_document.select_one("form#admin-login-form")

    assert error_response.status_code == 429
    assert admin_form is not None

    admin_action = admin_form.get("action") or "/guest/start"
    admin_csrf_input = admin_form.select_one('input[name="csrf_token"]')
    assert admin_csrf_input is not None
    assert admin_csrf_input.get("value")

    login_response = client.post(
        admin_action,
        data={
            "username": admin_auth_config.username,
            "password": admin_auth_config.password,
            "csrf_token": admin_csrf_input["value"],
        },
        follow_redirects=False,
    )

    assert admin_action == "/login"
    assert login_response.status_code == 302
    assert urlparse(login_response.headers["Location"]).path == "/home"
    with client.session_transaction() as session_data:
        assert session_data["_user_id"] == "admin"


@pytest.mark.parametrize("failure_kind", ["database", "configuration"])
def test_guest_start_infrastructure_failure_returns_safe_503(
    client,
    flask_app,
    monkeypatch,
    failure_kind,
):
    csrf_token = _guest_csrf_token(client)
    internal_marker = "sensitive-start-internal-detail"

    if failure_kind == "database":
        monkeypatch.setattr(
            app_module,
            "_acquire_guest_admission_lock",
            Mock(side_effect=SQLAlchemyError(internal_marker)),
        )
    else:
        flask_app.config["GUEST_ACTIVE_DATASET_LIMIT"] = internal_marker

    response = client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
    )
    response_text = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "現在ゲストデモを開始できません" in response_text
    assert internal_marker not in response_text
    assert Dataset.query.filter_by(kind="guest").count() == 0


def test_guest_start_login_failure_returns_safe_500(
    client,
    monkeypatch,
):
    csrf_token = _guest_csrf_token(client)
    monkeypatch.setattr(app_module, "login_user", Mock(return_value=False))

    response = client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
    )
    response_text = response.get_data(as_text=True)

    assert response.status_code == 500
    assert "ゲストデモを開始できませんでした" in response_text
    assert Dataset.query.filter_by(kind="guest").count() == 1
    with client.session_transaction() as session_data:
        assert "_user_id" not in session_data


def test_authenticated_admin_guest_start_is_409_without_rate_consumption(
    authenticated_client,
):
    csrf_token = _guest_csrf_token(authenticated_client)

    response = authenticated_client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
    )

    assert response.status_code == 409
    assert Dataset.query.filter_by(kind="guest").count() == 0
    assert GuestCreationRateLimit.query.count() == 0
    with authenticated_client.session_transaction() as session_data:
        assert session_data["_user_id"] == "admin"


def test_authenticated_guest_cannot_replace_identity_or_consume_rate(client):
    guest_dataset = _create_guest_dataset(active=True)
    db.session.commit()
    with client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_dataset.id}"
        session_data["_fresh"] = True
    csrf_token = _admin_login_csrf_token(client)

    response = client.post(
        "/guest/start",
        data={"csrf_token": csrf_token},
    )

    assert response.status_code == 409
    assert Dataset.query.filter_by(kind="guest").count() == 1
    assert GuestCreationRateLimit.query.count() == 0
    with client.session_transaction() as session_data:
        assert session_data["_user_id"] == f"guest:{guest_dataset.id}"


def test_guest_form_marks_button_for_submit_time_double_click_suppression(
    client,
):
    response = client.get("/login")
    document = _document(response)
    guest_form = document.select_one("#guest-demo-form")

    assert guest_form is not None
    assert guest_form.get("data-disable-on-submit") == "true"
    assert 'addEventListener("submit"' in response.get_data(as_text=True)
