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

from models import (
    DailySales,
    Dataset,
    MaterialOrderItem,
    Product,
    ShopMemo,
    ShopTask,
    ShopStaff,
    ShopShift,
    db,
)
from postgresql_test_utils import get_isolated_postgresql_test_url


POSTGRESQL_TEST_DATABASE_URL = get_isolated_postgresql_test_url()

pytestmark = pytest.mark.skipif(
    not POSTGRESQL_TEST_DATABASE_URL,
    reason="isolated PostgreSQL URL is not configured",
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

CLEANUP_WORKER_CODE = r"""
import datetime
import json
import os
from pathlib import Path
import time

from sqlalchemy import event

import app as app_module


dataset_id = os.environ["TEST_GUEST_DATASET_ID"]
cleanup_time = datetime.datetime.fromisoformat(os.environ["TEST_CLEANUP_TIME"])
pause_stage = os.environ["TEST_CLEANUP_PAUSE_STAGE"]
pause_marker = Path(os.environ["TEST_PAUSE_MARKER"])
release_marker = Path(os.environ["TEST_RELEASE_MARKER"])
result_path = Path(os.environ["TEST_RESULT_PATH"])


def wait_for_release():
    pause_marker.touch()
    deadline = time.monotonic() + 20
    while not release_marker.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("cleanup test release timed out")
        time.sleep(0.02)


if pause_stage == "after_initial_expiry_check":
    real_expiry_check = app_module._guest_dataset_is_expired
    initial_check_paused = False

    def pausing_expiry_check(dataset, now=None):
        global initial_check_paused
        result = real_expiry_check(dataset, now=now)
        if str(dataset.id) == dataset_id and not initial_check_paused:
            initial_check_paused = True
            wait_for_release()
        return result

    app_module._guest_dataset_is_expired = pausing_expiry_check


if pause_stage == "after_row_lock":
    row_lock_paused = False

    def pause_after_row_lock(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        del connection, cursor, parameters, context, executemany
        normalized = " ".join(statement.lower().split())
        if "from datasets" not in normalized or "for update" not in normalized:
            return

        global row_lock_paused
        if row_lock_paused:
            return
        row_lock_paused = True
        wait_for_release()

    with app_module.app.app_context():
        event.listen(
            app_module.db.engine,
            "after_cursor_execute",
            pause_after_row_lock,
        )


try:
    with app_module.app.app_context():
        deleted_count = app_module._cleanup_expired_guest_datasets(
            now=cleanup_time,
        )
        app_module.db.session.commit()
        app_module.db.session.remove()
    result_path.write_text(json.dumps({"deleted_count": deleted_count}))
except Exception as error:
    result_path.write_text(json.dumps({"error_type": type(error).__name__}))
    raise
"""

ACTIVITY_WORKER_CODE = r"""
import json
import os
from pathlib import Path

import app as app_module


dataset_id = os.environ["TEST_GUEST_DATASET_ID"]
result_path = Path(os.environ["TEST_RESULT_PATH"])

with app_module.app.test_request_context("/"):
    restored_user = app_module.load_user(f"guest:{dataset_id}")

result_path.write_text(json.dumps({"restored": restored_user is not None}))
"""

ROLLBACK_WORKER_CODE = r"""
import datetime
import json
import os
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

import app as app_module


cleanup_time = datetime.datetime.fromisoformat(os.environ["TEST_CLEANUP_TIME"])
result_path = Path(os.environ["TEST_RESULT_PATH"])

with app_module.app.app_context():
    try:
        deleted_count = app_module._cleanup_expired_guest_datasets(
            now=cleanup_time,
        )
        if deleted_count != 1:
            raise AssertionError(
                f"expected one cleanup candidate, got {deleted_count}"
            )
        raise SQLAlchemyError("forced cleanup rollback")
    except SQLAlchemyError:
        app_module.db.session.rollback()
        result_path.write_text(json.dumps({"rolled_back": True}))
    finally:
        app_module.db.session.remove()
"""


def _worker_environment(
    *,
    dataset_id,
    result_path,
    application_name,
    cleanup_time=None,
    pause_stage=None,
    pause_marker=None,
    release_marker=None,
):
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=POSTGRESQL_TEST_DATABASE_URL,
        SECRET_KEY="isolated-integration-test-only",
        TEST_GUEST_DATASET_ID=str(dataset_id),
        TEST_RESULT_PATH=str(result_path),
        PGAPPNAME=application_name,
    )
    if cleanup_time is not None:
        environment.update(
            TEST_CLEANUP_TIME=cleanup_time.isoformat(),
            TEST_CLEANUP_PAUSE_STAGE=pause_stage,
            TEST_PAUSE_MARKER=str(pause_marker),
            TEST_RELEASE_MARKER=str(release_marker),
        )
    return environment


def _wait_for_marker(marker_path, process, message):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker_path.exists():
            return
        if process.poll() is not None:
            pytest.fail(message)
        time.sleep(0.05)
    pytest.fail(message)


def _wait_for_activity_update_lock(engine, application_name):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock' "
                    "AND query ILIKE 'UPDATE datasets%'"
                ),
                {"application_name": application_name},
            ).scalar_one()
        if waiting_count >= 1:
            return
        time.sleep(0.05)
    pytest.fail("Guest activity update did not wait on the cleanup row lock.")


def _dataset_values(dataset_id, *, kind, system_key, created_at, activity_at):
    return {
        "id": dataset_id,
        "kind": kind,
        "system_key": system_key,
        "created_at": created_at,
        "last_activity_at": activity_at,
        "absolute_expires_at": (
            None
            if kind == "admin"
            else created_at + datetime.timedelta(hours=4)
        ),
        "guest_ai_usage_count": 0,
    }


def _seed_cleanup_scenario(engine, cleanup_time):
    expired_guest_id = uuid.uuid4()
    active_guest_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    created_at = cleanup_time - datetime.timedelta(hours=2)

    with engine.begin() as connection:
        db.metadata.drop_all(connection)
        db.metadata.create_all(connection)
        connection.execute(
            Dataset.__table__.insert(),
            [
                _dataset_values(
                    expired_guest_id,
                    kind="guest",
                    system_key=None,
                    created_at=created_at,
                    activity_at=(
                        cleanup_time - datetime.timedelta(minutes=31)
                    ),
                ),
                _dataset_values(
                    active_guest_id,
                    kind="guest",
                    system_key=None,
                    created_at=created_at,
                    activity_at=cleanup_time,
                ),
                _dataset_values(
                    admin_id,
                    kind="admin",
                    system_key="admin",
                    created_at=created_at,
                    activity_at=cleanup_time,
                ),
            ],
        )
        for dataset_id in (expired_guest_id, active_guest_id, admin_id):
            staff_id = connection.execute(ShopStaff.__table__.insert().values(
                dataset_id=dataset_id, display_name='架空cleanup担当', role='production'
            ).returning(ShopStaff.id)).scalar_one()
            connection.execute(ShopShift.__table__.insert().values(
                dataset_id=dataset_id, staff_id=staff_id,
                business_date=cleanup_time.date(), status='all_day'))
        product_ids = connection.execute(
            Product.__table__.insert().returning(
                Product.id,
                Product.dataset_id,
            ),
            [
                {
                    "dataset_id": dataset_id,
                    "year": 2026,
                    "month": 9,
                    "name": name,
                    "price": 100,
                    "is_active": True,
                }
                for dataset_id, name in (
                    (expired_guest_id, "削除候補商品"),
                    (active_guest_id, "別Guest保護商品"),
                    (admin_id, "Admin保護商品"),
                )
            ],
        ).all()
        connection.execute(
            DailySales.__table__.insert(),
            [
                {
                    "product_id": product_id,
                    "date": datetime.date(2026, 9, 1),
                    "quantity": 1,
                }
                for product_id, _dataset_id in product_ids
            ],
        )
        connection.execute(
            MaterialOrderItem.__table__.insert(),
            [
                {
                    "dataset_id": dataset_id,
                    "name": name,
                }
                for dataset_id, name in (
                    (expired_guest_id, "削除候補材料"),
                    (active_guest_id, "別Guest保護材料"),
                    (admin_id, "Admin保護材料"),
                )
            ],
        )
        connection.execute(
            ShopMemo.__table__.insert(),
            [
                {
                    "dataset_id": dataset_id,
                    "title": f"{label}通常メモ",
                    "body": f"{label}通常メモ",
                    "created_at": cleanup_time,
                    "updated_at": cleanup_time,
                    "deleted_at": None,
                }
                for dataset_id, label in (
                    (expired_guest_id, "削除候補"),
                    (active_guest_id, "別Guest保護"),
                    (admin_id, "Admin保護"),
                )
            ]
            + [
                {
                    "dataset_id": dataset_id,
                    "title": f"{label}ゴミ箱メモ",
                    "body": f"{label}ゴミ箱メモ",
                    "created_at": cleanup_time,
                    "updated_at": cleanup_time,
                    "deleted_at": cleanup_time,
                }
                for dataset_id, label in (
                    (expired_guest_id, "削除候補"),
                    (active_guest_id, "別Guest保護"),
                    (admin_id, "Admin保護"),
                )
            ],
        )
        connection.execute(
            ShopTask.__table__.insert(),
            [
                {
                    "dataset_id": dataset_id,
                    "title": title,
                }
                for dataset_id, title in (
                    (expired_guest_id, "削除候補タスク"),
                    (active_guest_id, "別Guest保護タスク"),
                    (admin_id, "Admin保護タスク"),
                )
            ],
        )

    return expired_guest_id, active_guest_id, admin_id


def _assert_preserved_dataset_counts(engine, expected):
    with engine.connect() as connection:
        actual = {
            dataset_id: (
                connection.execute(
                    text("SELECT COUNT(*) FROM datasets WHERE id = :id"),
                    {"id": dataset_id},
                ).scalar_one(),
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM products "
                        "WHERE dataset_id = :id"
                    ),
                    {"id": dataset_id},
                ).scalar_one(),
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM daily_sales sales "
                        "JOIN products product ON product.id = sales.product_id "
                        "WHERE product.dataset_id = :id"
                    ),
                    {"id": dataset_id},
                ).scalar_one(),
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM material_order_items "
                        "WHERE dataset_id = :id"
                    ),
                    {"id": dataset_id},
                ).scalar_one(),
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM shop_memos "
                        "WHERE dataset_id = :id"
                    ),
                    {"id": dataset_id},
                ).scalar_one(),
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM shop_tasks "
                        "WHERE dataset_id = :id"
                    ),
                    {"id": dataset_id},
                ).scalar_one(),
            )
            for dataset_id in expected
        }
    assert actual == expected
    with engine.connect() as connection:
        for dataset_id, counts in expected.items():
            for table in (ShopStaff.__table__, ShopShift.__table__):
                assert len(connection.execute(
                    table.select().where(table.c.dataset_id == dataset_id)
                ).fetchall()) == counts[0]


def test_activity_committed_before_cleanup_lock_preserves_guest_data(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    cleanup_time = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=2)
    )
    expired_guest_id, active_guest_id, admin_id = _seed_cleanup_scenario(
        engine,
        cleanup_time,
    )
    pause_marker = tmp_path / "cleanup-initial-check-paused"
    release_marker = tmp_path / "release-cleanup-initial-check"
    cleanup_result_path = tmp_path / "cleanup-result.json"
    activity_result_path = tmp_path / "activity-result.json"
    cleanup_process = None
    activity_process = None

    try:
        cleanup_process = subprocess.Popen(
            [sys.executable, "-c", CLEANUP_WORKER_CODE],
            cwd=REPOSITORY_ROOT,
            env=_worker_environment(
                dataset_id=expired_guest_id,
                result_path=cleanup_result_path,
                application_name=f"cleanup-a-{uuid.uuid4()}",
                cleanup_time=cleanup_time,
                pause_stage="after_initial_expiry_check",
                pause_marker=pause_marker,
                release_marker=release_marker,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_marker(
            pause_marker,
            cleanup_process,
            "Cleanup did not pause after its initial expiry check.",
        )

        activity_process = subprocess.Popen(
            [sys.executable, "-c", ACTIVITY_WORKER_CODE],
            cwd=REPOSITORY_ROOT,
            env=_worker_environment(
                dataset_id=expired_guest_id,
                result_path=activity_result_path,
                application_name=f"activity-a-{uuid.uuid4()}",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert activity_process.wait(timeout=20) == 0
        assert json.loads(activity_result_path.read_text()) == {
            "restored": True,
        }

        release_marker.touch()
        assert cleanup_process.wait(timeout=20) == 0
        assert json.loads(cleanup_result_path.read_text()) == {
            "deleted_count": 0,
        }

        _assert_preserved_dataset_counts(
            engine,
            {
                expired_guest_id: (1, 1, 1, 1, 2, 1),
                active_guest_id: (1, 1, 1, 1, 2, 1),
                admin_id: (1, 1, 1, 1, 2, 1),
            },
        )
    finally:
        release_marker.touch(exist_ok=True)
        for process in (cleanup_process, activity_process):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
        engine.dispose()


def test_cleanup_lock_prevents_late_activity_from_resurrecting_guest(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    cleanup_time = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=2)
    )
    expired_guest_id, active_guest_id, admin_id = _seed_cleanup_scenario(
        engine,
        cleanup_time,
    )
    pause_marker = tmp_path / "cleanup-row-lock-held"
    release_marker = tmp_path / "release-cleanup-row-lock"
    cleanup_result_path = tmp_path / "cleanup-result.json"
    activity_result_path = tmp_path / "activity-result.json"
    activity_application_name = f"activity-b-{uuid.uuid4()}"
    cleanup_process = None
    activity_process = None

    try:
        cleanup_process = subprocess.Popen(
            [sys.executable, "-c", CLEANUP_WORKER_CODE],
            cwd=REPOSITORY_ROOT,
            env=_worker_environment(
                dataset_id=expired_guest_id,
                result_path=cleanup_result_path,
                application_name=f"cleanup-b-{uuid.uuid4()}",
                cleanup_time=cleanup_time,
                pause_stage="after_row_lock",
                pause_marker=pause_marker,
                release_marker=release_marker,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_marker(
            pause_marker,
            cleanup_process,
            "Cleanup did not acquire the expired Guest row lock.",
        )

        activity_process = subprocess.Popen(
            [sys.executable, "-c", ACTIVITY_WORKER_CODE],
            cwd=REPOSITORY_ROOT,
            env=_worker_environment(
                dataset_id=expired_guest_id,
                result_path=activity_result_path,
                application_name=activity_application_name,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_activity_update_lock(engine, activity_application_name)

        release_marker.touch()
        assert cleanup_process.wait(timeout=20) == 0
        assert activity_process.wait(timeout=20) == 0
        assert json.loads(cleanup_result_path.read_text()) == {
            "deleted_count": 1,
        }
        assert json.loads(activity_result_path.read_text()) == {
            "restored": False,
        }

        _assert_preserved_dataset_counts(
            engine,
            {
                expired_guest_id: (0, 0, 0, 0, 0, 0),
                active_guest_id: (1, 1, 1, 1, 2, 1),
                admin_id: (1, 1, 1, 1, 2, 1),
            },
        )
    finally:
        release_marker.touch(exist_ok=True)
        for process in (cleanup_process, activity_process):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
        engine.dispose()


def test_cleanup_failure_rolls_back_all_guest_data_on_postgresql(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    cleanup_time = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=2)
    )
    expired_guest_id, active_guest_id, admin_id = _seed_cleanup_scenario(
        engine,
        cleanup_time,
    )
    result_path = tmp_path / "cleanup-rollback-result.json"

    try:
        process = subprocess.run(
            [sys.executable, "-c", ROLLBACK_WORKER_CODE],
            cwd=REPOSITORY_ROOT,
            env=_worker_environment(
                dataset_id=expired_guest_id,
                result_path=result_path,
                application_name=f"cleanup-rollback-{uuid.uuid4()}",
                cleanup_time=cleanup_time,
                pause_stage="unused",
                pause_marker=tmp_path / "unused-pause",
                release_marker=tmp_path / "unused-release",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )

        assert process.returncode == 0
        assert json.loads(result_path.read_text()) == {"rolled_back": True}
        _assert_preserved_dataset_counts(
            engine,
            {
                expired_guest_id: (1, 1, 1, 1, 2, 1),
                active_guest_id: (1, 1, 1, 1, 2, 1),
                admin_id: (1, 1, 1, 1, 2, 1),
            },
        )
    finally:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
        engine.dispose()
