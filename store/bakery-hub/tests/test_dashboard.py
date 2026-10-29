import datetime
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

import app as app_module
from conftest import post_ai
from models import DailySales, Dataset, Product, db


FIXED_AI_ADVICE = (
    "売上ランキングとグラフを更新しました。"
    "詳しい改善案を確認する場合は、"
    "「詳しいアドバイスを聞く」ボタンを押してください。"
)


def _product_snapshot():
    return [
        (
            product.id,
            product.year,
            product.month,
            product.name,
            product.price,
            product.is_active,
        )
        for product in Product.query.order_by(Product.id).all()
    ]


def _sales_snapshot():
    return [
        (sale.id, sale.product_id, sale.date, sale.quantity)
        for sale in DailySales.query.order_by(DailySales.id).all()
    ]


def _dashboard_document(response):
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def test_dashboard_keeps_sales_charts_with_one_visible_ranking_and_weather_detail(
    authenticated_client, admin_dataset,
):
    document = _dashboard_document(authenticated_client.get("/dashboard"))
    assert document.select_one("#periodSalesChart") is not None
    assert document.select_one("#salesChart") is not None
    assert document.select_one("#rankingPeriodTab") is not None
    assert document.select_one("#rankingLegacyTab") is not None
    assert not document.select_one("#periodSalesRanking").has_attr("hidden")
    assert document.select_one("#rankingContainer").has_attr("hidden")
    assert document.select_one("#periodWeatherDialog #periodWeatherEntries") is not None
    assert document.select_one(".dashboard-legacy-details") is None
    assert document.select_one("#selectYear") is not None
    assert document.select_one("#aiAdviceButton") is not None


def _dashboard_year_values(document):
    return [
        option["value"]
        for option in document.select("#selectYear option")
    ]


@pytest.fixture()
def dashboard_records(flask_app, admin_dataset, monkeypatch):
    def reject_gemini_client(*args, **kwargs):
        raise AssertionError("Gemini Client must not be used by dashboard API")

    monkeypatch.setattr(app_module.genai, "Client", reject_gemini_client)

    product_a = Product(
        dataset=admin_dataset,
        year=2026,
        month=8,
        name="商品A",
        price=100,
    )
    product_b = Product(
        dataset=admin_dataset,
        year=2026,
        month=8,
        name="商品B",
        price=200,
    )
    july_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=7,
        name="7月商品",
        price=300,
    )
    previous_year_product = Product(
        dataset=admin_dataset,
        year=2025,
        month=8,
        name="前年商品",
        price=400,
    )
    db.session.add_all([
        product_a,
        product_b,
        july_product,
        previous_year_product,
    ])
    db.session.flush()

    db.session.add_all([
        DailySales(
            product_id=product_a.id,
            date=datetime.date(2026, 8, 1),
            quantity=3,
        ),
        DailySales(
            product_id=product_a.id,
            date=datetime.date(2026, 8, 2),
            quantity=7,
        ),
        DailySales(
            product_id=product_b.id,
            date=datetime.date(2026, 8, 1),
            quantity=5,
        ),
        DailySales(
            product_id=july_product.id,
            date=datetime.date(2026, 7, 1),
            quantity=20,
        ),
        DailySales(
            product_id=previous_year_product.id,
            date=datetime.date(2025, 8, 1),
            quantity=30,
        ),
    ])
    db.session.commit()

    return {
        "product_a_id": product_a.id,
        "product_b_id": product_b.id,
    }


@pytest.fixture()
def cross_dataset_dashboard_records(flask_app, admin_dataset):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    admin_unique_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=8,
        name="管理者限定商品",
        price=100,
    )
    admin_same_name_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=8,
        name="クロワッサン",
        price=200,
    )
    guest_unique_product = Product(
        dataset=guest_dataset,
        year=2026,
        month=8,
        name="ゲスト限定商品",
        price=300,
    )
    guest_same_name_product = Product(
        dataset=guest_dataset,
        year=2026,
        month=8,
        name="クロワッサン",
        price=400,
    )
    db.session.add_all([
        guest_dataset,
        admin_unique_product,
        admin_same_name_product,
        guest_unique_product,
        guest_same_name_product,
    ])
    db.session.flush()
    db.session.add_all([
        DailySales(
            product_id=admin_unique_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=11,
        ),
        DailySales(
            product_id=admin_same_name_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=10,
        ),
        DailySales(
            product_id=guest_unique_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=987654,
        ),
        DailySales(
            product_id=guest_same_name_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=90,
        ),
    ])
    db.session.commit()


def test_dashboard_initial_all_year_period_matches_same_filter_api(
    authenticated_client,
    dashboard_records,
    monkeypatch,
):
    monkeypatch.setattr(
        app_module,
        "business_today",
        lambda: datetime.date(2026, 12, 31),
    )
    captured_ranked_sales = []
    real_render_template = app_module.render_template

    def capture_dashboard_context(template_name, *args, **kwargs):
        if template_name == "dashboard.html":
            captured_ranked_sales.extend(kwargs["ranked_sales"])
        return real_render_template(template_name, *args, **kwargs)

    monkeypatch.setattr(
        app_module,
        "render_template",
        capture_dashboard_context,
    )

    html_response = authenticated_client.get("/dashboard")
    document = _dashboard_document(html_response)
    year_select = document.select_one("#selectYear")
    month_select = document.select_one("#selectMonth")
    selected_year = year_select.select_one("option[selected]")
    period_title = document.select_one("#periodTitle")

    api_response = authenticated_client.get("/api/dashboard-data")
    payload = api_response.get_json()

    assert html_response.status_code == 200
    assert api_response.status_code == 200
    assert _dashboard_year_values(document) == ["", "2025", "2026"]
    assert selected_year is not None
    assert selected_year["value"] == ""
    assert month_select.has_attr("disabled")
    assert period_title.get_text(" ", strip=True) == "全年度・全月"
    assert payload["period_text"] == "全年度・全月"
    assert payload["sales_months"] == []
    assert payload["ranked_sales"] == [
        list(item)
        for item in captured_ranked_sales
    ]


def test_dashboard_without_sales_shows_current_year_and_empty_state(
    authenticated_client,
    admin_dataset,
    monkeypatch,
):
    monkeypatch.setattr(
        app_module,
        "business_today",
        lambda: datetime.date(2026, 6, 1),
    )

    response = authenticated_client.get("/dashboard")
    document = _dashboard_document(response)

    assert response.status_code == 200
    assert _dashboard_year_values(document) == ["", "2026"]
    assert document.select_one("#selectMonth").has_attr("disabled")
    assert "該当期間の売上データ（数量）が見つかりませんでした。" in (
        response.get_data(as_text=True)
    )


def test_dashboard_year_options_use_daily_sales_in_current_dataset(
    authenticated_client,
    dashboard_records,
    admin_dataset,
    monkeypatch,
):
    monkeypatch.setattr(
        app_module,
        "business_today",
        lambda: datetime.date(2026, 6, 1),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    admin_past_sales_product = Product(
        dataset=admin_dataset,
        year=2023,
        month=4,
        name="管理者2023年売上商品",
        price=200,
    )
    admin_product_without_sales = Product(
        dataset=admin_dataset,
        year=2022,
        month=4,
        name="管理者2022年未売上商品",
        price=300,
    )
    guest_sales_product = Product(
        dataset=guest_dataset,
        year=2024,
        month=4,
        name="Guest 2024年売上商品",
        price=400,
    )
    db.session.add_all([
        guest_dataset,
        admin_past_sales_product,
        admin_product_without_sales,
        guest_sales_product,
    ])
    db.session.flush()
    db.session.add_all([
        DailySales(
            product_id=admin_past_sales_product.id,
            date=datetime.date(2023, 4, 1),
            quantity=1,
        ),
        DailySales(
            product_id=guest_sales_product.id,
            date=datetime.date(2024, 4, 1),
            quantity=1,
        ),
    ])
    db.session.commit()

    response = authenticated_client.get("/dashboard")
    document = _dashboard_document(response)
    year_values = _dashboard_year_values(document)

    assert response.status_code == 200
    assert year_values == ["", "2023", "2025", "2026"]
    assert "2022" not in year_values
    assert "2024" not in year_values


def test_guest_dashboard_year_options_exclude_other_guest_years(
    flask_app,
    monkeypatch,
):
    monkeypatch.setattr(
        app_module,
        "business_today",
        lambda: datetime.date(2026, 6, 1),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_a_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    guest_b_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    guest_a_product = Product(
        dataset=guest_a_dataset,
        year=2024,
        month=3,
        name="Guest A 2024年商品",
        price=200,
    )
    guest_b_product = Product(
        dataset=guest_b_dataset,
        year=2025,
        month=3,
        name="Guest B 2025年商品",
        price=300,
    )
    db.session.add_all([
        guest_a_dataset,
        guest_b_dataset,
        guest_a_product,
        guest_b_product,
    ])
    db.session.flush()
    db.session.add_all([
        DailySales(
            product_id=guest_a_product.id,
            date=datetime.date(2024, 3, 1),
            quantity=1,
        ),
        DailySales(
            product_id=guest_b_product.id,
            date=datetime.date(2025, 3, 1),
            quantity=1,
        ),
    ])
    db.session.commit()

    guest_a_client = flask_app.test_client()
    with guest_a_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_a_dataset.id}"
        session_data["_fresh"] = True

    response = guest_a_client.get("/dashboard")
    year_values = _dashboard_year_values(_dashboard_document(response))

    assert response.status_code == 200
    assert year_values == ["", "2024", "2026"]
    assert "2025" not in year_values


def test_dashboard_selected_year_without_sales_stays_explicit(
    authenticated_client,
    dashboard_records,
    monkeypatch,
):
    monkeypatch.setattr(
        app_module,
        "business_today",
        lambda: datetime.date(2026, 6, 1),
    )

    response = authenticated_client.get("/dashboard?year=2024")
    document = _dashboard_document(response)
    selected_year = document.select_one("#selectYear option[selected]")
    period_title = document.select_one("#periodTitle")

    assert response.status_code == 200
    assert selected_year is not None
    assert selected_year["value"] == "2024"
    assert period_title.get_text(" ", strip=True) == "2024年・全月"
    assert "該当期間の売上データ（数量）が見つかりませんでした。" in (
        response.get_data(as_text=True)
    )


def test_all_year_period_ignores_month_for_html_api_and_ai(
    authenticated_client,
    dashboard_records,
    monkeypatch,
):
    captured_ai_sales = []

    def capture_ai_sales(ranked_sales, current_dataset=None):
        captured_ai_sales.extend(ranked_sales)
        return "期間整合性テスト"

    monkeypatch.setattr(
        app_module,
        "_generate_ai_advice",
        capture_ai_sales,
    )

    html_response = authenticated_client.get("/dashboard?month=8")
    document = _dashboard_document(html_response)
    month_select = document.select_one("#selectMonth")
    selected_month = month_select.select_one("option[selected]")

    api_response = authenticated_client.get(
        "/api/dashboard-data?month=8"
    )
    ai_response = post_ai(
        authenticated_client,
        "/api/ai-advice?month=8",
    )
    payload = api_response.get_json()

    assert html_response.status_code == 200
    assert api_response.status_code == 200
    assert ai_response.status_code == 200
    assert month_select.has_attr("disabled")
    assert selected_month is not None
    assert selected_month["value"] == ""
    assert document.select_one("#periodTitle").get_text(
        " ", strip=True
    ) == "全年度・全月"
    assert payload["period_text"] == "全年度・全月"
    assert payload["ranked_sales"] == [
        ["前年商品", 30],
        ["7月商品", 20],
        ["商品A", 10],
        ["商品B", 5],
    ]
    assert captured_ai_sales == [
        ("前年商品", 30),
        ("7月商品", 20),
        ("商品A", 10),
        ("商品B", 5),
    ]


def test_ai_advice_uses_applied_dashboard_period(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/dashboard")
    script_text = "\n".join(
        script.get_text()
        for script in _dashboard_document(response).find_all("script")
    )
    advice_source = script_text.split("function loadAiAdvice()", 1)[1]

    assert response.status_code == 200
    assert "activeYear" in advice_source
    assert "activeMonth" in advice_source
    assert "document.getElementById('selectYear').value" not in (
        advice_source
    )
    assert "document.getElementById('selectMonth').value" not in (
        advice_source
    )


def test_dashboard_marks_only_selected_year_current_dataset_sales_months(
    authenticated_client,
    dashboard_records,
    admin_dataset,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    guest_september_product = Product(
        dataset=guest_dataset,
        year=2026,
        month=9,
        name="Guest 9月商品",
        price=300,
    )
    admin_october_product_without_sales = Product(
        dataset=admin_dataset,
        year=2026,
        month=10,
        name="Admin 10月未入力商品",
        price=400,
    )
    admin_previous_year_november_product = Product(
        dataset=admin_dataset,
        year=2025,
        month=11,
        name="Admin前年11月商品",
        price=500,
    )
    admin_march_zero_sales_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=3,
        name="Admin 3月ゼロ売上商品",
        price=600,
    )
    db.session.add_all([
        guest_dataset,
        guest_september_product,
        admin_october_product_without_sales,
        admin_previous_year_november_product,
        admin_march_zero_sales_product,
    ])
    db.session.flush()
    db.session.add_all([
        DailySales(
            product_id=guest_september_product.id,
            date=datetime.date(2026, 9, 1),
            quantity=90,
        ),
        DailySales(
            product_id=admin_previous_year_november_product.id,
            date=datetime.date(2025, 11, 1),
            quantity=11,
        ),
        DailySales(
            product_id=admin_march_zero_sales_product.id,
            date=datetime.date(2026, 3, 1),
            quantity=0,
        ),
    ])
    db.session.commit()

    response = authenticated_client.get("/dashboard?year=2026")
    response_text = response.get_data(as_text=True)
    document = BeautifulSoup(response_text, "html.parser")
    month_labels = {
        int(option["value"]): option.get_text(" ", strip=True)
        for option in document.select("#selectMonth option[value]")
        if option["value"]
    }

    assert response.status_code == 200
    assert document.select_one("#periodTitle").get_text(
        " ", strip=True
    ) == "2026年・全月"
    assert month_labels[3] == "3月（登録済み）"
    assert month_labels[7] == "7月（登録済み）"
    assert month_labels[8] == "8月（登録済み）"
    assert month_labels[9] == "9月"
    assert month_labels[10] == "10月"
    assert month_labels[11] == "11月"
    assert "data.sales_months" in response_text

    api_response = authenticated_client.get(
        "/api/dashboard-data?year=2026"
    )
    assert api_response.status_code == 200
    api_payload = api_response.get_json()
    assert api_payload["sales_months"] == [3, 7, 8]
    assert ["前年商品", 30] not in api_payload["ranked_sales"]
    assert ["Admin前年11月商品", 11] not in api_payload["ranked_sales"]


def test_admin_dashboard_excludes_guest_dataset_sales(
    authenticated_client,
    cross_dataset_dashboard_records,
):
    response = authenticated_client.get(
        "/dashboard?year=2026&month=8"
    )
    response_text = response.get_data(as_text=True)
    document = _dashboard_document(response)

    assert response.status_code == 200
    assert document.select_one("#periodTitle").get_text(
        " ", strip=True
    ) == "2026年8月"
    assert "管理者限定商品" in response_text
    assert "11" in response_text
    assert "ゲスト限定商品" not in response_text
    assert "987654" not in response_text


def test_admin_dashboard_does_not_combine_same_name_across_datasets(
    authenticated_client,
    cross_dataset_dashboard_records,
    monkeypatch,
):
    captured_ranked_sales = []
    real_render_template = app_module.render_template

    def capture_dashboard_context(template_name, *args, **kwargs):
        if template_name == "dashboard.html":
            captured_ranked_sales.extend(kwargs["ranked_sales"])
        return real_render_template(template_name, *args, **kwargs)

    monkeypatch.setattr(
        app_module,
        "render_template",
        capture_dashboard_context,
    )

    response = authenticated_client.get(
        "/dashboard?year=2026&month=8"
    )

    assert response.status_code == 200
    assert dict(captured_ranked_sales)["クロワッサン"] == 10


def test_admin_dashboard_api_excludes_guest_dataset_sales(
    authenticated_client,
    cross_dataset_dashboard_records,
):
    response = authenticated_client.get(
        "/api/dashboard-data?year=2026&month=8"
    )
    payload = response.get_json()
    ranked_sales = dict(payload["ranked_sales"])
    chart_sales = dict(zip(
        payload["chart_labels"],
        payload["chart_values"],
    ))

    assert response.status_code == 200
    assert ranked_sales["管理者限定商品"] == 11
    assert ranked_sales["クロワッサン"] == 10
    assert "ゲスト限定商品" not in ranked_sales
    assert 987654 not in payload["chart_values"]
    assert chart_sales["管理者限定商品"] == 11
    assert chart_sales["クロワッサン"] == 10
    assert "ゲスト限定商品" not in payload["chart_labels"]


def test_guest_a_dashboard_api_excludes_guest_b_dataset_sales(
    flask_app,
):
    now = datetime.datetime.now(datetime.timezone.utc)

    guest_a_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    guest_b_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )

    guest_a_unique_product = Product(
        dataset=guest_a_dataset,
        year=2026,
        month=8,
        name="Guest A限定商品",
        price=100,
    )
    guest_a_same_name_product = Product(
        dataset=guest_a_dataset,
        year=2026,
        month=8,
        name="クロワッサン",
        price=200,
    )
    guest_b_unique_product = Product(
        dataset=guest_b_dataset,
        year=2026,
        month=8,
        name="Guest B限定商品",
        price=300,
    )
    guest_b_same_name_product = Product(
        dataset=guest_b_dataset,
        year=2026,
        month=8,
        name="クロワッサン",
        price=400,
    )

    db.session.add_all([
        guest_a_dataset,
        guest_b_dataset,
        guest_a_unique_product,
        guest_a_same_name_product,
        guest_b_unique_product,
        guest_b_same_name_product,
    ])
    db.session.flush()

    db.session.add_all([
        DailySales(
            product_id=guest_a_unique_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=11,
        ),
        DailySales(
            product_id=guest_a_same_name_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=10,
        ),
        DailySales(
            product_id=guest_b_unique_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=987654,
        ),
        DailySales(
            product_id=guest_b_same_name_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=90,
        ),
    ])
    db.session.commit()

    guest_a_client = flask_app.test_client()
    with guest_a_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_a_dataset.id}"
        session_data["_fresh"] = True

    response = guest_a_client.get(
        "/api/dashboard-data?year=2026&month=8"
    )

    payload = response.get_json()
    ranked_sales = dict(payload["ranked_sales"])
    chart_sales = dict(zip(
        payload["chart_labels"],
        payload["chart_values"],
    ))

    assert response.status_code == 200

    assert ranked_sales["Guest A限定商品"] == 11
    assert ranked_sales["クロワッサン"] == 10
    assert "Guest B限定商品" not in ranked_sales
    assert 987654 not in payload["chart_values"]

    assert chart_sales["Guest A限定商品"] == 11
    assert chart_sales["クロワッサン"] == 10
    assert "Guest B限定商品" not in payload["chart_labels"]


def test_guest_a_dashboard_html_excludes_guest_b_dataset_sales(
    flask_app,
    monkeypatch,
):
    now = datetime.datetime.now(datetime.timezone.utc)

    guest_a_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    guest_b_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )

    guest_a_unique_product = Product(
        dataset=guest_a_dataset,
        year=2026,
        month=8,
        name="Guest A限定商品",
        price=100,
    )
    guest_a_same_name_product = Product(
        dataset=guest_a_dataset,
        year=2026,
        month=8,
        name="クロワッサン",
        price=200,
    )
    guest_b_unique_product = Product(
        dataset=guest_b_dataset,
        year=2026,
        month=8,
        name="Guest B限定商品",
        price=300,
    )
    guest_b_same_name_product = Product(
        dataset=guest_b_dataset,
        year=2026,
        month=8,
        name="クロワッサン",
        price=400,
    )

    db.session.add_all([
        guest_a_dataset,
        guest_b_dataset,
        guest_a_unique_product,
        guest_a_same_name_product,
        guest_b_unique_product,
        guest_b_same_name_product,
    ])
    db.session.flush()

    db.session.add_all([
        DailySales(
            product_id=guest_a_unique_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=11,
        ),
        DailySales(
            product_id=guest_a_same_name_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=10,
        ),
        DailySales(
            product_id=guest_b_unique_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=987654,
        ),
        DailySales(
            product_id=guest_b_same_name_product.id,
            date=datetime.date(2026, 8, 1),
            quantity=90,
        ),
    ])
    db.session.commit()

    captured_ranked_sales = []
    real_render_template = app_module.render_template

    def capture_dashboard_context(template_name, *args, **kwargs):
        if template_name == "dashboard.html":
            captured_ranked_sales.extend(kwargs["ranked_sales"])
        return real_render_template(template_name, *args, **kwargs)

    monkeypatch.setattr(
        app_module,
        "render_template",
        capture_dashboard_context,
    )

    guest_a_client = flask_app.test_client()
    with guest_a_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_a_dataset.id}"
        session_data["_fresh"] = True

    response = guest_a_client.get(
        "/dashboard?year=2026&month=8"
    )
    response_text = response.get_data(as_text=True)
    ranked_sales = dict(captured_ranked_sales)

    assert response.status_code == 200

    assert ranked_sales["Guest A限定商品"] == 11
    assert ranked_sales["クロワッサン"] == 10
    assert "Guest B限定商品" not in ranked_sales

    assert "Guest A限定商品" in response_text
    assert "Guest B限定商品" not in response_text
    assert "987654" not in response_text


def test_dashboard_api_returns_sales_aggregation_for_selected_period(
    authenticated_client,
    dashboard_records,
):
    products_before = _product_snapshot()
    sales_before = _sales_snapshot()

    response = authenticated_client.get(
        "/api/dashboard-data?year=2026&month=8"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert response.is_json
    assert set(payload) == {
        "today_sales",
        "business_date",
        "ranked_sales",
        "chart_labels",
        "chart_values",
        "sales_months",
        "ai_advice",
        "period_text",
    }
    assert payload["ranked_sales"] == [
        ["商品A", 10],
        ["商品B", 5],
    ]
    assert payload["ai_advice"] == FIXED_AI_ADVICE
    assert payload["period_text"] == "2026年8月"
    assert payload["sales_months"] == [7, 8]
    assert _product_snapshot() == products_before
    assert _sales_snapshot() == sales_before


def test_dashboard_api_chart_matches_ranked_sales(
    authenticated_client,
    dashboard_records,
):
    response = authenticated_client.get(
        "/api/dashboard-data?year=2026&month=8"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ranked_sales"] == [
        ["商品A", 10],
        ["商品B", 5],
    ]
    assert payload["chart_labels"] == ["商品A", "商品B"]
    assert payload["chart_values"] == [10, 5]
    assert [item[0] for item in payload["ranked_sales"]] == payload[
        "chart_labels"
    ]
    assert [item[1] for item in payload["ranked_sales"]] == payload[
        "chart_values"
    ]


def test_dashboard_api_keeps_historical_sales_for_inactive_products(
    authenticated_client,
    dashboard_records,
):
    product_b = db.session.get(Product, dashboard_records["product_b_id"])
    product_b.is_active = False
    db.session.commit()
    products_before = _product_snapshot()
    sales_before = _sales_snapshot()

    response = authenticated_client.get(
        "/api/dashboard-data?year=2026&month=8"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert ["商品B", 5] in payload["ranked_sales"]
    assert db.session.get(Product, product_b.id).is_active is False
    assert _product_snapshot() == products_before
    assert _sales_snapshot() == sales_before


def test_dashboard_api_returns_all_periods_without_filters(
    authenticated_client,
    dashboard_records,
):
    response = authenticated_client.get("/api/dashboard-data")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ranked_sales"] == [
        ["前年商品", 30],
        ["7月商品", 20],
        ["商品A", 10],
        ["商品B", 5],
    ]
    assert payload["chart_labels"] == [
        "前年商品",
        "7月商品",
        "商品A",
        "商品B",
    ]
    assert payload["chart_values"] == [30, 20, 10, 5]
    assert payload["period_text"] == "全年度・全月"


@pytest.mark.parametrize(
    ("route", "invalid_query"),
    [
        pytest.param("/dashboard", "year=abc", id="dashboard-invalid-year"),
        pytest.param("/dashboard", "month=abc", id="dashboard-invalid-month"),
        pytest.param(
            "/api/dashboard-data",
            "year=abc",
            id="dashboard-api-invalid-year",
        ),
        pytest.param(
            "/api/dashboard-data",
            "month=abc",
            id="dashboard-api-invalid-month",
        ),
        pytest.param(
            "/api/ai-advice",
            "year=abc",
            id="ai-advice-api-invalid-year",
        ),
        pytest.param(
            "/api/ai-advice",
            "month=abc",
            id="ai-advice-api-invalid-month",
        ),
    ],
)
def test_dashboard_routes_reject_noninteger_query_with_bad_request(
    authenticated_client,
    flask_app,
    monkeypatch,
    route,
    invalid_query,
):
    gemini_client = Mock()
    monkeypatch.setattr(app_module.genai, "Client", gemini_client)
    monkeypatch.setitem(flask_app.config, "PROPAGATE_EXCEPTIONS", False)

    response = (
        post_ai(authenticated_client, f"{route}?{invalid_query}")
        if route == "/api/ai-advice"
        else authenticated_client.get(f"{route}?{invalid_query}")
    )

    if route == "/api/ai-advice":
        assert response.status_code == 400 and not gemini_client.called, (
            f"status={response.status_code}, "
            f"gemini_calls={gemini_client.call_count}"
        )
    else:
        assert response.status_code == 400


@pytest.mark.parametrize(
    ("route", "invalid_month"),
    [
        pytest.param("/dashboard", "0", id="dashboard-month-zero"),
        pytest.param(
            "/api/dashboard-data",
            "13",
            id="dashboard-api-month-thirteen",
        ),
        pytest.param(
            "/api/ai-advice",
            "-1",
            id="ai-advice-negative-month",
        ),
    ],
)
def test_dashboard_routes_reject_out_of_range_month(
    authenticated_client,
    admin_dataset,
    flask_app,
    monkeypatch,
    route,
    invalid_month,
):
    gemini_client = Mock()
    monkeypatch.setattr(app_module.genai, "Client", gemini_client)
    monkeypatch.setitem(flask_app.config, "PROPAGATE_EXCEPTIONS", False)

    path = f"{route}?year=2026&month={invalid_month}"
    response = (
        post_ai(authenticated_client, path)
        if route == "/api/ai-advice"
        else authenticated_client.get(path)
    )

    assert response.status_code == 400
    if route == "/api/ai-advice":
        assert not gemini_client.called


@pytest.mark.parametrize("period", ["", "?year=2025", "?year=2026&month=8"])
@pytest.mark.parametrize("day, expected", [(1, 1300), (2, 700), (3, 0)])
def test_today_sales_is_independent_of_analysis_period(
    authenticated_client, dashboard_records, monkeypatch, period, day, expected,
):
    monkeypatch.setattr(app_module, "business_today", lambda: datetime.date(2026, 8, day))
    response = authenticated_client.get("/dashboard" + period)
    assert response.status_code == 200
    document = _dashboard_document(response)
    assert document.select_one("#todaySalesAmount").get_text(strip=True) == f"¥{expected:,}"
    payload = authenticated_client.get("/api/dashboard-data" + period).get_json()
    assert payload["today_sales"] == expected
    assert payload["business_date"] == f"2026-08-{day:02d}"


@pytest.mark.parametrize("identity", ["admin", "guest", "other_guest"])
def test_today_sales_preserves_dataset_boundary(
    flask_app, cross_dataset_dashboard_records, monkeypatch, identity,
):
    monkeypatch.setattr(app_module, "business_today", lambda: datetime.date(2026, 8, 1))
    client = flask_app.test_client()
    with client.session_transaction() as session:
        from conftest import establish_test_admin_session
        establish_test_admin_session(session)
        session["_user_id"] = "admin"
        session["_fresh"] = True
        session[app_module.ADMIN_AUTH_FINGERPRINT_SESSION_KEY] = app_module._get_admin_auth_fingerprint(flask_app.config["ADMIN_PASSWORD_HASH"])
    expected = 3100
    if identity != "admin":
        dataset = Dataset.query.filter_by(kind="guest").one()
        if identity == "other_guest":
            now = datetime.datetime.now(datetime.timezone.utc)
            dataset = Dataset(kind="guest", created_at=now, last_activity_at=now,
                              absolute_expires_at=now + datetime.timedelta(hours=2))
            db.session.add(dataset)
            db.session.commit()
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = f"guest:{dataset.id}"
            session["_fresh"] = True
        expected = 987654 * 300 + 90 * 400 if identity == "guest" else 0
    response = client.get("/dashboard?year=2025")
    assert response.status_code == 200
    assert _dashboard_document(response).select_one("#todaySalesAmount").get_text(strip=True) == f"¥{expected:,}"
    assert client.get("/api/dashboard-data?year=2025").get_json()["today_sales"] == expected


def test_today_sales_includes_inactive_products_and_large_amounts(
    authenticated_client, dashboard_records, monkeypatch,
):
    monkeypatch.setattr(app_module, "business_today", lambda: datetime.date(2026, 8, 1))
    product = db.session.get(Product, dashboard_records["product_a_id"])
    product.price = 1_000_000
    product.is_active = False
    sale = DailySales.query.filter_by(product_id=product.id, date=datetime.date(2026, 8, 1)).one()
    sale.quantity = 100_000
    db.session.commit()
    assert authenticated_client.get("/api/dashboard-data").get_json()["today_sales"] == 100_000_001_000
