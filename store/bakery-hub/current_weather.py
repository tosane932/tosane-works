"""設定済みの店舗地点で現在のモデル推定天気を取得する。過去のERA5記録とは別。"""
import datetime
import json
import logging
import threading
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from weather_history import PAID_URL, WeatherHistoryError, _settings, weather_category

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
FORECAST_FREE = "https://api.open-meteo.com/v1/forecast"
FORECAST_PAID = "https://customer-api.open-meteo.com/v1/forecast"
_cache_lock = threading.Lock()
_cache = {}


def _read_weather(config):
    lat, lon, archive_url, api_key = _settings(config)
    url = FORECAST_PAID if archive_url == PAID_URL else FORECAST_FREE
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "weather_code",
        "timezone": "Asia/Tokyo",
    }
    if api_key:
        params["apikey"] = api_key
    with urlopen(Request(url + "?" + urlencode(params)), timeout=6) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("Oversized weather response")
    data = json.loads(raw)
    current = data["current"]
    code = current["weather_code"]
    observed_at = datetime.datetime.fromisoformat(current["time"])
    if type(code) is not int or not 0 <= code <= 99 or observed_at.tzinfo is not None:
        raise ValueError("Invalid current weather")
    observed_at = observed_at.replace(tzinfo=JST)
    now = datetime.datetime.now(JST)
    if observed_at.date() != now.date() or abs((now - observed_at).total_seconds()) > 7200:
        raise ValueError("Stale current weather")
    return {
        "available": True,
        "weather": weather_category(code),
        "weather_code": code,
        "valid_at": observed_at.isoformat(timespec="minutes"),
        "fetched_at": now.isoformat(timespec="minutes"),
        "location": (config.get("WEATHER_LOCATION_NAME") or "").strip()[:80] or f"北緯{lat:g}°・東経{lon:g}°",
        "source": "Open-Meteo 現在のモデル推定",
    }


def current_weather(config):
    """失敗や未設定はその理由を返す。HTTP API呼び出しを10分間隔に抑える。"""
    try:
        _settings(config)
    except WeatherHistoryError as exc:
        return {"available": False, "reason": str(exc)}
    key = (
        config.get("WEATHER_LATITUDE"),
        config.get("WEATHER_LONGITUDE"),
        config.get("WEATHER_API_MODE"),
        config.get("WEATHER_API_KEY"),
        config.get("LOCAL_DEVELOPMENT"),
        config.get("WEATHER_LOCATION_NAME"),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1].copy()
        try:
            result = _read_weather(config)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            logger.warning("Current weather could not be retrieved.")
            result = {"available": False, "reason": "現在の天気を取得できませんでした。時間をおいて再確認してください。"}
        lifetime = datetime.timedelta(minutes=10 if result["available"] else 2)
        _cache.clear()
        _cache[key] = (now + lifetime, result)
        return result.copy()
