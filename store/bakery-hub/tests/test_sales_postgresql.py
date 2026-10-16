import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from models import Dataset, Product, db
from postgresql_test_utils import get_isolated_postgresql_test_url


POSTGRESQL_TEST_DATABASE_URL = get_isolated_postgresql_test_url()

pytestmark = pytest.mark.skipif(
    not POSTGRESQL_TEST_DATABASE_URL,
    reason="isolated PostgreSQL URL is not configured",
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

WORKER_CODE = r"""
import concurrent.futures
import json
import os
from pathlib import Path
import threading
import time

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

import app as app_module


app_module.app.config["WTF_CSRF_ENABLED"] = False

guest_dataset_id = os.environ["TEST_GUEST_DATASET_ID"]
product_id = os.environ["TEST_PRODUCT_ID"]
sale_date = os.environ["TEST_SALE_DATE"]
lock_acquired_marker = Path(os.environ["TEST_LOCK_ACQUIRED_MARKER"])
lock_release_marker = Path(os.environ["TEST_LOCK_RELEASE_MARKER"])
result_path = Path(os.environ["TEST_RESULT_PATH"])

real_user_loader = app_module.login_manager._user_callback
authenticated_requests_ready = threading.Barrier(2)


def synchronized_user_loader(user_id):
    user = real_user_loader(user_id)
    authenticated_requests_ready.wait(timeout=15)
    return user


app_module.login_manager._user_callback = synchronized_user_loader

first_insert_guard = threading.Lock()
first_insert_is_held = False


def hold_first_daily_sales_insert(
    connection,
    cursor,
    statement,
    parameters,
    context,
    executemany,
):
    del connection, cursor, parameters, context, executemany
    normalized = " ".join(statement.lower().split())
    if "insert into daily_sales" not in normalized:
        return

    global first_insert_is_held
    with first_insert_guard:
        if first_insert_is_held:
            return
        first_insert_is_held = True

    lock_acquired_marker.touch()
    deadline = time.monotonic() + 20
    while not lock_release_marker.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("DailySales INSERT release timed out")
        time.sleep(0.02)


with app_module.app.app_context():
    event.listen(
        app_module.db.engine,
        "after_cursor_execute",
        hold_first_daily_sales_insert,
    )

commit_error_types = []
commit_error_guard = threading.Lock()
real_commit = app_module.db.session.commit


def recording_commit():
    try:
        return real_commit()
    except SQLAlchemyError as error:
        with commit_error_guard:
            commit_error_types.append(type(error).__name__)
        raise


app_module.db.session.commit = recording_commit


def submit_sales(quantity):
    client = app_module.app.test_client()
    with client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_dataset_id}"
        session_data["_fresh"] = True

    response = client.post(
        "/input",
        data={
            "date": sale_date,
            "product_id": [product_id],
            "quantity": [str(quantity)],
        },
    )

    with app_module.app.app_context():
        transaction_is_active = (
            app_module.db.session().in_transaction()
        )
        app_module.db.session.remove()

    return {
        "status_code": response.status_code,
        "transaction_is_active": transaction_is_active,
    }


try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit_sales, (11, 22)))
    result_path.write_text(json.dumps({
        "results": results,
        "commit_error_types": commit_error_types,
    }))
except Exception as error:
    result_path.write_text(json.dumps({"error_type": type(error).__name__}))
    raise
"""


def _wait_for_marker(marker_path, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker_path.exists():
            return
        if process.poll() is not None:
            pytest.fail("Sales worker exited before holding its first INSERT.")
        time.sleep(0.05)
    pytest.fail("Sales worker did not hold its first INSERT in time.")


def _wait_for_daily_sales_lock(engine, application_name):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock' "
                    "AND ((query ILIKE 'INSERT INTO daily_sales%') "
                    "OR (query ILIKE '%FROM datasets%' "
                    "AND query ILIKE '%FOR UPDATE%'))"
                ),
                {"application_name": application_name},
            ).scalar_one()
        if waiting_count >= 1:
            return
        time.sleep(0.05)
    pytest.fail("Second DailySales write did not wait on a database lock.")


def test_concurrent_first_sales_insert_succeeds_without_duplicate(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    guest_dataset_id = uuid.uuid4()
    application_name = f"daily-sales-test-{uuid.uuid4()}"
    sale_date = datetime.date(2039, 12, 1)
    lock_acquired_marker = tmp_path / "first-sales-insert-held"
    lock_release_marker = tmp_path / "release-first-sales-insert"
    result_path = tmp_path / "sales-worker-result.json"
    worker_process = None

    try:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
            db.metadata.create_all(connection)
            now = datetime.datetime.now(datetime.timezone.utc)
            connection.execute(
                Dataset.__table__.insert(),
                {
                    "id": guest_dataset_id,
                    "kind": "guest",
                    "system_key": None,
                    "created_at": now,
                    "last_activity_at": now,
                    "absolute_expires_at": now + datetime.timedelta(hours=2),
                    "guest_ai_usage_count": 0,
                },
            )
            product_id = connection.execute(
                Product.__table__.insert().returning(Product.id),
                {
                    "dataset_id": guest_dataset_id,
                    "year": sale_date.year,
                    "month": sale_date.month,
                    "name": "同時売上商品",
                    "price": 100,
                    "is_active": True,
                },
            ).scalar_one()

        worker_environment = os.environ.copy()
        worker_environment.update(
            DATABASE_URL=POSTGRESQL_TEST_DATABASE_URL,
            SECRET_KEY="isolated-integration-test-only",
            TEST_GUEST_DATASET_ID=str(guest_dataset_id),
            TEST_PRODUCT_ID=str(product_id),
            TEST_SALE_DATE=sale_date.isoformat(),
            TEST_LOCK_ACQUIRED_MARKER=str(lock_acquired_marker),
            TEST_LOCK_RELEASE_MARKER=str(lock_release_marker),
            TEST_RESULT_PATH=str(result_path),
            PGAPPNAME=application_name,
        )
        worker_process = subprocess.Popen(
            [sys.executable, "-c", WORKER_CODE],
            cwd=REPOSITORY_ROOT,
            env=worker_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        _wait_for_marker(lock_acquired_marker, worker_process)
        _wait_for_daily_sales_lock(engine, application_name)
        lock_release_marker.touch()

        assert worker_process.wait(timeout=30) == 0
        result = json.loads(result_path.read_text())
        assert "error_type" not in result
        assert sorted(
            item["status_code"] for item in result["results"]
        ) == [200, 200]
        assert result["commit_error_types"] == []
        assert all(
            not item["transaction_is_active"]
            for item in result["results"]
        )

        with engine.connect() as connection:
            saved_sales = connection.execute(
                text(
                    "SELECT quantity FROM daily_sales "
                    "WHERE product_id = :product_id AND date = :sale_date"
                ),
                {"product_id": product_id, "sale_date": sale_date},
            ).all()
        assert len(saved_sales) == 1
        assert saved_sales[0].quantity in {11, 22}
    finally:
        lock_release_marker.touch(exist_ok=True)
        if worker_process is not None and worker_process.poll() is None:
            worker_process.terminate()
            worker_process.wait(timeout=10)
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
        engine.dispose()


def test_dashboard_today_sales_large_amount_postgresql():
    """単価×数量が32bit整数を超えても、本番と同じDBで集計できる。"""
    worker_environment = os.environ.copy()
    worker_environment.update(
        DATABASE_URL=POSTGRESQL_TEST_DATABASE_URL,
        SECRET_KEY="isolated-integration-test-only",
    )
    result = subprocess.run(
        [sys.executable, "-c", r'''
import datetime
from app import app, _get_today_sales_amount
from models import db, Dataset, Product, DailySales
with app.app_context():
    try:
        db.create_all()
        dataset = Dataset(kind="admin", system_key="admin")
        db.session.add(dataset)
        product = Product(dataset=dataset, year=2039, month=12,
                          name="高額集計テスト", price=1000000)
        db.session.add(product)
        db.session.flush()
        today = datetime.date(2039, 12, 1)
        db.session.add(DailySales(product_id=product.id, date=today, quantity=100000))
        db.session.commit()
        assert _get_today_sales_amount(dataset, today) == 100000000000
        assert _get_today_sales_amount(dataset, today + datetime.timedelta(days=1)) == 0
    finally:
        db.session.remove()
        db.drop_all()
'''],
        cwd=REPOSITORY_ROOT,
        env=worker_environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "PostgreSQL today-sales aggregation failed"
