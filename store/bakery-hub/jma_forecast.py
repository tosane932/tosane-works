"""気象庁の広島県府県天気予報を、表示専用の予報へ変換する。観測値ではない。"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
URL = 'https://www.jma.go.jp/bosai/forecast/data/forecast/340000.json'
AREAS = {'340010': '広島県南部', '340020': '広島県北部'}

# 公式の天気コードのうち表示に使うものだけ。未知のコードは推測しない。
LABELS = {
    '100': '晴れ', '101': '晴れ時々曇り', '110': '晴れのち時々曇り', '111': '晴れのち曇り',
    '200': '曇り', '201': '曇り時々晴れ', '202': '曇り一時雨', '203': '曇り時々雨',
    '210': '曇りのち時々晴れ', '211': '曇りのち晴れ', '212': '曇りのち一時雨',
    '213': '曇りのち時々雨', '214': '曇りのち雨',
    '300': '雨', '301': '雨時々晴れ', '302': '雨時々やむ', '311': '雨のち晴れ',
    '313': '雨のち曇り', '350': '雨で雷を伴う',
    '400': '雪', '401': '雪時々晴れ', '402': '雪時々やむ', '411': '雪のち晴れ',
    '413': '雪のち曇り', '450': '雪で雷を伴う',
}


def _time(value):
    if not isinstance(value, str):
        raise ValueError('Invalid JMA time')
    result = dt.datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != dt.timedelta(hours=9):
        raise ValueError('Invalid JMA timezone')
    return result.astimezone(JST)


def _issue(value, now, hours):
    issue = _time(value)
    if not dt.timedelta(minutes=-10) <= now - issue <= dt.timedelta(hours=hours):
        raise ValueError('Stale JMA issue')
    return issue


def _series(block, index, area):
    series = block['timeSeries'][index]
    times = [_time(value) for value in series['timeDefines']]
    if not 1 <= len(times) <= 10 or times != sorted(set(times)):
        raise ValueError('Invalid JMA time definitions')
    matching = [entry for entry in series['areas'] if entry['area']['code'] == area]
    if len(matching) != 1:
        raise ValueError('Missing or duplicate JMA area')
    return times, matching[0]


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-4]\d{2}', value):
        return None
    return value if value in LABELS else None


def _icon(code):
    if not code:
        return None
    if code in {'350', '450'}:
        return 'storm'
    if code.startswith('4'):
        return 'snow'
    if code.startswith('3'):
        return 'rain'
    if code.startswith('2'):
        return 'partly' if '晴' in LABELS[code] else 'rain' if '雨' in LABELS[code] else 'cloud'
    return 'partly' if '曇' in LABELS[code] else 'sun'


def _probability(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9]\d?|100)', value):
        return None
    return int(value)


def parse_forecast(data, area, now):
    """短期は一次細分区域、週間は府県全域。別の区域の値を混ぜない。"""
    if area not in AREAS or not isinstance(data, list) or len(data) < 2:
        raise ValueError('Invalid JMA forecast')
    short, weekly = data[:2]
    if short.get('publishingOffice') != '広島地方気象台' or weekly.get('publishingOffice') != '広島地方気象台':
        raise ValueError('Wrong JMA office')
    issue = _issue(short['reportDatetime'], now, 16)
    weekly_issue = _issue(weekly['reportDatetime'], now, 40)
    if weekly_issue > issue:
        raise ValueError('Inconsistent JMA issue')
    dates, weathers = _series(short, 0, area)
    periods, pops = _series(short, 1, area)
    week_dates, week = _series(weekly, 0, '340000')
    if len(week_dates) > 8 or any(time.hour != 0 for time in week_dates):
        raise ValueError('Invalid JMA daily times')
    if any(time.hour % 6 for time in periods) or any(b - a != dt.timedelta(hours=6) for a, b in zip(periods, periods[1:])):
        raise ValueError('Invalid JMA probability periods')
    for entry, keys, length in ((weathers, ('weatherCodes', 'weathers'), len(dates)),
                                (pops, ('pops',), len(periods)), (week, ('weatherCodes', 'pops'), len(week_dates))):
        if any(not isinstance(entry.get(key), list) or len(entry[key]) != length for key in keys):
            raise ValueError('Misaligned JMA forecast')

    today = now.date()
    rows = {}
    for index, target in enumerate(week_dates):
        if 0 <= (target.date() - today).days < 7:
            code = _code(week['weatherCodes'][index])
            rows[target.date()] = dict(date=target.date().isoformat(), weather_code=None,
                weather_label=LABELS.get(code, '—'), weather_icon=_icon(code),
                location='広島県', precipitation_probability=_probability(week['pops'][index]),
                probability_basis='当日', temperature_max=None, temperature_min=None, complete=False)
    for index, target in enumerate(dates):
        if not 0 <= (target.date() - today).days < 7:
            continue
        code = _code(weathers['weatherCodes'][index])
        raw_text = weathers['weathers'][index]
        if not isinstance(raw_text, str) or len(raw_text) > 160:
            raise ValueError('Invalid JMA weather text')
        # 短期の公式表現は地域別。長文は簡潔な公式コード表現で表示する。
        label = LABELS.get(code, '—')
        row = rows.setdefault(target.date(), dict(date=target.date().isoformat(), weather_code=None,
            temperature_max=None, temperature_min=None, complete=False))
        row.update(weather_label=label, weather_icon=_icon(code), location=AREAS[area],
                   precipitation_probability=None, probability_basis=None)
    if today not in rows or rows[today].get('location') != AREAS[area] or rows[today]['weather_label'] == '—':
        raise ValueError('Missing JMA current day')
    for index, start in enumerate(periods):
        day = start.date()
        if day not in rows or rows[day].get('location') != AREAS[area]:
            continue
        # 現在の6時間帯を選ぶ。未来の日は最初の時間帯を選び、日全体の確率とは呼ばない。
        if day == today and not (start <= now < start + dt.timedelta(hours=6)
                                  or now < periods[0] and start == periods[0]):
            continue
        if rows[day].get('probability_basis'):
            continue
        probability = _probability(pops['pops'][index])
        rows[day]['precipitation_probability'] = probability
        rows[day]['probability_basis'] = f'{start.hour}〜{(start.hour + 6) % 24}時'
    current = rows[today]
    return dict(available=True, weather=current['weather_label'], weather_label=current['weather_label'],
                weather_icon=current['weather_icon'], weather_code=None, location=AREAS[area],
                source='気象庁 府県天気予報', credit_url=URL,
                valid_at=issue.isoformat(timespec='minutes'), forecast_date=today.isoformat(),
                fetched_at=now.isoformat(timespec='minutes'),
                forecast_days=[rows[day] for day in sorted(rows)][:7])
