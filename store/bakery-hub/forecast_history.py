"""MET Norwayの取得済み予報をDataset・日本時間の日付で一度だけ保存する。"""
import datetime

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from current_weather import JST, _met_coordinates
from models import DailyForecast, db
from weather_history import weather_category

UTC = datetime.timezone.utc


def save_forecast(dataset_id, result, config, *, now=None):
    """信頼済みサーバー取得結果のみ保存。DB一意制約とON CONFLICTで初回の値を保つ。"""
    if config.get("WEATHER_API_MODE") != "met_norway" or result.get("available") is not True:
        return False
    latitude, longitude = _met_coordinates(config)
    code = result.get("weather_code")
    if type(code) is not int or not 0 <= code <= 99 or weather_category(code) == "不明":
        return False
    try:
        valid_at = datetime.datetime.fromisoformat(result["valid_at"])
        fetched_at = datetime.datetime.fromisoformat(result["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return False
    current = now or datetime.datetime.now(UTC)
    if (valid_at.tzinfo is None or fetched_at.tzinfo is None or
            fetched_at.astimezone(JST).date() != current.astimezone(JST).date() or
            valid_at.astimezone(JST).date() != current.astimezone(JST).date() or
            valid_at < current - datetime.timedelta(hours=2) or
            fetched_at > current + datetime.timedelta(minutes=2) or
            fetched_at < current - datetime.timedelta(hours=12)):
        return False
    location = result.get("location")
    if (result.get("source") != "MET Norway 予報" or
            not isinstance(location, str) or not 0 < len(location) <= 80):
        return False
    dialect = db.session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = postgresql_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise RuntimeError("Unsupported database for forecast upsert")
    statement = insert(DailyForecast).values(
        dataset_id=dataset_id,
        business_date=current.astimezone(JST).date(),
        weather_code=code,
        forecast_valid_at=valid_at.astimezone(UTC),
        fetched_at=fetched_at.astimezone(UTC),
        latitude=latitude,
        longitude=longitude,
        location_name=location,
        source=result["source"],
    ).on_conflict_do_nothing(index_elements=["dataset_id", "business_date"])
    saved = db.session.execute(statement).rowcount == 1
    db.session.commit()
    return saved


def stored_forecasts(dataset_id, start, end):
    rows = DailyForecast.query.filter(
        DailyForecast.dataset_id == dataset_id,
        DailyForecast.business_date >= start,
        DailyForecast.business_date <= end,
    ).order_by(DailyForecast.business_date).all()

    def in_japan(value):
        # SQLite returns naive datetimes; the app stores all timestamps in UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(JST).isoformat(timespec="minutes")

    return [{
        "date": row.business_date.isoformat(),
        "weather": weather_category(row.weather_code),
        "weather_code": row.weather_code,
        "valid_at": in_japan(row.forecast_valid_at),
        "fetched_at": in_japan(row.fetched_at),
        "location": row.location_name,
        "source": row.source,
    } for row in rows]
