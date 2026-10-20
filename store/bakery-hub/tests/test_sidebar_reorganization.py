"""v3.1の入口統合と既存の権限・公開条件を守る回帰テスト。"""
import datetime

import pytest
from bs4 import BeautifulSoup
from flask import g

import app as app_module
from models import Dataset, RegisterCutover, RegisterProduct, Product, db


def guest_client(app):
    now = datetime.datetime.now(datetime.timezone.utc)
    owner = Dataset(kind='guest', system_key=None, created_at=now,
                    last_activity_at=now, absolute_expires_at=now + datetime.timedelta(hours=1))
    db.session.add(owner)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = f'guest:{owner.id}'
        session['_fresh'] = True
    return client, owner


def page(client, path):
    g.pop('_login_user', None)
    response = client.get(path)
    assert response.status_code == 200
    return BeautifulSoup(response.data, 'html.parser')


@pytest.mark.parametrize('guest', [False, True])
@pytest.mark.parametrize('products_enabled', [False, True])
def test_primary_entries_grouped_and_legacy_registration_reachable(
    flask_app, authenticated_client, admin_dataset, guest, products_enabled,
):
    flask_app.config['REGISTER_V3_PRODUCTS_PREVIEW_ENABLED'] = products_enabled
    client = guest_client(flask_app)[0] if guest else authenticated_client
    doc = page(client, '/products')
    nav = doc.select_one('#app-sidebar nav')
    entries = nav.select(':scope > a[data-page], .app-sidebar-home-row [data-page]')
    assert [e['data-page'] for e in entries] == [
        'home', 'product-catalog', 'sales', 'dashboard', 'shop-tools',
        *([] if guest else ['staff-shifts', 'prep']), 'news', 'system-overview',
    ]
    assert len(nav.select('.app-sidebar-nav-divider')) == 3
    assert nav.select_one('[data-page="product-catalog"][aria-current="page"]')
    assert not nav.select('[data-page="products"], [data-page="register-products"]')
    if products_enabled and not guest:
        assert doc.select_one('a[href="/register/products/new"]') is None  # write flag still OFF
        assert 'この環境では商品を変更できません' in doc.get_text()
    else:
        assert doc.select_one('main a[href="/"]')  # old monthly registration still reachable
        assert page(client, '/').select_one('form')
    assert Product.query.count() == RegisterProduct.query.count() == 0


@pytest.mark.parametrize('active', [False, True])
def test_sales_entry_uses_existing_cutover_without_enabling_it(
    flask_app, authenticated_client, admin_dataset, active,
):
    flask_app.config.update(REGISTER_V3_PRODUCTION_ENABLED=True, REGISTER_V3_PREVIEW_ENABLED=True)
    if active:
        db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                      first_register_date=app_module.business_today()))
        db.session.commit()
    doc = page(authenticated_client, '/home')
    link = doc.select_one('.app-sidebar-link-sales')
    assert link['href'] == ('/register/preview' if active else '/input')
    assert link.get_text(strip=True) == ('レジモード' if active else '日次売上入力')
    work_link = doc.select_one('.operations-work-sales')
    assert work_link['href'] == link['href']
    assert work_link.get_text(' ', strip=True).find('レジモード' if active else '日次売上入力') >= 0
    assert flask_app.config['REGISTER_V3_SAVE_ENABLED'] is False
    assert RegisterCutover.query.count() == int(active)


def test_admin_tools_collapsed_and_hidden_from_guest(flask_app, authenticated_client, admin_dataset):
    flask_app.config.update(REGISTER_V3_PUBLIC_DEMO_ENABLED=True,
                           REGISTER_V3_READINESS_ENABLED=True, REGISTER_V3_RECOVERY_ENABLED=True)
    doc = page(authenticated_client, '/home')
    tools = doc.select_one('#app-register-management')
    assert tools is not None and not tools.has_attr('open')
    assert tools.select_one('summary').get_text(strip=True) == 'レジ管理・検証'
    assert {a['href'] for a in tools.select('a')} == {
        '/register/demo', '/register/readiness', '/register/recovery',
    }
    guest, _ = guest_client(flask_app)
    doc = page(guest, '/home')
    assert doc.select_one('#app-register-management') is None
    for route in ('/register/demo', '/register/readiness', '/register/recovery'):
        g.pop('_login_user', None)
        assert guest.get(route).status_code == 403


@pytest.mark.parametrize('guest', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
def test_combined_news_preserves_get_handoff_and_role_gate(
    flask_app, authenticated_client, admin_dataset, monkeypatch, guest, enabled,
):
    monkeypatch.setitem(flask_app.config, 'PUOPPO_INTEGRATION_ENABLED', enabled)
    articles = [{'title': '<パンの公式情報>', 'published': '2026-10-20T00:00:00+00:00',
                 'source': '農林水産省', 'url': 'https://www.maff.go.jp/j/press/example.html'}]
    calls = []
    monkeypatch.setattr(app_module, 'get_news', lambda limit: calls.append(limit) or articles)
    monkeypatch.setattr(app_module.genai, 'Client', lambda **kw: pytest.fail('GET must not invoke Gemini'))
    client = guest_client(flask_app)[0] if guest else authenticated_client
    doc = page(client, '/bakery-news')
    assert doc.select_one('h1').get_text(strip=True) == 'ニュース・AI分析'
    assert doc.select_one('[data-page="news"][aria-current="page"]')
    assert doc.select_one('section[aria-label="記事一覧"] a')['href'] == articles[0]['url']
    assert doc.select_one('time')['datetime'] == articles[0]['published']
    assert '<パンの公式情報>' in doc.get_text() and not doc.select('パンの公式情報')
    form = doc.select_one('#puoppo form')
    if enabled and not guest:
        assert form['method'].upper() == 'GET'
        assert form['action'] == 'https://puoppo.onrender.com/'
        assert form.select_one('input[name="keyword"]')['maxlength'] == '100'
        assert 'ベーカリー パン ベーグル 最新トレンド' in form.get_text() or form.select_one('input')['placeholder'] == 'ベーカリー パン ベーグル 最新トレンド'
    else:
        assert form is None
        assert '利用できません' in doc.select_one('#puoppo').get_text()
    assert doc.select_one('#puoppo a[href="https://puoppo.onrender.com/"]')
    assert calls == [15]


def test_tools_and_home_children_remain_reachable(authenticated_client, admin_dataset):
    doc = page(authenticated_client, '/shop-tools/memo')
    nav = doc.select_one('#app-sidebar')
    for route in ('/material-orders', '/shop-tools/memo', '/shop-tools/tasks', '/schedule'):
        assert nav.select_one(f'a[href="{route}"]')
    for anchor in ('/home#weather', '/home#next'):
        assert nav.select_one(f'a[href="{anchor}"]')


def test_news_anonymous_redirect_and_failure_has_no_fake_articles(client, authenticated_client, admin_dataset, monkeypatch):
    g.pop('_login_user', None)
    assert client.get('/bakery-news').status_code == 302
    monkeypatch.setattr(app_module, 'get_news', lambda limit: [])
    doc = page(authenticated_client, '/bakery-news')
    assert not doc.select('section[aria-label="記事一覧"] li')
    assert '最近の関連する記事はありません' in doc.get_text()
    assert doc.select_one('#puoppo')
