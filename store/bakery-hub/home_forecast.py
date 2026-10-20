"""ホーム表示用の予報。履歴への保存・外部通信は行わない。"""
import datetime as dt
import math
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')


def _number(value, low, high):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def _row(day):
    return dict(date=day.isoformat(), weather_code=None, temperature_max=None,
                temperature_min=None, precipitation_probability=None,
                probability_basis=None, complete=False, from_at=None, to_at=None,
                representative_at=None, temperature_basis='時刻別予報の取得分')


def met_daily(series, units, today, symbol_to_code):
    """JSTの時刻別気温を集計。疎な時系列を全日の最高・最低と呼ばない。"""
    groups = {}
    temperatures_available = isinstance(units, dict) and units.get('air_temperature') == 'celsius'
    for item in series:
        try:
            target = dt.datetime.fromisoformat(item['time'].replace('Z', '+00:00'))
            if target.tzinfo is None:
                continue
            target = target.astimezone(JST)
            if not 0 <= (target.date() - today).days < 7:
                continue
            data = item['data']
            temperature = _number(data.get('instant', {}).get('details', {}).get('air_temperature'), -100, 70) if temperatures_available else None
            code = None
            for period in ('next_1_hours', 'next_6_hours', 'next_12_hours'):
                symbol = data.get(period, {}).get('summary', {}).get('symbol_code')
                if symbol is not None:
                    try:
                        code = symbol_to_code(symbol)
                    except ValueError:
                        pass
                    break
            if temperature is not None or code is not None:
                groups.setdefault(target.date(), {})[target] = (temperature, code)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    result = []
    for day, points in sorted(groups.items()):
        row = _row(day)
        temps = [(t, v[0]) for t, v in sorted(points.items()) if v[0] is not None]
        if temps:
            row.update(temperature_max=max(v for _, v in temps), temperature_min=min(v for _, v in temps),
                       from_at=temps[0][0].isoformat(timespec='minutes'), to_at=temps[-1][0].isoformat(timespec='minutes'))
            expected = {dt.datetime.combine(day, dt.time(h), JST) for h in range(24)}
            row['complete'] = expected.issubset({t for t, _ in temps})
        symbols = [(t, v[1]) for t, v in points.items() if v[1] is not None]
        if symbols:
            target, code = min(symbols, key=lambda pair: abs((pair[0] - dt.datetime.combine(day, dt.time(12), JST)).total_seconds()))
            row.update(weather_code=code, representative_at=target.isoformat(timespec='minutes'))
        # compact/global の降水量は降水確率ではない。確率は常に未提供。
        result.append(row)
    return result


def open_daily(data, today):
    """Open-MeteoがJSTで返す日別集計のみ使用。並列配列の欠損を許容する。"""
    daily, units = data.get('daily'), data.get('daily_units')
    if data.get('timezone') != 'Asia/Tokyo' or data.get('utc_offset_seconds') != 32400 or not isinstance(daily, dict) or not isinstance(units, dict):
        return []
    dates = daily.get('time')
    if not isinstance(dates, list) or len(dates) > 16:
        return []
    def value(key, index):
        values = daily.get(key)
        return values[index] if isinstance(values, list) and index < len(values) else None
    result, seen = [], set()
    for index, date in enumerate(dates):
        try:
            day = dt.date.fromisoformat(date)
        except (TypeError, ValueError):
            continue
        if day.isoformat() != date or not 0 <= (day - today).days < 7 or day in seen:
            continue
        seen.add(day)
        row = _row(day)
        high = _number(value('temperature_2m_max', index), -100, 70)
        low = _number(value('temperature_2m_min', index), -100, 70)
        if units.get('temperature_2m_max') == units.get('temperature_2m_min') == '°C' and high is not None and low is not None and low <= high:
            row.update(temperature_max=high, temperature_min=low, complete=True,
                       temperature_basis='提供元の日別予報', from_at=f'{date}T00:00+09:00', to_at=f'{date}T23:59+09:00')
        code = value('weather_code', index)
        row['weather_code'] = code if type(code) is int and 0 <= code <= 99 else None
        if units.get('precipitation_probability_max') == '%':
            row['precipitation_probability'] = _number(value('precipitation_probability_max', index), 0, 100)
            row['probability_basis'] = '日内最大'
        if row['weather_code'] is not None or row['temperature_max'] is not None or row['precipitation_probability'] is not None:
            result.append(row)
    return result
