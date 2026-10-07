"""The locked-Dataset denial remains HTTP 403 for public create requests."""

from unittest.mock import Mock

from models import Dataset, MaterialOrderItem, db


def test_missing_locked_dataset_returns_forbidden_http_response(
    authenticated_client, admin_dataset, csrf_token, flask_app, monkeypatch
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
    monkeypatch.setitem(flask_app.config, "PROPAGATE_EXCEPTIONS", False)

    response = authenticated_client.post(
        "/material-orders",
        data={"name": "ロック時に消失", "csrf_token": token},
        follow_redirects=False,
    )

    assert missing_lock.one_or_none.called
    assert response.status_code == 403
    rollback.assert_called_once_with()
    assert db.session.query(MaterialOrderItem).count() == 0
