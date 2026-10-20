"""設定済みの地点で現在モデル推定または直近予報を取得する。ERA5記録とは別。"""
import datetime
from copy import deepcopy
import gzip
import json
import logging
import math
import threading
from email.utils import parsedate_to_datetime
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from home_forecast import met_daily, open_daily
from weather_history import PAID_URL, WeatherHistoryError, _settings, weather_category

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
FORECAST_FREE = "https://api.open-meteo.com/v1/forecast"
FORECAST_PAID = "https://customer-api.open-meteo.com/v1/forecast"
MET_FORECAST = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
MET_USER_AGENT = "BakeryHub/1.0 bakery-salesdata.onrender.com github.com/tosane932/tosane-works"
UNAVAILABLE = "現在の天気を取得できませんでした。時間をおいて再確認してください。"
_cache_lock = threading.Lock()
_cache = {}


def _read_weather(config):
    lat, lon, archive_url, api_key = _settings(config)
    url = FORECAST_PAID if archive_url == PAID_URL else FORECAST_FREE
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 7,
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
        "credit_url": "https://open-meteo.com/",
        "forecast_days": open_daily(data, now.date()),
    }


def _met_coordinates(config):
    try:
        lat = float(config["WEATHER_LATITUDE"])
        lon = float(config["WEATHER_LONGITUDE"])
    except (TypeError, ValueError, KeyError) as exc:
        raise WeatherHistoryError("店舗の緯度経度が未設定です。") from exc
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise WeatherHistoryError("店舗の緯度経度が不正です。")
    return lat, lon


def _met_code(symbol):
    """予報記号を既存のホーム用アイコン・分類へ変換する。未知の記号は拒否。"""
    if not isinstance(symbol, str):
        raise ValueError("Invalid forecast symbol")
    if "thunder" in symbol:
        return 95
    if "snow" in symbol or "sleet" in symbol:
        return 71
    if "rain" in symbol:
        return 61
    if symbol == "fog":
        return 45
    normalized = symbol.removesuffix("_day").removesuffix("_night").removesuffix("_polartwilight")
    if normalized in {"clearsky", "fair", "partlycloudy", "cloudy"}:
        return {"clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3}[normalized]
    raise ValueError("Unknown forecast symbol")


def _response_expiry(headers):
    try:
        return parsedate_to_datetime(headers.get("Expires")) if headers.get("Expires") else None
    except (TypeError, ValueError):
        return None


def _read_met_weather(config, last_modified=None):
    lat, lon = _met_coordinates(config)
    # MET Norway は座標を小数点以下4桁までに制限する。
    url = MET_FORECAST + "?" + urlencode({"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"})
    headers = {"User-Agent": MET_USER_AGENT, "Accept-Encoding": "gzip"}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(url, headers=headers)
    with urlopen(request, timeout=6) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Oversized forecast response")
        if response.headers.get("Content-Encoding") == "gzip":
            with gzip.GzipFile(fileobj=BytesIO(raw)) as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Oversized forecast response")
        expires_at = _response_expiry(response.headers)
        last_modified = response.headers.get("Last-Modified")
    data = json.loads(raw)
    now = datetime.datetime.now(datetime.timezone.utc)
    properties = data["properties"]
    issued = datetime.datetime.fromisoformat(properties["meta"]["updated_at"].replace("Z", "+00:00"))
    if issued.tzinfo is None or not now - datetime.timedelta(hours=12) <= issued <= now + datetime.timedelta(minutes=10):
        raise ValueError("Stale forecast model")
    candidates = properties["timeseries"]
    if not isinstance(candidates, list) or len(candidates) > 500:
        raise ValueError("Invalid forecast series")
    for item in candidates:
        target = datetime.datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
        if target.tzinfo is None:
            raise ValueError("Invalid forecast time")
        if now - datetime.timedelta(minutes=30) <= target <= now + datetime.timedelta(minutes=90):
            if target.astimezone(JST).date() != now.astimezone(JST).date():
                continue
            summary = item["data"].get("next_1_hours", {}).get("summary", {})
            if "symbol_code" not in summary:
                continue
            code = _met_code(summary["symbol_code"])
            result = {
                "available": True,
                "weather": weather_category(code),
                "weather_code": code,
                "valid_at": target.astimezone(JST).isoformat(timespec="minutes"),
                "fetched_at": now.astimezone(JST).isoformat(timespec="minutes"),
                "location": (config.get("WEATHER_LOCATION_NAME") or "").strip()[:80] or f"北緯{lat:g}°・東経{lon:g}°",
                "source": "MET Norway 予報",
                "credit_url": "https://creativecommons.org/licenses/by/4.0/",
                "forecast_days": met_daily(candidates, properties["meta"].get("units", {}), now.astimezone(JST).date(), _met_code),
            }
            return result, expires_at, last_modified
    raise ValueError("No current forecast")


def current_weather(config):
    """失敗や未設定は理由を返す。10分以上キャッシュし提供元の期限も守る。"""
    try:
        is_met = config.get("WEATHER_API_MODE") == "met_norway"
        if is_met:
            _met_coordinates(config)
        else:
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
            result = deepcopy(cached[1])
            if result["available"]:
                target = datetime.datetime.fromisoformat(result["valid_at"])
                japan_now = now.astimezone(JST)
                if target.date() != japan_now.date() or target < japan_now - datetime.timedelta(hours=2):
                    return {"available": False, "reason": UNAVAILABLE}
            return result
        try:
            if is_met:
                try:
                    result, expires_at, last_modified = _read_met_weather(
                        config, cached[2] if cached else None,
                    )
                except HTTPError as exc:
                    if exc.code != 304 or not cached or not cached[1]["available"]:
                        raise
                    result = deepcopy(cached[1])
                    expires_at = _response_expiry(exc.headers)
                    last_modified = cached[2]
            else:
                result, expires_at, last_modified = _read_weather(config), None, None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, EOFError):
            logger.warning("Current weather could not be retrieved.")
            result, expires_at, last_modified = {"available": False, "reason": UNAVAILABLE}, None, None
        lifetime = datetime.timedelta(minutes=10 if result["available"] else 2)
        _cache.clear()
        expires_at = expires_at.astimezone(datetime.timezone.utc) if expires_at and expires_at.tzinfo else None
        _cache[key] = (max(now + lifetime, expires_at) if expires_at else now + lifetime, result, last_modified)
        if result["available"]:
            target = datetime.datetime.fromisoformat(result["valid_at"])
            japan_now = now.astimezone(JST)
            if target.date() != japan_now.date() or target < japan_now - datetime.timedelta(hours=2):
                return {"available": False, "reason": UNAVAILABLE}
        return deepcopy(result)
