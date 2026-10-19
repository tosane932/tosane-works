"""本番想定のAdmin練習画面は、商品マスタも売上も変更しない。"""

import json
import datetime

from bs4 import BeautifulSoup
from flask import g
from sqlalchemy import event

import config
from models import (DailySales, Dataset, PrepPlan, PrepRecipe, RegisterCutover,
                    RegisterProduct, RegisterTransaction, RegisterTransactionLine, db)
from product_images import PRODUCT_IMAGE_OPTIONS


def demo(client):
    g.pop("_login_user", None)
    return client.get("/register/demo")


def test_demo_default_off_and_unchanged_save_flags(flask_app, authenticated_client):
    assert config.REGISTER_V3_PUBLIC_DEMO_ENABLED is False
    assert demo(authenticated_client).status_code == 404
    flask_app.config["REGISTER_V3_PUBLIC_DEMO_ENABLED"] = True
    assert not any(flask_app.config[key] for key in (
        "REGISTER_V3_PREVIEW_ENABLED", "REGISTER_V3_SAVE_ENABLED",
        "REGISTER_V3_AGGREGATION_ENABLED", "REGISTER_V3_PRODUCTS_PREVIEW_ENABLED",
        "REGISTER_V3_PRODUCTS_WRITE_ENABLED"))
    assert authenticated_client.post("/api/register/transactions", json={}).status_code == 400  # CSRF
    page = BeautifulSoup(demo(authenticated_client).data, "html.parser")
    token = page.select_one('meta[name="csrf-token"]')["content"]
    response = authenticated_client.post("/api/register/transactions", json={},
                                         headers={"X-CSRFToken": token})
    assert (response.status_code, response.json["code"]) == (403, "disabled")


def test_demo_admin_only_and_nonwriting(flask_app, authenticated_client, admin_dataset, monkeypatch):
    flask_app.config["REGISTER_V3_PUBLIC_DEMO_ENABLED"] = True
    assert demo(flask_app.test_client()).status_code == 302
    now = datetime.datetime.now(datetime.timezone.utc)
    guest_dataset = Dataset(kind="guest", created_at=now,
                            last_activity_at=now,
                            absolute_expires_at=now + datetime.timedelta(hours=1))
    db.session.add(guest_dataset)
    db.session.commit()
    guest = flask_app.test_client()
    with guest.session_transaction() as cookie:
        cookie["_user_id"] = f"guest:{guest_dataset.id}"
        cookie["_fresh"] = True
    assert demo(guest).status_code == 403

    # 誤って従来の初期化・Dataset解決へ接続されてもテストが止まる。
    import register_routes
    monkeypatch.setattr(register_routes, "ensure_standard_products", lambda *_: 1 / 0)
    monkeypatch.setattr(register_routes, "RegisterProduct", type("NoDB", (), {
        "query": property(lambda self: 1 / 0),
    }))
    before = [model.query.count() for model in (RegisterProduct, DailySales,
        RegisterTransaction, RegisterTransactionLine, RegisterCutover, PrepPlan, PrepRecipe)]
    writes = []
    def track_writes(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)
    event.listen(db.engine, "before_cursor_execute", track_writes)
    try:
        response = demo(authenticated_client)
    finally:
        event.remove(db.engine, "before_cursor_execute", track_writes)
    assert writes == []
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    page = BeautifulSoup(response.data, "html.parser")
    config_data = json.loads(page.select_one("#register-config").string)
    assert set(config_data) == {"products", "demo"} and config_data["demo"] is True
    assert len(config_data["products"]) == len(PRODUCT_IMAGE_OPTIONS) == 18
    assert len(page.select(".product-tile")) == 18
    assert not page.select(".increase-one")
    for tile in page.select(".product-tile"):
        assert len(tile.select("button[data-action=plus]")) == 1
        assert tile.select_one(".remove-one").parent is tile
        assert tile.select_one(".quantity-label").parent is tile
        assert not tile.select("button button, button input, button label")
    assert all(item["image"].endswith(".webp") for item in config_data["products"])
    assert page.select_one('script[src="/static/register_demo.js?v=responsive-4"]')
    assert page.select_one('script[src="/static/register_layout.js?v=responsive-4"]')
    assert page.select_one('#checkout-payment #return-products')
    assert page.select_one('#register-layout-notice[hidden]')
    assert not page.select_one('script[src="/static/register.js?v=responsive-4"]')
    assert "練習用の画面です" in response.get_data(as_text=True)
    g.pop("_login_user", None)
    home = authenticated_client.get("/home")
    assert BeautifulSoup(home.data, "html.parser").select_one('a[href="/register/demo"]')
    g.pop("_login_user", None)
    guest_home = guest.get("/home")
    assert not BeautifulSoup(guest_home.data, "html.parser").select_one('a[href="/register/demo"]')
    assert [model.query.count() for model in (RegisterProduct, DailySales,
        RegisterTransaction, RegisterTransactionLine, RegisterCutover, PrepPlan, PrepRecipe)] == before


def test_missing_image_is_text_tile(flask_app, authenticated_client, monkeypatch):
    flask_app.config["REGISTER_V3_PUBLIC_DEMO_ENABLED"] = True
    import register_routes
    monkeypatch.setattr(register_routes, "register_image_choices", lambda *_args, **_kw: {})
    page = BeautifulSoup(demo(authenticated_client).data, "html.parser")
    assert len(page.select(".product-tile .no-image:not([hidden])")) == 18
    assert not page.select(".product-tile img")


def test_production_demo_flag_preserves_secure_cookie(monkeypatch):
    monkeypatch.setenv("REGISTER_V3_PUBLIC_DEMO_ENABLED", "true")
    monkeypatch.delenv("LOCAL_DEVELOPMENT", raising=False)
    import importlib
    try:
        updated = importlib.reload(config)
        assert updated.REGISTER_V3_PUBLIC_DEMO_ENABLED is True
        assert updated.SESSION_COOKIE_SECURE is True
        assert not any((updated.REGISTER_V3_PREVIEW_ENABLED, updated.REGISTER_V3_SAVE_ENABLED,
                        updated.REGISTER_V3_PRODUCTS_WRITE_ENABLED))
    finally:
        monkeypatch.delenv("REGISTER_V3_PUBLIC_DEMO_ENABLED", raising=False)
        importlib.reload(config)
