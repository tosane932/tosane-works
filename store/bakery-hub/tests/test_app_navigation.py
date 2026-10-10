import datetime
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import app as app_module
from models import Dataset, db


APP_ROUTES = {
    "products": "/",
    "product-catalog": "/products",
    "sales": "/input",
    "dashboard": "/dashboard",
    "shop-tools": "/material-orders",
}

APP_SIDEBAR_ITEMS = {
    "products": ("商品・メニュー登録", "package-plus"),
    "product-catalog": ("商品一覧", "list"),
    "sales": ("日次売上入力", "clipboard-pen-line"),
    "dashboard": ("売上データ分析", "chart-column"),
    "shop-tools": ("店舗メモツール", "wrench"),
    "system-overview": ("システム概要", "book-open"),
}

SIDEBAR_ROUTES = {**APP_ROUTES, "system-overview": "/system-overview"}


def _assert_inline_icon(container, icon_name):
    icon = container.select_one(f'svg[data-icon="{icon_name}"]')

    assert icon is not None
    assert icon.get("viewbox") == "0 0 24 24"
    assert icon.get("fill") == "none"
    assert icon.get("stroke") == "currentColor"
    assert icon.get("stroke-width") == "2"
    assert icon.get("aria-hidden") == "true"
    assert icon.get("focusable") == "false"


def _css_rule_body(style_source, selector):
    match = re.search(
        rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
        style_source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def _contrast_with_white(hex_color):
    channels = [
        int(hex_color[index:index + 2], 16) / 255
        for index in (1, 3, 5)
    ]
    linear_channels = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    luminance = (
        0.2126 * linear_channels[0]
        + 0.7152 * linear_channels[1]
        + 0.0722 * linear_channels[2]
    )
    return 1.05 / (luminance + 0.05)


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


def _assert_app_navigation(document, active_page, user_label):
    sidebar = document.select_one("#app-sidebar")
    assert sidebar is not None
    navigation = sidebar.select_one('nav[aria-label="主要メニュー"]')
    assert navigation is not None

    links = {
        link["data-page"]: link
        for link in navigation.select("a[data-page]")
    }
    assert set(links) == set(SIDEBAR_ROUTES)
    for page, path in SIDEBAR_ROUTES.items():
        label, icon_name = APP_SIDEBAR_ITEMS[page]
        assert links[page]["href"] == path
        assert links[page].get_text(" ", strip=True) == label
        assert "touch-control-no-select" in links[page].get("class", [])
        _assert_inline_icon(links[page], icon_name)
        if page == active_page:
            assert links[page].get("aria-current") == "page"
            assert "app-sidebar-link-current" in links[page].get(
                "class",
                [],
            )
        else:
            assert links[page].get("aria-current") is None

    assert user_label in sidebar.get_text()
    brand = sidebar.select_one("a.app-sidebar-brand")
    assert brand is not None
    assert brand.get_text(" ", strip=True) == "Bakery Hub"
    assert "🍞" not in brand.get_text()
    assert "touch-control-no-select" in brand.get("class", [])
    _assert_inline_icon(brand, "wheat")

    for emoji in ("🥐", "📝", "📊", "🧰"):
        assert emoji not in navigation.get_text()

    logout_form = sidebar.select_one('form[action="/logout"]')
    assert logout_form is not None
    assert logout_form.get("method", "").lower() == "post"
    assert logout_form.select_one('input[name="csrf_token"]') is not None


@pytest.mark.parametrize(
    ("active_page", "route"),
    APP_ROUTES.items(),
)
def test_admin_sees_shared_sidebar_with_correct_current_page(
    authenticated_client,
    admin_dataset,
    active_page,
    route,
):
    response = authenticated_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    _assert_app_navigation(document, active_page, "利用中：管理者")
    logout_button = document.select_one(
        "#app-sidebar .app-sidebar-logout-button"
    )
    assert "ログアウト" in logout_button.get_text()
    assert "🚪" not in logout_button.get_text()
    assert "ゲストを終了する" not in document.get_text()
    assert "ゲスト利用は継続します" not in document.get_text()
    assert "touch-control-no-select" in logout_button.get("class", [])
    _assert_inline_icon(logout_button, "log-out")


@pytest.mark.parametrize(
    ("active_page", "route"),
    APP_ROUTES.items(),
)
def test_guest_sees_shared_sidebar_with_correct_current_page(
    flask_app,
    active_page,
    route,
):
    guest_client = _guest_client(flask_app)
    response = guest_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    _assert_app_navigation(document, active_page, "利用中：ゲストデモ")
    login_link = document.select_one("#app-sidebar a.app-sidebar-login-link")
    assert login_link is not None
    assert login_link["href"] == "/login"
    assert "ログイン画面に戻る" in login_link.get_text()
    assert "管理者ログイン画面を開く" not in login_link.get_text()
    assert "ゲスト利用は継続します" in login_link.get_text()
    assert "🔐" not in login_link.get_text()
    assert "touch-control-no-select" in login_link.get("class", [])
    _assert_inline_icon(login_link, "log-in")
    logout_button = document.select_one(
        "#app-sidebar .app-sidebar-logout-button"
    )
    assert "ゲストを終了する" in logout_button.get_text()
    assert "🚪" not in logout_button.get_text()
    assert "ログアウト" not in logout_button.get_text()
    assert "touch-control-no-select" in logout_button.get("class", [])
    _assert_inline_icon(logout_button, "log-out")


def test_guest_returning_to_login_page_keeps_guest_session(flask_app):
    guest_client = _guest_client(flask_app)

    login_response = guest_client.get("/login")
    protected_response = guest_client.get("/material-orders")

    assert login_response.status_code == 200
    assert protected_response.status_code == 200
    assert "利用中：ゲストデモ" in protected_response.get_data(as_text=True)


@pytest.mark.parametrize("route", APP_ROUTES.values())
def test_anonymous_users_remain_blocked_from_app_routes(client, route):
    response = client.get(route, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?")


def test_mobile_navigation_has_accessible_open_and_close_controls(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    open_button = document.select_one("button.app-navigation-open-button")
    close_button = document.select_one("button.app-navigation-close-button")
    overlay = document.select_one("button.app-navigation-overlay")

    assert open_button is not None
    assert open_button.get("type") == "button"
    assert open_button.get("aria-label") == "主要メニューを開く"
    assert open_button.get("aria-controls") == "app-sidebar"
    assert open_button.get("aria-expanded") == "false"
    assert "☰" not in open_button.get_text()
    assert "touch-control-no-select" in open_button.get("class", [])
    _assert_inline_icon(open_button, "menu")
    assert close_button is not None
    assert close_button.get("aria-label") == "主要メニューを閉じる"
    assert "×" not in close_button.get_text()
    assert "touch-control-no-select" in close_button.get("class", [])
    _assert_inline_icon(close_button, "x")
    assert overlay is not None
    assert overlay.has_attr("hidden")


def test_sidebar_category_colors_use_contrasting_white_foreground():
    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()
    category_colors = {
        ".app-sidebar-link-products": "#9a641f",
        ".app-sidebar-link-catalog": "#8b4d3b",
        ".app-sidebar-link-sales": "#477b59",
        ".app-sidebar-link-dashboard": "#bc4848",
        ".app-sidebar-link-shop-tools": "#48739c",
    }

    link_rule = _css_rule_body(style_source, ".app-sidebar-link")
    current_rule = _css_rule_body(
        style_source,
        ".app-sidebar-link-current",
    )
    assert "color: #ffffff;" in link_rule
    assert "background: var(--app-menu-color);" in link_rule
    assert "color: #ffffff;" in current_rule
    assert "background: var(--app-menu-color);" in current_rule
    assert "text-decoration: underline;" in current_rule

    for selector, color in category_colors.items():
        category_rule = _css_rule_body(style_source, selector)
        assert f"--app-menu-color: {color};" in category_rule
        assert _contrast_with_white(color) >= 4.5


def test_catalog_navigation_uses_its_own_color_class(
    authenticated_client, admin_dataset,
):
    document = BeautifulSoup(
        authenticated_client.get("/products").get_data(as_text=True),
        "html.parser",
    )
    registration = document.select_one('#app-sidebar a[data-page="products"]')
    catalog = document.select_one('#app-sidebar a[data-page="product-catalog"]')
    assert "app-sidebar-link-products" in registration.get("class", [])
    assert "app-sidebar-link-catalog" in catalog.get("class", [])
    assert "app-sidebar-link-products" not in catalog.get("class", [])
    assert catalog.get("aria-current") == "page"
    assert registration.get("aria-current") is None


def test_mobile_sidebar_links_restore_press_shift_with_overflow_room():
    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()

    link_rule = _css_rule_body(style_source, ".app-sidebar-link")
    assert "transition: transform 0.16s ease;" in link_rule
    assert """
    .app-sidebar-link:active {
        transform: translateX(4px);
    }
""" in style_source
    assert """
    .app-sidebar-nav {
        box-sizing: border-box;
        padding-right: 6px;
    }
""" in style_source
    assert """
    .app-sidebar-link:hover,
    .app-sidebar-link:focus-visible,
    .app-sidebar-link:active {
        transform: none;
    }
""" in style_source


def test_app_navigation_supports_escape_and_focus_return_without_inner_html():
    navigation_source = (
        Path(app_module.app.root_path) / "templates" / "_app_navigation.html"
    ).read_text()

    assert 'event.key === "Escape"' in navigation_source
    assert "openButton.focus()" in navigation_source
    assert 'setAttribute("aria-expanded", "true")' in navigation_source
    assert "innerHTML" not in navigation_source
    assert "|safe" not in navigation_source


def test_shared_mobile_navigation_reveals_on_upward_scroll_and_keeps_menus_stable(
    client, authenticated_client, admin_dataset,
):
    navigation_source = (
        Path(app_module.app.root_path) / "templates" / "_app_navigation.html"
    ).read_text()
    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()

    for test_client, path in ((client, "/system-overview"), (authenticated_client, "/")):
        document = BeautifulSoup(test_client.get(path).get_data(as_text=True), "html.parser")
        assert document.select_one("button.app-navigation-open-button")
        assert any(
            "app-top-navigation-hidden" in script.get_text()
            for script in document.select("script:not([src])")
        )
    assert 'const hideDistance = 36;' in navigation_source
    assert 'const revealDistance = 18;' in navigation_source
    assert 'const topThreshold = 12;' in navigation_source
    assert 'window.addEventListener("scroll"' in navigation_source
    assert 'window.requestAnimationFrame(updateTopNavigation)' in navigation_source
    assert 'directionDistance = Math.max(0,' in navigation_source
    assert 'directionDistance + movement * (navigationHidden ? -1 : 1)' in navigation_source
    assert 'document.body.classList.add("app-top-navigation-hidden")' in navigation_source
    assert 'document.addEventListener("overview:contentschange", resetScrollDirection)' in navigation_source
    assert 'navigationIsOpen()' in navigation_source
    assert 'event.key === "Tab"' in navigation_source
    assert 'app-top-navigation-ready:not(.system-overview-page)::before' in style_source
    assert 'app-top-navigation-hidden .app-navigation-open-button' in style_source
    assert 'blur(5px)' in style_source
    assert 'visibility 0s linear 240ms' in style_source
    assert 'transition: none;' in style_source


def test_overview_chapter_bar_uses_milk_tea_colors_with_readable_arrow():
    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()
    overview_source = (
        Path(app_module.app.root_path) / "templates" / "system_overview.html"
    ).read_text()
    button_rules = re.findall(
        r"\.system-overview-page \.overview-location-button \{([^}]*)\}",
        style_source,
    )
    assert any(
        all(color in rule for color in (
            "border-color: #D3C0A7;",
            "color: #604A35;",
            "background: #E9DDCB;",
        ))
        for rule in button_rules
    )
    assert not any(
        "background: rgba(255, 254, 251, 0.98);" in rule
        for rule in button_rules
    )
    arrow = BeautifulSoup(overview_source, "html.parser").select_one(
        "#overview-location-button svg.ui-icon"
    )
    assert arrow is not None and arrow["stroke"] == "currentColor"


def test_mobile_primary_buttons_use_replayable_tap_bubble_animation():
    navigation_source = (
        Path(app_module.app.root_path) / "templates" / "_app_navigation.html"
    ).read_text()
    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()

    assert (
        'const tapBubbleSelector = '
        '".app-navigation-open-button, .app-navigation-close-button, '
        '.shop-tools-fab, .app-sidebar-link, .app-sidebar-brand, '
        '.app-sidebar-logout-button, .app-sidebar-login-link";'
    ) in navigation_source
    assert 'document.addEventListener("pointerdown"' in navigation_source
    assert "tapBubbleButton.classList.remove(tapBubbleClass)" in (
        navigation_source
    )
    assert "void tapBubbleButton.offsetWidth" in navigation_source
    assert "tapBubbleButton.classList.add(tapBubbleClass)" in (
        navigation_source
    )
    assert 'document.addEventListener("animationend"' in navigation_source
    assert "reducedMotion.matches" in navigation_source
    assert "setInterval" not in navigation_source

    assert "@keyframes shop-tool-button-bubble" in style_source
    assert "@keyframes app-sidebar-link-shimmer" in style_source
    assert ".app-sidebar-link.is-tap-bubbling::after" in style_source
    assert "--tap-bubble-color: rgba(111, 77, 40, 0.9);" in style_source
    assert ".app-navigation-open-button::after" in style_source
    assert ".app-navigation-close-button::after" in style_source
    assert ".shop-tools-fab::after" in style_source
    assert ".is-tap-bubbling::after" in style_source
    assert "@media (prefers-reduced-motion: reduce)" in style_source
    assert ".touch-control-no-select" in style_source
    assert "-webkit-user-select: none;" in style_source
    assert "user-select: none;" in style_source
    assert "-webkit-touch-callout: none;" in style_source


@pytest.mark.parametrize(
    ("route", "control_selectors", "content_selectors"),
    [
        (
            "/",
            [
                "#month-select",
                ".btn-del",
                ".btn-add",
                ".btn-menu-register",
            ],
            [".input-name", ".input-price", ".section-title"],
        ),
        (
            "/dashboard",
            [
                "#selectYear",
                "#selectMonth",
                ".btn-submit",
                ".btn-ai-advice",
                ".dashboard-action",
            ],
            ["#aiAdviceText", ".dashboard-filter-guidance"],
        ),
        (
            "/input",
            [".btn-greeting", ".input-home-link"],
            ["#greetingText", ".sales-input-guidance"],
        ),
    ],
)
def test_app_action_controls_disable_selection_without_covering_content(
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


def test_material_orders_has_distinct_app_and_shop_tools_navigation(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/material-orders")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert document.select_one('nav[aria-label="主要メニュー"]') is not None
    assert document.select_one(
        'nav[aria-label="店舗メモツールのメニュー"]'
    ) is not None


@pytest.mark.parametrize(
    "route",
    ["/shop-tools/memo", "/shop-tools/memo/trash"],
)
def test_memo_pages_keep_shop_tools_current_in_both_navigations(
    authenticated_client,
    admin_dataset,
    route,
):
    response = authenticated_client.get(route)
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    app_link = document.select_one(
        'nav[aria-label="主要メニュー"] a[data-page="shop-tools"]'
    )
    memo_link = document.select_one(
        'nav[aria-label="店舗メモツールのメニュー"] a[data-tool="memo"]'
    )
    assert app_link.get("aria-current") == "page"
    assert "app-sidebar-link-current" in app_link.get("class", [])
    assert memo_link.get("aria-current") == "page"
    assert "shop-tools-nav-link-current" in memo_link.get("class", [])
