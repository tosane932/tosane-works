"""Task API contracts exposed by the Phase 24 survivor review."""

import pytest
from flask import g

from models import ShopTask, db


def _task(dataset, title, *, position=0, task_id=None):
    values = {"dataset": dataset, "title": title, "position": position}
    if task_id is not None:
        values["id"] = task_id
    task = ShopTask(**values)
    db.session.add(task)
    db.session.commit()
    return task


def _post_json(client, csrf_token, path, payload):
    g.pop("csrf_token", None)
    return client.post(
        path,
        json=payload,
        headers={"X-CSRFToken": csrf_token(client, "/shop-tools/tasks")},
        follow_redirects=False,
    )


def test_zero_task_id_is_rejected_before_reorder_or_bulk_delete(
    authenticated_client, admin_dataset, csrf_token
):
    # The API accepts positive task IDs only, even if legacy data contains ID 0.
    zero = _task(admin_dataset, "ID 0のタスク", task_id=0)
    first = _task(admin_dataset, "通常タスク", position=1)
    assert zero.id == 0

    reorder = _post_json(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/reorder",
        {"task_ids": [first.id, zero.id]},
    )
    assert reorder.status_code == 400
    assert reorder.get_json()["ok"] is False

    bulk_delete = _post_json(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/bulk-delete",
        {"task_ids": [zero.id]},
    )
    assert bulk_delete.status_code == 400
    assert bulk_delete.get_json()["ok"] is False
    db.session.expire_all()
    assert db.session.get(ShopTask, zero.id) is not None
    assert db.session.get(ShopTask, first.id) is not None
    assert db.session.get(ShopTask, zero.id).position == 0
    assert db.session.get(ShopTask, first.id).position == 1


@pytest.mark.parametrize(
    "path", ["/shop-tools/tasks/reorder", "/shop-tools/tasks/bulk-delete"]
)
def test_empty_task_id_list_is_rejected_even_when_dataset_has_no_tasks(
    authenticated_client, admin_dataset, csrf_token, path
):
    assert ShopTask.query.filter_by(dataset_id=admin_dataset.id).count() == 0
    response = _post_json(
        authenticated_client, csrf_token, path, {"task_ids": []}
    )
    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert ShopTask.query.filter_by(dataset_id=admin_dataset.id).count() == 0


def test_create_json_success_has_true_ok_and_saved_task(
    authenticated_client, admin_dataset, csrf_token
):
    response = _post_json(
        authenticated_client, csrf_token, "/shop-tools/tasks", {"title": "仕込み確認"}
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["ok"] is True
    task = ShopTask.query.filter_by(dataset_id=admin_dataset.id).one()
    assert body["task"]["id"] == task.id
    assert body["task"]["title"] == task.title == "仕込み確認"


def test_favorite_json_success_has_true_ok_and_saved_state(
    authenticated_client, admin_dataset, csrf_token
):
    task = _task(admin_dataset, "お気に入り")
    response = _post_json(
        authenticated_client,
        csrf_token,
        f"/shop-tools/tasks/{task.id}/favorite",
        {"starred": True},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["task"]["id"] == task.id
    assert body["task"]["is_starred"] is True
    db.session.expire_all()
    assert db.session.get(ShopTask, task.id).is_starred is True


def test_reorder_json_success_has_true_ok_and_saved_order(
    authenticated_client, admin_dataset, csrf_token
):
    first = _task(admin_dataset, "最初", position=0)
    second = _task(admin_dataset, "次", position=1)
    expected = [second.id, first.id]
    response = _post_json(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/reorder",
        {"task_ids": expected},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["task_ids"] == expected
    db.session.expire_all()
    saved = ShopTask.query.order_by(ShopTask.position).all()
    assert [(task.id, task.position) for task in saved] == [
        (second.id, 0),
        (first.id, 1),
    ]


def test_bulk_delete_json_success_has_true_ok_and_deleted_ids(
    authenticated_client, admin_dataset, csrf_token
):
    deleted = _task(admin_dataset, "削除")
    kept = _task(admin_dataset, "保持", position=1)
    response = _post_json(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/bulk-delete",
        {"task_ids": [deleted.id]},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["deleted_ids"] == [deleted.id]
    assert db.session.get(ShopTask, deleted.id) is None
    assert db.session.get(ShopTask, kept.id) is not None


@pytest.mark.parametrize(
    "path", ["/shop-tools/tasks/reorder", "/shop-tools/tasks/bulk-delete"]
)
def test_anonymous_task_write_with_valid_csrf_redirects_to_login(
    client, admin_dataset, csrf_token, path
):
    _task(admin_dataset, "保護対象")
    g.pop("csrf_token", None)
    login_token = csrf_token(client, "/login")
    response = client.post(
        path,
        json={"task_ids": [1]},
        headers={"X-CSRFToken": login_token},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert ShopTask.query.count() == 1
