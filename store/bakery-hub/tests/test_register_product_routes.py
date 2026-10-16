"""レジ商品管理画面の公開境界、登録、編集、明示コピーと再送。"""

import datetime
import json
import uuid

from bs4 import BeautifulSoup
from flask import g

from models import DailySales, Dataset, Product, RegisterProduct, RegisterTransaction, RegisterTransactionLine, db
from register_service import save_register_transaction, utc_now


def enable(app, *, write=True):
    app.config.update(REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=True,
                      REGISTER_V3_PRODUCTS_WRITE_ENABLED=write)


def get(client, path="/register/products"):
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    return client.get(path)


def form(response):
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def submit(client, path, fields, page="/register/products"):
    html = form(get(client, page))
    token = html.select_one('input[name="csrf_token"]')["value"]
    g.pop("_login_user", None)
    return client.post(path, data={"csrf_token": token, **fields})


def guest_client(app, *, expired=False):
    now = utc_now()
    owner = Dataset(kind="guest", created_at=now - datetime.timedelta(hours=1),
                    last_activity_at=now - datetime.timedelta(minutes=31 if expired else 1),
                    absolute_expires_at=now + datetime.timedelta(hours=1))
    db.session.add(owner)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as cookie:
        cookie["_user_id"] = f"guest:{owner.id}"
        cookie["_fresh"] = True
    return owner, client


def create(client, name="独自パン", price="123", *, key=None):
    return submit(client, "/register/products/new",
                  {"name": name, "price_yen": price,
                   "request_key": key or str(uuid.uuid4())})


def test_flags_and_authentication(flask_app, authenticated_client, admin_dataset):
    assert get(authenticated_client).status_code == 404
    assert get(flask_app.test_client()).status_code == 404
    enable(flask_app, write=False)
    assert get(flask_app.test_client()).status_code == 302
    page = get(authenticated_client)
    assert page.status_code == 200
    assert "この環境では商品を変更できません" in page.get_data(as_text=True)
    assert submit(authenticated_client, "/register/products/new",
                  {"request_key": str(uuid.uuid4()), "name": "不可", "price_yen": "100"}).status_code == 403
    assert RegisterProduct.query.filter_by(dataset_id=admin_dataset.id).count() == 18
    token = form(get(authenticated_client)).select_one('input[name="csrf_token"]')["value"]
    flask_app.config.update(REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=False,
                            REGISTER_V3_PRODUCTS_WRITE_ENABLED=True)
    assert get(authenticated_client).status_code == 404
    assert authenticated_client.post("/register/products/new", data={
        "csrf_token": token, "name": "不可", "price_yen": "100",
        "request_key": str(uuid.uuid4())}).status_code == 404


def test_create_validation_retry_and_register_reflection(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    assert get(authenticated_client).status_code == 200
    for name, price in [("独自0", "0"), ("独自500", "500")]:
        assert create(authenticated_client, name, price).status_code == 303
    key = str(uuid.uuid4())
    assert create(authenticated_client, " 新商品 ", "123", key=key).status_code == 303
    assert create(authenticated_client, "新商品", "123", key=key).status_code == 303
    assert create(authenticated_client, "別名", "123", key=key).status_code == 409
    for name, price in [(" ", "100"), ("a" * 101, "100"), ("高額", "501"),
                        ("負数", "-1"), ("小数", "2.5"), ("文字", "abc")]:
        assert create(authenticated_client, name, price).status_code == 400
    assert RegisterProduct.query.filter_by(dataset_id=admin_dataset.id).count() == 21
    assert Product.query.count() == DailySales.query.count() == 0
    flask_app.config["REGISTER_V3_PREVIEW_ENABLED"] = True
    preview = get(authenticated_client, "/register/preview")
    config = json.loads(form(preview).select_one("#register-config").string)
    assert len(config["products"]) == 21
    assert config["products"][-1]["name"] == "新商品"
    assert config["products"][-1]["image"] is None
    assert get(authenticated_client).status_code == 200
    assert RegisterProduct.query.count() == 21


def test_edit_keeps_snapshot_and_detects_stale_forms(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    flask_app.config["REGISTER_V3_PREVIEW_ENABLED"] = True
    item = RegisterProduct(dataset_id=admin_dataset.id, name="発売時", price_yen=100)
    db.session.add(item)
    db.session.commit()
    from models import RegisterCutover
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                   first_register_date=datetime.date(2020, 1, 1)))
    db.session.commit()
    result = save_register_transaction(admin_dataset, submission_key=uuid.uuid4(), kind="sale",
                                       items=[(item.id, 1)], received_yen=100)
    assert result.total_yen == 100
    page_path = f"/register/products/{item.id}/edit"
    page = form(get(authenticated_client, page_path))
    original = page.select_one('input[name="signed_original"]')["value"]
    assert submit(authenticated_client, page_path,
                  {"signed_original": original, "name": "改名", "price_yen": "150"}, page_path).status_code == 303
    assert submit(authenticated_client, page_path,
                  {"signed_original": original, "name": "改名", "price_yen": "150"}, page_path).status_code == 303
    assert submit(authenticated_client, page_path,
                  {"signed_original": original, "name": "競合", "price_yen": "200"}, page_path).status_code == 409
    assert submit(authenticated_client, page_path,
                  {"signed_original": original + "x", "name": "改ざん", "price_yen": "1"}, page_path).status_code == 400
    line = RegisterTransactionLine.query.one()
    assert (line.product_name_snapshot, line.unit_price_yen, line.line_total_yen) == ("発売時", 100, 100)
    assert RegisterTransaction.query.one().total_yen == 100
    assert db.session.get(RegisterProduct, item.id).name == "改名"
    response = get(authenticated_client, "/register/preview")
    listed = json.loads(form(response).select_one("#register-config").string)["products"]
    assert any(row["id"] == item.id and row["price"] == 150 for row in listed)


def test_explicit_legacy_copy_preserves_old_rows(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    old = [Product(dataset_id=admin_dataset.id, year=2026, month=10, name="同名", price=price,
                   is_active=active, image_key="croissant") for price, active in
           [(250, True), (270, True), (510, True), (200, False)]]
    db.session.add_all(old)
    db.session.commit()
    db.session.add(DailySales(product_id=old[0].id, date=datetime.date(2026, 10, 15), quantity=2))
    db.session.commit()
    saved = [(p.id, p.name, p.price, p.image_key, p.is_active) for p in old]
    for source in old[:2]:
        path = f"/register/products/legacy/{source.id}/copy"
        html = form(get(authenticated_client, path))
        data = {"signed_original": html.select_one('input[name="signed_original"]')["value"],
                "request_key": html.select_one('input[name="request_key"]')["value"],
                "confirmed": "yes"}
        assert submit(authenticated_client, path, dict(data, confirmed="no"), path).status_code == 400
        assert submit(authenticated_client, path, data, path).status_code == 303
        assert submit(authenticated_client, path, data, path).status_code == 303
    for source in old[2:]:
        assert "コピーできません" in get(authenticated_client, f"/register/products/legacy/{source.id}/copy").get_data(as_text=True)
    assert [(p.id, p.name, p.price, p.image_key, p.is_active) for p in old] == saved
    assert DailySales.query.one().quantity == 2
    assert [(p.name, p.price_yen, p.image_key) for p in RegisterProduct.query.filter_by(
        dataset_id=admin_dataset.id, template_key=None).order_by(RegisterProduct.id)] == [
        ("同名", 250, None), ("同名", 270, None)]


def test_guest_limit_dataset_expiry_and_csrf(flask_app, authenticated_client, admin_dataset):
    enable(flask_app)
    guest_a, client_a = guest_client(flask_app)
    guest_b, client_b = guest_client(flask_app)
    assert get(client_a).status_code == get(client_b).status_code == 200
    assert create(client_a, "A", "100").status_code == 303
    assert create(client_b, "B", "200").status_code == 303
    assert RegisterProduct.query.filter_by(dataset_id=guest_a.id, template_key=None).count() == 1
    assert RegisterProduct.query.filter_by(dataset_id=guest_b.id, template_key=None).count() == 1
    item_b = RegisterProduct.query.filter_by(dataset_id=guest_b.id, template_key=None).one()
    assert get(client_a, f"/register/products/{item_b.id}/edit").status_code == 404
    assert get(authenticated_client, f"/register/products/{item_b.id}/edit").status_code == 404
    for i in range(29):
        assert create(client_a, f"A{i}", "10").status_code == 303
    assert create(client_a, "31件目", "100").status_code == 409
    assert RegisterProduct.query.filter_by(dataset_id=guest_a.id, template_key=None).count() == 30
    assert client_b.post("/register/products/new", data={"name": "CSRF", "price_yen": "100",
                                                         "request_key": str(uuid.uuid4())}).status_code == 400
    token = form(get(client_a)).select_one('input[name="csrf_token"]')["value"]
    guest_a.last_activity_at = utc_now() - datetime.timedelta(minutes=31)
    db.session.commit()
    assert get(client_a).status_code in (302, 403)
    assert client_a.post("/register/products/new", data={"csrf_token": token,
                        "name": "期限切れ", "price_yen": "100",
                        "request_key": str(uuid.uuid4())}).status_code in (302, 403)
    assert RegisterProduct.query.filter_by(dataset_id=guest_b.id, template_key=None).count() == 1


def test_create_commit_failure_rolls_back(flask_app, authenticated_client, admin_dataset, monkeypatch):
    enable(flask_app)
    token = form(get(authenticated_client)).select_one('input[name="csrf_token"]')["value"]
    original = db.session.commit

    def fail():
        raise __import__("sqlalchemy").exc.OperationalError("INSERT", {}, Exception("DB down"))

    monkeypatch.setattr(db.session, "commit", fail)
    assert authenticated_client.post("/register/products/new", data={
        "csrf_token": token, "name": "障害", "price_yen": "100",
        "request_key": str(uuid.uuid4())}).status_code == 503
    monkeypatch.setattr(db.session, "commit", original)
    assert RegisterProduct.query.filter_by(dataset_id=admin_dataset.id, template_key=None).count() == 0
