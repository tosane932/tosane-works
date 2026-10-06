import base64
import datetime

import pytest
from bs4 import BeautifulSoup

import app as app_module
import product_images
from models import DailySales, Dataset, Product, db


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl0"
    "AAAAAElFTkSuQmCC"
)


@pytest.fixture()
def image_options(flask_app, monkeypatch, tmp_path):
    image_directory = tmp_path / "product_images"
    image_directory.mkdir()
    options = {
        "bread_01": {"filename": "bread_01.png", "label": "食パン", "guest_allowed": True},
        "croissant_01": {"filename": "croissant_01.png", "label": "クロワッサン", "guest_allowed": True},
        "pastry_admin_01": {"filename": "pastry_admin_01.png", "label": "管理者用", "guest_allowed": False},
    }
    for option in options.values():
        (image_directory / option["filename"]).write_bytes(_ONE_PIXEL_PNG)
    monkeypatch.setattr(flask_app, "static_folder", str(tmp_path))
    monkeypatch.setattr(product_images, "PRODUCT_IMAGE_OPTIONS", options)
    return options


def _guest_client(flask_app):
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(
        kind="guest", system_key=None, created_at=now, last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    db.session.add(dataset)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{dataset.id}"
        session["_fresh"] = True
    return client, dataset


def _form(*, name="食パン", key="", product_id="", price="200"):
    return {
        "year": str(app_module.business_today().year), "month": "1",
        "product_id": product_id, "prod_name": name, "prod_price": price,
        "prod_image_key": key,
    }


def _snapshot():
    return (
        [(p.id, p.dataset_id, p.year, p.month, p.name, p.price, p.image_key, p.is_active)
         for p in Product.query.order_by(Product.id)],
        [(s.id, s.product_id, s.date, s.quantity)
         for s in DailySales.query.order_by(DailySales.id)],
    )


def test_admin_can_choose_guest_and_admin_images(
    admin_dataset, authenticated_client, csrf_post, image_options,
):
    response = csrf_post(authenticated_client, "/", _form(key="pastry_admin_01"))
    assert response.status_code == 200
    product = Product.query.filter_by(dataset_id=admin_dataset.id).one()
    assert product.image_key == "pastry_admin_01"
    form = BeautifulSoup(authenticated_client.get("/").get_data(as_text=True), "html.parser")
    assert {option["value"] for option in form.select('[name="prod_template_key"] option')} >= {
        "", "bread_01", "pastry_admin_01",
    }


def test_guest_can_choose_only_guest_image(flask_app, csrf_post, image_options):
    client, dataset = _guest_client(flask_app)
    form = BeautifulSoup(client.get("/").get_data(as_text=True), "html.parser")
    choices = {option["value"] for option in form.select('[name="prod_template_key"] option')}
    assert "bread_01" in choices
    assert "pastry_admin_01" not in choices
    response = csrf_post(client, "/", _form(key="bread_01"))
    assert response.status_code == 200
    assert Product.query.filter_by(dataset_id=dataset.id).one().image_key == "bread_01"


@pytest.mark.parametrize("key", [
    "pastry_admin_01", "unknown", "../x", "../../static/x",
    "https://example.com/x.jpg",
])
def test_guest_rejects_unapproved_image_without_writes(
    flask_app, csrf_post, image_options, key,
):
    client, dataset = _guest_client(flask_app)
    existing = Product(dataset=dataset, year=app_module.business_today().year,
                       month=1, name="既存", price=100, image_key="bread_01")
    db.session.add(existing)
    db.session.commit()
    before = _snapshot()
    data = {
        "year": str(existing.year), "month": "1",
        "product_id": [str(existing.id), ""],
        "prod_name": ["変更", "新商品"], "prod_price": ["300", "400"],
        "prod_image_key": ["bread_01", key],
    }
    response = csrf_post(client, "/", data)
    assert response.status_code == 400
    assert _snapshot() == before


def test_unselected_image_is_null_and_legacy_update_keeps_existing(
    admin_dataset, authenticated_client, csrf_post, image_options,
):
    response = csrf_post(authenticated_client, "/", _form())
    assert response.status_code == 200
    product = Product.query.filter_by(dataset_id=admin_dataset.id).one()
    assert product.image_key is None
    product.image_key = "bread_01"
    db.session.commit()
    data = _form(product_id=str(product.id), price="250")
    del data["prod_image_key"]
    response = csrf_post(authenticated_client, "/", data)
    assert response.status_code == 200
    assert db.session.get(Product, product.id).image_key == "bread_01"


def test_image_array_length_mismatch_rejects_all_rows(
    admin_dataset, authenticated_client, csrf_post, image_options,
):
    data = {
        "year": str(app_module.business_today().year), "month": "1",
        "product_id": ["", ""], "prod_name": ["食パン", "クロワッサン"],
        "prod_price": ["200", "300"], "prod_image_key": ["bread_01"],
    }
    response = csrf_post(authenticated_client, "/", data)
    assert response.status_code == 400
    assert Product.query.filter_by(dataset_id=admin_dataset.id).count() == 0


def test_multiple_images_follow_their_product_rows(
    admin_dataset, authenticated_client, csrf_post, image_options,
):
    data = {
        "year": str(app_module.business_today().year), "month": "1",
        "product_id": ["", ""], "prod_name": ["食パン", "クロワッサン"],
        "prod_price": ["200", "300"],
        "prod_image_key": ["bread_01", "croissant_01"],
    }
    response = csrf_post(authenticated_client, "/", data)
    assert response.status_code == 200
    products = Product.query.filter_by(dataset_id=admin_dataset.id).order_by(Product.id).all()
    assert [(p.name, p.image_key) for p in products] == [
        ("食パン", "bread_01"), ("クロワッサン", "croissant_01"),
    ]


def test_existing_image_changes_only_when_explicitly_selected(
    admin_dataset, authenticated_client, csrf_post, image_options,
):
    product = Product(dataset=admin_dataset, year=app_module.business_today().year,
                      month=1, name="食パン", price=200, image_key="bread_01")
    db.session.add(product)
    db.session.commit()
    response = csrf_post(authenticated_client, "/", _form(
        product_id=str(product.id), key="croissant_01",
    ))
    assert response.status_code == 200
    assert db.session.get(Product, product.id).image_key == "croissant_01"


@pytest.mark.parametrize("key", ["unknown", "../x", "https://example.com/x.jpg"])
def test_admin_rejects_unapproved_image_without_writes(
    admin_dataset, authenticated_client, csrf_post, image_options, key,
):
    before = _snapshot()
    response = csrf_post(authenticated_client, "/", _form(key=key))
    assert response.status_code == 400
    assert _snapshot() == before


def test_manifest_entry_without_file_is_neither_shown_nor_accepted(
    admin_dataset, authenticated_client, csrf_post, image_options,
):
    product_images.PRODUCT_IMAGE_OPTIONS["missing_01"] = {
        "filename": "missing_01.png", "label": "素材なし", "guest_allowed": True,
    }
    form = BeautifulSoup(authenticated_client.get("/").get_data(as_text=True), "html.parser")
    assert "missing_01" not in {
        option["value"] for option in form.select('[name="prod_template_key"] option')
    }
    response = csrf_post(authenticated_client, "/", _form(key="missing_01"))
    assert response.status_code == 400
    assert Product.query.filter_by(dataset_id=admin_dataset.id).count() == 0


def test_catalog_uses_latest_image_within_dataset_and_does_not_write(
    flask_app, admin_dataset, authenticated_client, image_options,
):
    old = Product(dataset=admin_dataset, year=2025, month=12,
                  name="クロワッサン", price=250, image_key="bread_01")
    latest = Product(dataset=admin_dataset, year=2026, month=10,
                     name="クロワッサン", price=300, image_key="croissant_01")
    db.session.add_all([old, latest])
    db.session.flush()
    db.session.add(DailySales(product_id=old.id, date=datetime.date(2025, 12, 10), quantity=2))
    guest_client, guest_dataset = _guest_client(flask_app)
    db.session.add(Product(dataset=guest_dataset, year=2027, month=1,
                           name="クロワッサン", price=999, image_key="bread_01"))
    db.session.commit()
    before = _snapshot()
    response = authenticated_client.get("/products")
    assert response.status_code == 200
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    items = document.select(".product-catalog-item")
    assert len(items) == 1
    image = items[0].select_one("img.product-catalog-image")
    assert image is not None
    assert image["src"].endswith("/product_images/croissant_01.png")
    assert items[0].select_one(".product-catalog-price").get_text(strip=True) == "300円"
    assert items[0].select_one(".product-catalog-period").get_text(strip=True) == "2026年10月"
    assert _snapshot() == before


@pytest.mark.parametrize("key", [None, "../secret", "missing_01"])
def test_catalog_unknown_or_missing_image_uses_placeholder(
    admin_dataset, authenticated_client, image_options, key,
):
    db.session.add(Product(dataset=admin_dataset, year=2026, month=1,
                           name="画像なし", price=100, image_key=key))
    db.session.commit()
    response = authenticated_client.get("/products")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert document.select_one(".product-catalog-image") is None
    assert document.select_one(".product-catalog-image-empty").get_text(strip=True) == "画像なし"
    assert "secret" not in response.get_data(as_text=True)


def test_guest_template_selection_accepts_matching_name_and_image(
    flask_app, csrf_post, image_options,
):
    client, dataset = _guest_client(flask_app)
    data = _form(name="食パン", key="bread_01")
    data["prod_template_key"] = "bread_01"

    response = csrf_post(client, "/", data)

    assert response.status_code == 200
    product = Product.query.filter_by(dataset_id=dataset.id).one()
    assert (product.name, product.image_key) == ("食パン", "bread_01")


@pytest.mark.parametrize(
    ("name", "image_key", "template_key"),
    [
        ("自由な商品名", "bread_01", "bread_01"),
        ("食パン", "croissant_01", "bread_01"),
        ("自由な商品名", "bread_01", ""),
    ],
)
def test_guest_rejects_invalid_name_image_template_combinations_without_writes(
    flask_app,
    csrf_post,
    image_options,
    name,
    image_key,
    template_key,
):
    client, dataset = _guest_client(flask_app)
    before = _snapshot()

    data = _form(name=name, key=image_key)
    data["prod_template_key"] = template_key

    response = csrf_post(client, "/", data)

    assert response.status_code == 400
    assert _snapshot() == before
    assert Product.query.filter_by(dataset_id=dataset.id).count() == 0


def test_guest_free_name_without_image_is_allowed(
    flask_app, csrf_post, image_options,
):
    client, dataset = _guest_client(flask_app)
    data = _form(name="自由な新商品", key="")
    data["prod_template_key"] = ""

    response = csrf_post(client, "/", data)

    assert response.status_code == 200
    product = Product.query.filter_by(dataset_id=dataset.id).one()
    assert product.name == "自由な新商品"
    assert product.image_key is None


def test_admin_template_name_can_use_another_approved_image(
    admin_dataset,
    authenticated_client,
    csrf_post,
    image_options,
):
    data = _form(name="食パン", key="croissant_01")
    data["prod_template_key"] = "bread_01"

    response = csrf_post(authenticated_client, "/", data)

    assert response.status_code == 200
    product = Product.query.filter_by(dataset_id=admin_dataset.id).one()
    assert product.name == "食パン"
    assert product.image_key == "croissant_01"



def test_product_form_uses_single_template_selector_for_images(
    authenticated_client,
    image_options,
):
    response = authenticated_client.get("/")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert document.select('select[name="prod_template_key"]')
    assert not document.select('select[name="prod_image_key"]')
    assert document.select('input[type="hidden"][name="prod_image_key"]')
