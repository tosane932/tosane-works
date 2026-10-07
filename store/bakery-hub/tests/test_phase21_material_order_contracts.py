"""Business contracts exposed by the Phase 20 material-order survivors."""

from unittest.mock import Mock
from urllib.parse import quote

import pytest
from flask import g
from sqlalchemy.exc import SQLAlchemyError

from models import Dataset, MaterialOrderItem, db


def _post_create(client, csrf_token, payload):
    token = csrf_token(client, "/material-orders")
    return client.post(
        "/material-orders",
        data={**payload, "csrf_token": token},
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    ("field", "limit", "character", "error"),
    [
        ("name", 100, "材", "材料名は100文字以内で入力してください。"),
        ("quantity_text", 30, "量", "数量・単位は30文字以内で入力してください。"),
        ("memo", 300, "メ", "メモは300文字以内で入力してください。"),
    ],
    ids=["name", "quantity_text", "memo"],
)
def test_material_text_limit_accepts_limit_and_rejects_next_character(
    authenticated_client, admin_dataset, csrf_token, field, limit, character, error
):
    for expected_count, length in enumerate((limit - 1, limit, limit + 1), start=1):
        payload = {"name": "通常材料"}
        payload[field] = character * length
        response = _post_create(authenticated_client, csrf_token, payload)
        rows = db.session.query(MaterialOrderItem).order_by(MaterialOrderItem.id).all()

        if length <= limit:
            assert response.status_code == 303
            assert len(rows) == expected_count
            assert rows[-1].dataset_id == admin_dataset.id
            assert getattr(rows[-1], field) == payload[field]
        else:
            assert response.status_code == 400
            assert len(rows) == expected_count - 1
            assert error in response.get_data(as_text=True)


def test_material_list_db_failure_rolls_back_and_returns_safe_response(
    authenticated_client, admin_dataset, flask_app, monkeypatch
):
    broken_query = Mock()
    broken_query.filter_by.return_value = broken_query
    broken_query.order_by.return_value = broken_query
    broken_query.all.side_effect = SQLAlchemyError("controlled list read failure")
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(MaterialOrderItem, "query", broken_query)
    monkeypatch.setattr(db.session, "rollback", rollback)
    monkeypatch.setitem(flask_app.config, "PROPAGATE_EXCEPTIONS", False)

    response = authenticated_client.get("/material-orders")

    assert response.status_code == 500
    assert response.get_data(as_text=True) == "材料発注リストを表示できませんでした。"
    rollback.assert_called_once_with()


def test_material_create_rejects_dataset_disappearing_at_write_lock(
    authenticated_client, admin_dataset, csrf_token, monkeypatch
):
    token = csrf_token(authenticated_client, "/material-orders")
    real_query = Dataset.query
    missing_lock = Mock()
    missing_lock.populate_existing.return_value = missing_lock
    missing_lock.with_for_update.return_value = missing_lock
    missing_lock.one_or_none.return_value = None
    query_proxy = Mock()
    query_proxy.filter_by.side_effect = (
        lambda **filters: missing_lock
        if "id" in filters
        else real_query.filter_by(**filters)
    )
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(Dataset, "query", query_proxy)
    monkeypatch.setattr(db.session, "rollback", rollback)

    response = authenticated_client.post(
        "/material-orders",
        data={"name": "ロック時に消失", "csrf_token": token},
        follow_redirects=False,
    )

    assert missing_lock.one_or_none.called
    assert response.status_code == 403
    rollback.assert_called_once_with()
    assert db.session.query(MaterialOrderItem).count() == 0


def test_material_create_refuses_dataset_already_over_limit(
    authenticated_client, admin_dataset, csrf_token
):
    db.session.add_all(
        [MaterialOrderItem(dataset=admin_dataset, name=f"既存{index}") for index in range(101)]
    )
    db.session.commit()

    response = _post_create(authenticated_client, csrf_token, {"name": "102件目"})

    assert response.status_code == 400
    assert "材料発注リストは100件まで登録できます。" in response.get_data(as_text=True)
    assert db.session.query(MaterialOrderItem).count() == 101
    assert db.session.query(MaterialOrderItem).filter_by(name="102件目").count() == 0


@pytest.mark.parametrize("operation", ["completion", "delete"])
def test_anonymous_material_write_redirects_to_login_without_changes(
    client, admin_dataset, csrf_token, operation
):
    item = MaterialOrderItem(dataset=admin_dataset, name="保護対象")
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    token = csrf_token(client, "/login")
    g.pop("_login_user", None)
    payload = {"csrf_token": token}
    if operation == "completion":
        payload["completed"] = "1"

    response = client.post(
        f"/material-orders/{item_id}/{operation}",
        data=payload,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    db.session.expire_all()
    unchanged = db.session.get(MaterialOrderItem, item_id)
    assert unchanged is not None
    assert unchanged.is_completed is False


def test_percent_encoded_completion_one_marks_item_completed(
    authenticated_client, admin_dataset, csrf_token
):
    item = MaterialOrderItem(dataset=admin_dataset, name="完了対象")
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    token = csrf_token(authenticated_client, "/material-orders")
    response = authenticated_client.post(
        f"/material-orders/{item_id}/completion",
        data="completed=%31&csrf_token=" + quote(token, safe=""),
        content_type="application/x-www-form-urlencoded",
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.session.expire_all()
    completed_item = db.session.get(MaterialOrderItem, item_id)
    assert completed_item.is_completed is True
    assert completed_item.completed_at is not None
