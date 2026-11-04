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
    assert [item["href"] for item in navigation.select("a[data-page]")] == [
        "/home", "/products", "/input", "/dashboard", "/material-orders", "/staff-shifts", "/prep", "/bakery-news", "/system-overview",
    ]
    assert navigation.select_one('a[href="/schedule"]') is not None
    assert len(navigation.select('a[aria-current="page"]')) == 1


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


def test_overview_ids_are_unique_and_every_internal_anchor_resolves(client):
    document = _document(client.get("/system-overview"))
    ids = [element["id"] for element in document.select("[id]")]
    assert len(ids) == len(set(ids))
    for link in document.select('main a[href^="#"]'):
        assert link["href"][1:] in ids


def test_overview_loads_only_its_local_deferred_navigation_script(client):
    document = _document(client.get("/system-overview"))
    scripts = document.select("script[src]")
    assert [script["src"] for script in scripts] == [
        "/static/system_overview.js", "/static/system_overview_scrubber.js",
    ]
    for script in scripts:
        assert script.has_attr("defer")
        script_response = client.get(script["src"])
        assert script_response.status_code == 200
        assert "javascript" in script_response.content_type


@pytest.mark.parametrize("path", ["/", "/products", "/input", "/dashboard", "/material-orders", "/shop-tools/memo", "/shop-tools/tasks"])
def test_chapter_navigation_does_not_appear_on_business_pages(authenticated_client, admin_dataset, path):
    document = _document(authenticated_client.get(path))
    assert document.select_one("#overview-reader-navigation") is None
    assert document.select_one('script[src="/static/system_overview.js"]') is None
    assert document.select_one('script[src="/static/system_overview_scrubber.js"]') is None


def test_scrubber_refinement_preserves_hit_area_and_chapter_navigation():
    style = (Path(app_module.app.root_path) / "static" / "style.css").read_text()
    scrubber = (Path(app_module.app.root_path) / "static" / "system_overview_scrubber.js").read_text()
    overview = (Path(app_module.app.root_path) / "static" / "system_overview.js").read_text()

    thumb = style.split(".system-overview-page .overview-scrubber-thumb {", 1)[1].split("}", 1)[0]
    mark = style.split(".system-overview-page .overview-scrubber-thumb::before {", 1)[1].split("}", 1)[0]
    assert "width: 44px;" in thumb and "height: 44px;" in thumb
    assert "width: 14px;" in mark and "height: 22px;" in mark
    assert "border-radius: 999px;" in mark
    assert ".overview-chapter-scrubber.is-dragging .overview-scrubber-thumb::before" in style
    assert "outline: none;" in style.split(".overview-scrubber-thumb:focus-visible {", 1)[1].split("}", 1)[0]
    assert "visibleFor = 1600" in scrubber
    assert "if (commit && moved) jumpTo(index)" in scrubber
    assert "reader.offsetTop + button.offsetHeight" in overview

CHAPTERS = ('about', 'features', 'architecture', 'safety', 'textbook')


def test_concise_guide_preserves_readable_contents_and_reader_controls(client):
    document = _document(client.get('/system-overview'))
    chapters = document.select('main > section[data-overview-chapter]')
    assert [node['id'] for node in chapters] == list(CHAPTERS)
    for number, node in enumerate(chapters, 1):
        assert node['data-overview-chapter'] == str(number)
        assert node.h2['tabindex'] == '-1'
        assert node.h2.get_text().startswith(f'{number}.')
    contents = document.select('nav[aria-label="このページの目次"]')
    assert len(contents) == 1
    assert [a['href'] for a in contents[0].select('a[data-overview-link]')] == [f'#{c}' for c in CHAPTERS]
    assert not contents[0].select('[aria-current]')
    assert not document.select('main details, #quiz, #python-quiz')
    assert '理解度チェック' not in document.select_one('main').get_text()
    assert 'Python基礎チェック' not in document.select_one('main').get_text()
    assert document.select_one('#overview-reader-navigation').has_attr('hidden')
    assert not document.select_one('#overview-page-contents').has_attr('hidden')
    assert document.select_one('[data-overview-close]').has_attr('hidden')
    button = document.select_one('#overview-location-button')
    assert button['aria-expanded'] == 'false'
    assert button['aria-controls'] == 'overview-page-contents'
    backdrop = document.select_one('#overview-contents-backdrop')
    assert backdrop.has_attr('hidden') and backdrop['aria-label'] == '目次を閉じる'
    thumb = document.select_one('#overview-chapter-scrubber [role=slider]')
    assert thumb['tabindex'] == '-1'
    assert thumb['aria-valuemax'] == str(len(CHAPTERS))
    assert thumb['aria-orientation'] == 'vertical'
    assert document.select_one('[data-scrubber-preview]').has_attr('hidden')


def test_publication_status_and_pdf_are_real_readable_and_public(client):
    from io import BytesIO
    from pypdf import PdfReader
    document = _document(client.get('/system-overview'))
    text = document.select_one('main').get_text()
    for phrase in ('本番未公開', 'NO-GO', 'Guest', '2026年11月4日', '6f61e12'):
        assert phrase in text
    link = document.select_one('a[href$="bakery-hub-system-guide.pdf"]')
    response = client.get(link['href'])
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    pdf = PdfReader(BytesIO(response.data))
    assert len(pdf.pages) >= 10
    all_text = '\n'.join(p.extract_text() for p in pdf.pages)
    for phrase in ('システム教科書', '第8章', 'Dataset', 'submission_key', '本番未公開', 'NO-GO'):
        assert phrase in all_text
    assert '答えを見る' not in all_text
    for page in pdf.pages:
        assert abs(float(page.mediabox.width) - 595.28) < 1
        assert abs(float(page.mediabox.height) - 841.89) < 1


def test_scrubber_adapts_to_document_chapter_count():
    source = (Path(app_module.app.root_path) / 'static/system_overview_scrubber.js').read_text()
    assert 'chapters.length !== 9' not in source
    assert 'chapters.length !== links.length' in source
    assert 'String(chapters.length)' in source
    assert source.index('const hadFocus = document.activeElement === thumb') < source.index('if (!enabled)')
    assert 'else if (hadFocus)' in source


def test_moved_code_addresses_and_excerpt_still_match_actual_source():
    root = Path(app_module.app.root_path)
    import json
    addresses = json.loads((root / 'docs/system-guide/code-addresses.json').read_text())
    assert len(addresses) >= 17
    def definitions(source):
        result = set()
        def visit(node, prefix=''):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                prefix = f'{prefix}.{node.name}' if prefix else node.name
                result.add(prefix)
            for child in ast.iter_child_nodes(node):
                visit(child, prefix)
        visit(ast.parse(source))
        return result
    for address in addresses:
        file = (root / address['file']).resolve()
        assert file.is_relative_to(root.resolve())
        assert address['symbol'] in definitions(file.read_text())
    excerpt = 'previous = _existing(owner.id, key, digest)\n        if previous is not None:\n            db.session.commit()\n            return previous'
    assert excerpt in (root / 'register_service.py').read_text()
