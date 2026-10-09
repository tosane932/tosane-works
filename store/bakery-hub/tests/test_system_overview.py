import ast
import datetime
from pathlib import Path
from urllib.parse import urlsplit

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
    "/", "/products", "/input", "/dashboard", "/material-orders",
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
        "/", "/products", "/input", "/dashboard", "/material-orders", "/system-overview",
    ]
    assert len(navigation.select('a[aria-current="page"]')) == 1


def test_overview_has_readable_sections_and_valid_internal_links(client):
    document = _document(client.get("/system-overview"))
    main = document.select_one("main")
    for section_id in ("flow", "features", "basics", "technologies", "tests"):
        assert main.select_one(f"section#{section_id} > h2")
    for feature_id in (
        "products", "product-catalog", "sales", "dashboard", "materials", "memos", "tasks",
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


def test_overview_explains_catalog_vs_monthly_product_and_history(client):
    document = _document(client.get('/system-overview'))
    registration = document.select_one('#feature-products').get_text(' ', strip=True)
    listing = document.select_one('#feature-product-catalog').get_text(' ', strip=True)
    for phrase in ('18品', '初期価格', '自由入力', '0〜9,990円', 'request.files',
                   'GitHub API', 'PR', 'PostgreSQL', 'DailySales', '画像なし'):
        assert phrase in registration
    for phrase in ('今月登録の商品', '過去に登録された商品', '最終登録年月',
                   'business_today()', 'Dataset', 'DailySales'):
        assert phrase in listing


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


def test_learning_sections_have_headings_and_contents_links(client):
    document = _document(client.get("/system-overview"))
    contents = document.select_one('nav[aria-label="このページの目次"]')
    for section_id, title in (
        ("walkthrough", "1つの処理を最後まで追ってみる"),
        ("quiz", "理解度チェック"),
        ("troubleshooting", "困ったときはどこを見る？"),
    ):
        section = document.select_one(f"main section#{section_id}")
        assert section is not None
        assert title in section.select_one("h2").get_text()
        assert contents.select_one(f'a[href="#{section_id}"]')


def test_sales_walkthrough_explains_actual_save_and_dashboard_path(client):
    document = _document(client.get("/system-overview"))
    steps = document.select("#walkthrough ol > li")
    assert len(steps) == 6
    for step, title in zip(steps, (
        "売上入力画面を開く", "入力して送信する", "Pythonで入力を確認する",
        "DBへ保存する", "保存結果からDashboardへ進む", "Dashboardで表示する",
    )):
        assert title in step.select_one("h3").get_text()
        assert step.select_one("details > summary").get_text() == "処理と基礎を読む"
        assert "ここで学べる基礎" in step.get_text()
        assert step.select_one(".overview-foundations code")
    assert "GET /input" in steps[0].get_text()
    assert "POST /input" in steps[1].get_text()
    assert "request.form" in steps[1].get_text()
    assert "Dataset" in steps[2].get_text()
    assert "db.session.commit()" in steps[3].get_text()
    assert "rollback()" in steps[3].get_text()
    assert "PostgreSQL" in steps[3].get_text()
    assert "リダイレクトはしません" in steps[4].get_text()
    assert "success=True" in steps[4].get_text()
    assert "GET /dashboard" in steps[4].get_text()
    assert "db.func.sum()" in steps[5].get_text()
    assert "sorted()" in steps[5].get_text()
    assert "/api/dashboard-data" in steps[5].get_text()
    assert steps[0].select_one('a[href$="/templates/input.html"]')
    assert steps[3].select_one('a[href$="/app.py"]')
    assert steps[5].select_one('a[href$="/templates/dashboard.html"]')


def test_quiz_keeps_eight_explained_answers_in_closed_native_details(client):
    document = _document(client.get("/system-overview"))
    questions = document.select("#quiz article")
    assert len(questions) == 8
    for number, question in enumerate(questions, start=1):
        assert question["id"] == f"quiz-q{number}"
        assert question.select_one("h3").get_text().startswith(f"Q{number}.")
        details = question.select_one("details")
        assert details is not None
        assert not details.has_attr("open")
        assert details.select_one("summary").get_text() == "答えを見る"
        answer = details.select_one(".overview-answer")
        assert answer is not None
        assert len(answer.get_text(strip=True)) >= 50
    for question, expected in zip(questions, (
        "request.form", "GET", "db.session.commit()", "return",
        "Model", "Jinja", "rollback", "Flask-Login",
    )):
        assert expected in question.select_one(".overview-answer").get_text()
    # Questions are ordinary headings; answer text belongs only to the details.
    assert all(q.select_one("h3").find_parent("details") is None for q in questions)
    assert document.select("script[src]") == []


def test_troubleshooting_maps_real_symptoms_to_places_and_reasons(client):
    document = _document(client.get("/system-overview"))
    cards = document.select("#troubleshooting article")
    assert len(cards) == 8
    for card in cards:
        assert card.select_one("h3")
        assert card.select_one("details > summary").get_text() == "確認場所と理由を見る"
        assert card.select_one("code")
        assert card.select_one("details p")
        assert card.select_one('a[href^="#feature-"]')
    csrf_card = document.select_one("#trouble-post")
    assert "400" in csrf_card.select_one("h3").get_text()
    assert "CSRF" in csrf_card.get_text()
    assert "入力検証" in csrf_card.get_text()
    assert "business_today()" in document.select_one("#trouble-date").get_text()
    assert "require_current_dataset()" in document.select_one("#trouble-dataset").get_text()
    assert "fixture" in document.select_one("#trouble-pytest").get_text()


PYTHON_CATEGORIES = (
    "values", "conditions", "loops", "structures", "functions", "classes",
    "exceptions", "iteration", "strings", "state", "modules", "stdlib",
    "regex", "paths", "context", "decorators", "dates",
)


@pytest.mark.parametrize("category", PYTHON_CATEGORIES)
def test_python_map_has_grouped_lessons_with_purpose_and_code_address(client, category):
    document = _document(client.get("/system-overview"))
    card = document.select_one(f"#basics article#python-{category}")
    assert card is not None
    assert card.select_one("h3")
    assert card.select_one("details > summary")
    assert not card.select_one("details").has_attr("open")
    assert document.select_one(
        f'nav[aria-label="Python基礎・応用マップの目次"] a[href="#python-{category}"]'
    )
    lessons = card.select(".python-lesson")
    assert lessons
    for lesson in lessons:
        assert lesson.select_one("h4")
        labels = [item.get_text(strip=True) for item in lesson.select("dt")]
        assert labels == ["これは何か", "Bakery Hubでは", "なぜ使うか", "教材の住所"]
        assert all(item.get_text(strip=True) for item in lesson.select("dd"))
        assert lesson.select_one("a[data-code-address]")
        assert lesson.select_one('a[href^="#feature-"]')


def _source_definitions(source):
    """Nested Blueprint handlers and class methods also have a real address."""
    definitions = {}

    def visit(node, prefix=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            prefix = f"{prefix}.{node.name}" if prefix else node.name
            definitions[prefix] = node
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(ast.parse(source))
    return definitions


def test_python_code_links_resolve_to_existing_files_definitions_and_lines(client):
    document = _document(client.get("/system-overview"))
    app_root = Path(app_module.__file__).parent.resolve()
    links = document.select("#basics a[data-code-address]")
    assert len(links) >= len(PYTHON_CATEGORIES)
    for link in links:
        path = (app_root / link["data-source-file"]).resolve()
        assert path.is_relative_to(app_root)
        assert path.is_file()
        source = path.read_text(encoding="utf-8")
        definition = _source_definitions(source)[link["data-source-symbol"]]
        url = urlsplit(link["href"])
        assert url.scheme == "https"
        assert url.netloc == "github.com"
        assert url.path == (
            "/tosane932/tosane-works/blob/main/store/bakery-hub/"
            + link["data-source-file"]
        )
        assert url.fragment == f"L{definition.lineno}"
        assert link["data-source-file"] in link.get_text()
        assert link["data-source-symbol"] in link.get_text()
        assert link["data-source-scope"] in {"業務処理", "テスト", "初期データ作成", "migration"}


def test_python_snippets_are_short_actual_source_without_template_execution(client):
    document = _document(client.get("/system-overview"))
    app_root = Path(app_module.__file__).parent
    snippets = document.select("#basics .python-source-snippet code")
    assert snippets
    for snippet in snippets:
        lesson = snippet.find_parent(class_="python-lesson")
        link = lesson.select_one("a[data-code-address]")
        source = (app_root / link["data-source-file"]).read_text(encoding="utf-8")
        definition = _source_definitions(source)[link["data-source-symbol"]]
        excerpt = ast.get_source_segment(source, definition)
        text = snippet.get_text()
        assert 1 <= len(text.splitlines()) <= 8
        assert text in excerpt


def test_python_map_distinguishes_builtin_calls_from_database_and_test_tools(client):
    document = _document(client.get("/system-overview"))
    # all() and range() were found in tests; query.all() is a SQLAlchemy method.
    all_lesson = document.select_one("#python-all-range")
    assert "テスト" in all_lesson.get_text()
    assert all(
        link["data-source-scope"] == "テスト"
        for link in all_lesson.select("a[data-code-address]")
    )
    assert "query.all()" in all_lesson.get_text()
    hints = document.select_one("#python-type-hints").get_text()
    assert "prompts.py" in hints
    assert "全体" in hints and "入力検証" in hints
    assert "SQLAlchemy" in document.select_one("#python-models").get_text()
    context_text = document.select_one("#python-context").get_text()
    assert "_serialize_admin_login_attempt" in context_text
    assert "flask_app" in context_text
    assert "大量" in context_text  # Do not describe these yield uses as CSV streaming.


def test_python_map_excludes_unimplemented_features_and_preserves_web_connections(client):
    document = _document(client.get("/system-overview"))
    text = document.select_one("#basics").get_text()
    for unused in ("dataclass", "Enum", "csv", "Counter", "defaultdict", "TypedDict", "match / case", "cache"):
        assert unused not in text
    web = document.select_one("#web-connections")
    assert web is not None
    for term in ("Python", "Flask", "SQLAlchemy", "JavaScript", "request.form", "fetch", "GET", "POST", "CRUD"):
        assert term in web.get_text()
    assert document.select_one('#basics a[href="#python-help"]')
    assert document.select_one('#python-help a[href="#python-functions"]')
    assert document.select_one('#python-help a[href="#python-state"]')


def test_python_quiz_adds_fifteen_closed_answers_with_links_back_to_lessons(client):
    document = _document(client.get("/system-overview"))
    questions = document.select("#python-quiz article")
    assert len(questions) == 15
    contents = document.select_one('nav[aria-label="このページの目次"]')
    assert contents.select_one('a[href="#python-quiz"]')
    for number, question in enumerate(questions, start=1):
        assert question["id"] == f"python-q{number}"
        assert question.select_one("h3").get_text().startswith(f"Q{number}.")
        details = question.select_one("details")
        assert not details.has_attr("open")
        assert details.select_one("summary").get_text() == "答えを見る"
        answer = details.select_one(".overview-answer")
        assert len(answer.get_text(strip=True)) >= 50
        link = answer.select_one('a[href^="#python-"]')
        assert document.select_one(link["href"])
        assert question.select_one("h3").find_parent("details") is None
    assert len(document.select("#quiz article")) == 8
    assert len(document.select("#troubleshooting article")) == 8


def test_overview_ids_are_unique_and_every_internal_anchor_resolves(client):
    document = _document(client.get("/system-overview"))
    ids = [element["id"] for element in document.select("[id]")]
    assert len(ids) == len(set(ids))
    for link in document.select('main a[href^="#"]'):
        assert link["href"][1:] in ids
