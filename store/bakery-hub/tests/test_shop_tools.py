import datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import app as app_module
from models import Dataset, db


SHOP_TOOL_ROUTES = {
    "orders": "/material-orders",
    "memo": "/shop-tools/memo",
    "tasks": "/shop-tools/tasks",
}


def _guest_client(flask_app):
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

    test_client = flask_app.test_client()
    with test_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{dataset.id}"
        session_data["_fresh"] = True
    return test_client


def _assert_shop_tools_navigation(document, active_tool):
    navigation = document.select_one(
        'nav[aria-label="店舗メモツールのメニュー"]'
    )
    assert navigation is not None

    links = {
        link["data-tool"]: link
        for link in navigation.select("a[data-tool]")
    }
    assert set(links) == set(SHOP_TOOL_ROUTES)
    for tool, path in SHOP_TOOL_ROUTES.items():
        assert links[tool]["href"] == path
        if tool == active_tool:
            assert links[tool].get("aria-current") == "page"
            assert "shop-tools-nav-link-current" in links[tool].get(
                "class",
                [],
            )
        else:
            assert links[tool].get("aria-current") is None


def test_index_uses_sidebar_as_its_single_shop_tools_entry(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    links = document.select(
        'nav[aria-label="主要メニュー"] a[data-page="shop-tools"]'
    )

    assert response.status_code == 200
    assert len(links) == 1
    assert links[0]["href"] == "/material-orders"
    assert "店舗メモツール" in links[0].get_text()
    assert document.select("a.menu-link-shop-tools") == []


@pytest.mark.parametrize(
    ("active_tool", "route"),
    SHOP_TOOL_ROUTES.items(),
)
def test_admin_can_open_each_shop_tool_and_see_current_navigation(
    authenticated_client,
    admin_dataset,
    active_tool,
    route,
):
    response = authenticated_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert "店舗メモツール" in document.get_text()
    assert "利用中：管理者" in document.get_text()
    _assert_shop_tools_navigation(document, active_tool)


@pytest.mark.parametrize(
    ("active_tool", "route"),
    SHOP_TOOL_ROUTES.items(),
)
def test_guest_can_open_each_shop_tool_and_see_current_navigation(
    flask_app,
    active_tool,
    route,
):
    guest_client = _guest_client(flask_app)
    response = guest_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert "利用中：ゲストデモ" in document.get_text()
    _assert_shop_tools_navigation(document, active_tool)


@pytest.mark.parametrize("route", SHOP_TOOL_ROUTES.values())
def test_anonymous_user_cannot_open_shop_tools(client, route):
    response = client.get(route, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?")


def test_tasks_tool_exposes_writable_checklist_ui_with_existing_svg_language(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/shop-tools/tasks")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert document.select_one("h1").get_text(strip=True).endswith("タスク")
    assert "準備中" not in document.select_one("#shop-tools-content").get_text()
    assert document.select_one(
        'form[action="/shop-tools/tasks"] input[name="title"]'
    ) is not None

    header_icon = document.select_one(
        '.shop-tools-header svg[data-icon="list-todo"]'
    )
    assert header_icon is not None
    add_button = document.select_one(".shop-task-add-button")
    assert add_button.select_one('svg[data-icon="plus"]') is not None
    assert add_button.get_text(" ", strip=True) == "追加"
    assert "✅" not in document.get_text()


@pytest.mark.parametrize(
    ("route", "first_content_selector"),
    [
        ("/shop-tools/memo/trash", ".shop-memo-back-link"),
        ("/material-orders", ".material-order-create-section"),
        ("/shop-tools/tasks", ".shop-task-list-root"),
    ],
)
def test_secondary_shop_tools_hide_duplicate_header_only_on_mobile(
    authenticated_client,
    admin_dataset,
    route,
    first_content_selector,
):
    response = authenticated_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    header = document.select_one(".shop-tools-header")
    main = document.select_one(".shop-tools-content")

    assert response.status_code == 200
    assert header is not None
    assert "shop-tools-header-mobile-hidden" in header.get("class", [])
    assert main.select_one(f":scope > {first_content_selector}") is not None

    css_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()
    assert ".shop-tools-header-mobile-hidden" in css_source
    assert "display: none;" in css_source
    assert (
        ".shop-tools-header-mobile-hidden ~ .shop-tools-content"
        in css_source
    )
    assert "padding-top: 16px;" in css_source


def test_material_orders_shell_keeps_visible_form_and_plus_only_accessible_fab(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/material-orders")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    fab = document.select_one("button.shop-tools-fab")
    add_button = document.select_one("button.material-order-add-button")

    assert response.status_code == 200
    assert document.select_one('label[for="material-name"]') is not None
    assert document.select_one('label[for="material-quantity"]') is not None
    assert document.select_one('label[for="material-memo"]') is not None
    assert fab is not None
    assert fab.get("type") == "button"
    assert fab.get("aria-label") == "材料を追加"
    assert fab.get("aria-controls") == "material-name"
    assert fab.get_text(strip=True) == "＋"
    assert "touch-control-no-select" in fab.get("class", [])
    assert add_button is not None
    assert "touch-control-no-select" in add_button.get("class", [])
    assert document.select_one(".shop-tools-fab-label") is None


@pytest.mark.parametrize(
    "route",
    ["/material-orders", "/shop-tools/memo", "/shop-tools/tasks"],
)
def test_shop_tool_fabs_share_mobile_no_select_control_class(
    authenticated_client,
    admin_dataset,
    route,
):
    response = authenticated_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    fab = document.select_one("button.shop-tools-fab")

    assert response.status_code == 200
    assert fab is not None
    assert "touch-control-no-select" in fab.get("class", [])


@pytest.mark.parametrize(
    ("route", "control_selectors", "content_selectors"),
    [
        (
            "/material-orders",
            [
                ".shop-tools-nav-link",
                ".material-order-add-button",
                ".shop-tools-fab",
            ],
            [
                ".material-order-field input",
                ".material-order-field textarea",
                ".material-order-empty",
            ],
        ),
        (
            "/shop-tools/memo",
            [
                ".shop-tools-nav-link",
                ".shop-memo-search-icon-button",
                ".shop-memo-search-clear",
                ".shop-memo-sort-link",
                ".shop-memo-trash-link",
                ".shop-tools-fab",
            ],
            [
                ".shop-memo-field input",
                ".shop-memo-field textarea",
                ".shop-memo-empty",
            ],
        ),
        (
            "/shop-tools/memo/trash",
            [
                ".shop-tools-nav-link",
                ".shop-memo-back-link",
                ".shop-memo-search-icon-button",
                ".shop-memo-search-clear",
            ],
            [".shop-memo-empty"],
        ),
        (
            "/shop-tools/tasks",
            [
                ".shop-tools-nav-link",
                ".shop-task-add-button",
                ".shop-tools-fab",
            ],
            [
                ".shop-task-create-row input",
                ".shop-task-empty",
            ],
        ),
    ],
)
def test_shop_tool_actions_disable_selection_without_covering_content(
    authenticated_client,
    admin_dataset,
    route,
    control_selectors,
    content_selectors,
):
    response = authenticated_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    for selector in control_selectors:
        controls = document.select(selector)
        assert controls
        assert all(
            "touch-control-no-select" in control.get("class", [])
            for control in controls
        )

    for selector in content_selectors:
        content_elements = document.select(selector)
        assert content_elements
        assert all(
            "touch-control-no-select" not in element.get("class", [])
            for element in content_elements
        )


def test_shop_tools_templates_keep_autoescape_and_safe_dom_updates():
    template_root = Path(app_module.app.root_path) / "templates"
    template_paths = [
        template_root / "material_orders.html",
        template_root / "shop_tools" / "base.html",
        template_root / "shop_tools" / "memos.html",
        template_root / "shop_tools" / "memo_trash.html",
        template_root / "shop_tools" / "_memo_search.html",
        template_root / "shop_tasks.html",
    ]

    for template_path in template_paths:
        source = template_path.read_text()
        assert "|safe" not in source
        assert "innerHTML" not in source


def test_shop_tools_css_preserves_tap_targets_and_keyboard_focus():
    css_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()

    assert ".shop-tools-nav-link" in css_source
    assert "min-height: 48px" in css_source
    assert ".shop-memo-body a:focus-visible" in css_source
    assert ".shop-task-toggle:focus-visible" in css_source
    assert ":focus-visible" in css_source
