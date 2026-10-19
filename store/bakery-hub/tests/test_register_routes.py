"""レジ公開フラグ、認可、CSRF、価格・営業日差分、旧売上保全。"""

import datetime
import json
import uuid

from bs4 import BeautifulSoup
from flask import g
import pytest

import app as app_module
from models import DailySales, Dataset, Product, RegisterCutover, RegisterProduct, RegisterTransaction, db
from register_service import TOKYO, utc_now
from register_products import ensure_standard_products


def enabled(flask_app, *, save=True):
    flask_app.config.update(REGISTER_V3_PREVIEW_ENABLED=True, REGISTER_V3_SAVE_ENABLED=save)
    owner = Dataset.query.filter_by(kind="admin", system_key="admin").one_or_none()
    if owner:
        ensure_standard_products(owner)


def guest(flask_app, *, expired=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    owner = Dataset(kind="guest", created_at=now - datetime.timedelta(hours=1),
                    last_activity_at=now - datetime.timedelta(minutes=31 if expired else 1),
                    absolute_expires_at=now + datetime.timedelta(hours=1))
    db.session.add(owner)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as cookie:
        cookie["_user_id"] = f"guest:{owner.id}"
        cookie["_fresh"] = True
    return owner, client


def checkout(client, product, token, **overrides):
    body = {"submission_key": str(uuid.uuid4()), "kind": "sale",
            "items": [[product.id, 1]], "displayed_prices": [[product.id, product.price_yen]],
            "received_yen": 500, "refund_confirmed": False,
            "client_business_date": utc_now().astimezone(TOKYO).date().isoformat()}
    body.update(overrides)
    g.pop("_login_user", None)  # fixtureの長寿命app context内でclientを切り替える。
    return client.post("/api/register/transactions", json=body,
                       headers={"X-CSRFToken": token})


def preview(client):
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    return client.get("/register/preview")


def token_and_product(response):
    page = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    token = page.select_one('meta[name="csrf-token"]')["content"]
    config = json.loads(page.select_one("#register-config").string)
    assert len([row for row in config["products"] if row["standard"]]) == 18
    return token, config, page


def cutover(owner):
    db.session.add(RegisterCutover(dataset_id=owner.id,
                                   first_register_date=datetime.date(2020, 1, 1)))
    db.session.commit()


def test_default_flags_reject_get_and_post(flask_app, authenticated_client, admin_dataset):
    assert not flask_app.config["REGISTER_V3_PREVIEW_ENABLED"]
    assert not flask_app.config["REGISTER_V3_SAVE_ENABLED"]
    assert preview(authenticated_client).status_code == 404
    token = BeautifulSoup(authenticated_client.get("/input").get_data(as_text=True),
                          "html.parser").select_one('meta[name="csrf-token"]')["content"]
    response = authenticated_client.post("/api/register/transactions", json={},
                                         headers={"X-CSRFToken": token})
    assert (response.status_code, response.json["code"]) == (403, "disabled")
    assert RegisterTransaction.query.count() == RegisterProduct.query.count() == 0


def test_preview_auth_and_eighteen_plus_custom_text_tiles(flask_app, authenticated_client, admin_dataset):
    enabled(flask_app, save=False)
    custom = RegisterProduct(dataset_id=admin_dataset.id, name="独自パン", price_yen=123, image_key=None)
    db.session.add(custom)
    db.session.commit()
    response = preview(authenticated_client)
    token, config, page = token_and_product(response)
    assert response.status_code == 200 and token
    assert len(config["products"]) == 19
    assert next(row for row in config["products"] if row["id"] == custom.id)["name"] == "独自パン"
    assert next(row for row in config["products"] if row["id"] == custom.id)["image"] is None
    assert all(row["image"].endswith(".webp") for row in config["products"] if row["id"] != custom.id)
    assert page.select_one("#complete-checkout").has_attr("disabled")
    assert "本番の売上記録には使用できません" in response.get_data(as_text=True)
    assert page.select_one(".register-mode-note").get_text(strip=True) == "開発用・未公開・保存停止"
    assert preview(authenticated_client).status_code == 200
    assert RegisterProduct.query.filter_by(dataset_id=admin_dataset.id).count() == 19
    assert preview(flask_app.test_client()).status_code == 302


def test_admin_save_retry_and_old_sales_untouched(flask_app, authenticated_client, admin_dataset):
    enabled(flask_app)
    cutover(admin_dataset)
    old = Product(dataset_id=admin_dataset.id, year=2020, month=1, name="旧", price=200)
    db.session.add(old)
    db.session.commit()
    db.session.add(DailySales(product_id=old.id, date=datetime.date(2020, 1, 1), quantity=3))
    db.session.commit()
    token, config, _ = token_and_product(preview(authenticated_client))
    item = next(row for row in config["products"] if row["price"] > 0)
    product = db.session.get(RegisterProduct, item["id"])
    key = str(uuid.uuid4())
    first = checkout(authenticated_client, product, token, submission_key=key)
    assert first.status_code == 200
    assert first.json["submission_key"] == key and first.json["total_yen"] == product.price_yen
    before = (Product.query.count(), DailySales.query.one().quantity)
    product.price_yen += 1
    product.is_active = False
    db.session.commit()
    repeated = checkout(authenticated_client, product, token, submission_key=key,
                        displayed_prices=[[product.id, item["price"]]],
                        client_business_date="2020-01-01")
    assert repeated.status_code == 200 and repeated.json == first.json
    assert RegisterTransaction.query.count() == 1
    assert (Product.query.count(), DailySales.query.one().quantity) == before


def test_price_date_cutover_and_csrf_fail_closed(flask_app, authenticated_client, admin_dataset):
    enabled(flask_app)
    token, config, _ = token_and_product(preview(authenticated_client))
    item = next(row for row in config["products"] if row["price"] > 0)
    product = db.session.get(RegisterProduct, item["id"])
    assert checkout(authenticated_client, product, token).status_code == 400  # 切替日なし。
    cutover(admin_dataset)
    price = checkout(authenticated_client, product, token,
                     displayed_prices=[[product.id, product.price_yen - 1]])
    assert (price.status_code, price.json["code"]) == (409, "price_changed")
    assert price.json["differences"] == [{"product_id": product.id, "name": product.name,
                                          "displayed_yen": product.price_yen - 1,
                                          "current_yen": product.price_yen}]
    date = checkout(authenticated_client, product, token, client_business_date="2020-01-01")
    assert (date.status_code, date.json["code"]) == (409, "business_day_changed")
    good = {"submission_key": str(uuid.uuid4()), "kind": "sale", "items": [[product.id, 1]],
            "displayed_prices": [[product.id, product.price_yen]], "received_yen": 500,
            "refund_confirmed": False,
            "client_business_date": utc_now().astimezone(TOKYO).date().isoformat()}
    assert authenticated_client.post("/api/register/transactions", json=good).status_code == 400
    assert authenticated_client.post("/api/register/transactions", json={}).status_code == 400
    assert RegisterTransaction.query.count() == 0


def test_refund_and_submission_conflict_over_http(flask_app, authenticated_client, admin_dataset):
    enabled(flask_app)
    cutover(admin_dataset)
    token, config, _ = token_and_product(preview(authenticated_client))
    product = db.session.get(RegisterProduct, config["products"][0]["id"])
    key = str(uuid.uuid4())
    refund = checkout(authenticated_client, product, token, submission_key=key,
                      kind="refund", items=[[product.id, -1]],
                      received_yen=None, refund_confirmed=True)
    assert refund.status_code == 200
    assert refund.json["kind"] == "refund" and refund.json["total_yen"] < 0
    conflict = checkout(authenticated_client, product, token, submission_key=key,
                        kind="refund", items=[[product.id, -2]],
                        received_yen=None, refund_confirmed=True)
    assert (conflict.status_code, conflict.json["code"]) == (409, "key_conflict")
    assert RegisterTransaction.query.count() == 1


def test_dataset_isolation_expiry_and_input_limits(flask_app, authenticated_client, admin_dataset):
    enabled(flask_app)
    cutover(admin_dataset)
    admin_token, admin_config, _ = token_and_product(preview(authenticated_client))
    admin_product = db.session.get(RegisterProduct, admin_config["products"][0]["id"])
    guest_a, client_a = guest(flask_app)
    guest_b, client_b = guest(flask_app)
    token_a, config_a, _ = token_and_product(preview(client_a))
    token_b, config_b, _ = token_and_product(preview(client_b))
    assert {row["id"] for row in config_a["products"]}.isdisjoint(
        {row["id"] for row in config_b["products"]})
    assert {row["id"] for row in config_a["products"]}.isdisjoint(
        {row["id"] for row in admin_config["products"]})
    foreign = checkout(client_a, admin_product, token_a)
    assert foreign.status_code == 400  # 切替日未設定Guest。
    cutover(guest_a)
    foreign = checkout(client_a, admin_product, token_a)
    assert foreign.status_code == 403
    a_product = db.session.get(RegisterProduct, config_a["products"][0]["id"])
    cutover(guest_b)
    assert checkout(client_b, a_product, token_b).status_code == 403
    assert checkout(authenticated_client, a_product, admin_token).status_code == 403
    assert checkout(client_a, a_product, token_a, dataset_id=str(admin_dataset.id)).status_code == 400
    huge = client_a.post("/api/register/transactions", data="x" * 17000,
                         content_type="application/json", headers={"X-CSRFToken": token_a})
    assert huge.status_code == 413
    guest_a.last_activity_at = utc_now() - datetime.timedelta(minutes=31)
    db.session.commit()
    assert preview(client_a).status_code in (302, 403)
    expired = checkout(client_a, a_product, token_a)
    assert expired.status_code == 403 and expired.json["code"] == "guest_expired"
    assert RegisterTransaction.query.count() == 0


def test_preview_off_save_on_does_not_open_route(flask_app, authenticated_client, admin_dataset):
    flask_app.config.update(REGISTER_V3_PREVIEW_ENABLED=False, REGISTER_V3_SAVE_ENABLED=True)
    assert preview(authenticated_client).status_code == 404
    token = BeautifulSoup(authenticated_client.get("/input").get_data(as_text=True),
                          "html.parser").select_one('meta[name="csrf-token"]')["content"]
    assert authenticated_client.post("/api/register/transactions", json={},
                                     headers={"X-CSRFToken": token}).status_code == 403


def test_read_only_status_lookup_matches_server_snapshot_and_dataset(
        flask_app, authenticated_client, admin_dataset):
    enabled(flask_app)
    cutover(admin_dataset)
    token, config, page = token_and_product(preview(authenticated_client))
    assert page.select_one("#check-register-status") is not None
    assert config["statusBase"] == "/api/register/transactions"
    product = db.session.get(RegisterProduct, config["products"][0]["id"])
    key = str(uuid.uuid4())
    assert checkout(authenticated_client, product, token, submission_key=key).status_code == 200
    product.name = "改名後"
    product.price_yen += 1
    product.is_active = False
    db.session.commit()
    path = f"/api/register/transactions/{key}/status"
    before = (RegisterTransaction.query.count(), DailySales.query.count())
    g.pop("_login_user", None)
    found = authenticated_client.get(path)
    assert found.status_code == 200 and found.headers["Cache-Control"] == "no-store"
    assert found.json["status"] == "saved"
    assert found.json["submission_key"] == key
    assert found.json["items"] == [[product.id, 1]]
    assert found.json["displayed_prices"] == [[product.id, product.price_yen - 1]]
    assert found.json["total_yen"] == product.price_yen - 1
    assert found.json["received_yen"] == 500
    guest_owner, guest_client = guest(flask_app)
    g.pop("_login_user", None)
    assert guest_client.get(path).json == {"status": "not_found"}
    g.pop("_login_user", None)
    assert authenticated_client.get(f"/api/register/transactions/{uuid.uuid4()}/status"
                                    ).json == {"status": "not_found"}
    assert (RegisterTransaction.query.count(), DailySales.query.count()) == before
    assert guest_owner.id != admin_dataset.id
    flask_app.config["REGISTER_V3_SAVE_ENABLED"] = False
    g.pop("_login_user", None)
    assert authenticated_client.get(path).status_code == 403
    flask_app.config["REGISTER_V3_SAVE_ENABLED"] = True
    g.pop("_login_user", None)
    assert flask_app.test_client().get(path).status_code == 401
