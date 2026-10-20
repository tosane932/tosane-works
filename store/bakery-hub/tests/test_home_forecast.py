"""予報は合成データのみ。JSTの日境界・欠損・部分日の誤表示を防ぐ。"""
import datetime as dt

from home_forecast import met_daily, open_daily

JST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.date(2026, 10, 20)


def point(day, hour, temperature=20, symbol='clearsky_day'):
    target = dt.datetime.combine(day, dt.time(hour), JST)
    return {'time': target.astimezone(dt.timezone.utc).isoformat(), 'data': {
        'instant': {'details': {'air_temperature': temperature, 'precipitation_amount': 80}},
        'next_1_hours': {'summary': {'symbol_code': symbol}},
    }}


def test_met_jst_coverage_and_no_precipitation_probability():
    series = [point(TODAY, h, h) for h in range(12, 24)]
    tomorrow = TODAY + dt.timedelta(days=1)
    series += [point(tomorrow, h, h) for h in range(24)]
    result = met_daily(series, {'air_temperature': 'celsius'}, TODAY, lambda _: 0)
    assert len(result) == 2
    assert result[0]['date'] == '2026-10-20'
    assert result[0]['temperature_min'] == 12
    assert result[0]['temperature_max'] == 23
    assert result[0]['complete'] is False
    assert result[0]['from_at'].startswith('2026-10-20T12:00')
    assert result[0]['precipitation_probability'] is None
    assert result[1]['complete'] is True
    assert result[1]['temperature_min'] == 0


def test_met_horizon_sparse_invalid_and_missing_units():
    series = [point(TODAY + dt.timedelta(days=d), h, 20) for d in range(10) for h in (0, 6, 12, 18)]
    result = met_daily(series, {'air_temperature': 'celsius'}, TODAY, lambda _: 0)
    assert len(result) == 7
    assert all(not row['complete'] for row in result)
    assert met_daily(series, {}, TODAY, lambda _: 0)[0]['temperature_max'] is None
    bad = [point(TODAY, 0, float('nan')), point(TODAY, 1, True), {'time': 'bad'}, point(TODAY, 2, 999)]
    result = met_daily(bad, {'air_temperature': 'celsius'}, TODAY, lambda _: 0)
    assert all(row['temperature_max'] is None for row in result)
    assert met_daily([{'time': '2026-10-20T00:00:00', 'data': {}}], {}, TODAY, lambda _: 0) == []


def test_met_transition_requires_dense_unambiguous_morning_and_afternoon():
    series = [point(TODAY, h, symbol='clearsky_day' if h < 12 else 'rain')
              for h in list(range(6, 12)) + list(range(13, 19))]
    codes = lambda symbol: 0 if symbol == 'clearsky_day' else 61
    assert met_daily(series, {'air_temperature': 'celsius'}, TODAY, codes)[0]['transition_label'] == '晴れのち雨'
    assert met_daily(series[::3], {'air_temperature': 'celsius'}, TODAY, codes)[0]['transition_label'] is None
    ambiguous = series + [point(TODAY, 9, symbol='partlycloudy_day')]
    assert met_daily(ambiguous, {'air_temperature': 'celsius'}, TODAY,
                     lambda symbol: 2 if symbol == 'partlycloudy_day' else codes(symbol))[0]['transition_label'] is None
    assert open_daily(open_payload(), TODAY)[0]['transition_label'] is None


def open_payload():
    return {'timezone': 'Asia/Tokyo', 'utc_offset_seconds': 32400,
            'daily_units': {'temperature_2m_max': '°C', 'temperature_2m_min': '°C',
                            'precipitation_probability_max': '%'},
            'daily': {'time': ['2026-10-20'], 'weather_code': [61],
                      'temperature_2m_max': [25], 'temperature_2m_min': [15],
                      'precipitation_probability_max': [70]}}


def test_open_daily_probability_max_and_temperature_validation():
    data = open_payload()
    row = open_daily(data, TODAY)[0]
    assert row['complete'] is True
    assert row['precipitation_probability'] == 70
    assert row['probability_basis'] == '日内最大'
    data['daily']['precipitation_probability_max'] = [None]
    data['daily']['precipitation_sum'] = [80]
    assert open_daily(data, TODAY)[0]['precipitation_probability'] is None
    data['daily']['temperature_2m_min'] = [30]
    assert open_daily(data, TODAY)[0]['temperature_max'] is None
    data['timezone'] = 'UTC'
    assert open_daily(data, TODAY) == []


def test_open_invalid_shapes_and_dates_are_optional():
    for daily in (None, [], {'time': ['bad']}, {'time': '2026-10-20'}):
        data = open_payload()
        data['daily'] = daily
        assert open_daily(data, TODAY) == []


def test_open_request_adds_daily_without_another_fetch(monkeypatch):
    import json
    from urllib.parse import parse_qs, urlsplit
    import current_weather as weather
    weather._cache.clear()
    data = open_payload()
    today = dt.datetime.now(JST)
    data['current'] = {'weather_code': 61, 'time': today.strftime('%Y-%m-%dT%H:%M')}
    data['daily']['time'] = [today.date().isoformat()]
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def read(self, size): return json.dumps(data).encode()[:size]
    def request(req, timeout):
        calls.append(req)
        assert timeout == 6
        return Response()
    monkeypatch.setattr(weather, 'urlopen', request)
    config = dict(WEATHER_API_MODE='noncommercial', LOCAL_DEVELOPMENT=True,
                  WEATHER_LATITUDE='34.4', WEATHER_LONGITUDE='132.4')
    result = weather.current_weather(config)
    assert result['available'] is True
    assert result['forecast_days'][0]['precipitation_probability'] == 70
    assert parse_qs(urlsplit(calls[0].full_url).query)['forecast_days'] == ['7']
    assert 'precipitation_probability_max' in parse_qs(urlsplit(calls[0].full_url).query)['daily'][0]
    assert weather.current_weather(config) == result
    assert len(calls) == 1
