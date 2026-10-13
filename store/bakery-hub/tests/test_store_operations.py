import datetime
import json
from urllib.parse import urlparse

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy.exc import SQLAlchemyError

import app as app_module
import current_weather as weather_module
from models import Dataset, ShopSchedule, ShopTask, db
from shop_schedules import JST, UTC, daily_schedules, parse_local_minute, schedule_snapshot


def guest_client(flask_app, *, expired=False):
    now = datetime.datetime.now(UTC)
    dataset = Dataset(
        kind="guest", system_key=None,
        created_at=now - datetime.timedelta(hours=3 if expired else 1),
        last_activity_at=now - datetime.timedelta(minutes=31 if expired else 1),
        absolute_expires_at=now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1),
    )
    db.session.add(dataset)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{dataset.id}"
        session["_fresh"] = True
    return client, dataset


def create_row(dataset_id, local_start, local_end=None, title="追加製造"):
    row = ShopSchedule(
        dataset_id=dataset_id, title=title,
        starts_at=parse_local_minute(local_start),
        ends_at=parse_local_minute(local_end) if local_end else None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_home_requires_identity_and_dataset(client, authenticated_client, admin_dataset, flask_app):
    g.pop("_login_user", None)
    assert urlparse(client.get("/home").headers["Location"]).path == "/login"
    g.pop("_login_user", None)
    assert urlparse(client.get("/api/home/weather").headers["Location"]).path == "/login"
    g.pop("_login_user", None)
    assert authenticated_client.get("/home").status_code == 200
    guest, _ = guest_client(flask_app)
    g.pop("_login_user", None)
    assert guest.get("/home").status_code == 200
    expired, _ = guest_client(flask_app, expired=True)
    g.pop("_login_user", None)
    assert urlparse(expired.get("/home").headers["Location"]).path == "/login"


def test_home_is_separate_from_product_form(authenticated_client, admin_dataset):
    document = BeautifulSoup(authenticated_client.get("/home").get_data(as_text=True), "html.parser")
    home_control = document.select_one('button[data-page="home"]')
    assert home_control["aria-current"] == "page"
    assert home_control["aria-controls"] == "app-home-submenu"
    assert home_control["aria-expanded"] == "false"
    assert document.select_one("#app-home-submenu").has_attr("hidden")
    assert document.select_one("#staff") is None
    assert document.select_one("#weather summary") is not None
    assert document.select_one("#puoppo form") is None
    assert "AI分析連携は現在利用できません" in document.select_one("#puoppo").get_text()
    assert document.select_one('#operations-schedule-list li').get_text(strip=True) == "今日の予定はありません。"
    assert authenticated_client.get("/").status_code == 200
    assert "商品・メニュー登録" in authenticated_client.get("/").get_data(as_text=True)


def test_puoppo_handoff_is_admin_only_and_get_has_no_ai_side_effect(
    authenticated_client, admin_dataset, flask_app,
):
    flask_app.config["PUOPPO_INTEGRATION_ENABLED"] = True
    try:
        document = BeautifulSoup(authenticated_client.get("/home").get_data(as_text=True), "html.parser")
        form = document.select_one("#puoppo form")
        assert form["method"].lower() == "get"
        assert form["action"] == "https://puoppo.onrender.com/"
        assert form.select_one('input[name="keyword"]')["maxlength"] == "100"
        guest, _ = guest_client(flask_app)
        g.pop("_login_user", None)
        guest_document = BeautifulSoup(guest.get("/home").get_data(as_text=True), "html.parser")
        assert guest_document.select_one("#puoppo form") is None
    finally:
        flask_app.config["PUOPPO_INTEGRATION_ENABLED"] = False


def test_schedule_page_opens_parent_submenu(authenticated_client, admin_dataset):
    document = BeautifulSoup(authenticated_client.get("/schedule").get_data(as_text=True), "html.parser")
    assert document.select_one(".app-sidebar-submenu-toggle") is None
    assert not document.select_one("#app-home-submenu").has_attr("hidden")
    assert document.select_one('#app-home-submenu a[href="/schedule"]')["aria-current"] == "page"
    assert document.select_one('a[data-page="home"]').get("aria-current") is None
    assert document.select_one('a[href="/home"]').get_text(" ", strip=True) == "Bakery Hub"


def test_schedule_create_edit_delete_and_home_reflection(authenticated_client, admin_dataset, csrf_token, monkeypatch):
    day = app_module.business_today()
    start = f"{day.isoformat()}T10:30"
    end = f"{day.isoformat()}T11:00"
    payload = {
        "title": "<追加製造>",
        "starts_at": start,
        "ends_at": end,
        "note": "<script>alert(1)</script>",
        "csrf_token": csrf_token(authenticated_client, "/schedule/new"),
    }
    result = authenticated_client.post("/schedule/new", data=payload)
    assert result.status_code == 303
    row = ShopSchedule.query.one()
    assert row.dataset_id == admin_dataset.id
    assert row.starts_at.tzinfo is None or row.starts_at.utcoffset() == datetime.timedelta(0)
    detail = authenticated_client.get(result.headers["Location"]).get_data(as_text=True)
    assert "&lt;script&gt;" in detail and "<script>alert(1)</script>" not in detail
    assert "&lt;追加製造&gt;" in authenticated_client.get("/home").get_data(as_text=True)
    response = authenticated_client.get(f"/api/home/schedule")
    assert response.json["schedules"][0]["title"] == "<追加製造>"
    edit_url = f"/schedule/{row.id}/edit"
    result = authenticated_client.post(edit_url, data={
        **payload, "title": "材料納品",
        "csrf_token": csrf_token(authenticated_client, edit_url),
    })
    assert result.status_code == 303
    assert ShopSchedule.query.one().title == "材料納品"
    delete_url = f"/schedule/{row.id}/delete"
    assert authenticated_client.post(delete_url).status_code == 400  # CSRF
    assert authenticated_client.post(delete_url, data={
        "csrf_token": csrf_token(authenticated_client, "/home"),
    }).status_code == 303
    assert ShopSchedule.query.count() == 0


def test_schedule_validation_and_rollback(authenticated_client, admin_dataset, csrf_token, monkeypatch):
    token = csrf_token(authenticated_client, "/schedule/new")
    for start, end in (
        ("2026-02-30T10:00", ""), ("2026-10-13T10:00Z", ""),
        ("2026-10-13T10:00", "2026-10-13T09:59"),
    ):
        response = authenticated_client.post("/schedule/new", data={
            "title": "検証", "starts_at": start, "ends_at": end, "csrf_token": token,
        })
        assert response.status_code == 400
    assert ShopSchedule.query.count() == 0
    assert authenticated_client.get("/schedule?date=2026-02-30").status_code == 400

    def fail():
        raise SQLAlchemyError("expected database failure")
    monkeypatch.setattr(db.session, "commit", fail)
    response = authenticated_client.post("/schedule/new", data={
        "title": "失敗", "starts_at": "2026-10-13T10:00", "csrf_token": token,
    })
    assert response.status_code == 503
    assert ShopSchedule.query.count() == 0


def test_japan_date_filtering_order_and_dataset_boundary(flask_app, authenticated_client, admin_dataset, csrf_token):
    guest, guest_dataset = guest_client(flask_app)
    a = create_row(admin_dataset.id, "2026-10-13T00:05", title="早朝")
    b = create_row(admin_dataset.id, "2026-10-13T23:55", title="夜")
    create_row(admin_dataset.id, "2026-10-14T00:00", title="翌日")
    other = create_row(guest_dataset.id, "2026-10-13T01:00", title="ゲスト専用")
    day = datetime.date(2026, 10, 13)
    assert [row.id for row in daily_schedules(admin_dataset.id, day)] == [a.id, b.id]
    assert [row.id for row in daily_schedules(guest_dataset.id, day)] == [other.id]
    admin_html = authenticated_client.get("/schedule?date=2026-10-13").get_data(as_text=True)
    g.pop("_login_user", None)
    guest_html = guest.get("/schedule?date=2026-10-13").get_data(as_text=True)
    assert "ゲスト専用" not in admin_html
    assert "早朝" not in guest_html
    g.pop("_login_user", None)
    for path in (f"/schedule/{other.id}", f"/schedule/{other.id}/edit"):
        assert authenticated_client.get(path).status_code == 404
    assert authenticated_client.post(f"/schedule/{other.id}/delete", data={
        "csrf_token": csrf_token(authenticated_client, "/home"),
    }).status_code == 404
    assert ShopSchedule.query.filter_by(id=other.id).count() == 1
    assert ShopTask.query.count() == 0


def test_guest_can_create_own_schedule_without_touching_tasks(flask_app, csrf_token):
    guest, dataset = guest_client(flask_app)
    token = csrf_token(guest, "/schedule/new")
    response = guest.post("/schedule/new", data={
        "title": "材料納品", "starts_at": "2026-10-13T13:00",
        "csrf_token": token,
    })
    assert response.status_code == 303
    assert ShopSchedule.query.filter_by(dataset_id=dataset.id).one().title == "材料納品"
    assert ShopTask.query.count() == 0


def test_guest_cleanup_includes_only_expired_schedules(flask_app, admin_dataset):
    active, active_dataset = guest_client(flask_app)
    expired, expired_dataset = guest_client(flask_app, expired=True)
    admin_row = create_row(admin_dataset.id, "2026-10-13T10:00")
    active_row = create_row(active_dataset.id, "2026-10-13T11:00")
    old_row = create_row(expired_dataset.id, "2026-10-13T12:00")
    old_id, admin_id, active_id = old_row.id, admin_row.id, active_row.id
    assert app_module._cleanup_expired_guest_datasets() == 1
    db.session.commit()
    assert ShopSchedule.query.filter_by(id=old_id).first() is None
    assert ShopSchedule.query.filter_by(id=admin_id).first() is not None
    assert ShopSchedule.query.filter_by(id=active_id).first() is not None


def test_current_weather_is_not_archive_and_needs_valid_configuration(authenticated_client, admin_dataset, flask_app, monkeypatch):
    monkeypatch.setattr(weather_module, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setitem(flask_app.config, "WEATHER_LATITUDE", None)
    assert authenticated_client.get("/api/home/weather").json == {
        "available": False, "reason": "店舗の緯度経度が未設定です。",
    }

    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size):
            return json.dumps({"current": {
                "time": datetime.datetime.now(JST).strftime("%Y-%m-%dT%H:%M"),
                "weather_code": 61,
            }}).encode()
    def open_weather(request, timeout):
        calls.append(request.full_url)
        return Response()
    monkeypatch.setattr(weather_module, "urlopen", open_weather)
    for key, value in {
        "WEATHER_LATITUDE": "34.4", "WEATHER_LONGITUDE": "132.4",
        "WEATHER_API_MODE": "commercial", "WEATHER_API_KEY": "test-key",
        "WEATHER_LOCATION_NAME": "広島の店舗",
    }.items():
        monkeypatch.setitem(flask_app.config, key, value)
    result = authenticated_client.get("/api/home/weather").json
    assert result["available"] is True
    assert result["weather"] == "雨"
    assert result["location"] == "広島の店舗"
    assert result["source"] == "Open-Meteo 現在のモデル推定"
    assert calls[0].startswith(weather_module.FORECAST_PAID)
    assert "archive" not in calls[0]
    assert len(calls) == 1
    authenticated_client.get("/api/home/weather")
    assert len(calls) == 1  # cache
