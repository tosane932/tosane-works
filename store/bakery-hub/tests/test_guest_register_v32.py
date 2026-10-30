"""Guest公開とAdmin本番ゲートの独立、認証主体ごとのデータ保護。"""
import datetime
import io
import json
import uuid

import pytest
from bs4 import BeautifulSoup
from flask import g
from PIL import Image

import app as app_module
from models import Dataset, RegisterCutover, RegisterProduct, RegisterTransaction, db
from register_products import ensure_standard_products
from tests.test_register_routes import preview, checkout, guest

def token_and_product(response):
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    return (page.select_one('meta[name="csrf-token"]')["content"],
            json.loads(page.select_one("#register-config").string), page)


def start(flask_app):
    client = flask_app.test_client()
    g.pop('_login_user', None)
    g.pop('csrf_token', None)
    page = BeautifulSoup(client.get('/login').text, 'html.parser')
    token = page.select_one('input[name="csrf_token"]')['value']
    assert client.post('/guest/start', data={'csrf_token': token}).status_code == 303
    with client.session_transaction() as cookie:
        owner = db.session.get(Dataset, uuid.UUID(cookie['_user_id'].split(':')[1]))
    return owner, client


def test_guest_start_seeds_atomically_and_admin_remains_closed(flask_app, authenticated_client, admin_dataset):
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    owner, client = start(flask_app)
    assert db.session.get(RegisterCutover, owner.id) is not None
    products = RegisterProduct.query.filter_by(dataset_id=owner.id).all()
    assert products and all(p.image_key for p in products)
    assert ensure_standard_products(owner) == 0
    assert RegisterProduct.query.filter_by(dataset_id=admin_dataset.id).count() == 0
    assert preview(authenticated_client).status_code == 404
    token, config, _ = token_and_product(preview(client))
    assert config['saveEnabled'] is True
    p = next(p for p in products if p.price_yen > 0)
    key = str(uuid.uuid4())
    for _ in range(2):
        assert checkout(client, p, token, submission_key=key).status_code == 200
    assert RegisterTransaction.query.filter_by(dataset_id=owner.id).count() == 1
    assert RegisterTransaction.query.filter_by(dataset_id=admin_dataset.id).count() == 0
    assert 'レジモード' in client.get('/home').text
    assert client.get('/products').status_code == 200
    assert client.post('/input', data={'csrf_token': token, 'date': app_module.business_today().isoformat()}).status_code in (400, 409)


@pytest.mark.parametrize('production', [False, True])
def test_guest_gate_is_independent_and_foreign_ids_rejected(flask_app, admin_dataset, production):
    flask_app.config.update(REGISTER_V32_GUEST_ENABLED=True, REGISTER_V3_PRODUCTION_ENABLED=production)
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    ensure_standard_products(admin_dataset)
    token, _, _ = token_and_product(preview(ca))
    for other in (b, admin_dataset):
        p = RegisterProduct.query.filter_by(dataset_id=other.id).first()
        assert checkout(ca, p, token).status_code == 403
        for route in ('edit', 'image', 'photo'):
            assert ca.get(f'/register/products/{p.id}/{route}').status_code == 404
        assert ca.post(f'/register/products/{p.id}/edit', data={'csrf_token': token}).status_code == 404
    assert ca.post('/api/register/transactions', json={}).status_code == 400
    assert ca.post('/api/register/offline/prepare', json={}, headers={'X-CSRFToken': token}).status_code == 403
    assert ca.get('/register/readiness').status_code in (403, 404)


def test_existing_guest_not_silently_cut_over(flask_app):
    owner, client = guest(flask_app)
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    assert preview(client).status_code == 404
    assert db.session.get(RegisterCutover, owner.id) is None


def test_guest_initialization_failure_rolls_back(flask_app, monkeypatch):
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    def fail(*args, **kwargs):
        raise ValueError('synthetic invalid template')
    monkeypatch.setattr('register_products._standard_rows', fail)
    with flask_app.test_request_context('/'):
        from werkzeug.exceptions import ServiceUnavailable
        with pytest.raises(ServiceUnavailable):
            app_module.start_guest_session()
    assert Dataset.query.filter_by(kind='guest').count() == 0
    assert RegisterCutover.query.count() == RegisterProduct.query.count() == 0


def fields(client, path):
    g.pop('_login_user', None)
    g.pop('csrf_token', None)
    page = BeautifulSoup(client.get(path).text, 'html.parser')
    return {i['name']: i.get('value', '') for i in page.select('input[type="hidden"][name]')}


def photo():
    output = io.BytesIO()
    Image.new('RGB', (24, 24), 'orange').save(output, format='PNG')
    output.seek(0)
    return output, 'fictional.png', 'image/png'


def test_guest_product_photo_edit_retire_and_three_principal_isolation(flask_app, authenticated_client, admin_dataset):
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    data = fields(ca, '/register/products/new')
    data.update(name='架空パン', price_yen='777', bread_kind='sweet', product_class='seasonal', photo=photo())
    assert ca.post('/register/products/new', data=data).status_code == 303
    item = RegisterProduct.query.filter_by(dataset_id=a.id, name='架空パン').one()
    assert item.photo_webp and len(item.photo_webp) <= 512000
    token, config, _ = token_and_product(preview(ca))
    image_url = next(p['image'] for p in config['products'] if p['id'] == item.id)
    assert ca.get(image_url).status_code == 200
    for client in (cb, authenticated_client):
        g.pop('_login_user', None)
        assert client.get(image_url).status_code == 404
        assert client.get(f'/register/products/{item.id}/edit').status_code == 404
    path = f'/register/products/{item.id}/edit'
    data = fields(ca, path)
    data.update(name='架空パン改', price_yen='888', bread_kind='sweet', product_class='seasonal')
    assert ca.post(path, data=data).status_code == 303
    assert item.price_yen == 888 and item.photo_webp
    data = fields(ca, path)
    data['is_active'] = 'false'
    assert ca.post(f'/register/products/{item.id}/status', data=data).status_code == 303
    assert not item.is_active
    _, config, _ = token_and_product(preview(ca))
    assert item.id not in [p['id'] for p in config['products']]
    bad = fields(ca, '/register/products/new')
    bad.update(name='不正画像', price_yen='200', photo=(io.BytesIO(b'bad'), 'bad.png', 'image/png'))
    assert ca.post('/register/products/new', data=bad).status_code == 400


def test_actual_legacy_post_blocked_and_dashboard_uses_own_sales(flask_app):
    from models import Product, DailySales
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    token, _, _ = token_and_product(preview(ca))
    p = RegisterProduct.query.filter_by(dataset_id=a.id).first()
    assert checkout(ca, p, token, received_yen=100000).status_code == 200
    today = app_module.business_today()
    old = Product(dataset_id=a.id, name='旧商品', price=100, year=today.year, month=today.month, is_active=True)
    db.session.add(old)
    db.session.commit()
    response = ca.post('/input', data={'csrf_token': token, 'date': today.isoformat(), 'product_id': str(old.id), 'quantity': '5'})
    assert response.status_code == 409
    assert DailySales.query.count() == 0
    assert ca.get('/api/dashboard-data').json['today_sales'] == p.price_yen
    g.pop('_login_user', None)
    assert cb.get('/api/dashboard-data').json['today_sales'] == 0
    # 停止しても既存売上を隠さず、保存は停止する。
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = False
    g.pop('_login_user', None)
    assert ca.get('/api/dashboard-data').json['today_sales'] == p.price_yen
    assert checkout(ca, p, token).status_code == 403


def test_guest_status_expiry_and_limit(flask_app, monkeypatch):
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    token, _, _ = token_and_product(preview(ca))
    p = RegisterProduct.query.filter_by(dataset_id=a.id).first()
    key = str(uuid.uuid4())
    assert checkout(ca, p, token, submission_key=key, received_yen=100000).status_code == 200
    g.pop('_login_user', None)
    assert cb.get(f'/api/register/transactions/{key}/status').json['status'] == 'not_found'
    monkeypatch.setattr('register_service.GUEST_TRANSACTION_LIMIT', 1)
    assert checkout(ca, p, token, received_yen=100000).status_code == 403
    assert checkout(ca, p, token, submission_key=key, received_yen=100000).status_code == 200
    a.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    a.absolute_expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    db.session.commit()
    assert checkout(ca, p, token).status_code == 403
    assert RegisterTransaction.query.filter_by(dataset_id=a.id).count() == 1


def test_guest_period_sales_refund_and_dataset_isolation(flask_app):
    """Guestの販売・返金を全期間で集計し、別Guestへ混入させない。"""
    flask_app.config["REGISTER_V32_GUEST_ENABLED"] = True
    owner_a, client_a = start(flask_app)
    owner_b, client_b = start(flask_app)

    token, config, page = token_and_product(preview(client_a))
    assert config["guestExperience"] is True
    link = page.select_one("#guest-sales-analysis-link")
    assert link is not None
    assert link["href"] == "/dashboard"
    assert link.has_attr("hidden")

    product = RegisterProduct.query.filter(
        RegisterProduct.dataset_id == owner_a.id,
        RegisterProduct.price_yen > 0
    ).first()
    assert product is not None
    price = product.price_yen

    sale = checkout(
        client_a, product, token,
        items=[[product.id, 2]], received_yen=100000
    )
    assert sale.status_code == 200

    refund = checkout(
        client_a, product, token,
        kind="refund", items=[[product.id, -1]],
        received_yen=None, refund_confirmed=True
    )
    assert refund.status_code == 200

    for period in ("day", "week", "month", "year"):
        g.pop("_login_user", None)
        response = client_a.get(
            f"/api/sales-period-summary?period={period}&offset=0"
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["total_sales"] == price
        assert data["transaction_count"] == 2
        assert data["sale_count"] == 1
        assert data["refund_count"] == 1
        assert len(data["rankings"]) == 1
        assert data["rankings"][0]["quantity"] == 1
        assert data["rankings"][0]["revenue"] == price

        g.pop("_login_user", None)
        other = client_b.get(
            f"/api/sales-period-summary?period={period}&offset=0"
        )
        assert other.status_code == 200
        assert other.get_json()["total_sales"] == 0

    assert RegisterTransaction.query.filter_by(
        dataset_id=owner_a.id
    ).count() == 2
    assert RegisterTransaction.query.filter_by(
        dataset_id=owner_b.id
    ).count() == 0


def test_guest_register_ai_prompt_uses_own_net_sales(flask_app, monkeypatch):
    """Guestレジの販売・返金をAI入力へ渡し、他Guestを除外する。"""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from conftest import post_ai

    flask_app.config["REGISTER_V32_GUEST_ENABLED"] = True
    a, ca = start(flask_app)
    b, cb = start(flask_app)

    pa = RegisterProduct(
        dataset_id=a.id, name="GuestA専用クロワッサン", price_yen=180
    )
    pb = RegisterProduct(
        dataset_id=b.id, name="GuestB専用メロンパン", price_yen=270
    )
    db.session.add_all([pa, pb])
    db.session.commit()

    token_a, _, _ = token_and_product(preview(ca))
    token_b, _, _ = token_and_product(preview(cb))

    assert checkout(
        ca, pa, token_a, items=[[pa.id, 2]], received_yen=1000
    ).status_code == 200

    assert checkout(
        ca, pa, token_a, kind="refund",
        items=[[pa.id, -1]], received_yen=None,
        refund_confirmed=True
    ).status_code == 200

    assert checkout(
        cb, pb, token_b, received_yen=500
    ).status_code == 200

    generate = Mock(
        return_value=SimpleNamespace(text="架空のAIアドバイス")
    )
    fake = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate)
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-key")
    monkeypatch.setattr(
        app_module.genai, "Client", lambda **kwargs: fake
    )

    today = app_module.business_today()
    path = f"/api/ai-advice?year={today.year}&month={today.month}"

    g.pop("_login_user", None)
    ra = post_ai(ca, path)
    assert ra.status_code == 200
    assert ra.json["ai_advice"] == "架空のAIアドバイス"
    prompt_a = generate.call_args.kwargs["contents"]
    assert "GuestA専用クロワッサン" in prompt_a
    assert "差引1個、差引180円" in prompt_a
    assert "GuestB専用メロンパン" not in prompt_a

    g.pop("_login_user", None)
    rb = post_ai(cb, path)
    assert rb.status_code == 200
    prompt_b = generate.call_args.kwargs["contents"]
    assert "GuestB専用メロンパン" in prompt_b
    assert "GuestA専用クロワッサン" not in prompt_b

    db.session.expire_all()
    assert db.session.get(Dataset, a.id).guest_ai_usage_count == 1
    assert db.session.get(Dataset, b.id).guest_ai_usage_count == 1
    assert generate.call_count == 2
