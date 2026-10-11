"""保存済みの気象履歴と取り込みの安全性を検証。"""
import datetime

import pytest

import app as app_module
from models import DailyWeather, db
from weather_history import WeatherHistoryError, import_weather, stored_weather


def test_weather_history_api_requires_authentication(client):
    response = client.get("/api/weather-history")
    assert response.status_code in (302, 401, 403)


def test_admin_can_read_persisted_weather(authenticated_client, admin_dataset):
    db.session.add(DailyWeather(
        dataset_id=admin_dataset.id,
        date=datetime.date(2026, 10, 5),
        weather_code=63,
        precipitation_mm=8.0,
        max_temperature_c=19.5,
        latitude=34.4, longitude=132.4,
        source="Open-Meteo ERA5",
    ))
    db.session.commit()
    records = stored_weather(
        admin_dataset.id, datetime.date(2026, 10, 5), datetime.date(2026, 10, 6)
    )
    assert records == [{
        "date": "2026-10-05", "weather": "雨", "weather_code": 63,
        "precipitation_mm": 8.0, "max_temperature_c": 19.5,
        "source": "Open-Meteo ERA5",
    }]


def test_import_is_opt_in_and_keeps_existing_data(flask_app, admin_dataset, monkeypatch):
    today = datetime.date(2026, 10, 20)
    config = {
        "WEATHER_LATITUDE": "34.4", "WEATHER_LONGITUDE": "132.4",
        "WEATHER_API_MODE": "noncommercial", "LOCAL_DEVELOPMENT": True,
    }
    observed_calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, _limit):
            import json
            return json.dumps({"daily": {
                "time": ["2026-10-05"],
                "weather_code": [61], "precipitation_sum": [7.0],
                "temperature_2m_max": [22.0],
            }}).encode()

    def mock_open(request, timeout):
        observed_calls.append(request.full_url)
        return Response()

    monkeypatch.setattr("weather_history.urlopen", mock_open)
    result = import_weather(admin_dataset.id, "day", -15, today, config)
    assert result["saved"] == 1
    assert len(observed_calls) == 1
    record = DailyWeather.query.filter_by(dataset_id=admin_dataset.id).one()
    assert record.weather_code == 61
    assert import_weather(admin_dataset.id, "day", -15, today, config)["saved"] == 0
    assert len(observed_calls) == 1


def test_weather_import_unconfigured_does_not_call_network(admin_dataset, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("network must not be called")
    monkeypatch.setattr("weather_history.urlopen", fail)
    with pytest.raises(WeatherHistoryError):
        import_weather(admin_dataset.id, "day", -14, datetime.date(2026, 10, 20), {})


def test_weather_import_rejects_future_or_year(authenticated_client, admin_dataset):
    assert authenticated_client.get(
        "/api/weather-history?period=week&offset=1"
    ).status_code == 400
    assert authenticated_client.post(
        "/api/weather-history/import", json={"period": "year", "offset": 0}
    ).status_code in (400, 403)


def test_guest_weather_write_denied(flask_app, admin_dataset):
    from models import Dataset
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = Dataset(kind="guest", system_key=None, created_at=now,
                    last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(guest)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{guest.id}"
        session["_fresh"] = True
    response = client.post("/api/weather-history/import",
                           json={"period": "day", "offset": -10})
    assert response.status_code == 403
