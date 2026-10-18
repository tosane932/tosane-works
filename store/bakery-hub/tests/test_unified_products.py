"""共通マスタの再利用・写真・競合と履歴保全を検証する。"""
import datetime
import io
import json
import uuid

import pytest
from bs4 import BeautifulSoup
from flask import g
from PIL import Image

from models import (DailySales, Product, RegisterCutover, RegisterPriceHistory,
                    RegisterProduct, RegisterTransactionLine, db)
from register_products import create_custom_product, update_register_product
from register_service import save_register_transaction


def enable(app):
    app.config.update(REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=True,
                      REGISTER_V3_PRODUCTS_WRITE_ENABLED=True,
                      REGISTER_V3_PREVIEW_ENABLED=True)


def page(client, path="/products"):
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    return client.get(path)


def fields(client, path):
    response = page(client, path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    return {i["name"]: i.get("value", "") for i in soup.select('input[type="hidden"][name]')}


def image():
    stream = io.BytesIO()
    Image.new("RGB", (40, 40), "orange").save(stream, "PNG")
    stream.seek(0)
    return stream, "bread.png", "image/png"


def test_admin_get_is_read_only_and_has_one_entry(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    response = page(authenticated_client)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one('a[href="/register/products/new"]')
    assert "＋ 新しい商品を登録" in soup.get_text()
    assert "終売" in soup.get_text()
    assert page(authenticated_client, "/register/products").status_code == 303
    assert page(authenticated_client, "/").status_code == 303
    assert RegisterProduct.query.count() == RegisterPriceHistory.query.count() == 0


def test_photo_create_retry_edit_retire_resume_preserves_history(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    old = Product(dataset_id=admin_dataset.id, year=2026, month=10, name="同名", price=280)
    db.session.add(old)
    db.session.commit()
    db.session.add(DailySales(product_id=old.id, date=datetime.date(2026, 10, 1), quantity=3))
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=datetime.date(2020, 1, 1)))
    db.session.commit()
    data = fields(authenticated_client, "/register/products/new")
    data.update(name="栗のパン", price_yen="850", bread_kind="sweet", product_class="seasonal")
    for _ in range(2):
        result = authenticated_client.post("/register/products/new", data=dict(data, photo=image()))
        assert result.status_code == 303
    product = RegisterProduct.query.one()
    product_id = product.id
    photo = bytes(product.photo_webp)
    assert Image.open(io.BytesIO(photo)).format == "WEBP"
    assert (product.bread_kind, product.product_class) == ("sweet", "seasonal")
    assert RegisterPriceHistory.query.count() == 1
    sale = save_register_transaction(admin_dataset, submission_key=uuid.uuid4(), kind="sale",
                                    items=[(product.id, 1)], received_yen=1000,
                                    displayed_prices=[(product.id, 850)])
    assert sale.total_yen == 850
    edit_path = f"/register/products/{product.id}/edit"
    edit = fields(authenticated_client, edit_path)
    edit.update(name="栗のパン", price_yen="900", bread_kind="hard", product_class="special",
                image_key="__keep__")
    assert authenticated_client.post(edit_path, data=edit).status_code == 303
    assert authenticated_client.post(edit_path, data=edit).status_code == 303
    stale = dict(edit, name="上書き禁止")
    assert authenticated_client.post(edit_path, data=stale).status_code == 409
    status_path = f"/register/products/{product_id}/status"
    status = fields(authenticated_client, edit_path)
    status.update(is_active="false")
    assert authenticated_client.post(status_path, data=status).status_code == 303
    assert authenticated_client.post(status_path, data=status).status_code == 303
    listing = json.loads(BeautifulSoup(page(authenticated_client, "/register/preview").data,
                                      "html.parser").select_one("#register-config").string)
    assert not listing["products"]
    resume = fields(authenticated_client, edit_path)
    resume.update(is_active="true")
    assert authenticated_client.post(status_path, data=resume).status_code == 303
    assert authenticated_client.post(status_path, data=status).status_code == 409
    db.session.refresh(product)
    assert (product.id, bytes(product.photo_webp), product.price_yen, product.is_active) == (product_id, photo, 900, True)
    assert (product.bread_kind, product.product_class) == ("hard", "special")
    line = RegisterTransactionLine.query.one()
    assert (line.product_name_snapshot, line.unit_price_yen, line.line_total_yen) == ("栗のパン", 850, 850)
    assert RegisterPriceHistory.query.count() == 2
    assert (old.id, old.price, DailySales.query.one().quantity) == (old.id, 280, 3)


@pytest.mark.parametrize("price", [501, 850, 100000])
def test_prices_above_500_work_in_sale_refund_and_history(flask_app, admin_dataset, price):
    item = create_custom_product(admin_dataset, name="食事パン", price_yen=price)
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=datetime.date(2020, 1, 1)))
    db.session.commit()
    for kind, quantity in (("sale", 2), ("refund", -2)):
        row = save_register_transaction(admin_dataset, submission_key=uuid.uuid4(), kind=kind,
            items=[(item.id, quantity)], received_yen=price * 2 if quantity > 0 else None,
            refund_confirmed=quantity < 0, displayed_prices=[(item.id, price)])
        assert row.total_yen == price * quantity
    update_register_product(admin_dataset, item.id, name=item.name, price_yen=500)
    assert RegisterPriceHistory.query.count() == 2


def test_photo_auth_dataset_csrf_and_invalid_upload(flask_app, authenticated_client, admin_dataset):
    from tests.test_register_product_routes import guest_client
    enable(flask_app)
    data = fields(authenticated_client, "/register/products/new")
    data.update(name="写真のパン", price_yen="850", bread_kind="sweet", product_class="seasonal")
    invalid = dict(data, photo=(io.BytesIO(b"not an image"), "bad.png", "image/png"))
    assert authenticated_client.post("/register/products/new", data=invalid).status_code == 400
    assert RegisterProduct.query.count() == 0
    assert authenticated_client.post("/register/products/new", data=dict(data, photo=image())).status_code == 303
    item = RegisterProduct.query.one()
    config = json.loads(BeautifulSoup(page(authenticated_client, "/register/preview").data,
        "html.parser").select_one("#register-config").string)
    photo_url = config["products"][0]["image"]
    response = page(authenticated_client, photo_url)
    assert response.status_code == 200 and response.mimetype == "image/webp"
    assert response.headers["Cache-Control"] == "no-store"
    assert page(flask_app.test_client(), photo_url).status_code == 302
    _, guest = guest_client(flask_app)
    assert page(guest, photo_url).status_code == 404
    edit_path = f"/register/products/{item.id}/edit"
    original = fields(authenticated_client, edit_path)
    g.pop("_login_user", None)
    assert authenticated_client.post(f"/register/products/{item.id}/status", data={
        "is_active": "false", "signed_original": original["signed_original"]}).status_code == 400
    assert authenticated_client.post(f"/register/products/{item.id}/status", data=dict(
        original, is_active="false", signed_original=original["signed_original"] + "x")).status_code == 400
    assert page(guest, edit_path).status_code == 404
    # A photo edit invalidates an older status form even when price/name stay the same.
    g.pop("_login_user", None)
    original.update(name=item.name, price_yen="850", image_key="croissant")
    assert authenticated_client.post(edit_path, data=original).status_code == 303
    assert authenticated_client.post(f"/register/products/{item.id}/status",
                                    data=dict(original, is_active="false")).status_code == 409
    assert page(authenticated_client, photo_url).status_code == 404
    assert item.is_active


def test_categories_price_limits_and_idempotency_digest(flask_app, admin_dataset):
    from register_products import ProductSubmissionConflict
    key = str(uuid.uuid4())
    for price in (-1, 100001, True, 1.5, "1e3", "9" * 100):
        with pytest.raises(ValueError):
            create_custom_product(admin_dataset, name="拒否", price_yen=price)
    for kind, classification in (("unknown", "regular"), ("sweet", "unknown")):
        with pytest.raises(ValueError):
            create_custom_product(admin_dataset, name="拒否", price_yen=800,
                                  bread_kind=kind, product_class=classification)
    item = create_custom_product(admin_dataset, name="同名", price_yen=800,
        bread_kind="sweet", product_class="seasonal", image_key="croissant", request_key=key)
    assert create_custom_product(admin_dataset, name="同名", price_yen=800, bread_kind="sweet",
        product_class="seasonal", image_key="croissant", request_key=key).id == item.id
    for extra in ({"bread_kind": "loaf"}, {"product_class": "special"}, {"image_key": "melonpan"}):
        args = dict(bread_kind="sweet", product_class="seasonal", image_key="croissant")
        args.update(extra)
        with pytest.raises(ProductSubmissionConflict):
            create_custom_product(admin_dataset, name="同名", price_yen=800, request_key=key, **args)
    assert RegisterProduct.query.count() == 1


def test_photo_offline_catalog_is_embedded_not_public_cache(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    data = fields(authenticated_client, "/register/products/new")
    data.update(name="写真パン", price_yen="850", bread_kind="sweet", product_class="seasonal")
    assert authenticated_client.post("/register/products/new", data=dict(data, photo=image())).status_code == 303
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=datetime.date(2020, 1, 1)))
    db.session.commit()
    flask_app.config.update(REGISTER_V3_PRODUCTION_ENABLED=True, REGISTER_V3_SAVE_ENABLED=True,
                            REGISTER_V3_OFFLINE_MODE_ENABLED=True)
    token = fields(authenticated_client, f"/register/products/{RegisterProduct.query.one().id}/edit")["csrf_token"]
    response = authenticated_client.post("/api/register/offline/prepare", headers={"X-CSRFToken": token},
        json={"device_id": str(uuid.uuid4()), "next_day": False})
    assert response.status_code == 200
    assert response.json["products"][0]["image"].startswith("data:image/webp;base64,")
    assert response.headers["Cache-Control"] == "no-store"


def test_initial_eighteen_are_optional_and_never_forced_active(flask_app, admin_dataset):
    from register_cutover_audit import audit_cutover
    from register_products import ensure_standard_products, set_product_active
    item = create_custom_product(admin_dataset, name="お店のパン", price_yen=850)
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Tokyo")).date()
    audit = audit_cutover(admin_dataset, planned_date=today, today=today)
    assert not audit["blockers"]
    ensure_standard_products(admin_dataset)
    candidate = RegisterProduct.query.filter_by(dataset_id=admin_dataset.id, template_key="croissant").one()
    candidate_id = candidate.id
    set_product_active(admin_dataset, candidate_id, is_active=False, expected_revision=0)
    ensure_standard_products(admin_dataset)
    assert (candidate.id, candidate.is_active) == (candidate_id, False)
    assert db.session.get(RegisterProduct, item.id).price_yen == 850
