import base64
import hashlib
import json
from pathlib import Path

import pytest

import catalog_repository as repository
import product_images


class Response:
    def __init__(self, status, data):
        self.status_code = status
        self.data = data

    def json(self):
        return self.data


@pytest.fixture()
def fake_github(monkeypatch):
    calls = []
    existing = {"products": {}, "images": {}}

    def request(method, url, **kwargs):
        path = url.removeprefix(repository.API_ROOT + '/')
        calls.append((method, path, kwargs.get('json')))
        if method == 'GET' and path == 'git/ref/heads/main':
            return Response(200, {'object': {'sha': 'base-sha'}})
        if method == 'GET' and path == 'git/commits/base-sha':
            return Response(200, {'tree': {'sha': 'base-tree'}})
        if method == 'GET' and path == f'contents/{repository.CATALOG_PATH}':
            return Response(200, {'content': base64.b64encode(
                json.dumps(existing).encode()).decode()})
        if method == 'POST' and path == 'git/blobs':
            return Response(201, {'sha': 'image-blob'})
        if method == 'POST' and path == 'git/trees':
            return Response(201, {'sha': 'combined-tree'})
        if method == 'POST' and path == 'git/commits':
            return Response(201, {'sha': 'combined-commit'})
        if method == 'POST' and path == 'git/refs':
            return Response(201, {})
        if method == 'POST' and path == 'pulls':
            return Response(201, {'html_url':
                'https://github.com/tosane932/tosane-works/pull/42'})
        if method == 'DELETE' and path.startswith('git/refs/heads/catalog/'):
            return Response(204, {})
        raise AssertionError((method, path))

    monkeypatch.setenv('GITHUB_CATALOG_TOKEN', 'fixture-token')
    monkeypatch.setattr(repository.requests, 'request', request)
    return calls, existing, request


def _publish(**kwargs):
    fields = dict(
        label='塩パン', default_price=180, reading='しおぱん',
        image=None,
    )
    fields.update(kwargs)
    return repository.publish_catalog_change(**fields)


def test_image_free_catalog_is_one_json_commit_without_monthly_product(fake_github):
    calls, _, _ = fake_github
    assert _publish() == 'https://github.com/tosane932/tosane-works/pull/42'
    assert not any(path == 'git/blobs' for _, path, _ in calls)
    tree = next(body for method, path, body in calls if path == 'git/trees')
    assert [entry['path'] for entry in tree['tree']] == [repository.CATALOG_PATH]
    data = json.loads(tree['tree'][0]['content'])
    key, item = next(iter(data['products'].items()))
    assert key.startswith('admin_') and len(key) == 38
    assert item['filename'] is None and item['image_key'] is None
    assert item['default_price'] == 180
    assert [(method, path) for method, path, _ in calls[-2:]] == [
        ('POST', 'git/refs'), ('POST', 'pulls'),
    ]


def test_image_and_catalog_are_in_same_commit_and_old_asset_is_retained(fake_github):
    calls, existing, _ = fake_github
    existing['products']['admin_' + 'a'*32] = {
        'label': '塩パン', 'default_price': 180, 'reading': 'しおぱん',
        'filename': 'admin/' + 'b'*32 + '.webp',
        'image_key': 'admin_image_' + 'b'*32, 'guest_allowed': True,
    }
    existing['images']['admin_image_' + 'b'*32] = {
        'filename': 'admin/' + 'b'*32 + '.webp', 'label': '塩パン',
        'product_key': 'admin_' + 'a'*32,
    }
    repository.publish_catalog_change(
        label='塩パン', default_price=190, reading='しおぱん',
        image=('webp', b'validated-image'), edit_key='admin_' + 'a'*32,
        guest_allowed=True,
    )
    tree = next(body for method, path, body in calls if path == 'git/trees')
    assert len(tree['tree']) == 2
    assert tree['tree'][0]['path'].startswith(repository.IMAGE_DIRECTORY + '/')
    assert tree['tree'][0]['sha'] == 'image-blob'
    data = json.loads(tree['tree'][1]['content'])
    assert data['images']['admin_image_' + 'b'*32]['filename'].endswith('b'*32 + '.webp')
    assert data['products']['admin_' + 'a'*32]['image_key'] != 'admin_image_' + 'b'*32
    commit = next(body for method, path, body in calls if path == 'git/commits')
    assert commit['parents'] == ['base-sha'] and commit['tree'] == 'combined-tree'
    assert not any(method == 'PATCH' for method, _, _ in calls)


def test_image_can_be_added_later_to_an_image_free_catalog_item(fake_github):
    calls, existing, _ = fake_github
    key = 'admin_' + 'a' * 32
    existing['products'][key] = {
        'label': '塩パン', 'default_price': 180, 'reading': 'しおぱん',
        'filename': None, 'image_key': None, 'guest_allowed': True,
    }
    _publish(edit_key=key, image=('webp', b'validated-image'), guest_allowed=True)
    tree = next(body for method, path, body in calls if path == 'git/trees')
    data = json.loads(tree['tree'][1]['content'])
    assert data['products'][key]['filename'].startswith('admin/')
    assert data['products'][key]['image_key'] in data['images']
    assert data['images'][data['products'][key]['image_key']]['product_key'] == key


def test_duplicate_name_does_not_publish_or_overwrite_standard(fake_github):
    calls, _, _ = fake_github
    with pytest.raises(ValueError, match='同じ商品名'):
        repository.publish_catalog_change(
            label='食パン', default_price=200, reading='しょくぱん', image=None,
        )
    assert not any(method == 'POST' for method, _, _ in calls)


def test_same_original_filename_cannot_replace_previous_image(fake_github):
    calls, _, _ = fake_github
    repository.publish_catalog_change(
        label='塩パン', default_price=180, reading='しおぱん',
        image=('webp', b'first-image'),
    )
    repository.publish_catalog_change(
        label='くるみパン', default_price=260, reading='くるみぱん',
        image=('webp', b'second-image'),
    )
    paths = [body['tree'][0]['path'] for method, path, body in calls
             if method == 'POST' and path == 'git/trees']
    assert len(paths) == 2 and paths[0] != paths[1]
    assert all(path.startswith(repository.IMAGE_DIRECTORY + '/') for path in paths)


def test_failed_pr_deletes_its_new_branch(fake_github, monkeypatch):
    calls, _, request = fake_github

    def fail_pr(method, url, **kwargs):
        if method == 'POST' and url.endswith('/pulls'):
            calls.append((method, 'pulls', kwargs.get('json')))
            return Response(503, {})
        return request(method, url, **kwargs)

    monkeypatch.setattr(repository.requests, 'request', fail_pr)
    with pytest.raises(repository.CatalogPublishError):
        _publish(image=('webp', b'validated-image'))
    assert any(method == 'DELETE' and path.startswith('git/refs/heads/catalog/')
               for method, path, _ in calls)
    assert not any(method in ('PATCH', 'PUT') for method, _, _ in calls)


def test_without_secret_no_github_request(fake_github, monkeypatch):
    calls, _, _ = fake_github
    monkeypatch.delenv('GITHUB_CATALOG_TOKEN')
    with pytest.raises(repository.CatalogPublishError):
        _publish()
    assert calls == []


def test_catalog_without_image_is_a_template_and_old_images_remain(tmp_path, monkeypatch):
    catalog = tmp_path / 'admin_products.json'
    catalog.write_text(json.dumps({
        'products': {'admin_'+'a'*32: {
            'label': '塩パン', 'default_price': 180, 'reading': 'しおぱん',
            'filename': None, 'image_key': None, 'guest_allowed': True,
        }},
        'images': {'admin_image_'+'b'*32: {
            'filename': 'admin/'+'b'*32+'.webp', 'label': '塩パン',
            'product_key': 'admin_'+'a'*32,
        }},
    }), encoding='utf-8')
    monkeypatch.setattr(product_images, 'ADMIN_CATALOG_PATH', catalog)
    static = tmp_path / 'static'
    folder = static / 'product_images' / 'admin'
    folder.mkdir(parents=True)
    (folder / ('b'*32+'.webp')).write_bytes(b'placeholder')
    options = product_images.available_product_images(static, is_guest=True)
    assert options['admin_'+'a'*32]['filename'] is None
    assets = product_images.available_product_image_assets(static, is_guest=True)
    assert assets['admin_image_'+'b'*32]['filename'] == 'admin/'+'b'*32+'.webp'
    assert len(product_images.PRODUCT_IMAGE_OPTIONS) == 18


def test_admin_only_catalog_asset_is_not_available_to_guest(tmp_path, monkeypatch):
    catalog = tmp_path / 'admin_products.json'
    catalog.write_text(json.dumps({
        'products': {'admin_'+'a'*32: {
            'label': '限定パン', 'default_price': 100, 'reading': 'げんていぱん',
            'filename': 'admin/'+'b'*32+'.webp',
            'image_key': 'admin_image_'+'b'*32, 'guest_allowed': False,
        }},
        'images': {'admin_image_'+'b'*32: {
            'filename': 'admin/'+'b'*32+'.webp', 'label': '限定パン',
            'product_key': 'admin_'+'a'*32,
        }},
    }), encoding='utf-8')
    monkeypatch.setattr(product_images, 'ADMIN_CATALOG_PATH', catalog)
    static = tmp_path / 'static'
    folder = static / 'product_images' / 'admin'
    folder.mkdir(parents=True)
    (folder / ('b'*32+'.webp')).write_bytes(b'placeholder')
    assert 'admin_'+'a'*32 in product_images.available_product_images(static, is_guest=False)
    assert 'admin_'+'a'*32 not in product_images.available_product_images(static, is_guest=True)
    assert 'admin_image_'+'b'*32 not in product_images.available_product_image_assets(static, is_guest=True)


def test_standard_eighteen_catalog_items_keep_their_original_details(flask_app):
    options = product_images.PRODUCT_IMAGE_OPTIONS
    assert len(options) == 18
    canonical = json.dumps(options, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':')).encode('utf-8')
    assert hashlib.sha256(canonical).hexdigest() == (
        'fd4953ee3ed3cfca1e52dc714f6316229ceeb72498e3de4cb66881666755601c'
    )
    assert all((Path(flask_app.static_folder) / 'product_images'
                / value['filename']).is_file() for value in options.values())
