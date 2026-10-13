"""日別予報の保存、認可、履歴表示、Guest削除を検証する。"""
import datetime
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy.exc import OperationalError

import app as app_module
from forecast_history import save_forecast, stored_forecasts
from models import DailyForecast, DailyWeather, db
from test_store_operations import guest_client
from weather_history import WeatherHistoryError, import_weather

JST = ZoneInfo("Asia/Tokyo")
UTC = datetime.timezone.utc


def met_config():
    return {"WEATHER_API_MODE": "met_norway", "WEATHER_LATITUDE": "34.4", "WEATHER_LONGITUDE": "132.45"}


def forecast(now=None, **changes):
    now = now or datetime.datetime.now(UTC)
    local = now.astimezone(JST)
    target = local + datetime.timedelta(minutes=15)
    if target.date() != local.date():
        target = local
    result = {
        "available": True, "weather_code": 61, "source": "MET Norway 予報", "location": "広島市周辺",
        "valid_at": target.isoformat(timespec="minutes"),
        "fetched_at": local.isoformat(timespec="minutes"),
    }
    result.update(changes)
    return result


def csrf(client):
    html = client.get("/home").get_data(as_text=True)
    return BeautifulSoup(html, "html.parser").select_one('meta[name="csrf-token"]')["content"]


def test_home_post_records_first_forecast_only(authenticated_client, admin_dataset, flask_app, monkeypatch):
    flask_app.config.update(met_config())
    first = forecast()
    monkeypatch.setattr(app_module, "current_weather", lambda config: first)
    token = csrf(authenticated_client)
    assert authenticated_client.get("/api/home/weather").status_code == 200
    assert DailyForecast.query.count() == 0  # GET has no DB side effect
    assert authenticated_client.post("/api/home/weather/record").status_code == 400  # CSRF
    assert authenticated_client.post("/api/home/weather/record", headers={"X-CSRFToken": token}).json == {"saved": True}
    monkeypatch.setattr(app_module, "current_weather", lambda config: forecast(weather_code=0, location="変更後"))
    assert authenticated_client.post("/api/home/weather/record", headers={"X-CSRFToken": token}).json == {"saved": False}
    row = DailyForecast.query.one()
    assert row.dataset_id == admin_dataset.id
    assert row.business_date == datetime.datetime.now(JST).date()
    assert row.weather_code == 61 and row.location_name == "広島市周辺"
    assert row.latitude == 34.4 and row.longitude == 132.45
    assert DailyWeather.query.count() == 0
    assert stored_forecasts(admin_dataset.id, row.business_date, row.business_date)[0]["weather"] == "雨"


def test_invalid_failed_and_cross_midnight_forecast_are_not_saved(admin_dataset):
    now = datetime.datetime(2026, 10, 13, 15, 1, tzinfo=UTC)  # 10/14 00:01 JST
    for result in (
        {"available": False, "reason": "API failure"},
        forecast(now, weather_code=101),
        forecast(now, source="unknown"),
        forecast(now, fetched_at="2026-10-13T23:59:00+09:00"),
        forecast(now, valid_at="2026-10-13T23:59:00+09:00"),
        forecast(now, valid_at="broken"),
    ):
        assert save_forecast(admin_dataset.id, result, met_config(), now=now) is False
    assert DailyForecast.query.count() == 0
    assert save_forecast(admin_dataset.id, forecast(now), met_config(), now=now)
    assert DailyForecast.query.one().business_date == datetime.date(2026, 10, 14)


def test_unique_constraint_and_conflict_do_nothing(admin_dataset):
    now = datetime.datetime.now(UTC)
    assert save_forecast(admin_dataset.id, forecast(now), met_config(), now=now)
    assert not save_forecast(admin_dataset.id, forecast(now, weather_code=0), met_config(), now=now)
    row = DailyForecast.query.one()
    duplicate = DailyForecast(dataset_id=admin_dataset.id, business_date=row.business_date, weather_code=0,
                              forecast_valid_at=now, fetched_at=now, latitude=34.4, longitude=132.45,
                              location_name="別記録", source="MET Norway 予報")
    db.session.add(duplicate)
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert DailyForecast.query.one().weather_code == 61


def test_api_dataset_boundary_and_no_archive_network(flask_app, authenticated_client, admin_dataset, monkeypatch):
    from models import Dataset
    another = Dataset(kind="guest", absolute_expires_at=datetime.datetime.now(UTC) + datetime.timedelta(hours=2))
    db.session.add(another)
    db.session.commit()
    day = app_module.business_today()
    assert save_forecast(another.id, forecast(), met_config())
    db.session.add(DailyWeather(dataset_id=admin_dataset.id, date=day, weather_code=3,
                                latitude=34.4, longitude=132.45, source="Open-Meteo ERA5"))
    db.session.commit()
    monkeypatch.setattr("weather_history.urlopen", lambda *a, **kw: pytest.fail("archive API called"))
    monkeypatch.setattr("current_weather.urlopen", lambda *a, **kw: pytest.fail("forecast API called"))
    payload = authenticated_client.get("/api/weather-history?period=day&offset=0").json
    assert payload["forecasts"] == []
    assert payload["weather"][0]["weather"] == "曇り"
    assert payload["start"] == payload["end"] == day.isoformat()
    assert save_forecast(admin_dataset.id, forecast(weather_code=95), met_config())
    payload = authenticated_client.get("/api/weather-history?period=day").json
    assert payload["forecasts"][0]["weather"] == "雷雨"
    assert payload["forecasts"][0]["weather_code"] == 95
    assert payload["weather"][0]["weather"] == "曇り"
    assert payload["forecasts"][0]["valid_at"].endswith("+09:00")
    assert payload["forecasts"][0]["source"] == "MET Norway 予報"


def test_guest_forecast_cleanup_and_rollback(flask_app, admin_dataset, monkeypatch):
    active, active_dataset = guest_client(flask_app)
    expired, expired_dataset = guest_client(flask_app, expired=True)
    assert save_forecast(active_dataset.id, forecast(), met_config())
    assert save_forecast(expired_dataset.id, forecast(), met_config())
    token = csrf(active)
    flask_app.config.update(met_config())
    monkeypatch.setattr(app_module, "current_weather", lambda config: forecast())
    assert active.post("/api/home/weather/record", headers={"X-CSRFToken": token}).status_code == 200
    assert active.get("/api/weather-history?period=day").json["forecasts"][0]["weather"] == "雨"
    g.pop("_login_user", None)
    assert authenticated_status(expired) in (302, 401, 403)
    app_module._cleanup_expired_guest_datasets()
    assert DailyForecast.query.filter_by(dataset_id=expired_dataset.id).count() == 0
    assert DailyForecast.query.filter_by(dataset_id=active_dataset.id).count() == 1
    monkeypatch.setattr(app_module, "save_forecast", lambda *a, **kw: (_ for _ in ()).throw(OperationalError("insert", {}, Exception("db"))))
    rolled_back = []
    actual_rollback = db.session.rollback
    def track_rollback():
        rolled_back.append(True)
        actual_rollback()
    monkeypatch.setattr(db.session, "rollback", track_rollback)
    g.pop("_login_user", None)
    response = active.post("/api/home/weather/record", headers={"X-CSRFToken": token})
    assert response.status_code == 503
    assert rolled_back
    assert active.get("/home").status_code == 200


def authenticated_status(client):
    return client.get("/api/weather-history").status_code


def test_met_mode_does_not_treat_forecast_as_archived_observation(admin_dataset, monkeypatch):
    monkeypatch.setattr("weather_history.urlopen", lambda *a, **kw: pytest.fail("archive API called"))
    with pytest.raises(WeatherHistoryError, match="MET Norwayの直近予報から過去の観測データは取得できません"):
        import_weather(admin_dataset.id, "day", -15, datetime.date(2026, 10, 20), met_config())
