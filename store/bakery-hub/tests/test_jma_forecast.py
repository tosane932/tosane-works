"""気象庁への実通信はせず、区域・日時・欠損を合成JSONで検証する。"""
import copy
import datetime as dt
import json

import pytest
from bs4 import BeautifulSoup

import app as app_module
import current_weather
from jma_forecast import JST, URL, parse_forecast
from test_store_operations import guest_client


NOW = dt.datetime(2026, 10, 21, 13, 10, tzinfo=JST)


def sample():
    base = dt.date(2026, 10, 21)
    dates = [(base + dt.timedelta(days=i)).isoformat() + 'T00:00:00+09:00' for i in range(8)]
    return [
        {'publishingOffice': '広島地方気象台', 'reportDatetime': '2026-10-21T11:00:00+09:00', 'timeSeries': [
            {'timeDefines': dates[:2], 'areas': [
                {'area': {'code': '340010'}, 'weatherCodes': ['203', '100'], 'weathers': ['くもり 時々 雨', '晴れ']},
                {'area': {'code': '340020'}, 'weatherCodes': ['400', '101'], 'weathers': ['雪', '晴れ 時々 くもり']},
            ]},
            {'timeDefines': [f'2026-10-21T{h:02}:00:00+09:00' for h in (6, 12, 18)] +
                            [f'2026-10-22T{h:02}:00:00+09:00' for h in (0, 6, 12, 18)], 'areas': [
                {'area': {'code': '340010'}, 'pops': ['20', '0', '80', '10', '20', '30', '40']},
                {'area': {'code': '340020'}, 'pops': ['90', '70', '50', '40', '30', '20', '10']},
            ]},
        ]},
        {'publishingOffice': '広島地方気象台', 'reportDatetime': '2026-10-20T17:00:00+09:00', 'timeSeries': [
            {'timeDefines': dates, 'areas': [
                {'area': {'code': '340000'}, 'weatherCodes': ['203', '101', '212', '300', '400', '350', '100', '200'],
                 'pops': ['', '20', '30', '40', '50', '60', '70', '80']},
            ]},
        ]},
    ]


def test_area_period_zero_and_weekly_prefecture_boundary():
    south = parse_forecast(sample(), '340010', NOW)
    north = parse_forecast(sample(), '340020', NOW)
    assert south['location'] == '広島県南部'
    assert south['weather'] == '曇り時々雨'
    assert south['forecast_days'][0]['precipitation_probability'] == 0
    assert south['forecast_days'][0]['probability_basis'] == '12〜18時'
    assert south['forecast_days'][1]['precipitation_probability'] == 10
    assert south['forecast_days'][1]['probability_basis'] == '0〜6時'
    assert north['weather'] == '雪'
    assert north['forecast_days'][0]['precipitation_probability'] == 70
    assert north['forecast_days'][0]['weather_icon'] == 'snow'
    assert len(south['forecast_days']) == 7
    assert south['forecast_days'][2]['location'] == '広島県'
    assert south['forecast_days'][2]['precipitation_probability'] == 30
    assert south['forecast_days'][2]['probability_basis'] == '当日'
    assert south['forecast_days'][2]['weather_label'] == '曇りのち一時雨'
    assert all(row['temperature_max'] is None for row in south['forecast_days'])


def test_missing_probability_and_unknown_weather_are_not_filled():
    data = sample()
    data[0]['timeSeries'][1]['areas'][0]['pops'][1] = ''
    data[1]['timeSeries'][0]['areas'][0]['weatherCodes'][2] = '999'
    result = parse_forecast(data, '340010', NOW)
    assert result['forecast_days'][0]['precipitation_probability'] is None
    assert result['forecast_days'][0]['probability_basis'] == '12〜18時'
    assert result['forecast_days'][2]['weather_label'] == '—'
    assert result['forecast_days'][2]['weather_icon'] is None


def test_previous_evening_issue_keeps_today_and_chooses_next_period():
    data = sample()
    data[0]['reportDatetime'] = '2026-10-20T17:00:00+09:00'
    now = dt.datetime(2026, 10, 21, 5, 20, tzinfo=JST)
    result = parse_forecast(data, '340010', now)
    assert result['valid_at'].startswith('2026-10-20')
    assert result['forecast_date'] == '2026-10-21'
    assert result['forecast_days'][0]['precipitation_probability'] == 20
    assert result['forecast_days'][0]['probability_basis'] == '6〜12時'


@pytest.mark.parametrize('change', [
    lambda d: d[0].update(reportDatetime='2026-10-19T11:00:00+09:00'),
    lambda d: d[0].update(reportDatetime='2026-10-21T11:00:00'),
    lambda d: d[0]['timeSeries'][1]['areas'][0].update(pops=['0']),
    lambda d: d[0]['timeSeries'][1]['timeDefines'].__setitem__(1, '2026-10-21T15:00:00+09:00'),
    lambda d: d[0]['timeSeries'][0]['areas'][0]['area'].update(code='340020'),
    lambda d: d[1]['timeSeries'][0]['areas'][0]['area'].update(code='340010'),
    lambda d: d[0].update(publishingOffice='別の官署'),
])
def test_invalid_or_stale_data_is_rejected(change):
    data = sample()
    change(data)
    with pytest.raises((ValueError, KeyError)):
        parse_forecast(data, '340010', NOW)


def test_opt_in_mode_fixed_https_and_cache_failure(monkeypatch):
    current_weather._cache.clear()
    config = {'WEATHER_API_MODE': 'jma', 'WEATHER_JMA_AREA_CODE': '340010'}
    assert current_weather.current_weather({'WEATHER_API_MODE': 'jma'})['available'] is False
    assert current_weather.current_weather({**config, 'WEATHER_JMA_AREA_CODE': '340000'})['available'] is False
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return URL
        def read(self, size): return json.dumps(data).encode()[:size]
    calls = []
    def fetch(request, timeout):
        assert request.full_url == URL and timeout == 6
        calls.append(request)
        return Response()
    monkeypatch.setattr(current_weather, 'urlopen', fetch)
    monkeypatch.setattr(current_weather.datetime, 'datetime', type('Clock', (dt.datetime,), {
        'now': classmethod(lambda cls, zone=None: NOW.astimezone(zone) if zone else NOW)
    }))
    data = sample()
    first = current_weather.current_weather(config)
    assert first['available'] and first['source'] == '気象庁 府県天気予報'
    assert current_weather.current_weather(config) == first and len(calls) == 1
    cached_key = next(iter(current_weather._cache))
    expiry, cached, modified = current_weather._cache[cached_key]
    stale = copy.deepcopy(cached)
    stale['valid_at'] = (NOW - dt.timedelta(days=2)).isoformat()
    current_weather._cache[cached_key] = (expiry, stale, modified)
    assert current_weather.current_weather(config)['available'] is False
    current_weather._cache.clear()
    monkeypatch.setattr(current_weather, 'urlopen', lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    assert current_weather.current_weather(config)['available'] is False


def test_response_redirect_and_size_are_rejected(monkeypatch):
    class Response:
        def __init__(self, url, body): self.url, self.body = url, body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return self.url
        def read(self, size): return self.body[:size]
    config = {'WEATHER_JMA_AREA_CODE': '340010'}
    for response in (Response('https://example.com/unexpected', b'[]'), Response(URL, b'x' * (256 * 1024 + 1))):
        monkeypatch.setattr(current_weather, 'urlopen', lambda *a, **k: response)
        with pytest.raises(ValueError):
            current_weather._read_jma_weather(config)


def test_jma_admin_guest_auth_and_history_is_not_rewritten(flask_app, authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'WEATHER_API_MODE', 'jma')
    monkeypatch.setitem(flask_app.config, 'WEATHER_JMA_AREA_CODE', '340010')
    monkeypatch.setattr(app_module, 'current_weather', lambda config: parse_forecast(sample(), '340010', NOW))
    assert authenticated_client.get('/api/home/weather').json['location'] == '広島県南部'
    assert authenticated_client.post('/api/home/weather/record').status_code == 400
    token = BeautifulSoup(authenticated_client.get('/home').get_data(as_text=True), 'html.parser').select_one('meta[name="csrf-token"]')['content']
    assert authenticated_client.post('/api/home/weather/record', headers={'X-CSRFToken': token}).json == {'saved': False}
    guest, _ = guest_client(flask_app)
    assert guest.get('/api/home/weather').json['available'] is True
    assert guest.get('/weather').status_code == 200
