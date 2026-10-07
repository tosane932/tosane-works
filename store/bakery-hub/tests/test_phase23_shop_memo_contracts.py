"""Phase 23: observable contracts behind Phase 22 shop-memo survivors."""

import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import SQLAlchemyError

from models import ShopMemo, db
from shop_memos import _linkify_memo_body, _validate_autosave_body, _validate_memo


FIXED_TIME = datetime.datetime(2026, 9, 14, tzinfo=datetime.timezone.utc)


def _post_form(client, csrf_token, path, **fields):
    return client.post(
        path,
        data={"csrf_token": csrf_token(client, "/shop-tools/memo"), **fields},
        follow_redirects=False,
    )


def _post_json(client, csrf_token, path, payload):
    return client.post(
        path,
        json=payload,
        headers={"X-CSRFToken": csrf_token(client, "/shop-tools/memo")},
        follow_redirects=False,
    )


def _memo(dataset, *, title, body, deleted=False, created_at=FIXED_TIME, updated_at=FIXED_TIME):
    memo = ShopMemo(
        dataset_id=dataset.id,
        title=title,
        body=body,
        deleted_at=created_at if deleted else None,
        created_at=created_at,
        updated_at=updated_at,
    )
    db.session.add(memo)
    db.session.commit()
    return memo


def _seed_memos(dataset, count):
    db.session.add_all(
        [
            ShopMemo(
                dataset_id=dataset.id,
                title=f"既存メモ{index}",
                body="既存本文",
                created_at=FIXED_TIME,
                updated_at=FIXED_TIME,
            )
            for index in range(count)
        ]
    )
    db.session.commit()


@pytest.mark.parametrize(
    ("length", "expected_error"),
    [
        (100, None),
        (101, "タイトルは100文字以内で入力してください。"),
    ],
    ids=["title-100-accepted", "title-101-rejected"],
)
def test_title_validation_has_exact_100_character_boundary(length, expected_error):
    title = "題" * length
    validated_title, body, _, _, error = _validate_memo(
        {"title": title, "body": "本文"}
    )

    assert error == expected_error
    if expected_error is None:
        assert validated_title == title
        assert body == "本文"


def test_search_accepts_exactly_100_characters(authenticated_client, admin_dataset):
    response = authenticated_client.get(
        "/shop-tools/memo", query_string={"q": "検" * 100}
    )

    assert response.status_code == 200
    assert "検索文字は100文字以内で入力してください。" not in response.get_data(as_text=True)


def test_autosave_accepts_exactly_2000_body_characters():
    body = "メ" * 2000
    title, validated_body, error = _validate_autosave_body({"body": body})

    assert error is None
    assert validated_body == body
    assert title == body[:100]


def test_create_dialog_starts_closed(authenticated_client, admin_dataset):
    response = authenticated_client.get("/shop-tools/memo")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert document.select_one("#shop-memo-create-dialog")["data-open-on-load"] == "false"


def test_invalid_create_reopens_dialog_with_input(
    authenticated_client, admin_dataset, csrf_token
):
    response = _post_form(
        authenticated_client, csrf_token, "/shop-tools/memo", title="", body="入力を保持"
    )
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 400
    assert document.select_one("#shop-memo-create-dialog")["data-open-on-load"] == "true"
    assert "入力を保持" in response.get_data(as_text=True)
    assert ShopMemo.query.count() == 0


def test_sort_uses_selected_timestamp_and_normalizes_unknown_sort(
    authenticated_client, admin_dataset
):
    older_created = _memo(
        admin_dataset,
        title="先に作成",
        body="本文A",
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME + datetime.timedelta(days=3),
    )
    newer_created = _memo(
        admin_dataset,
        title="後で作成",
        body="本文B",
        created_at=FIXED_TIME + datetime.timedelta(days=1),
        updated_at=FIXED_TIME + datetime.timedelta(days=1),
    )

    def ordered_ids(sort):
        response = authenticated_client.get(
            "/shop-tools/memo", query_string={"sort": sort}
        )
        assert response.status_code == 200
        document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        return [int(card["data-memo-id"]) for card in document.select("article.shop-memo-card")]

    assert ordered_ids("created") == [newer_created.id, older_created.id]
    assert ordered_ids("updated") == [older_created.id, newer_created.id]
    assert ordered_ids("unexpected") == [older_created.id, newer_created.id]


@pytest.mark.parametrize(
    "candidate", ["http://user@", "http://:80", "http://["],
    ids=["hostless-user", "hostless-port", "malformed-bracket"],
)
def test_invalid_http_candidates_remain_plain_text_without_crashing(candidate):
    # Rendering an invalid candidate must not leak a parser/group error to the UI.
    try:
        rendered = str(_linkify_memo_body(candidate))
        error_type = None
    except Exception as error:  # contract: even malformed URLs must render safely
        rendered = None
        error_type = type(error).__name__

    assert error_type is None
    assert rendered == candidate
    assert "<a " not in rendered


def test_create_limit_error_keeps_active_view_and_dialog_open(
    authenticated_client, admin_dataset, csrf_token
):
    _seed_memos(admin_dataset, 100)
    response = _post_form(
        authenticated_client, csrf_token, "/shop-tools/memo", title="追加", body="本文"
    )
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 400
    assert document.title.get_text(strip=True).startswith("メモ |")
    assert document.select_one("#shop-memo-create-dialog")["data-open-on-load"] == "true"
    assert ShopMemo.query.count() == 100


@pytest.mark.parametrize("operation", ["create", "autosave", "duplicate"])
def test_over_capacity_state_rejects_every_new_memo_operation(
    authenticated_client, admin_dataset, csrf_token, operation
):
    # Count 101 can arise from older data or concurrent writes. The guard is >= 100.
    _seed_memos(admin_dataset, 101)
    if operation == "create":
        response = _post_form(
            authenticated_client, csrf_token, "/shop-tools/memo", title="追加", body="本文"
        )
    elif operation == "autosave":
        response = _post_json(
            authenticated_client, csrf_token, "/shop-tools/memo/autosave", {"body": "追加本文"}
        )
    else:
        source_id = ShopMemo.query.order_by(ShopMemo.id).first().id
        response = _post_json(
            authenticated_client, csrf_token,
            f"/shop-tools/memo/{source_id}/duplicate", {},
        )

    assert response.status_code == 400
    assert ShopMemo.query.count() == 101


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/shop-tools/memo/1/edit"),
        ("POST", "/shop-tools/memo/1/trash"),
        ("GET", "/shop-tools/memo/trash"),
        ("POST", "/shop-tools/memo/trash/empty"),
        ("POST", "/shop-tools/memo/1/restore"),
        ("POST", "/shop-tools/memo/1/delete"),
    ],
    ids=["edit", "trash", "trash-list", "empty-trash", "restore", "delete"],
)
def test_anonymous_regular_memo_routes_redirect_to_login(
    client, admin_dataset, csrf_token, method, path
):
    if method == "GET":
        response = client.get(path, follow_redirects=False)
    else:
        token = csrf_token(client, "/login")
        response = client.post(
            path,
            data={"csrf_token": token, "title": "変更", "body": "本文"},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?")
    assert ShopMemo.query.count() == 0


def test_edit_commit_error_renders_active_memo_view(
    authenticated_client, admin_dataset, csrf_token, monkeypatch
):
    memo = _memo(admin_dataset, title="編集前", body="編集前本文")
    token = csrf_token(authenticated_client, "/shop-tools/memo")

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("phase23 simulated edit failure")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    response = authenticated_client.post(
        f"/shop-tools/memo/{memo.id}/edit",
        data={"title": "編集後", "body": "保存しない", "csrf_token": token},
    )
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 500
    assert document.title.get_text(strip=True).startswith("メモ |")
    assert document.select_one('dialog.shop-memo-edit-dialog[data-open-on-load="true"]') is not None


@pytest.mark.parametrize("old_title", ["ALPHA", "ZULU"])
def test_autosave_repairs_derived_title_even_when_body_is_unchanged(
    authenticated_client, admin_dataset, csrf_token, old_title
):
    memo = _memo(admin_dataset, title=old_title, body="MIDDLE\n詳細")
    memo_id = memo.id
    response = _post_json(
        authenticated_client, csrf_token,
        f"/shop-tools/memo/{memo_id}/autosave", {"body": "MIDDLE\n詳細"},
    )
    db.session.expire_all()

    assert response.status_code == 200
    assert db.session.get(ShopMemo, memo_id).title == "MIDDLE"


def test_autosave_noop_does_not_change_updated_time(
    authenticated_client, admin_dataset, csrf_token
):
    title = "同じタイトルをそのまま維持する"
    body = title + "\n本文"
    memo = _memo(admin_dataset, title=title, body=body)
    memo_id = memo.id
    db.session.expire_all()
    saved_updated_at = db.session.get(ShopMemo, memo_id).updated_at
    response = _post_json(
        authenticated_client, csrf_token,
        f"/shop-tools/memo/{memo_id}/autosave", {"body": body},
    )
    db.session.expire_all()

    assert response.status_code == 200
    assert db.session.get(ShopMemo, memo_id).updated_at == saved_updated_at


def test_autosave_updates_body_when_derived_title_stays_the_same(
    authenticated_client, admin_dataset, csrf_token
):
    memo = _memo(admin_dataset, title="見出し", body="見出し\n古い本文")
    memo_id = memo.id
    response = _post_json(
        authenticated_client, csrf_token,
        f"/shop-tools/memo/{memo_id}/autosave", {"body": "見出し\n新しい本文"},
    )
    db.session.expire_all()

    assert response.status_code == 200
    assert db.session.get(ShopMemo, memo_id).body == "見出し\n新しい本文"


def test_malformed_pin_json_uses_structured_error(
    authenticated_client, admin_dataset, csrf_token
):
    memo = _memo(admin_dataset, title="ピン対象", body="本文")
    response = authenticated_client.post(
        f"/shop-tools/memo/{memo.id}/pin",
        data="{",
        content_type="application/json",
        headers={"X-CSRFToken": csrf_token(authenticated_client, "/shop-tools/memo")},
    )

    assert response.status_code == 400
    assert response.is_json
    assert response.json["ok"] is False
    assert db.session.get(ShopMemo, memo.id).pinned_at is None


def test_json_restore_reports_success_flag(
    authenticated_client, admin_dataset, csrf_token
):
    memo = _memo(admin_dataset, title="復元対象", body="本文", deleted=True)
    response = _post_json(
        authenticated_client, csrf_token,
        f"/shop-tools/memo/{memo.id}/restore", {},
    )

    assert response.status_code == 200
    assert response.json["ok"] is True
    assert db.session.get(ShopMemo, memo.id).deleted_at is None
