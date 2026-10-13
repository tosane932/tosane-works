"""Real PostgreSQL concurrent POST coverage for staff shift constraints and locking."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text

from models import Dataset, ShopStaff, ShopShift, db
from postgresql_test_utils import get_isolated_postgresql_test_url

DATABASE_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="isolated PostgreSQL URL is not configured")

WORKER = r"""
import concurrent.futures
import json
import os
import threading

from werkzeug.security import generate_password_hash
import app as app_module

app = app_module.app
app.config.update(
    TESTING=True,
    SECRET_KEY="only-for-isolated-integration-test",
    ADMIN_USERNAME="shift-test-admin",
    ADMIN_PASSWORD_HASH=generate_password_hash("shift-test-password"),
    SESSION_COOKIE_SECURE=False,
    WTF_CSRF_ENABLED=False,
    ADMIN_LOGIN_RATE_LIMIT_TEST_CLIENT_IP="127.0.0.1",
)
barrier = threading.Barrier(2)
staff_id = os.environ["TEST_STAFF_ID"]
mode = os.environ["TEST_SHIFT_MODE"]


def submit(index):
    client = app.test_client()
    login = client.post("/login", data={
        "username": "shift-test-admin", "password": "shift-test-password",
    })
    assert login.status_code == 302
    if mode == "same-day":
        values = {
            "business_date": "2026-10-14", "start_time": "",
            "end_time": "", "end_next_day": "",
        }
    elif index == 0:
        values = {
            "business_date": "2026-10-14", "start_time": "22:00",
            "end_time": "02:00", "end_next_day": "1",
        }
    else:
        values = {
            "business_date": "2026-10-15", "start_time": "01:00",
            "end_time": "05:00", "end_next_day": "",
        }
    barrier.wait(timeout=15)
    result = client.post("/staff-shifts/shifts/new", data={
        **values, "staff_id": staff_id, "status": "off" if mode == "same-day" else "work", "note": "",
    })
    return result.status_code


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    statuses = list(pool.map(submit, range(2)))
print(json.dumps(statuses))
"""


@pytest.mark.parametrize(("mode", "expected"), [
    ("same-day", [303, 409]),
    ("overlap", [303, 400]),
])
def test_concurrent_shift_posts_keep_one_valid_plan(mode, expected):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
            db.metadata.create_all(connection)
            admin_id = uuid.uuid4()
            connection.execute(Dataset.__table__.insert().values(
                id=admin_id, kind="admin", system_key="admin",
                absolute_expires_at=None,
            ))
            staff_id = connection.execute(
                ShopStaff.__table__.insert().values(
                    dataset_id=admin_id, display_name="並行テスト担当",
                    role="production", submission_key=uuid.uuid4().hex,
                ).returning(ShopStaff.id)
            ).scalar_one()

        env = os.environ.copy()
        env.update(
            DATABASE_URL=DATABASE_URL, TEST_STAFF_ID=str(staff_id),
            TEST_SHIFT_MODE=mode,
        )
        worker = subprocess.run(
            [sys.executable, "-c", WORKER],
            cwd=Path(__file__).resolve().parent.parent,
            env=env, capture_output=True, text=True, timeout=45,
        )
        assert worker.returncode == 0, worker.stderr[-1500:]
        statuses = json.loads(worker.stdout.strip().splitlines()[-1])
        assert sorted(statuses) == expected
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM shop_shifts")).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        engine.dispose()
