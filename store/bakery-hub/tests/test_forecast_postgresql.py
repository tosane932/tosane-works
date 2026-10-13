"""隔離されたPostgreSQLで同時保存とDataset一意制約を確認する。"""
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from flask import Flask
from flask_migrate import Migrate, upgrade
from sqlalchemy import create_engine, text

from forecast_history import save_forecast, stored_forecasts
from models import DailyForecast, Dataset, db
from postgresql_test_utils import get_isolated_postgresql_test_url
from test_forecast_history import forecast, met_config

TEST_URL = get_isolated_postgresql_test_url()
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
pytestmark = pytest.mark.skipif(not TEST_URL, reason="isolated PostgreSQL URL is not configured")


def test_postgresql_two_simultaneous_forecasts_keep_one_first_record():
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    application = Flask("forecast_postgresql_test")
    application.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(application)
    Migrate(application, db, directory=str(MIGRATIONS))
    with application.app_context():
        upgrade(directory=str(MIGRATIONS), revision="head")
        dataset = Dataset.query.filter_by(system_key="admin").one()
        dataset_id = dataset.id
    now = datetime.datetime.now(datetime.timezone.utc)
    barrier = threading.Barrier(2)

    def write(code):
        with application.app_context():
            barrier.wait(timeout=10)
            return save_forecast(dataset_id, forecast(now, weather_code=code,
                valid_at=now.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes")), met_config(), now=now)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [61, 95]))
        assert sorted(results) == [False, True]
        with application.app_context():
            assert DailyForecast.query.filter_by(dataset_id=dataset_id).count() == 1
            day = now.astimezone(datetime.timezone(datetime.timedelta(hours=9))).date()
            assert stored_forecasts(dataset_id, day, day)[0]["weather_code"] in (61, 95)
    finally:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()
