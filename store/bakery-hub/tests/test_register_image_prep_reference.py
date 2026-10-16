"""承認済み画像と仕込み向け参考数量の境界。"""

import datetime
import json
import uuid
from pathlib import Path

from bs4 import BeautifulSoup
from flask import g

import product_images
from models import (DailySales, Dataset, PrepPlan, PrepRecipe, Product, RegisterCutover,
                    RegisterProduct, RegisterTransaction, RegisterTransactionLine, db)


def register(owner, date, *lines, kind="sale"):
    total = sum(price * quantity for _, _, price, quantity in lines)
    transaction = RegisterTransaction(dataset_id=owner.id, submission_key=uuid.uuid4(),
                                      request_digest="a" * 64, business_date=date, kind=kind,
                                      total_yen=total,
                                      received_yen=total if kind == "sale" else None,
                                      change_yen=0 if kind == "sale" else None)
    db.session.add(transaction)
    db.session.flush()
    for product, name, price, quantity in lines:
        db.session.add(RegisterTransactionLine(transaction_id=transaction.id,
            dataset_id=owner.id, register_product_id=product.id,
            product_name_snapshot=name, unit_price_yen=price, quantity=quantity,
            line_total_yen=price * quantity))
    db.session.commit()


def page(client, path):
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    return client.get(path)


def post_image(client, path, key, original):
    token = BeautifulSoup(page(client, path).data, "html.parser").select_one(
        'input[name="csrf_token"]')["value"]
    g.pop("_login_user", None)
    return client.post(path, data={"csrf_token": token, "signed_original": original,
                                   "image_key": key})


def guest(flask_app):
    now = datetime.datetime.now(datetime.timezone.utc)
    owner = Dataset(kind="guest", created_at=now, last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(owner)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{owner.id}"
        session["_fresh"] = True
    return owner, client


def test_image_gallery_approval_guest_scope_stale_edit_and_register(flask_app, authenticated_client,
                                                                     admin_dataset, tmp_path, monkeypatch):
    flask_app.config.update(REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=True,
                            REGISTER_V3_PRODUCTS_WRITE_ENABLED=True,
                            REGISTER_V3_PREVIEW_ENABLED=True)
    catalog = tmp_path / "catalog.json"
    public_key = "admin_image_" + "a" * 32
    private_key = "admin_image_" + "b" * 32
    catalog.write_text(json.dumps({"products": {
        "admin_" + "c" * 32: {"label": "公開", "default_price": 100,
                                  "reading": "こうかい", "filename": None,
                                  "image_key": None, "guest_allowed": True},
        "admin_" + "d" * 32: {"label": "非公開", "default_price": 100,
                                  "reading": "ひこうかい", "filename": None,
                                  "image_key": None, "guest_allowed": False}},
        "images": {public_key: {"filename": "admin/" + "a" * 32 + ".webp",
                                "label": "公開", "product_key": "admin_" + "c" * 32},
                   private_key: {"filename": "admin/" + "b" * 32 + ".webp",
                                 "label": "非公開", "product_key": "admin_" + "d" * 32}}}), encoding="utf-8")
    monkeypatch.setattr(product_images, "ADMIN_CATALOG_PATH", catalog)
    static = tmp_path / "static"
    monkeypatch.setattr(flask_app, "static_folder", str(static))
    admin_dir = static / "product_images" / "admin"
    admin_dir.mkdir(parents=True)
    for ch in "ab":
        (admin_dir / (ch * 32 + ".webp")).write_bytes(b"RIFFtestWEBP")
    # 標準画像も隔離された静的フォルダに用意し、実カタログや画像は変更しない。
    (static / "product_images" / "croissant.png").write_bytes(b"PNG placeholder")
    (static / "register_images").mkdir()
    (static / "register_images" / "croissant.webp").write_bytes(b"RIFFtestWEBP")
    product = RegisterProduct(dataset_id=admin_dataset.id, name="独自", price_yen=240)
    db.session.add(product)
    db.session.commit()
    path = f"/register/products/{product.id}/image"
    html = BeautifulSoup(page(authenticated_client, path).data, "html.parser")
    assert len(html.select('input[name="image_key"]')) == 4  # なし + 隔離標準1 + 承認済み2
    original = html.select_one('input[name="signed_original"]')["value"]
    assert post_image(authenticated_client, path, "../../not-approved.webp", original).status_code == 400
    assert authenticated_client.post(path, data={"signed_original": original,
                                                 "image_key": public_key}).status_code == 400
    assert post_image(authenticated_client, path, "unknown", original).status_code == 400
    (admin_dir / ("a" * 32 + ".webp")).unlink()
    assert post_image(authenticated_client, path, public_key, original).status_code == 400
    (admin_dir / ("a" * 32 + ".webp")).write_bytes(b"RIFFtestWEBP")
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                   first_register_date=datetime.date(2020, 1, 1)))
    db.session.commit()
    register(admin_dataset, datetime.date(2026, 10, 16), (product, "独自", 240, 1))
    assert post_image(authenticated_client, path, public_key, original).status_code == 303
    assert post_image(authenticated_client, path, public_key, original).status_code == 303
    assert (product.id, product.name, product.price_yen, product.image_key) == (
        product.id, "独自", 240, public_key)
    shown = page(authenticated_client, "/register/preview").get_data(as_text=True)
    assert "product_images/admin/" + "a" * 32 + ".webp" in shown
    assert post_image(authenticated_client, path, private_key, original).status_code == 409
    fresh = BeautifulSoup(page(authenticated_client, path).data, "html.parser")
    signed = fresh.select_one('input[name="signed_original"]')["value"]
    assert post_image(authenticated_client, path, "", signed).status_code == 303
    assert product.image_key is None
    guest_owner, guest_client = guest(flask_app)
    guest_product = RegisterProduct(dataset_id=guest_owner.id, name="G", price_yen=100)
    db.session.add(guest_product)
    db.session.commit()
    guest_path = f"/register/products/{guest_product.id}/image"
    guest_page = BeautifulSoup(page(guest_client, guest_path).data, "html.parser")
    assert private_key not in guest_page.get_text() and guest_page.select_one(
        f'input[value="{private_key}"]') is None
    guest_signed = guest_page.select_one('input[name="signed_original"]')["value"]
    assert post_image(guest_client, guest_path, private_key, guest_signed).status_code == 400
    assert post_image(guest_client, guest_path, public_key, guest_signed).status_code == 303
    assert page(guest_client, path).status_code == 404
    assert page(authenticated_client, guest_path).status_code == 404
    assert product.image_key is None and guest_product.image_key == public_key
    assert (RegisterTransactionLine.query.one().product_name_snapshot,
            RegisterTransactionLine.query.one().unit_price_yen) == ("独自", 240)
    flask_app.config["REGISTER_V3_PRODUCTS_WRITE_ENABLED"] = False
    assert page(authenticated_client, path).status_code == 200
    assert post_image(authenticated_client, path, public_key,
                      BeautifulSoup(page(authenticated_client, path).data, "html.parser")
                      .select_one('input[name="signed_original"]')["value"]).status_code == 403
    flask_app.config["REGISTER_V3_PRODUCTS_PREVIEW_ENABLED"] = False
    assert page(authenticated_client, path).status_code == 404


def test_prep_reference_cutover_refund_unrecorded_and_no_write(flask_app, authenticated_client,
                                                                admin_dataset):
    day = datetime.date(2026, 10, 16)
    old = Product(dataset_id=admin_dataset.id, year=2026, month=10,
                  name="クロワッサン", price=100)
    db.session.add(old)
    db.session.flush()
    db.session.add(DailySales(product_id=old.id, date=day - datetime.timedelta(days=1), quantity=7))
    recipe_std = PrepRecipe(dataset_id=admin_dataset.id, product_key="template:croissant",
                            display_name="クロワッサン", base_quantity=10)
    recipe_old = PrepRecipe(dataset_id=admin_dataset.id, product_key=f"product:{old.id}",
                            display_name="クロワッサン（旧）", base_quantity=10)
    db.session.add_all([recipe_std, recipe_old])
    db.session.commit()
    db.session.add(PrepPlan(dataset_id=admin_dataset.id, recipe_id=recipe_std.id,
                            business_date=day, quantity=50))
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=day))
    std = RegisterProduct(dataset_id=admin_dataset.id, template_key="croissant",
                          name="クロワッサン", price_yen=120)
    custom = RegisterProduct(dataset_id=admin_dataset.id, name="クロワッサン", price_yen=140)
    db.session.add_all([std, custom])
    db.session.commit()
    register(admin_dataset, day, (std, "クロワッサン", 120, 4),
             (custom, "クロワッサン", 140, 3))
    register(admin_dataset, day, (std, "クロワッサン", 120, -4), kind="refund")
    before = (PrepPlan.query.one().quantity, PrepRecipe.query.count(), DailySales.query.one().quantity)
    off = page(authenticated_client, "/prep?date=2026-10-16")
    assert "販売実績（" not in off.get_data(as_text=True)
    flask_app.config["REGISTER_V3_AGGREGATION_ENABLED"] = True
    html = BeautifulSoup(page(authenticated_client, "/prep?date=2026-10-16").data,
                         "html.parser")
    rows = html.select(".prep-sales-reference")
    assert "0個" in rows[0].get_text() and "未設定" in rows[1].get_text()
    assert "レジ商品ID" in html.get_text() and "3個" in html.get_text()
    previous = page(authenticated_client, "/prep/plans?date=2026-10-15").get_data(as_text=True)
    assert "7個" in previous and "レジ商品・旧商品との対応が未設定" in previous
    blank = page(authenticated_client, "/prep?date=2026-10-17").get_data(as_text=True)
    assert "未記録" in blank and "0個（返金差引）" not in blank
    assert (PrepPlan.query.one().quantity, PrepRecipe.query.count(), DailySales.query.one().quantity) == before
    guest_owner, guest_client = guest(flask_app)
    assert page(guest_client, "/prep?date=2026-10-16").status_code == 403
    db.session.add(DailySales(product_id=old.id, date=day, quantity=1))
    db.session.commit()
    unavailable = page(authenticated_client, "/prep?date=2026-10-16")
    assert unavailable.status_code == 503
    assert "販売実績（" not in unavailable.get_data(as_text=True)
