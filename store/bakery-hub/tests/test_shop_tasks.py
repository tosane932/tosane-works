import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app as app_module
import shop_tasks as shop_tasks_module
from models import Dataset, ShopTask, db


NOW = datetime.datetime(2026, 9, 23, 3, 0, tzinfo=datetime.timezone.utc)


def _create_guest_dataset(*, minute_offset=0):
    created_at = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minute_offset)
    )
    dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=created_at,
        last_activity_at=created_at,
        absolute_expires_at=created_at + datetime.timedelta(hours=2),
    )
    db.session.add(dataset)
    db.session.commit()
    return dataset


def _guest_client(flask_app, dataset):
    test_client = flask_app.test_client()
    with test_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{dataset.id}"
        session_data["_fresh"] = True
    return test_client


def _create_task(
    dataset,
    *,
    title,
    is_completed=False,
    is_starred=False,
    position=None,
    created_at=NOW,
):
    task_values = dict(
        dataset=dataset,
        title=title,
        is_completed=is_completed,
        is_starred=is_starred,
        created_at=created_at,
        completed_at=NOW if is_completed else None,
    )
    if position is not None:
        task_values["position"] = position
    task = ShopTask(**task_values)
    db.session.add(task)
    db.session.commit()
    return task


def _post_with_csrf(client, csrf_token, path, data=None):
    payload = dict(data or {})
    g.pop("csrf_token", None)
    payload["csrf_token"] = csrf_token(client, "/shop-tools/tasks")
    return client.post(path, data=payload, follow_redirects=False)


def _post_json_with_csrf(client, csrf_token, path, payload=None):
    g.pop("csrf_token", None)
    return client.post(
        path,
        json=payload or {},
        headers={"X-CSRFToken": csrf_token(client, "/shop-tools/tasks")},
    )


def _tamper_csrf_token(token):
    replacement = "A" if token[0] != "A" else "B"
    return replacement + token[1:]


def _task_snapshot():
    return [
        (
            task.id,
            task.dataset_id,
            task.title,
            task.is_completed,
            task.is_starred,
            task.position,
            task.created_at,
            task.completed_at,
        )
        for task in ShopTask.query.order_by(ShopTask.id)
    ]


def test_admin_can_view_task_ui_and_empty_state(
    authenticated_client,
    admin_dataset,
):
    response = authenticated_client.get("/shop-tools/tasks")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert document.select_one('nav a[data-tool="tasks"][aria-current="page"]')
    assert document.select_one('.shop-tools-header svg[data-icon="list-todo"]')
    assert document.select_one('form[action="/shop-tools/tasks"]')
    assert document.select_one('input[name="title"][maxlength="100"]')
    empty_state = document.select_one(".shop-task-empty")
    assert empty_state is not None
    assert empty_state.select_one("strong").get_text(strip=True) == "タスクはまだありません"
    assert "追加ボタンから登録できます。" in empty_state.get_text(" ", strip=True)

    empty_example = empty_state.select_one(".shop-tools-empty-example")
    assert empty_example is not None
    assert empty_example.get_text(strip=True) == "例：明日の予約分を準備"
    assert "準備中" not in document.get_text()
    assert "✅" not in document.get_text()


@pytest.mark.parametrize(
    ("submitted_title", "saved_title"),
    [
        ("  開店前に冷蔵庫を確認  ", "開店前に冷蔵庫を確認"),
        ("タ" * 100, "タ" * 100),
    ],
)
def test_create_task_normalizes_valid_title(
    authenticated_client,
    admin_dataset,
    csrf_token,
    submitted_title,
    saved_title,
):
    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks",
        {"title": submitted_title},
    )

    assert response.status_code == 303
    task = ShopTask.query.one()
    assert task.dataset_id == admin_dataset.id
    assert task.title == saved_title
    assert task.is_completed is False
    assert task.is_starred is False
    assert task.position == 0
    assert task.completed_at is None


@pytest.mark.parametrize("invalid_title", ["", "   ", "タ" * 101])
def test_invalid_title_is_preserved_without_database_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
    invalid_title,
):
    _create_task(admin_dataset, title="既存タスク")
    before = _task_snapshot()

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks",
        {"title": invalid_title},
    )
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 400
    assert _task_snapshot() == before
    assert document.select_one('input[name="title"]').get("value") == (
        invalid_title
    )
    assert "タスク名を入力してください。" in document.get_text() or (
        "タスク名は100文字以内で入力してください。" in document.get_text()
    )


def test_tasks_list_incomplete_before_completed_and_position_order(
    authenticated_client,
    admin_dataset,
):
    _create_task(admin_dataset, title="INCOMPLETE_SECOND", position=1)
    _create_task(
        admin_dataset,
        title="COMPLETED_SECOND",
        is_completed=True,
        position=3,
    )
    _create_task(
        admin_dataset,
        title="INCOMPLETE_FIRST",
        position=0,
    )
    _create_task(
        admin_dataset,
        title="COMPLETED_FIRST",
        is_completed=True,
        position=2,
    )

    html = authenticated_client.get("/shop-tools/tasks").get_data(
        as_text=True
    )

    assert html.index("INCOMPLETE_FIRST") < html.index("INCOMPLETE_SECOND")
    assert html.index("INCOMPLETE_SECOND") < html.index("COMPLETED_FIRST")
    assert html.index("COMPLETED_FIRST") < html.index("COMPLETED_SECOND")


def test_task_cards_expose_accessible_post_controls(
    authenticated_client,
    admin_dataset,
):
    incomplete = _create_task(admin_dataset, title="未完了UI確認")
    completed = _create_task(
        admin_dataset,
        title="完了UI確認",
        is_completed=True,
    )

    response = authenticated_client.get("/shop-tools/tasks")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    cards = {
        card.select_one(".shop-task-title").get_text(strip=True): card
        for card in document.select(".shop-task-card")
    }

    incomplete_card = cards["未完了UI確認"]
    assert incomplete_card.get("data-task-id") == str(incomplete.id)
    assert incomplete_card.get("data-edit-url") == (
        f"/shop-tools/tasks/{incomplete.id}/edit"
    )
    incomplete_form = incomplete_card.select_one(
        f'form[action="/shop-tools/tasks/{incomplete.id}/completion"]'
    )
    incomplete_toggle = incomplete_form.select_one(".shop-task-toggle")
    assert incomplete_form.select_one('input[name="csrf_token"]') is not None
    assert incomplete_form.select_one('input[name="completed"]')["value"] == "1"
    assert incomplete_toggle.get("aria-pressed") == "false"
    assert incomplete_toggle.select_one('svg[data-icon="square"]') is not None
    assert incomplete_card.select_one(".shop-task-edit-trigger") is not None
    assert incomplete_card.select_one(".shop-task-inline-edit") is not None
    assert incomplete_card.select_one(
        'button.shop-task-star[aria-pressed="false"] svg[data-icon="star"]'
    ) is not None

    completed_card = cards["完了UI確認"]
    completed_form = completed_card.select_one(
        f'form[action="/shop-tools/tasks/{completed.id}/completion"]'
    )
    completed_toggle = completed_form.select_one(".shop-task-toggle")
    assert completed_form.select_one('input[name="completed"]')["value"] == "0"
    assert completed_toggle.get("aria-pressed") == "true"
    assert completed_toggle.select_one(
        'svg[data-icon="square-check-big"]'
    ) is not None
    assert "shop-task-card-completed" in completed_card.get("class", [])

    assert completed_card.select_one(".shop-task-delete-confirmation") is None
    assert document.select_one("[data-task-selection-toolbar][hidden]")
    assert document.select_one("[data-task-select-all]")
    assert document.select_one("[data-task-bulk-delete]")
    assert document.select_one("[data-task-selection-cancel]")
    assert document.select_one("[data-task-start-selection]")
    clear_form = document.select_one(
        'form[action="/shop-tools/tasks/completed/delete"]'
    )
    assert clear_form.select_one('input[name="csrf_token"]') is not None
    assert document.select_one("details.shop-task-completed-group")
    assert document.select_one('[data-task-snackbar][hidden]')
    assert document.select_one('script[src$="/static/shop_tasks.js"]')


def test_completion_posts_are_explicit_and_idempotent(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
):
    task = _create_task(admin_dataset, title="業者へ電話")
    first_completed_at = NOW + datetime.timedelta(minutes=5)
    monkeypatch.setattr(
        shop_tasks_module,
        "utc_now",
        lambda: first_completed_at,
    )
    path = f"/shop-tools/tasks/{task.id}/completion"

    first_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        path,
        {"completed": "1"},
    )
    db.session.expire_all()
    completed_task = db.session.get(ShopTask, task.id)
    assert first_response.status_code == 303
    assert completed_task.is_completed is True
    assert app_module._as_utc(completed_task.completed_at) == first_completed_at

    monkeypatch.setattr(
        shop_tasks_module,
        "utc_now",
        lambda: first_completed_at + datetime.timedelta(hours=1),
    )
    second_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        path,
        {"completed": "1"},
    )
    db.session.expire_all()
    assert second_response.status_code == 303
    assert app_module._as_utc(
        db.session.get(ShopTask, task.id).completed_at
    ) == first_completed_at

    for _ in range(2):
        response = _post_with_csrf(
            authenticated_client,
            csrf_token,
            path,
            {"completed": "0"},
        )
        assert response.status_code == 303

    db.session.expire_all()
    reopened_task = db.session.get(ShopTask, task.id)
    assert reopened_task.is_completed is False
    assert reopened_task.completed_at is None


def test_invalid_completion_state_does_not_change_task(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    task = _create_task(admin_dataset, title="状態保持")
    before = _task_snapshot()

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/tasks/{task.id}/completion",
        {"completed": "toggle"},
    )

    assert response.status_code == 400
    assert _task_snapshot() == before


def test_delete_is_post_only_and_removes_only_requested_task(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    deleted_task = _create_task(admin_dataset, title="削除対象")
    kept_task = _create_task(admin_dataset, title="保持対象")
    path = f"/shop-tools/tasks/{deleted_task.id}/delete"

    get_response = authenticated_client.get(path)
    assert get_response.status_code == 405
    assert db.session.get(ShopTask, deleted_task.id) is not None

    post_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        path,
    )
    assert post_response.status_code == 303
    assert db.session.get(ShopTask, deleted_task.id) is None
    assert db.session.get(ShopTask, kept_task.id) is not None


def test_hundredth_task_succeeds_and_hundred_first_is_rejected(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    db.session.add_all(
        [
            ShopTask(dataset=admin_dataset, title=f"既存タスク{index}")
            for index in range(99)
        ]
    )
    db.session.commit()

    hundredth_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks",
        {"title": "100件目"},
    )
    rejected_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks",
        {"title": "101件目"},
    )

    assert hundredth_response.status_code == 303
    assert rejected_response.status_code == 400
    assert "タスクは100件まで登録できます。" in rejected_response.get_data(
        as_text=True
    )
    assert 'value="101件目"' in rejected_response.get_data(as_text=True)
    assert ShopTask.query.filter_by(dataset_id=admin_dataset.id).count() == 100


def test_admin_and_guests_only_see_their_own_tasks(
    flask_app,
    authenticated_client,
    admin_dataset,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    _create_task(admin_dataset, title="ADMIN_ONLY_TASK")
    _create_task(guest_a, title="GUEST_A_ONLY_TASK")
    _create_task(guest_b, title="GUEST_B_ONLY_TASK")
    clients = (
        (authenticated_client, "ADMIN_ONLY_TASK"),
        (_guest_client(flask_app, guest_a), "GUEST_A_ONLY_TASK"),
        (_guest_client(flask_app, guest_b), "GUEST_B_ONLY_TASK"),
    )

    for test_client, expected_title in clients:
        g.pop("_login_user", None)
        html = test_client.get("/shop-tools/tasks").get_data(as_text=True)
        assert expected_title in html
        for protected_title in {
            "ADMIN_ONLY_TASK",
            "GUEST_A_ONLY_TASK",
            "GUEST_B_ONLY_TASK",
        } - {expected_title}:
            assert protected_title not in html


def test_external_dataset_id_is_ignored_when_creating_task(
    flask_app,
    admin_dataset,
    csrf_token,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    guest_a_client = _guest_client(flask_app, guest_a)

    response = _post_with_csrf(
        guest_a_client,
        csrf_token,
        "/shop-tools/tasks",
        {
            "title": "Guest Aタスク",
            "dataset_id": str(guest_b.id),
            "admin_dataset_id": str(admin_dataset.id),
        },
    )

    assert response.status_code == 303
    assert ShopTask.query.one().dataset_id == guest_a.id


@pytest.mark.parametrize(
    "actor_and_target",
    [
        "admin_to_guest_a",
        "guest_a_to_guest_b",
        "guest_b_to_guest_a",
    ],
)
@pytest.mark.parametrize(
    ("route_name", "payload", "initially_completed"),
    [
        ("completion", {"completed": "1"}, False),
        ("completion", {"completed": "0"}, True),
        ("delete", {}, False),
    ],
)
def test_principal_cannot_modify_another_dataset_task_and_gets_404(
    flask_app,
    authenticated_client,
    admin_dataset,
    csrf_token,
    actor_and_target,
    route_name,
    payload,
    initially_completed,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    if actor_and_target == "admin_to_guest_a":
        actor_client = authenticated_client
        target_dataset = guest_a
    elif actor_and_target == "guest_a_to_guest_b":
        actor_client = _guest_client(flask_app, guest_a)
        target_dataset = guest_b
    else:
        actor_client = _guest_client(flask_app, guest_b)
        target_dataset = guest_a
    target_task = _create_task(
        target_dataset,
        title=f"{actor_and_target}保護タスク",
        is_completed=initially_completed,
    )
    before = _task_snapshot()
    g.pop("_login_user", None)

    response = _post_with_csrf(
        actor_client,
        csrf_token,
        f"/shop-tools/tasks/{target_task.id}/{route_name}",
        payload,
    )

    assert response.status_code == 404
    assert _task_snapshot() == before


@pytest.mark.parametrize(
    ("operation", "initially_completed"),
    [
        ("create", False),
        ("complete", False),
        ("reopen", True),
        ("delete", False),
    ],
)
def test_task_writes_reject_missing_and_tampered_csrf_without_changes(
    authenticated_client,
    admin_dataset,
    csrf_token,
    operation,
    initially_completed,
):
    task = _create_task(
        admin_dataset,
        title="CSRF保護タスク",
        is_completed=initially_completed,
    )
    if operation == "create":
        path = "/shop-tools/tasks"
        payload = {"title": "不正追加"}
    elif operation == "delete":
        path = f"/shop-tools/tasks/{task.id}/delete"
        payload = {}
    else:
        path = f"/shop-tools/tasks/{task.id}/completion"
        payload = {"completed": "1" if operation == "complete" else "0"}

    token = csrf_token(authenticated_client, "/shop-tools/tasks")
    before = _task_snapshot()
    missing_response = authenticated_client.post(path, data=payload)
    tampered_response = authenticated_client.post(
        path,
        data={**payload, "csrf_token": _tamper_csrf_token(token)},
    )

    assert missing_response.status_code == 400
    assert tampered_response.status_code == 400
    assert _task_snapshot() == before


@pytest.mark.parametrize("operation", ["create", "completion", "delete"])
def test_database_failure_rolls_back_task_write(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
    operation,
):
    task = _create_task(admin_dataset, title="既存タスク")
    token = csrf_token(authenticated_client, "/shop-tools/tasks")
    before = _task_snapshot()
    real_commit = db.session.commit
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test task write failure")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    if operation == "create":
        path = "/shop-tools/tasks"
        payload = {"title": "追加失敗", "csrf_token": token}
    elif operation == "completion":
        path = f"/shop-tools/tasks/{task.id}/completion"
        payload = {"completed": "1", "csrf_token": token}
    else:
        path = f"/shop-tools/tasks/{task.id}/delete"
        payload = {"csrf_token": token}

    response = authenticated_client.post(path, data=payload)

    assert response.status_code == 500
    assert rollback.call_count == 1
    assert _task_snapshot() == before
    monkeypatch.setattr(db.session, "commit", real_commit)


def test_anonymous_user_cannot_view_or_create_tasks(
    client,
    admin_dataset,
    csrf_token,
):
    get_response = client.get("/shop-tools/tasks", follow_redirects=False)
    login_token = csrf_token(client, "/login")
    post_response = client.post(
        "/shop-tools/tasks",
        data={"title": "匿名タスク", "csrf_token": login_token},
        follow_redirects=False,
    )

    assert get_response.status_code == 302
    assert post_response.status_code == 302
    assert ShopTask.query.count() == 0


def test_task_title_is_escaped_and_template_avoids_inner_html(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    attack = "<script>alert(1)</script>"
    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks",
        {"title": attack},
    )
    assert response.status_code == 303

    html = authenticated_client.get("/shop-tools/tasks").get_data(as_text=True)
    template_source = (
        Path(app_module.app.root_path) / "templates" / "shop_tasks.html"
    ).read_text()
    assert attack not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "|safe" not in template_source
    assert "innerHTML" not in template_source


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"title": "   "},
        {"title": "タ" * 101},
        {"title": "不整合", "is_completed": True, "completed_at": None},
        {
            "title": "逆向き不整合",
            "is_completed": False,
            "completed_at": NOW,
        },
        {"title": "不正な順序", "position": -1},
    ],
)
def test_shop_task_database_constraints_reject_invalid_rows(
    flask_app,
    admin_dataset,
    invalid_values,
):
    task_values = {
        "dataset": admin_dataset,
        "title": "制約確認",
        **invalid_values,
    }
    db.session.add(ShopTask(**task_values))

    with pytest.raises(IntegrityError):
        db.session.commit()

    db.session.rollback()
    assert ShopTask.query.count() == 0


def test_completion_json_response_supports_undo_without_reload(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    task = _create_task(admin_dataset, title="即時完了", position=0)
    path = f"/shop-tools/tasks/{task.id}/completion"

    completed_response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        path,
        {"completed": True},
    )
    assert completed_response.status_code == 200
    assert completed_response.get_json() == {
        "ok": True,
        "task": {
            "id": task.id,
            "is_completed": True,
            "is_starred": False,
            "position": 0,
            "title": "即時完了",
        },
    }

    undo_response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        path,
        {"completed": False},
    )
    assert undo_response.status_code == 200
    db.session.expire_all()
    restored = db.session.get(ShopTask, task.id)
    assert restored.is_completed is False
    assert restored.completed_at is None


@pytest.mark.parametrize(
    ("submitted_title", "expected_status", "expected_title"),
    [
        ("  仕込み表を直す  ", 200, "仕込み表を直す"),
        ("", 400, "変更前"),
        ("   ", 400, "変更前"),
        ("タ" * 101, 400, "変更前"),
        (123, 400, "変更前"),
    ],
)
def test_edit_task_validates_title_and_returns_json(
    authenticated_client,
    admin_dataset,
    csrf_token,
    submitted_title,
    expected_status,
    expected_title,
):
    task = _create_task(admin_dataset, title="変更前", position=0)

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/tasks/{task.id}/edit",
        {"title": submitted_title},
    )

    assert response.status_code == expected_status
    db.session.expire_all()
    assert db.session.get(ShopTask, task.id).title == expected_title
    if expected_status == 200:
        assert response.get_json()["task"]["title"] == expected_title
    else:
        assert response.get_json()["ok"] is False


def test_edit_task_has_safe_non_javascript_form_fallback(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    task = _create_task(admin_dataset, title="変更前", position=0)

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/tasks/{task.id}/edit",
        {"title": "フォーム更新"},
    )

    assert response.status_code == 303
    assert db.session.get(ShopTask, task.id).title == "フォーム更新"


@pytest.mark.parametrize("desired_state", [True, False])
def test_favorite_can_be_set_idempotently_without_reload(
    authenticated_client,
    admin_dataset,
    csrf_token,
    desired_state,
):
    task = _create_task(
        admin_dataset,
        title="お気に入り",
        is_starred=not desired_state,
        position=0,
    )
    path = f"/shop-tools/tasks/{task.id}/favorite"

    for _ in range(2):
        response = _post_json_with_csrf(
            authenticated_client,
            csrf_token,
            path,
            {"starred": desired_state},
        )
        assert response.status_code == 200
        assert response.get_json()["task"]["is_starred"] is desired_state

    db.session.expire_all()
    assert db.session.get(ShopTask, task.id).is_starred is desired_state


def test_favorite_rejects_non_boolean_json_without_changes(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    task = _create_task(admin_dataset, title="状態保持", position=0)

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/tasks/{task.id}/favorite",
        {"starred": {"unexpected": True}},
    )

    assert response.status_code == 400
    assert db.session.get(ShopTask, task.id).is_starred is False


def test_reorder_persists_exact_dataset_order(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    first = _create_task(admin_dataset, title="最初", position=0)
    second = _create_task(admin_dataset, title="中央", position=1)
    third = _create_task(admin_dataset, title="最後", position=2)

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/reorder",
        {"task_ids": [third.id, first.id, second.id]},
    )

    assert response.status_code == 200
    db.session.expire_all()
    ordered = ShopTask.query.order_by(ShopTask.position).all()
    assert [task.id for task in ordered] == [third.id, first.id, second.id]
    assert [task.position for task in ordered] == [0, 1, 2]

    html = authenticated_client.get("/shop-tools/tasks").get_data(as_text=True)
    assert html.index("最後") < html.index("最初") < html.index("中央")


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda own, foreign: {"task_ids": [own[0].id, own[0].id]},
        lambda own, foreign: {"task_ids": [own[0].id]},
        lambda own, foreign: {
            "task_ids": [own[0].id, own[1].id, foreign.id]
        },
        lambda own, foreign: {"task_ids": [own[0].id, "invalid"]},
        lambda own, foreign: {"task_ids": "not-a-list"},
    ],
)
def test_reorder_rejects_duplicates_missing_invalid_and_foreign_ids(
    flask_app,
    authenticated_client,
    admin_dataset,
    csrf_token,
    payload_factory,
):
    guest = _create_guest_dataset(minute_offset=1)
    own = [
        _create_task(admin_dataset, title="A", position=0),
        _create_task(admin_dataset, title="B", position=1),
    ]
    foreign = _create_task(guest, title="別Dataset", position=0)
    before = _task_snapshot()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/reorder",
        payload_factory(own, foreign),
    )

    assert response.status_code == 400
    assert _task_snapshot() == before


def test_bulk_delete_removes_selected_current_dataset_tasks(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    deleted = [
        _create_task(admin_dataset, title="削除A", position=0),
        _create_task(admin_dataset, title="削除B", position=1),
    ]
    kept = _create_task(admin_dataset, title="保持", position=2)

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/bulk-delete",
        {"task_ids": [task.id for task in deleted]},
    )

    assert response.status_code == 200
    assert response.get_json()["deleted_ids"] == [
        task.id for task in deleted
    ]
    assert ShopTask.query.filter_by(dataset_id=admin_dataset.id).all() == [
        kept
    ]


def test_bulk_delete_fails_closed_when_foreign_id_is_mixed_in(
    flask_app,
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    guest = _create_guest_dataset(minute_offset=1)
    own = _create_task(admin_dataset, title="自Dataset", position=0)
    foreign = _create_task(guest, title="別Dataset", position=0)
    before = _task_snapshot()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/bulk-delete",
        {"task_ids": [own.id, foreign.id]},
    )

    assert response.status_code == 400
    assert _task_snapshot() == before


def test_clear_completed_never_deletes_incomplete_or_other_dataset_tasks(
    flask_app,
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    guest = _create_guest_dataset(minute_offset=1)
    incomplete = _create_task(admin_dataset, title="未完了", position=0)
    completed = _create_task(
        admin_dataset,
        title="完了済み",
        is_completed=True,
        position=1,
    )
    foreign_completed = _create_task(
        guest,
        title="別Dataset完了",
        is_completed=True,
        position=0,
    )

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/tasks/completed/delete",
    )

    assert response.status_code == 200
    assert response.get_json()["deleted_count"] == 1
    assert db.session.get(ShopTask, completed.id) is None
    assert db.session.get(ShopTask, incomplete.id) is not None
    assert db.session.get(ShopTask, foreign_completed.id) is not None


@pytest.mark.parametrize(
    ("route_suffix", "payload"),
    [
        ("edit", {"title": "越境編集"}),
        ("favorite", {"starred": True}),
    ],
)
def test_new_single_task_actions_cannot_cross_dataset_boundary(
    flask_app,
    authenticated_client,
    admin_dataset,
    csrf_token,
    route_suffix,
    payload,
):
    guest = _create_guest_dataset(minute_offset=1)
    foreign = _create_task(guest, title="保護対象", position=0)
    before = _task_snapshot()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/tasks/{foreign.id}/{route_suffix}",
        payload,
    )

    assert response.status_code == 404
    assert _task_snapshot() == before


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/shop-tools/tasks/1/edit", {"title": "不正"}),
        ("/shop-tools/tasks/1/favorite", {"starred": True}),
        ("/shop-tools/tasks/reorder", {"task_ids": [1]}),
        ("/shop-tools/tasks/bulk-delete", {"task_ids": [1]}),
        ("/shop-tools/tasks/completed/delete", {}),
    ],
)
def test_new_task_writes_require_csrf(
    authenticated_client,
    admin_dataset,
    path,
    payload,
):
    _create_task(admin_dataset, title="CSRF保護", position=0)
    before = _task_snapshot()

    response = authenticated_client.post(path, json=payload)

    assert response.status_code == 400
    assert _task_snapshot() == before


@pytest.mark.parametrize(
    ("operation", "path", "payload"),
    [
        ("edit", "/shop-tools/tasks/{id}/edit", {"title": "変更後"}),
        ("favorite", "/shop-tools/tasks/{id}/favorite", {"starred": True}),
        ("reorder", "/shop-tools/tasks/reorder", {"task_ids": None}),
        ("bulk-delete", "/shop-tools/tasks/bulk-delete", {"task_ids": None}),
        ("clear", "/shop-tools/tasks/completed/delete", {}),
    ],
)
def test_new_task_database_failures_roll_back(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
    operation,
    path,
    payload,
):
    first = _create_task(
        admin_dataset,
        title="既存A",
        is_completed=operation == "clear",
        position=0,
    )
    second = _create_task(admin_dataset, title="既存B", position=1)
    if operation in {"reorder", "bulk-delete"}:
        payload = {
            "task_ids": (
                [second.id, first.id]
                if operation == "reorder"
                else [first.id]
            )
        }
    path = path.format(id=first.id)
    before = _task_snapshot()
    real_commit = db.session.commit
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test task UX write failure")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        path,
        payload,
    )

    assert response.status_code == 500
    assert rollback.call_count == 1
    assert _task_snapshot() == before
    monkeypatch.setattr(db.session, "commit", real_commit)


def test_task_template_and_script_expose_direct_manipulation_contract():
    template_source = (
        Path(app_module.app.root_path) / "templates" / "shop_tasks.html"
    ).read_text()
    script_source = (
        Path(app_module.app.root_path) / "static" / "shop_tasks.js"
    ).read_text()

    assert "data-task-list-root" in template_source
    assert "data-task-selection-toolbar" in template_source
    assert "data-task-completed-group" in template_source
    assert 'data-icon="star"' in template_source
    assert 'data-icon="circle"' in template_source
    assert "LONG_PRESS_MS: 420" in script_source
    assert "SELECTION_REORDER_PRESS_MS: 280" in script_source
    assert "duration: 180" in script_source
    assert "MOVE_CANCEL_PX: 10" in script_source
    assert "pointerdown" in script_source
    assert "fetch(" in script_source
    assert "innerHTML" not in script_source
