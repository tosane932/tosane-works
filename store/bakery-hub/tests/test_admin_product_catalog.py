import io
import datetime
import json

import pytest
from bs4 import BeautifulSoup
from PIL import Image

import app as app_module
import product_images
from models import Dataset, Product, db


def _png():
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'orange').save(output, format='PNG')
    return output.getvalue()


def _form(**changes):
    data = {
        'catalog_label': '塩パン',
        'catalog_default_price': '180',
        'catalog_reading': 'しおぱん',
    }
    data.update(changes)
    return data


def _post(client, csrf_token, data):
    return client.post('/admin/product-catalog', data={
        **data, 'csrf_token': csrf_token(client, '/'),
    })


def test_admin_submits_catalog_without_creating_monthly_product(
    authenticated_client, admin_dataset, csrf_token, monkeypatch,
):
    published = []
    monkeypatch.setattr(app_module, 'publish_catalog_change', lambda **data: (
        published.append(data) or 'https://github.com/tosane932/tosane-works/pull/123'
    ), raising=False)
    response = _post(authenticated_client, csrf_token, _form())
    assert response.status_code == 200
    assert published and published[0]['label'] == '塩パン'
    assert published[0]['image'] is None
    assert Product.query.filter_by(dataset_id=admin_dataset.id).count() == 0
    assert '/pull/123' in response.get_data(as_text=True)


def test_admin_uploads_valid_image_as_one_catalog_change(
    authenticated_client, admin_dataset, csrf_token, monkeypatch,
):
    published = []
    monkeypatch.setattr(app_module, 'publish_catalog_change', lambda **data: (
        published.append(data) or 'https://github.com/tosane932/tosane-works/pull/124'
    ), raising=False)
    data = _form(catalog_image=(io.BytesIO(_png()), 'salt.png', 'image/png'))
    response = _post(authenticated_client, csrf_token, data)
    assert response.status_code == 200
    assert published and published[0]['image'] is not None
    assert published[0]['image'][0] == 'webp'
    assert Product.query.count() == 0


@pytest.mark.parametrize('price', ['0', '9990'])
def test_catalog_price_boundaries_publish(
    authenticated_client, admin_dataset, csrf_token, monkeypatch, price,
):
    published = []
    monkeypatch.setattr(app_module, 'publish_catalog_change', lambda **fields: (
        published.append(fields) or 'https://github.com/tosane932/tosane-works/pull/125'
    ))
    assert _post(authenticated_client, csrf_token,
                 _form(catalog_default_price=price)).status_code == 200
    assert published[0]['default_price'] == int(price)


@pytest.mark.parametrize('data', [
    _form(catalog_label=''),
    _form(catalog_label='x' * 101),
    _form(catalog_default_price='9991'),
    _form(catalog_default_price='10000'),
    _form(catalog_default_price='-10'),
    _form(catalog_reading=''),
    _form(catalog_reading='../../../tmp'),
])
def test_bad_catalog_fields_are_rejected_before_publishing(
    authenticated_client, admin_dataset, csrf_token, monkeypatch, data,
):
    monkeypatch.setattr(app_module, 'publish_catalog_change', lambda **kwargs: pytest.fail('publish called'), raising=False)
    response = _post(authenticated_client, csrf_token, data)
    assert response.status_code == 400


@pytest.mark.parametrize('name,content,mime', [
    ('fake.png', b'<script>alert(1)</script>', 'image/png'),
    ('bad.svg', b'<svg></svg>', 'image/svg+xml'),
    ('empty.png', b'', 'image/png'),
    ('../../evil.png', _png(), 'image/png'),
    ('wrong.jpg', _png(), 'image/jpeg'),
])
def test_bad_uploads_are_rejected_before_publishing(
    authenticated_client, admin_dataset, csrf_token, monkeypatch, name, content, mime,
):
    monkeypatch.setattr(app_module, 'publish_catalog_change', lambda **kwargs: pytest.fail('publish called'), raising=False)
    response = _post(authenticated_client, csrf_token,
                     _form(catalog_image=(io.BytesIO(content), name, mime)))
    assert response.status_code == 400


def test_oversize_upload_is_rejected_before_publishing(
    authenticated_client, admin_dataset, csrf_token, monkeypatch,
):
    monkeypatch.setattr(app_module, 'publish_catalog_change',
                        lambda **kwargs: pytest.fail('publish called'))
    response = _post(authenticated_client, csrf_token, _form(
        catalog_image=(io.BytesIO(b'x' * (200 * 1024 + 1)), 'large.png', 'image/png'),
    ))
    assert response.status_code == 400


def test_guest_and_anonymous_cannot_publish(
    flask_app, client, admin_dataset, csrf_token, monkeypatch,
):
    monkeypatch.setattr(app_module, 'publish_catalog_change', lambda **kwargs: pytest.fail('publish called'), raising=False)
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = Dataset(kind='guest', system_key=None, created_at=now,
                    last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(guest)
    db.session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = f'guest:{guest.id}'
        session['_fresh'] = True
    guest_response = _post(client, csrf_token, _form())
    assert guest_response.status_code == 403
    anonymous = flask_app.test_client()
    response = anonymous.post('/admin/product-catalog', data=_form())
    assert response.status_code == 400  # Global CSRF rejects anonymous direct POST.


def test_guest_catalog_controls_are_absent(flask_app, admin_dataset):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = Dataset(kind='guest', system_key=None, created_at=now,
                    last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(guest)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = f'guest:{guest.id}'
        session['_fresh'] = True
    html = client.get('/').get_data(as_text=True)
    doc = BeautifulSoup(html, 'html.parser')
    assert doc.select('form[action="/admin/product-catalog"]') == []
    assert doc.select('[name="catalog_image"]') == []


def test_image_free_catalog_template_can_be_used_without_creating_it_automatically(
    authenticated_client, admin_dataset, csrf_token, monkeypatch, tmp_path,
):
    key = 'admin_' + 'a' * 32
    path = tmp_path / 'admin_products.json'
    path.write_text(json.dumps({'products': {key: {
        'label': '塩パン', 'default_price': 180, 'reading': 'しおぱん',
        'filename': None, 'image_key': None, 'guest_allowed': True,
    }}, 'images': {}}), encoding='utf-8')
    monkeypatch.setattr(product_images, 'ADMIN_CATALOG_PATH', path)
    form = BeautifulSoup(authenticated_client.get('/').get_data(as_text=True), 'html.parser')
    option = form.select_one(f'[name="prod_template_key"] option[value="{key}"]')
    assert option['data-product-name'] == '塩パン'
    assert option['data-default-price'] == '180'
    assert option['data-image-key'] == ''
    assert Product.query.count() == 0
    today = app_module.business_today()
    response = _post_monthly_product(authenticated_client, csrf_token, {
        'year': str(today.year), 'month': str(today.month),
        'product_id': '', 'prod_name': '塩パン', 'prod_price': '190',
        'prod_template_key': key, 'prod_image_key': '',
    })
    assert response.status_code == 200
    product = Product.query.filter_by(dataset_id=admin_dataset.id).one()
    assert product.price == 190 and product.image_key is None
    catalog = BeautifulSoup(authenticated_client.get('/products').get_data(as_text=True), 'html.parser')
    assert catalog.select_one('#current-month-products .product-catalog-name').get_text(strip=True) == '塩パン'
    assert catalog.select_one('#current-month-products .product-catalog-image-empty')


def _post_monthly_product(client, csrf_token, data):
    return client.post('/', data={**data, 'csrf_token': csrf_token(client, '/')})
