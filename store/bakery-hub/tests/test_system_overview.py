import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import event

import app as app_module
from models import Dataset, Product, db


def _document(response):
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def test_anonymous_overview_is_public_without_database_or_ai_calls(
    client, monkeypatch,
):
    def unexpected_call(*args, **kwargs):
        pytest.fail("The public overview must not query business data or call AI")

    monkeypatch.setattr(app_module, "require_current_dataset", unexpected_call)
    monkeypatch.setattr(app_module.genai, "Client", unexpected_call)
    engine = db.engine
    event.listen(engine, "before_cursor_execute", unexpected_call)
    try:
        response = client.get("/system-overview")
    finally:
        event.remove(engine, "before_cursor_execute", unexpected_call)

    document = _document(response)
    assert document.select_one("main h1").get_text(strip=True) == "システム概要"
    assert "利用状態：未ログイン" in document.get_text()
    assert "利用中：管理者" not in document.get_text()
    assert document.select_one('form[action="/logout"]') is None
    assert document.select_one('a.app-sidebar-login-link[href="/login"]')
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("principal", ["admin", "guest", "expired-guest"])
def test_overview_remains_public_for_every_session_state(
    flask_app, client, principal, request,
):
    if principal == "admin":
        test_client = request.getfixturevalue("authenticated_client")
        expected_label = "利用中：管理者"
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
        expired = principal == "expired-guest"
        created = now - datetime.timedelta(hours=3) if expired else now
        dataset = Dataset(
            kind="guest", system_key=None, created_at=created,
            last_activity_at=created,
            absolute_expires_at=created + datetime.timedelta(hours=2),
        )
        db.session.add(dataset)
        db.session.commit()
        test_client = client
        with test_client.session_transaction() as session_data:
            session_data["_user_id"] = f"guest:{dataset.id}"
            session_data["_fresh"] = True
        expected_label = "利用状態：未ログイン" if expired else "利用中：ゲストデモ"

    document = _document(test_client.get("/system-overview"))
    assert expected_label in document.get_text()
    current_links = document.select('#app-sidebar a[aria-current="page"]')
    assert len(current_links) == 1
    assert current_links[0]["href"] == "/system-overview"
    assert current_links[0].select_one('svg[data-icon="book-open"]')
    assert Product.query.count() == 0
    if principal != "expired-guest":
        assert document.select_one('form[action="/logout"] input[name="csrf_token"]')
    if principal == "guest":
        assert "ゲスト利用は継続します" in document.get_text()
        with test_client.session_transaction() as session_data:
            assert session_data["_user_id"] == f"guest:{dataset.id}"


@pytest.mark.parametrize("path", [
    "/", "/input", "/dashboard", "/material-orders",
    "/shop-tools/memo", "/shop-tools/memo/trash", "/shop-tools/tasks",
])
def test_existing_pages_link_to_overview_without_losing_navigation(
    authenticated_client, admin_dataset, path,
):
    document = _document(authenticated_client.get(path))
    navigation = document.select_one('nav[aria-label="主要メニュー"]')
    link = navigation.select_one('a[href="/system-overview"]')
    assert link.get_text(strip=True) == "システム概要"
    assert link.get("aria-current") is None
    assert [item["href"] for item in navigation.select("a")] == [
        "/", "/input", "/dashboard", "/material-orders", "/system-overview",
    ]
    assert len(navigation.select('a[aria-current="page"]')) == 1


def test_overview_has_readable_sections_and_valid_internal_links(client):
    document = _document(client.get("/system-overview"))
    main = document.select_one("main")
    for section_id in ("flow", "features", "basics", "technologies", "tests"):
        assert main.select_one(f"section#{section_id} > h2")
    for feature_id in (
        "products", "sales", "dashboard", "materials", "memos", "tasks",
        "guest", "auth", "ai",
    ):
        card = main.select_one(f"article#feature-{feature_id}")
        assert card.select_one("h3")
        assert card.select_one("details > summary")
        assert {label.get_text(strip=True) for label in card.select("dt")} >= {
            "Python / Flask", "Database", "Frontend", "pytest", "確認するコード",
        }
    for link in main.select('a[href^="#"]'):
        assert document.find(id=link["href"][1:])
    assert document.select_one('meta[name="viewport"]')
    assert document.select_one('button[aria-controls="app-sidebar"]')
    # The overview itself only needs the shared menu script; no external SDKs.
    assert document.select("script[src]") == []


def test_overview_does_not_render_business_data_or_arbitrary_query_values(
    authenticated_client, admin_dataset,
):
    private_product = "overview-private-product-marker"
    db.session.add(Product(
        dataset=admin_dataset, year=2026, month=10,
        name=private_product, price=300,
    ))
    db.session.commit()
    response = authenticated_client.get(
        "/system-overview?debug=overview-query-marker&dataset_id=untrusted"
    )
    document = _document(response)
    assert private_product not in str(document)
    assert "overview-query-marker" not in str(document)
    assert Product.query.one().name == private_product


def test_overview_does_not_accept_writes(client, csrf_token):
    token = csrf_token(client, "/login")
    response = client.post("/system-overview", data={"csrf_token": token})
    assert response.status_code == 405
