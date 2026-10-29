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

from models import Dataset, ShopMemo, db
from postgresql_test_utils import get_isolated_postgresql_test_url


POSTGRESQL_TEST_DATABASE_URL = get_isolated_postgresql_test_url()

pytestmark = pytest.mark.skipif(
    not POSTGRESQL_TEST_DATABASE_URL,
    reason="isolated PostgreSQL URL is not configured",
)


WORKER_CODE = r"""
import concurrent.futures
import json
import os
from pathlib import Path
import threading
import time

from sqlalchemy import event

import app as app_module


app_module.app.config["WTF_CSRF_ENABLED"] = False

guest_dataset_id = os.environ["TEST_GUEST_DATASET_ID"]
lock_acquired_marker = Path(os.environ["TEST_LOCK_ACQUIRED_MARKER"])
lock_release_marker = Path(os.environ["TEST_LOCK_RELEASE_MARKER"])
result_path = Path(os.environ["TEST_RESULT_PATH"])
memo_bodies = ["同時追加メモA", "同時追加メモB"]

real_user_loader = app_module.login_manager._user_callback
authenticated_requests_ready = threading.Barrier(2)


def synchronized_user_loader(user_id):
    user = real_user_loader(user_id)
    authenticated_requests_ready.wait(timeout=15)
    return user


app_module.login_manager._user_callback = synchronized_user_loader

first_lock_guard = threading.Lock()
first_lock_is_held = False


def hold_first_memo_admission_lock(
    connection,
    cursor,
    statement,
    parameters,
    context,
    executemany,
):
    del connection, cursor, parameters, context, executemany
    normalized_statement = " ".join(statement.lower().split())
    if (
        "from datasets" not in normalized_statement
        or "for update" not in normalized_statement
    ):
        return

    global first_lock_is_held
    with first_lock_guard:
        if first_lock_is_held:
            return
        first_lock_is_held = True

    lock_acquired_marker.touch()
    deadline = time.monotonic() + 20
    while not lock_release_marker.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("shop memo lock release timed out")
        time.sleep(0.02)


with app_module.app.app_context():
    event.listen(
        app_module.db.engine,
        "after_cursor_execute",
        hold_first_memo_admission_lock,
    )


def submit_memo(body):
    client = app_module.app.test_client()
    with client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_dataset_id}"
        session_data["_fresh"] = True

    response = client.post(
        "/shop-tools/memo",
        data={"title": body, "body": body},
        follow_redirects=False,
    )
    return response.status_code


try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit_memo, memo_bodies))
    result_path.write_text(json.dumps({"statuses": statuses}))
except Exception as error:
    result_path.write_text(json.dumps({"error_type": type(error).__name__}))
    raise
"""


LIFECYCLE_WORKER_CODE = r"""
import json
import os
import uuid
from pathlib import Path

import app as app_module
from models import ShopMemo


app_module.app.config.update(
    TESTING=True,
    SESSION_COOKIE_SECURE=False,
    WTF_CSRF_ENABLED=False,
)

guest_a_id = os.environ["TEST_GUEST_DATASET_ID"]
guest_a_uuid = uuid.UUID(guest_a_id)
own_active_id = int(os.environ["TEST_OWN_ACTIVE_MEMO_ID"])
own_deleted_id = int(os.environ["TEST_OWN_DELETED_MEMO_ID"])
other_guest_deleted_id = int(os.environ["TEST_OTHER_GUEST_MEMO_ID"])
admin_deleted_id = int(os.environ["TEST_ADMIN_MEMO_ID"])
result_path = Path(os.environ["TEST_RESULT_PATH"])

client = app_module.app.test_client()
from admin_sessions import ADMIN_SESSION_TOKEN_KEY, issue_admin_session
with app_module.app.app_context():
    admin_token = issue_admin_session()
    app_module.db.session.commit()
with client.session_transaction() as session_data:
    session_data[ADMIN_SESSION_TOKEN_KEY] = admin_token
    session_data["_user_id"] = f"guest:{guest_a_id}"
    session_data["_fresh"] = True


def post(path, data=None):
    return client.post(path, data=data or {}, follow_redirects=False)


try:
    active_before = client.get("/shop-tools/memo").get_data(as_text=True)
    trash_before = client.get("/shop-tools/memo/trash").get_data(as_text=True)

    rejected_create = post(
        "/shop-tools/memo",
        {"title": "上限中は作成不可", "body": "上限中は作成不可"},
    )
    move_to_trash = post(f"/shop-tools/memo/{own_active_id}/trash")
    restore = post(f"/shop-tools/memo/{own_deleted_id}/restore")
    cross_guest_restore = post(
        f"/shop-tools/memo/{other_guest_deleted_id}/restore"
    )
    cross_admin_delete = post(
        f"/shop-tools/memo/{admin_deleted_id}/delete"
    )
    active_delete = post(f"/shop-tools/memo/{own_deleted_id}/delete")
    permanent_delete = post(f"/shop-tools/memo/{own_active_id}/delete")
    accepted_create = post(
        "/shop-tools/memo",
        {
            "title": "完全削除後の100件目",
            "body": "完全削除後の100件目",
        },
    )

    with app_module.app.app_context():
        app_module.db.session.expire_all()
        restored_memo = app_module.db.session.get(ShopMemo, own_deleted_id)
        result = {
            "visibility": {
                "active_has_own_active": "Guest A通常メモ" in active_before,
                "active_has_own_deleted": "Guest Aゴミ箱メモ" in active_before,
                "trash_has_own_active": "Guest A通常メモ" in trash_before,
                "trash_has_own_deleted": "Guest Aゴミ箱メモ" in trash_before,
                "trash_has_other_guest": "Guest Bゴミ箱メモ" in trash_before,
                "trash_has_admin": "Adminゴミ箱メモ" in trash_before,
            },
            "statuses": {
                "rejected_create": rejected_create.status_code,
                "move_to_trash": move_to_trash.status_code,
                "restore": restore.status_code,
                "cross_guest_restore": cross_guest_restore.status_code,
                "cross_admin_delete": cross_admin_delete.status_code,
                "active_delete": active_delete.status_code,
                "permanent_delete": permanent_delete.status_code,
                "accepted_create": accepted_create.status_code,
            },
            "restore_location": restore.headers.get("Location"),
            "restored_is_active": (
                restored_memo is not None
                and restored_memo.deleted_at is None
            ),
            "permanently_deleted": (
                app_module.db.session.get(ShopMemo, own_active_id) is None
            ),
            "guest_a_count": ShopMemo.query.filter_by(
                dataset_id=guest_a_uuid
            ).count(),
            "new_memo_count": ShopMemo.query.filter_by(
                dataset_id=guest_a_uuid,
                body="完全削除後の100件目",
            ).count(),
        }
    result_path.write_text(json.dumps(result))
except Exception as error:
    result_path.write_text(json.dumps({"error_type": type(error).__name__}))
    raise
"""


ROLLBACK_WORKER_CODE = r"""
import json
import os
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

import app as app_module
from models import ShopMemo


app_module.app.config.update(
    TESTING=True,
    SESSION_COOKIE_SECURE=False,
    WTF_CSRF_ENABLED=False,
)

restore_id = int(os.environ["TEST_RESTORE_MEMO_ID"])
delete_id = int(os.environ["TEST_DELETE_MEMO_ID"])
result_path = Path(os.environ["TEST_RESULT_PATH"])

with app_module.app.app_context():
    from admin_sessions import issue_admin_session
    admin_token = issue_admin_session()
    app_module.db.session.commit()

client = app_module.app.test_client()
with client.session_transaction() as session_data:
    session_data["_user_id"] = "admin"
    session_data["_fresh"] = True
    session_data[app_module.ADMIN_SESSION_TOKEN_KEY] = admin_token
    session_data[app_module.ADMIN_AUTH_FINGERPRINT_SESSION_KEY] = (
        app_module._get_admin_auth_fingerprint(
            app_module.app.config["ADMIN_PASSWORD_HASH"]
        )
    )


def fail_after_flush():
    app_module.db.session.flush()
    raise SQLAlchemyError("forced PostgreSQL memo failure after flush")


try:
    with patch.object(
        app_module.db.session,
        "commit",
        side_effect=fail_after_flush,
    ):
        restore_response = client.post(
            f"/shop-tools/memo/{restore_id}/restore",
            follow_redirects=False,
        )

    with patch.object(
        app_module.db.session,
        "commit",
        side_effect=fail_after_flush,
    ):
        delete_response = client.post(
            f"/shop-tools/memo/{delete_id}/delete",
            follow_redirects=False,
        )

    with app_module.app.app_context():
        app_module.db.session.expire_all()
        restore_memo = app_module.db.session.get(ShopMemo, restore_id)
        delete_memo = app_module.db.session.get(ShopMemo, delete_id)
        result = {
            "restore_status": restore_response.status_code,
            "delete_status": delete_response.status_code,
            "restore_rolled_back": (
                restore_memo is not None
                and restore_memo.deleted_at is not None
            ),
            "delete_rolled_back": (
                delete_memo is not None
                and delete_memo.deleted_at is not None
            ),
        }
    result_path.write_text(json.dumps(result))
except Exception as error:
    result_path.write_text(json.dumps({"error_type": type(error).__name__}))
    raise
"""


def _dataset_values(dataset_id, *, kind, system_key, now):
    return {
        "id": dataset_id,
        "kind": kind,
        "system_key": system_key,
        "created_at": now,
        "last_activity_at": now,
        "absolute_expires_at": (
            None if kind == "admin" else now + datetime.timedelta(hours=2)
        ),
        "guest_ai_usage_count": 0,
    }


def _wait_for_marker(marker_path, process, timeout_seconds=20):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker_path.exists():
            return
        if process.poll() is not None:
            pytest.fail("Memo worker exited before acquiring the Dataset lock.")
        time.sleep(0.05)
    pytest.fail("Memo worker did not acquire the Dataset lock.")


def _wait_for_dataset_lock_waiter(engine, application_name, timeout_seconds=20):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock' "
                    "AND query ILIKE '%FROM datasets%' "
                    "AND query ILIKE '%FOR UPDATE%'"
                ),
                {"application_name": application_name},
            ).scalar_one()
        if waiting_count >= 1:
            return
        time.sleep(0.05)
    pytest.fail("The second memo request did not wait on the Dataset lock.")


def _postgresql_worker_environment(result_path, **values):
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=POSTGRESQL_TEST_DATABASE_URL,
        SECRET_KEY="isolated-integration-test-only",
        ADMIN_USERNAME="integration-admin",
        ADMIN_PASSWORD_HASH="integration-test-hash",
        TEST_RESULT_PATH=str(result_path),
        **{key: str(value) for key, value in values.items()},
    )
    return environment


def test_memo_trash_lifecycle_and_limits_on_postgresql(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    repository_root = Path(__file__).resolve().parent.parent
    guest_a_id = uuid.uuid4()
    guest_b_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    result_path = tmp_path / "memo-lifecycle-result.json"

    try:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
            db.metadata.create_all(connection)
            now = datetime.datetime.now(datetime.timezone.utc)
            connection.execute(
                Dataset.__table__.insert(),
                [
                    _dataset_values(
                        guest_a_id,
                        kind="guest",
                        system_key=None,
                        now=now,
                    ),
                    _dataset_values(
                        guest_b_id,
                        kind="guest",
                        system_key=None,
                        now=now,
                    ),
                    _dataset_values(
                        admin_id,
                        kind="admin",
                        system_key="admin",
                        now=now,
                    ),
                ],
            )
            connection.execute(
                ShopMemo.__table__.insert(),
                [
                    {
                        "dataset_id": guest_a_id,
                        "title": f"上限用メモ{index}",
                        "body": f"上限用メモ{index}",
                        "created_at": now,
                        "updated_at": now,
                        "deleted_at": None,
                    }
                    for index in range(98)
                ],
            )
            own_active_id = connection.execute(
                ShopMemo.__table__.insert()
                .values(
                    dataset_id=guest_a_id,
                    title="Guest A通常メモ",
                    body="Guest A通常メモ",
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                .returning(ShopMemo.id)
            ).scalar_one()
            own_deleted_id = connection.execute(
                ShopMemo.__table__.insert()
                .values(
                    dataset_id=guest_a_id,
                    title="Guest Aゴミ箱メモ",
                    body="Guest Aゴミ箱メモ",
                    created_at=now,
                    updated_at=now,
                    deleted_at=now,
                )
                .returning(ShopMemo.id)
            ).scalar_one()
            other_guest_deleted_id = connection.execute(
                ShopMemo.__table__.insert()
                .values(
                    dataset_id=guest_b_id,
                    title="Guest Bゴミ箱メモ",
                    body="Guest Bゴミ箱メモ",
                    created_at=now,
                    updated_at=now,
                    deleted_at=now,
                )
                .returning(ShopMemo.id)
            ).scalar_one()
            admin_deleted_id = connection.execute(
                ShopMemo.__table__.insert()
                .values(
                    dataset_id=admin_id,
                    title="Adminゴミ箱メモ",
                    body="Adminゴミ箱メモ",
                    created_at=now,
                    updated_at=now,
                    deleted_at=now,
                )
                .returning(ShopMemo.id)
            ).scalar_one()

        process = subprocess.run(
            [sys.executable, "-c", LIFECYCLE_WORKER_CODE],
            cwd=repository_root,
            env=_postgresql_worker_environment(
                result_path,
                TEST_GUEST_DATASET_ID=guest_a_id,
                TEST_OWN_ACTIVE_MEMO_ID=own_active_id,
                TEST_OWN_DELETED_MEMO_ID=own_deleted_id,
                TEST_OTHER_GUEST_MEMO_ID=other_guest_deleted_id,
                TEST_ADMIN_MEMO_ID=admin_deleted_id,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

        assert process.returncode == 0
        result = json.loads(result_path.read_text())
        assert "error_type" not in result
        assert result["visibility"] == {
            "active_has_own_active": True,
            "active_has_own_deleted": False,
            "trash_has_own_active": False,
            "trash_has_own_deleted": True,
            "trash_has_other_guest": False,
            "trash_has_admin": False,
        }
        assert result["statuses"] == {
            "rejected_create": 400,
            "move_to_trash": 303,
            "restore": 303,
            "cross_guest_restore": 404,
            "cross_admin_delete": 404,
            "active_delete": 404,
            "permanent_delete": 303,
            "accepted_create": 303,
        }
        assert result["restore_location"].endswith("/shop-tools/memo/trash")
        assert result["restored_is_active"] is True
        assert result["permanently_deleted"] is True
        assert result["guest_a_count"] == 100
        assert result["new_memo_count"] == 1

        with engine.connect() as connection:
            protected_states = connection.execute(
                text(
                    "SELECT id, deleted_at IS NOT NULL AS is_deleted "
                    "FROM shop_memos WHERE id IN (:guest_id, :admin_id)"
                ),
                {
                    "guest_id": other_guest_deleted_id,
                    "admin_id": admin_deleted_id,
                },
            ).all()
        assert {row.id: row.is_deleted for row in protected_states} == {
            other_guest_deleted_id: True,
            admin_deleted_id: True,
        }
    finally:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
        engine.dispose()


def test_restore_and_permanent_delete_roll_back_on_postgresql(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    repository_root = Path(__file__).resolve().parent.parent
    admin_id = uuid.uuid4()
    result_path = tmp_path / "memo-rollback-result.json"

    try:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
            db.metadata.create_all(connection)
            now = datetime.datetime.now(datetime.timezone.utc)
            connection.execute(
                Dataset.__table__.insert(),
                _dataset_values(
                    admin_id,
                    kind="admin",
                    system_key="admin",
                    now=now,
                ),
            )
            restore_id = connection.execute(
                ShopMemo.__table__.insert()
                .values(
                    dataset_id=admin_id,
                    title="復元rollback対象",
                    body="復元rollback対象",
                    created_at=now,
                    updated_at=now,
                    deleted_at=now,
                )
                .returning(ShopMemo.id)
            ).scalar_one()
            delete_id = connection.execute(
                ShopMemo.__table__.insert()
                .values(
                    dataset_id=admin_id,
                    title="完全削除rollback対象",
                    body="完全削除rollback対象",
                    created_at=now,
                    updated_at=now,
                    deleted_at=now,
                )
                .returning(ShopMemo.id)
            ).scalar_one()

        process = subprocess.run(
            [sys.executable, "-c", ROLLBACK_WORKER_CODE],
            cwd=repository_root,
            env=_postgresql_worker_environment(
                result_path,
                TEST_RESTORE_MEMO_ID=restore_id,
                TEST_DELETE_MEMO_ID=delete_id,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

        assert process.returncode == 0
        assert json.loads(result_path.read_text()) == {
            "restore_status": 500,
            "delete_status": 500,
            "restore_rolled_back": True,
            "delete_rolled_back": True,
        }

        with engine.connect() as connection:
            persisted = connection.execute(
                text(
                    "SELECT id, deleted_at IS NOT NULL AS is_deleted "
                    "FROM shop_memos WHERE id IN (:restore_id, :delete_id)"
                ),
                {"restore_id": restore_id, "delete_id": delete_id},
            ).all()
        assert {row.id: row.is_deleted for row in persisted} == {
            restore_id: True,
            delete_id: True,
        }
    finally:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
        engine.dispose()


def test_concurrent_memo_posts_never_exceed_dataset_limit(tmp_path):
    engine = create_engine(POSTGRESQL_TEST_DATABASE_URL, pool_pre_ping=True)
    repository_root = Path(__file__).resolve().parent.parent
    guest_a_id = uuid.uuid4()
    guest_b_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    application_name = f"shop-memo-limit-test-{uuid.uuid4()}"
    lock_acquired_marker = tmp_path / "memo-lock-acquired"
    lock_release_marker = tmp_path / "memo-lock-release"
    result_path = tmp_path / "memo-worker-result.json"
    worker_process = None
    schema_is_ready = False

    try:
        with engine.begin() as connection:
            db.metadata.drop_all(connection)
            db.metadata.create_all(connection)
            now = datetime.datetime.now(datetime.timezone.utc)
            connection.execute(
                Dataset.__table__.insert(),
                [
                    _dataset_values(
                        guest_a_id,
                        kind="guest",
                        system_key=None,
                        now=now,
                    ),
                    _dataset_values(
                        guest_b_id,
                        kind="guest",
                        system_key=None,
                        now=now,
                    ),
                    _dataset_values(
                        admin_id,
                        kind="admin",
                        system_key="admin",
                        now=now,
                    ),
                ],
            )
            existing_memos = [
                {
                    "dataset_id": guest_a_id,
                    "title": f"Guest A既存メモ{index}",
                    "body": f"Guest A既存メモ{index}",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": now if index == 0 else None,
                }
                for index in range(99)
            ]
            existing_memos.extend(
                [
                    {
                        "dataset_id": guest_b_id,
                        "title": "Guest B保護メモ",
                        "body": "Guest B保護メモ",
                        "created_at": now,
                        "updated_at": now,
                        "deleted_at": None,
                    },
                    {
                        "dataset_id": admin_id,
                        "title": "Admin保護メモ",
                        "body": "Admin保護メモ",
                        "created_at": now,
                        "updated_at": now,
                        "deleted_at": None,
                    },
                ]
            )
            connection.execute(ShopMemo.__table__.insert(), existing_memos)

        schema_is_ready = True
        environment = os.environ.copy()
        environment.update(
            DATABASE_URL=POSTGRESQL_TEST_DATABASE_URL,
            SECRET_KEY="isolated-integration-test-only",
            TEST_GUEST_DATASET_ID=str(guest_a_id),
            TEST_LOCK_ACQUIRED_MARKER=str(lock_acquired_marker),
            TEST_LOCK_RELEASE_MARKER=str(lock_release_marker),
            TEST_RESULT_PATH=str(result_path),
            PGAPPNAME=application_name,
        )
        worker_process = subprocess.Popen(
            [sys.executable, "-c", WORKER_CODE],
            cwd=repository_root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        _wait_for_marker(lock_acquired_marker, worker_process)
        _wait_for_dataset_lock_waiter(engine, application_name)
        lock_release_marker.touch()

        assert worker_process.wait(timeout=30) == 0
        worker_result = json.loads(result_path.read_text())
        assert "error_type" not in worker_result
        assert sorted(worker_result["statuses"]) == [303, 400]

        with engine.connect() as connection:
            counts = {
                dataset_id: connection.execute(
                    text(
                        "SELECT COUNT(*) FROM shop_memos "
                        "WHERE dataset_id = :dataset_id"
                    ),
                    {"dataset_id": dataset_id},
                ).scalar_one()
                for dataset_id in (guest_a_id, guest_b_id, admin_id)
            }
            new_guest_a_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM shop_memos "
                    "WHERE dataset_id = :dataset_id "
                    "AND body IN ('同時追加メモA', '同時追加メモB')"
                ),
                {"dataset_id": guest_a_id},
            ).scalar_one()

        assert counts == {
            guest_a_id: 100,
            guest_b_id: 1,
            admin_id: 1,
        }
        assert new_guest_a_count == 1
    finally:
        lock_release_marker.touch(exist_ok=True)
        if worker_process is not None and worker_process.poll() is None:
            worker_process.terminate()
            worker_process.wait(timeout=10)
        if schema_is_ready:
            with engine.begin() as connection:
                db.metadata.drop_all(connection)
        engine.dispose()
