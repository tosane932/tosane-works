"""無料公開の現在予報は外部通信をモックし、認証と失敗系も検証する。"""
import datetime
import gzip
import json
import sys
from types import SimpleNamespace

import pytest
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import current_weather as weather
from test_store_operations import guest_client

JST = ZoneInfo("Asia/Tokyo")
UTC = datetime.timezone.utc


@pytest.fixture(autouse=True, params=["12:00", "23:55", "00:05"])
def weather_clock(monkeypatch, request):
    """予報と判定だけを同じ時計に固定し、認証・DBの時計には触れない。"""
    instant = datetime.datetime.fromisoformat(f"2026-10-30T{request.param}:00+09:00")
    real_datetime = datetime.datetime

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.astimezone().replace(tzinfo=None)

    module = SimpleNamespace(**{name: getattr(datetime, name) for name in dir(datetime)})
    module.datetime = Clock
    monkeypatch.setattr(weather, "datetime", module)
    monkeypatch.setattr(sys.modules[__name__], "datetime", module)
    weather._cache.clear()
    yield instant
    weather._cache.clear()


def settings(**overrides):
    config = {
        "WEATHER_LATITUDE": "34.4000", "WEATHER_LONGITUDE": "132.4500",
        "WEATHER_API_MODE": "met_norway", "WEATHER_API_KEY": None,
        "WEATHER_LOCATION_NAME": "広島市周辺", "LOCAL_DEVELOPMENT": False,
    }
    config.update(overrides)
    return config


def payload(*, symbol="partlycloudy_day", target=None, issued=None):
    now = datetime.datetime.now(UTC)
    # 正常系は当日の予報。未来の別営業日は専用テストで明示する。
    target = target or now
    issued = issued or now - datetime.timedelta(hours=1)
    return {
        "properties": {"meta": {"updated_at": issued.isoformat().replace("+00:00", "Z")}, "timeseries": [{
            "time": target.isoformat().replace("+00:00", "Z"),
            "data": {"next_1_hours": {"summary": {"symbol_code": symbol}}},
        }]},
    }


class Response:
    def __init__(self, data=None, *, expires=None, compressed=False, last_modified=None):
        self.data = data if isinstance(data, bytes) else json.dumps(data if data is not None else payload()).encode()
        self.headers = {"Expires": expires} if expires else {}
        if last_modified:
            self.headers["Last-Modified"] = last_modified
        if compressed:
            self.data = gzip.compress(self.data)
            self.headers["Content-Encoding"] = "gzip"

    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, size): return self.data[:size]


def test_met_forecast_and_cache(monkeypatch):
    weather._cache.clear()
    calls = []
    expires = (datetime.datetime.now(UTC) + datetime.timedelta(minutes=35)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def open_weather(request, timeout):
        calls.append(request)
        assert timeout == 6
        return Response(expires=expires)

    monkeypatch.setattr(weather, "urlopen", open_weather)
    result = weather.current_weather(settings())
    assert result["available"] is True
    assert result["location"] == "広島市周辺"
    assert result["weather"] == "晴れ・曇り"
    assert result["source"] == "MET Norway 予報"
    assert result["credit_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert datetime.datetime.fromisoformat(result["valid_at"]).utcoffset() == datetime.timedelta(hours=9)
    assert len(calls) == 1
    assert "34.4" in calls[0].full_url and "132.45" in calls[0].full_url
    assert "github.com/tosane932/tosane-works" in calls[0].get_header("User-agent")
    assert weather.current_weather(settings()) == result
    assert len(calls) == 1
    assert next(iter(weather._cache.values()))[0] > datetime.datetime.now(UTC) + datetime.timedelta(minutes=30)


def test_met_settings_fail_closed_without_network(monkeypatch):
    monkeypatch.setattr(weather, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    for config, expected in (
        (settings(WEATHER_LATITUDE=None), "店舗の緯度経度が未設定です。"),
        (settings(WEATHER_LONGITUDE="inf"), "店舗の緯度経度が不正です。"),
        (settings(WEATHER_LATITUDE="91"), "店舗の緯度経度が不正です。"),
        (settings(WEATHER_API_MODE="commercial"), "気象APIの利用設定がありません。"),
    ):
        assert weather.current_weather(config) == {"available": False, "reason": expected}


def test_met_gzip_and_expired_forecast_cache(monkeypatch):
    weather._cache.clear()
    calls = []
    monkeypatch.setattr(weather, "urlopen", lambda *a, **k: (calls.append(1), Response(compressed=True))[1])
    config = settings(WEATHER_LOCATION_NAME="")
    result = weather.current_weather(config)
    assert result["available"] is True
    assert result["location"] == "北緯34.4°・東経132.45°"
    key, (expires_at, cached, last_modified) = next(iter(weather._cache.items()))
    cached["valid_at"] = (datetime.datetime.now(JST) - datetime.timedelta(hours=3)).isoformat()
    weather._cache[key] = (expires_at, cached, last_modified)
    assert weather.current_weather(config)["available"] is False
    assert len(calls) == 1  # Provider expiryまで古い予報を出さず、再取得もしない


def test_met_revalidation_uses_last_modified(monkeypatch):
    weather._cache.clear()
    calls = []
    modified = "Tue, 13 Oct 2026 03:00:00 GMT"

    def open_weather(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            return Response(last_modified=modified)
        raise HTTPError(request.full_url, 304, "Not Modified", {"Expires": (
            datetime.datetime.now(UTC) + datetime.timedelta(minutes=20)
        ).strftime("%a, %d %b %Y %H:%M:%S GMT")}, None)

    monkeypatch.setattr(weather, "urlopen", open_weather)
    first = weather.current_weather(settings())
    key, (_, cached, last_modified) = next(iter(weather._cache.items()))
    weather._cache[key] = (datetime.datetime.now(UTC) - datetime.timedelta(seconds=1), cached, last_modified)
    assert weather.current_weather(settings()) == first
    assert calls[1].get_header("If-modified-since") == modified
    assert len(calls) == 2
    assert weather.current_weather(settings()) == first
    assert len(calls) == 2


def test_met_failure_timeout_invalid_json_and_old_forecast(monkeypatch):
    config = settings()
    def broken_gzip():
        response = Response(compressed=True)
        response.data = response.data[:-2]
        return response

    bad = [
        lambda: (_ for _ in ()).throw(TimeoutError("timeout")),
        lambda: (_ for _ in ()).throw(URLError("failed")),
        lambda: Response(b"{"),
        broken_gzip,
        lambda: Response(b"x" * (2 * 1024 * 1024 + 1)),
        lambda: Response(payload(target=datetime.datetime.now(UTC) - datetime.timedelta(hours=4))),
        lambda: Response(payload(issued=datetime.datetime.now(UTC) - datetime.timedelta(days=2))),
        lambda: Response(payload(symbol="unrecognised")),
    ]
    for factory in bad:
        weather._cache.clear()
        monkeypatch.setattr(weather, "urlopen", lambda *args, **kwargs: factory())
        result = weather.current_weather(config)
        assert result["available"] is False
        assert "取得できませんでした" in result["reason"]


def test_met_admin_and_guest_home_api(flask_app, authenticated_client, admin_dataset, monkeypatch):
    for key, value in settings().items():
        monkeypatch.setitem(flask_app.config, key, value)
    weather._cache.clear()
    monkeypatch.setattr(weather, "urlopen", lambda *args, **kwargs: Response())
    assert authenticated_client.get("/home").status_code == 200
    assert authenticated_client.get("/api/home/weather").json["available"] is True
    guest, _ = guest_client(flask_app)
    assert guest.get("/home").status_code == 200
    assert guest.get("/api/home/weather").json["available"] is True


def test_weekly_forecast_reuses_request_and_cache_isolation(monkeypatch):
    weather._cache.clear()
    now = datetime.datetime.now(JST)
    data = payload()
    data['properties']['meta']['units'] = {'air_temperature': 'celsius'}
    for day_offset in range(7):
        target = (now + datetime.timedelta(days=day_offset)).replace(hour=12, minute=0, second=0, microsecond=0)
        data['properties']['timeseries'].append({'time': target.isoformat(), 'data': {
            'instant': {'details': {'air_temperature': 20 + day_offset}},
            'next_6_hours': {'summary': {'symbol_code': 'clearsky_day'}, 'details': {'precipitation_amount': 90}},
        }})
    calls = []
    monkeypatch.setattr(weather, 'urlopen', lambda *a, **k: (calls.append(1), Response(data))[1])
    result = weather.current_weather(settings())
    assert len(result['forecast_days']) == 7
    assert all(row['precipitation_probability'] is None for row in result['forecast_days'])
    result['forecast_days'][0]['temperature_max'] = 999
    cached = weather.current_weather(settings())
    assert cached['forecast_days'][0]['temperature_max'] != 999
    assert len(calls) == 1


def test_met_next_jst_day_is_rejected(monkeypatch, weather_clock):
    """23:55の15分後は翌日なので本体の当日保護を維持する。"""
    target = (weather_clock + datetime.timedelta(days=1)).replace(hour=0, minute=10)
    monkeypatch.setattr(weather, "urlopen", lambda *a, **k: Response(payload(target=target)))
    result = weather.current_weather(settings())
    assert result == {"available": False, "reason": weather.UNAVAILABLE}


def test_met_default_payload_stays_on_current_jst_day(weather_clock):
    target = datetime.datetime.fromisoformat(payload()["properties"]["timeseries"][0]["time"])
    assert target.astimezone(JST).date() == weather_clock.date()
