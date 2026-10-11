"""過去の売上日とひもづく天気記録の表示補助。"""

def weather_category(code):
    if code == 0:
        return "晴れ"
    if code in (1, 2):
        return "晴れ・曇り"
    if code == 3:
        return "曇り"
    if code in (45, 48):
        return "霧"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "雨"
    if code in (71, 73, 75, 77, 85, 86):
        return "雪"
    if code in (95, 96, 99):
        return "雷雨"
    return "不明"


import datetime
import json
import math
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from models import DailyWeather, db
from sales_period_summary import period_bounds

ARCHIVE_LAG_DAYS = 6
FREE_URL = "https://archive-api.open-meteo.com/v1/archive"
PAID_URL = "https://customer-archive-api.open-meteo.com/v1/archive"


class WeatherHistoryError(Exception):
    pass


def _settings(config):
    try:
        lat = float(config["WEATHER_LATITUDE"])
        lon = float(config["WEATHER_LONGITUDE"])
    except (TypeError, ValueError, KeyError) as exc:
        raise WeatherHistoryError("店舗の緯度経度が未設定です。") from exc
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise WeatherHistoryError("店舗の緯度経度が不正です。")
    if config.get("WEATHER_API_MODE") == "commercial" and config.get("WEATHER_API_KEY"):
        return lat, lon, PAID_URL, config["WEATHER_API_KEY"]
    if config.get("WEATHER_API_MODE") == "noncommercial" and config.get("LOCAL_DEVELOPMENT") is True:
        return lat, lon, FREE_URL, None
    raise WeatherHistoryError("気象APIの利用設定がありません。")


def stored_weather(dataset_id, start, end):
    rows = DailyWeather.query.filter(
        DailyWeather.dataset_id == dataset_id,
        DailyWeather.date >= start,
        DailyWeather.date <= end,
    ).order_by(DailyWeather.date).all()
    return [{
        "date": row.date.isoformat(),
        "weather": weather_category(row.weather_code),
        "weather_code": row.weather_code,
        "precipitation_mm": row.precipitation_mm,
        "max_temperature_c": row.max_temperature_c,
        "source": row.source,
    } for row in rows]


def import_weather(dataset_id, period, offset, today, config):
    """明示的な管理者操作時に、公開済みのERA5過去データを一括保存する。"""
    if period not in ("day", "week", "month"):
        raise WeatherHistoryError("天気の取得は日・週・月単位です。")
    start, end = period_bounds(period, offset, today)
    end = min(end, today - datetime.timedelta(days=ARCHIVE_LAG_DAYS))
    if end < start:
        return {"saved": 0, "message": "確定データ公開前です。"}
    if (end - start).days > 30:
        raise WeatherHistoryError("取得期間が長すぎます。")
    lat, lon, base, api_key = _settings(config)
    already = set(date for date, in db.session.query(DailyWeather.date).filter(
        DailyWeather.dataset_id == dataset_id,
        DailyWeather.date >= start, DailyWeather.date <= end,
    ).all())
    if len(already) == (end - start).days + 1:
        return {"saved": 0, "message": "保存済みです。"}
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "weather_code,precipitation_sum,temperature_2m_max",
        "timezone": "Asia/Tokyo", "models": "era5",
    }
    if api_key:
        params["apikey"] = api_key
    try:
        with urlopen(Request(base + "?" + urlencode(params)), timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("large response")
        daily = json.loads(raw)["daily"]
        dates = daily["time"]
        codes = daily["weather_code"]
        rains = daily["precipitation_sum"]
        temps = daily["temperature_2m_max"]
        length = (end - start).days + 1
        if any(len(values) != length for values in (dates, codes, rains, temps)):
            raise ValueError("unexpected response size")
        pending = []
        for n in range(length):
            day = start + datetime.timedelta(days=n)
            code = codes[n]
            if dates[n] != day.isoformat() or type(code) is not int or code not in range(100):
                raise ValueError("unexpected weather entry")
            rain = rains[n]
            temp = temps[n]
            for value, low, high in ((rain, 0, 2000), (temp, -100, 70)):
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high):
                    raise ValueError("invalid weather measure")
            if day not in already:
                pending.append(DailyWeather(
                    dataset_id=dataset_id, date=day, weather_code=code,
                    precipitation_mm=rain, max_temperature_c=temp,
                    latitude=lat, longitude=lon, source="Open-Meteo ERA5",
                ))
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise WeatherHistoryError("過去の天気の取得・検証に失敗しました。") from exc
    try:
        db.session.add_all(pending)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise WeatherHistoryError("過去の天気の保存に失敗しました。") from exc
    return {"saved": len(pending), "message": "履歴を保存しました。"}
