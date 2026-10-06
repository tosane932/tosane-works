import datetime
from pathlib import Path

from bs4 import BeautifulSoup

import app as app_module
from models import MaterialOrderItem, Product, ShopMemo, db, utc_now


def _document(response):
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _assert_labeled_icon(container, icon_name, label):
    assert container is not None
    icon = container.select_one(f'svg[data-icon="{icon_name}"]')
    assert icon is not None
    assert icon.get("aria-hidden") == "true"
    assert icon.get("focusable") == "false"
    assert container.get_text(" ", strip=True) == label


def test_product_registration_fixed_ui_uses_semantic_svg_icons(
    authenticated_client,
    admin_dataset,
    csrf_post,
):
    today = datetime.date.today()
    product = Product(
        dataset=admin_dataset,
        year=today.year,
        month=today.month,
        name="SVG確認商品",
        price=250,
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()

    document = _document(authenticated_client.get("/"))
    _assert_labeled_icon(
        document.select_one("main h1, .container h1"),
        "package-plus",
        "商品・メニュー登録",
    )
    _assert_labeled_icon(
        document.select_one("button.btn-add"),
        "plus",
        "新メニューを追加",
    )
    _assert_labeled_icon(
        document.select_one("button.btn-menu-register"),
        "save",
        "この内容で今月のメニューを登録する",
    )

    delete_button = document.select_one("button.btn-del")
    assert delete_button.select_one('svg[data-icon="trash-2"]') is not None
    assert delete_button.get("aria-label") == "SVG確認商品を削除"

    month_label = document.select_one(
        f'#month-select option[value="{today.month}"]'
    ).get_text(" ", strip=True)
    assert month_label == f"{today.month}月（登録済み）"
    assert "🍞 ベーカリー売上管理システム" not in document.get_text()
    assert "🚀" not in document.get_text()

    success = _document(
        csrf_post(
            authenticated_client,
            "/",
            {
                "year": str(today.year),
                "month": str(today.month),
                "product_id": str(product.id),
                "prod_name": product.name,
                "prod_price": str(product.price),
            },
        )
    )
    _assert_labeled_icon(
        success.select_one(".success-page .icon"),
        "circle-check",
        "",
    )
    _assert_labeled_icon(
        success.select_one("a.btn-home"),
        "house",
        "トップページに戻る",
    )
    _assert_labeled_icon(
        success.select_one("a.btn-input"),
        "clipboard-pen-line",
        "日次売上を入力する",
    )
    _assert_labeled_icon(
        success.select_one("a.btn-dashboard"),
        "chart-column",
        "お店の健康診断書を見る",
    )


def test_sales_input_fixed_ui_uses_svg_and_css_status_dot(
    authenticated_client,
    admin_dataset,
):
    today = datetime.date.today()
    db.session.add(
        Product(
            dataset=admin_dataset,
            year=today.year,
            month=today.month,
            name="入力画面SVG確認商品",
            price=300,
            is_active=True,
        )
    )
    db.session.commit()

    document = _document(authenticated_client.get("/input"))
    _assert_labeled_icon(
        document.select_one("h1"),
        "clipboard-pen-line",
        "日次売上入力",
    )
    assert document.select_one('.date-display svg[data-icon="calendar-days"]')
    _assert_labeled_icon(
        document.select_one(".ai-greeting-label"),
        "bot",
        "AIアシスタントより",
    )
    _assert_labeled_icon(
        document.select_one("#greetingButton"),
        "message-circle",
        "今日のひとことを聞く",
    )
    _assert_labeled_icon(
        document.select_one('button[type="submit"].btn-submit'),
        "save",
        "本日の売上個数を更新する",
    )
    _assert_labeled_icon(
        document.select_one(".btn-dashboard-link"),
        "chart-column",
        "お店の健康診断書（売上分析・ランキング）を見る",
    )
    _assert_labeled_icon(
        document.select_one(".input-home-link"),
        "house",
        "トップページに戻る",
    )
    assert document.select_one(".registered-quantity .status-dot") is not None
    assert "🟢" not in document.select_one(".registered-quantity").get_text()

    template_source = (
        Path(app_module.app.root_path) / "templates" / "input.html"
    ).read_text()
    assert 'data-icon="circle-check"' in template_source
    assert 'data-icon="loader-circle"' in template_source
    assert "✅ 本日の売上個数" not in template_source
    assert "🌀 今日のひとこと" not in template_source
    assert "greetingButton.textContent" not in template_source


def test_dashboard_fixed_ui_uses_specific_svg_icons(
    authenticated_client,
    admin_dataset,
):
    document = _document(authenticated_client.get("/dashboard"))

    expected = (
        ("h1", "chart-column", "売上データ分析"),
        ("#filterForm button", "search", "データを抽出"),
        (".dashboard-period", "calendar-days", None),
        ("#ranking-heading", "trophy", "売れ筋ランキング"),
        ("#sales-chart-heading", "chart-column", "商品ごとの売上"),
        ("#ai-advice-heading", "bot", "AIアドバイス"),
        ("#aiAdviceButton", "message-circle", "詳しいアドバイスを聞く"),
        (".dashboard-input-link", "clipboard-pen-line", "日次売上を入力する"),
        (".dashboard-home-link", "house", "トップページに戻る"),
    )
    for selector, icon_name, label in expected:
        container = document.select_one(selector)
        if label is None:
            assert container.select_one(f'svg[data-icon="{icon_name}"]')
        else:
            _assert_labeled_icon(container, icon_name, label)

    assert document.select_one('svg[data-icon="loader-circle"]') is not None
    assert document.select_one('svg[data-icon="triangle-alert"]') is not None

    rendered_text = document.get_text(" ", strip=True)
    for emoji in ("📊", "🔍", "🏆", "📅", "🤖", "🍞"):
        assert emoji not in rendered_text

    script_text = "\n".join(
        script.get_text() for script in document.find_all("script")
    )
    assert "（登録済み）" in script_text
    assert "aiButton.textContent" not in script_text
    for emoji in ("✅", "🌀", "🚨", "🤖"):
        assert emoji not in script_text


def test_login_brand_uses_wheat_svg(client):
    document = _document(client.get("/login"))
    _assert_labeled_icon(
        document.select_one(".portfolio-project-heading"),
        "wheat",
        "Bakery Hub",
    )


def test_remaining_shop_tool_fixed_icons_use_existing_lucide_language(
    authenticated_client,
    admin_dataset,
):
    now = utc_now()
    db.session.add_all(
        [
            MaterialOrderItem(
                dataset=admin_dataset,
                name="完了済み材料",
                is_completed=True,
                completed_at=now,
            ),
            ShopMemo(
                dataset=admin_dataset,
                title="SVG確認メモ",
                body="固定UIだけを確認",
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    db.session.commit()

    orders = _document(authenticated_client.get("/material-orders"))
    assert orders.select_one('.shop-tools-brand svg[data-icon="wrench"]')
    _assert_labeled_icon(
        orders.select_one(".shop-tools-header h1"),
        "shopping-cart",
        "材料発注リスト",
    )
    _assert_labeled_icon(
        orders.select_one(".material-order-card-completed .material-order-state"),
        "circle-check",
        "完了済み",
    )

    memos = _document(authenticated_client.get("/shop-tools/memo"))
    _assert_labeled_icon(
        memos.select_one("#shop-memo-menu-edit"),
        "pencil",
        "編集",
    )

    trash = _document(authenticated_client.get("/shop-tools/memo/trash"))
    _assert_labeled_icon(
        trash.select_one(".shop-tools-header h1"),
        "trash-2",
        "メモのゴミ箱",
    )
