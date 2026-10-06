import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy.exc import SQLAlchemyError

import app as app_module
from models import Dataset, ShopMemo, db


NOW = datetime.datetime(2026, 9, 14, 15, 0, tzinfo=datetime.timezone.utc)


def _create_guest_dataset(*, minute_offset=0):
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minute_offset
    )
    dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
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


def _create_memo(
    dataset,
    *,
    body,
    title=None,
    deleted=False,
    minute_offset=0,
):
    created_at = NOW + datetime.timedelta(minutes=minute_offset)
    deleted_at = created_at if deleted else None
    memo = ShopMemo(
        dataset=dataset,
        title=body[:100] if title is None else title,
        body=body,
        created_at=created_at,
        updated_at=created_at,
        deleted_at=deleted_at,
    )
    db.session.add(memo)
    db.session.commit()
    return memo


def _post_with_csrf(client, csrf_token, path, data=None):
    payload = dict(data or {})
    if "body" in payload and "title" not in payload:
        payload["title"] = "テストタイトル"
    payload["csrf_token"] = csrf_token(client, "/shop-tools/memo")
    return client.post(path, data=payload, follow_redirects=False)


def _post_json_with_csrf(client, csrf_token, path, payload=None):
    return client.post(
        path,
        json=payload or {},
        headers={
            "X-CSRFToken": csrf_token(client, "/shop-tools/memo"),
        },
        follow_redirects=False,
    )


def _memo_snapshot():
    return [
        (
            memo.id,
            memo.dataset_id,
            memo.title,
            memo.body,
            memo.created_at,
            memo.updated_at,
            memo.deleted_at,
            memo.pinned_at,
        )
        for memo in ShopMemo.query.order_by(ShopMemo.id)
    ]


def _tamper_csrf_token(token):
    replacement = "A" if token[0] != "A" else "B"
    return replacement + token[1:]


def test_admin_can_open_memo_list_and_create_trimmed_multiline_memo(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    empty_response = authenticated_client.get("/shop-tools/memo")
    empty_document = BeautifulSoup(
        empty_response.get_data(as_text=True), "html.parser"
    )

    assert empty_response.status_code == 200
    assert "0件のメモ" in empty_document.get_text()
    assert empty_document.select_one('label[for="shop-memo-body"]') is not None

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": "  牛乳の納品時間\n担当者へ確認  "},
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/shop-tools/memo"
    memo = ShopMemo.query.one()
    assert memo.dataset_id == admin_dataset.id
    assert memo.body == "牛乳の納品時間\n担当者へ確認"


def test_create_accepts_two_thousand_characters(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    body = "メ" * 2000

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": body},
    )

    assert response.status_code == 303
    assert ShopMemo.query.one().body == body


def test_mobile_autosave_creates_body_only_memo_with_derived_title(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    body = "\n  洋生卸 間違い注意  \n木曜日は数量を再確認する"

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo/autosave",
        {"body": body},
    )

    assert response.status_code == 201
    assert response.json["ok"] is True
    memo = ShopMemo.query.one()
    assert memo.dataset_id == admin_dataset.id
    assert memo.title == "洋生卸 間違い注意"
    assert memo.body == body
    assert response.json["memo"]["id"] == memo.id
    assert response.json["memo"]["pinned"] is False


@pytest.mark.parametrize("body", ["", "   ", "\n\t\n"])
def test_mobile_autosave_does_not_create_empty_or_blank_memo(
    authenticated_client,
    admin_dataset,
    csrf_token,
    body,
):
    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo/autosave",
        {"body": body},
    )

    assert response.status_code == 400
    assert response.json["ok"] is False
    assert ShopMemo.query.count() == 0


@pytest.mark.parametrize("body", ["メ" * 2001, 123, None])
def test_mobile_autosave_rejects_invalid_body_without_database_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
    body,
):
    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo/autosave",
        {"body": body},
    )

    assert response.status_code == 400
    assert response.json["ok"] is False
    assert ShopMemo.query.count() == 0


def test_mobile_autosave_edits_body_and_derived_title(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memo = _create_memo(admin_dataset, title="編集前", body="編集前の本文")
    memo_id = memo.id
    previous_updated_at = memo.updated_at
    body = "新しい先頭行\n編集後の本文"

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/autosave",
        {"body": body},
    )

    assert response.status_code == 200
    db.session.expire_all()
    saved = db.session.get(ShopMemo, memo_id)
    assert saved.title == "新しい先頭行"
    assert saved.body == body
    assert saved.updated_at >= previous_updated_at


def test_mobile_autosave_escapes_html_when_rendered(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    attack = "<img src=x onerror=alert(1)>"

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo/autosave",
        {"body": attack},
    )
    html = authenticated_client.get("/shop-tools/memo").get_data(as_text=True)

    assert response.status_code == 201
    assert attack not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_oversized_memo_request_is_rejected_before_database_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    token = csrf_token(authenticated_client, "/shop-tools/memo")

    response = authenticated_client.post(
        "/shop-tools/memo",
        data={
            "body": "メモ",
            "padding": "x"
            * (app_module.app.config["MAX_CONTENT_LENGTH"] + 1),
            "csrf_token": token,
        },
    )

    assert response.status_code == 413
    assert ShopMemo.query.count() == 0


def test_memo_full_lifecycle_updates_active_and_trash_views(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memo = _create_memo(admin_dataset, body="編集前")
    memo_id = memo.id

    edit_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/edit",
        {"body": "編集後"},
    )
    assert edit_response.status_code == 303
    assert db.session.get(ShopMemo, memo_id).body == "編集後"

    trash_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/trash",
    )
    assert trash_response.status_code == 303
    db.session.expire_all()
    trashed_memo = db.session.get(ShopMemo, memo_id)
    assert trashed_memo.deleted_at is not None
    assert trashed_memo.updated_at == trashed_memo.deleted_at
    assert "編集後" not in authenticated_client.get(
        "/shop-tools/memo"
    ).get_data(as_text=True)
    assert "編集後" in authenticated_client.get(
        "/shop-tools/memo/trash"
    ).get_data(as_text=True)

    restore_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/restore",
    )
    assert restore_response.status_code == 303
    assert restore_response.headers["Location"].endswith("/shop-tools/memo/trash")
    db.session.expire_all()
    assert db.session.get(ShopMemo, memo_id).deleted_at is None
    assert "編集後" in authenticated_client.get(
        "/shop-tools/memo"
    ).get_data(as_text=True)
    assert "編集後" not in authenticated_client.get(
        "/shop-tools/memo/trash"
    ).get_data(as_text=True)

    _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/trash",
    )
    delete_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/delete",
    )
    assert delete_response.status_code == 303
    assert db.session.get(ShopMemo, memo_id) is None


def test_pin_and_unpin_are_idempotent_and_do_not_change_content_updated_at(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memo = _create_memo(admin_dataset, body="ピン対象")
    memo_id = memo.id
    content_updated_at = memo.updated_at

    first_pin = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/pin",
        {"pinned": True},
    )
    db.session.expire_all()
    first_pinned_at = db.session.get(ShopMemo, memo_id).pinned_at
    second_pin = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/pin",
        {"pinned": True},
    )
    db.session.expire_all()
    pinned_again = db.session.get(ShopMemo, memo_id)

    assert first_pin.status_code == 200
    assert second_pin.status_code == 200
    assert first_pinned_at is not None
    assert pinned_again.pinned_at == first_pinned_at
    assert pinned_again.updated_at == content_updated_at

    unpin = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo_id}/pin",
        {"pinned": False},
    )
    db.session.expire_all()
    unpinned = db.session.get(ShopMemo, memo_id)

    assert unpin.status_code == 200
    assert unpinned.pinned_at is None
    assert unpinned.updated_at == content_updated_at


@pytest.mark.parametrize("pinned", [None, 1, "true", [], {}])
def test_pin_rejects_non_boolean_state_without_database_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
    pinned,
):
    memo = _create_memo(admin_dataset, body="ピン形式確認")
    before = _memo_snapshot()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo.id}/pin",
        {"pinned": pinned},
    )

    assert response.status_code == 400
    assert response.json["ok"] is False
    assert _memo_snapshot() == before


def test_pinned_memos_are_listed_before_newer_unpinned_memos(
    authenticated_client,
    admin_dataset,
):
    pinned = _create_memo(
        admin_dataset,
        title="ピン留め",
        body="ピン留め本文",
        minute_offset=0,
    )
    pinned.pinned_at = NOW + datetime.timedelta(minutes=2)
    unpinned = _create_memo(
        admin_dataset,
        title="新しい通常メモ",
        body="新しい通常本文",
        minute_offset=10,
    )
    db.session.commit()

    document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )
    listed_ids = [
        int(card["data-memo-id"])
        for card in document.select("article.shop-memo-card")
    ]

    assert listed_ids[:2] == [pinned.id, unpinned.id]
    assert document.select_one(
        f'article[data-memo-id="{pinned.id}"] .shop-memo-pin-indicator'
    ) is not None


def test_duplicate_creates_unpinned_copy_with_new_identity_and_timestamps(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    source = _create_memo(
        admin_dataset,
        title="複製元タイトル",
        body="複製元の本文",
    )
    source.pinned_at = NOW + datetime.timedelta(minutes=1)
    db.session.commit()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{source.id}/duplicate",
    )

    assert response.status_code == 201
    memos = ShopMemo.query.order_by(ShopMemo.id).all()
    assert len(memos) == 2
    duplicate = memos[1]
    assert duplicate.id != source.id
    assert duplicate.dataset_id == admin_dataset.id
    assert duplicate.title == source.title
    assert duplicate.body == source.body
    assert duplicate.created_at >= source.created_at
    assert duplicate.updated_at >= source.updated_at
    assert duplicate.pinned_at is None


def test_duplicate_obeys_total_memo_limit(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memos = [
        ShopMemo(
            dataset=admin_dataset,
            title=f"上限メモ{index}",
            body=f"上限メモ{index}",
            created_at=NOW,
            updated_at=NOW,
            deleted_at=NOW if index == 0 else None,
        )
        for index in range(100)
    ]
    db.session.add_all(memos)
    db.session.commit()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memos[1].id}/duplicate",
    )

    assert response.status_code == 400
    assert response.json["ok"] is False
    assert ShopMemo.query.count() == 100


def test_json_trash_and_restore_support_mobile_undo(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memo = _create_memo(admin_dataset, body="Undo対象")

    trash_response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo.id}/trash",
    )
    assert trash_response.status_code == 200
    assert trash_response.json["ok"] is True
    assert trash_response.json["restore_url"].endswith(
        f"/shop-tools/memo/{memo.id}/restore"
    )
    db.session.expire_all()
    assert db.session.get(ShopMemo, memo.id).deleted_at is not None

    restore_response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        trash_response.json["restore_url"],
    )
    assert restore_response.status_code == 200
    db.session.expire_all()
    assert db.session.get(ShopMemo, memo.id).deleted_at is None


def test_trash_lists_only_current_dataset_deleted_memos_with_deleted_time(
    flask_app,
    authenticated_client,
    admin_dataset,
):
    other_guest = _create_guest_dataset(minute_offset=1)
    _create_memo(admin_dataset, body="通常一覧だけのメモ")
    _create_memo(admin_dataset, body="Adminゴミ箱メモ", deleted=True)
    _create_memo(other_guest, body="別Datasetゴミ箱メモ", deleted=True)

    response = authenticated_client.get("/shop-tools/memo/trash")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert response.status_code == 200
    assert "Adminゴミ箱メモ" in document.get_text()
    assert "削除日時：2026/09/15 00:00" in document.get_text()
    assert "通常一覧だけのメモ" not in document.get_text()
    assert "別Datasetゴミ箱メモ" not in document.get_text()


def test_empty_trash_deletes_only_current_dataset_trash_and_reports_count(
    flask_app,
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    other_guest = _create_guest_dataset(minute_offset=1)
    active_memo = _create_memo(admin_dataset, body="Admin通常メモ")
    deleted_memos = [
        _create_memo(admin_dataset, body="Adminゴミ箱A", deleted=True),
        _create_memo(admin_dataset, body="Adminゴミ箱B", deleted=True),
    ]
    other_deleted_memo = _create_memo(
        other_guest,
        body="別Datasetゴミ箱メモ",
        deleted=True,
    )
    active_memo_id = active_memo.id
    deleted_memo_ids = [memo.id for memo in deleted_memos]
    other_deleted_memo_id = other_deleted_memo.id

    response = authenticated_client.post(
        "/shop-tools/memo/trash/empty",
        data={
            "csrf_token": csrf_token(
                authenticated_client,
                "/shop-tools/memo/trash",
            )
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "ゴミ箱のメモを2件完全に削除しました。" in response.get_data(
        as_text=True
    )
    db.session.expire_all()
    assert db.session.get(ShopMemo, active_memo_id) is not None
    assert all(
        db.session.get(ShopMemo, memo_id) is None
        for memo_id in deleted_memo_ids
    )
    assert db.session.get(ShopMemo, other_deleted_memo_id) is not None


def test_guest_empty_trash_cannot_cross_admin_or_other_guest_dataset(
    flask_app,
    admin_dataset,
    csrf_token,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    own_active_memo = _create_memo(guest_a, body="Guest A通常メモ")
    own_deleted_memo = _create_memo(
        guest_a,
        body="Guest Aゴミ箱メモ",
        deleted=True,
    )
    protected_memos = [
        _create_memo(admin_dataset, body="Adminゴミ箱メモ", deleted=True),
        _create_memo(guest_b, body="Guest Bゴミ箱メモ", deleted=True),
    ]
    own_active_memo_id = own_active_memo.id
    own_deleted_memo_id = own_deleted_memo.id
    protected_memo_ids = [memo.id for memo in protected_memos]
    guest_client = _guest_client(flask_app, guest_a)

    response = guest_client.post(
        "/shop-tools/memo/trash/empty",
        data={
            "csrf_token": csrf_token(
                guest_client,
                "/shop-tools/memo/trash",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.session.expire_all()
    assert db.session.get(ShopMemo, own_active_memo_id) is not None
    assert db.session.get(ShopMemo, own_deleted_memo_id) is None
    assert all(
        db.session.get(ShopMemo, memo_id) is not None
        for memo_id in protected_memo_ids
    )


def test_empty_trash_is_post_only_and_requires_valid_csrf(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    deleted_memo = _create_memo(
        admin_dataset,
        body="CSRF保護ゴミ箱メモ",
        deleted=True,
    )
    valid_token = csrf_token(
        authenticated_client,
        "/shop-tools/memo/trash",
    )

    get_response = authenticated_client.get("/shop-tools/memo/trash/empty")
    missing_response = authenticated_client.post(
        "/shop-tools/memo/trash/empty"
    )
    tampered_response = authenticated_client.post(
        "/shop-tools/memo/trash/empty",
        data={"csrf_token": _tamper_csrf_token(valid_token)},
    )

    assert get_response.status_code == 405
    assert missing_response.status_code == 400
    assert tampered_response.status_code == 400
    assert db.session.get(ShopMemo, deleted_memo.id) is not None


def test_empty_trash_commit_failure_rolls_back_all_deletions(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
):
    _create_memo(admin_dataset, body="残る通常メモ")
    _create_memo(admin_dataset, body="rollbackゴミ箱A", deleted=True)
    _create_memo(admin_dataset, body="rollbackゴミ箱B", deleted=True)
    before = _memo_snapshot()
    token = csrf_token(authenticated_client, "/shop-tools/memo/trash")
    real_rollback = db.session.rollback
    rollback = Mock(wraps=real_rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test empty trash failure after flush")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)

    response = authenticated_client.post(
        "/shop-tools/memo/trash/empty",
        data={"csrf_token": token},
    )

    assert response.status_code == 500
    assert rollback.call_count == 1
    assert "ゴミ箱を空にできませんでした。" in response.get_data(
        as_text=True
    )
    assert _memo_snapshot() == before


@pytest.mark.parametrize(
    ("path", "expected", "excluded"),
    [
        ("/shop-tools/memo?q=納品", "牛乳の納品時間", "発注数を確認"),
        ("/shop-tools/memo?q=%25", "割引率%を確認", "発注数を確認"),
        ("/shop-tools/memo?q=_", "商品_A", "発注数を確認"),
        (
            "/shop-tools/memo/trash?q=削除",
            "削除済み納品メモ",
            "牛乳の納品時間",
        ),
    ],
)
def test_memo_search_is_literal_and_scoped_to_current_state(
    authenticated_client,
    admin_dataset,
    path,
    expected,
    excluded,
):
    _create_memo(admin_dataset, body="牛乳の納品時間")
    _create_memo(admin_dataset, body="発注数を確認")
    _create_memo(admin_dataset, body="割引率%を確認")
    _create_memo(admin_dataset, body="商品_A")
    _create_memo(admin_dataset, body="削除済み納品メモ", deleted=True)

    response = authenticated_client.get(path)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert expected in html
    assert excluded not in html


def test_search_ui_shows_clear_button_only_for_nonempty_query(
    authenticated_client,
    admin_dataset,
):
    empty_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )
    search_document = BeautifulSoup(
        authenticated_client.get(
            "/shop-tools/memo?q=%E7%B4%8D%E5%93%81"
        ).get_data(as_text=True),
        "html.parser",
    )

    empty_clear = empty_document.select_one("#memo-search-clear")
    search_clear = search_document.select_one("#memo-search-clear")
    assert empty_clear is not None
    assert empty_clear.has_attr("hidden")
    assert search_clear is not None
    assert not search_clear.has_attr("hidden")
    assert search_clear.get("type") == "button"
    assert search_clear.get("aria-label") == "検索文字をすべて消す"
    assert search_clear.get("data-clear-url") == "/shop-tools/memo"
    assert "「納品」の検索結果：0件" in search_document.get_text()


def test_memo_empty_states_distinguish_context_and_show_next_action(
    authenticated_client,
    admin_dataset,
):
    expected_by_path = {
        "/shop-tools/memo": (
            "まだメモはありません。"
            "＋ボタンから最初のメモを追加できます。"
        ),
        "/shop-tools/memo?q=none": (
            "検索条件に一致するメモはありません。"
            "検索文字を変えるか、×ボタンで検索を解除してください。"
        ),
        "/shop-tools/memo/trash": (
            "ゴミ箱は空です。削除済みメモはここに表示されます。"
        ),
        "/shop-tools/memo/trash?q=none": (
            "検索条件に一致する削除済みメモはありません。"
            "検索文字を変えるか、×ボタンで検索を解除してください。"
        ),
    }

    for path, expected in expected_by_path.items():
        response = authenticated_client.get(path)
        document = BeautifulSoup(
            response.get_data(as_text=True), "html.parser"
        )
        assert response.status_code == 200
        assert expected in document.get_text()


def test_active_memo_list_reserves_space_below_fixed_fab(
    authenticated_client,
    admin_dataset,
):
    active_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )
    trash_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo/trash").get_data(
            as_text=True
        ),
        "html.parser",
    )
    css_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()

    assert active_document.select_one(
        ".shop-memo-list-section.shop-memo-list-section-has-fab"
    ) is not None
    assert trash_document.select_one(
        ".shop-memo-list-section-has-fab"
    ) is None
    assert ".shop-memo-list-section-has-fab" in css_source


def test_admin_and_guests_only_search_their_own_memos(
    flask_app,
    authenticated_client,
    admin_dataset,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    _create_memo(admin_dataset, body="COMMON ADMIN")
    _create_memo(guest_a, body="COMMON GUEST A")
    _create_memo(guest_b, body="COMMON GUEST B")
    clients = {
        "ADMIN": authenticated_client,
        "GUEST A": _guest_client(flask_app, guest_a),
        "GUEST B": _guest_client(flask_app, guest_b),
    }

    for expected, test_client in clients.items():
        g.pop("_login_user", None)
        html = test_client.get("/shop-tools/memo?q=COMMON").get_data(
            as_text=True
        )
        assert f"COMMON {expected}" in html
        for other in clients:
            if other != expected:
                assert f"COMMON {other}" not in html


@pytest.mark.parametrize("body", ["", "   ", "メ" * 2001])
def test_invalid_create_preserves_input_without_database_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
    body,
):
    before = _memo_snapshot()

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": body},
    )

    assert response.status_code == 400
    assert _memo_snapshot() == before
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert document.select_one('[name="body"]').text == body


def test_hundredth_memo_succeeds_and_limit_counts_trashed_memos(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    db.session.add_all(
        [
            ShopMemo(
                dataset=admin_dataset,
                title=f"既存メモ{index}",
                body=f"既存メモ{index}",
                deleted_at=NOW if index == 0 else None,
                created_at=NOW,
                updated_at=NOW,
            )
            for index in range(99)
        ]
    )
    db.session.commit()

    hundredth_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": "100件目"},
    )
    assert hundredth_response.status_code == 303
    assert ShopMemo.query.count() == 100

    rejected_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": "101件目"},
    )

    assert rejected_response.status_code == 400
    assert "メモはゴミ箱を含めて100件まで登録できます。" in (
        rejected_response.get_data(as_text=True)
    )
    assert ShopMemo.query.count() == 100


def test_permanent_delete_frees_one_memo_slot(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memos = [
        ShopMemo(
            dataset=admin_dataset,
            title=f"上限確認メモ{index}",
            body=f"上限確認メモ{index}",
            deleted_at=NOW if index == 0 else None,
            created_at=NOW,
            updated_at=NOW,
        )
        for index in range(100)
    ]
    db.session.add_all(memos)
    db.session.commit()

    rejected_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": "上限で拒否"},
    )
    assert rejected_response.status_code == 400

    delete_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memos[0].id}/delete",
    )
    assert delete_response.status_code == 303
    assert ShopMemo.query.count() == 99

    accepted_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": "完全削除後の100件目"},
    )
    assert accepted_response.status_code == 303
    assert ShopMemo.query.count() == 100


def test_search_rejects_more_than_one_hundred_characters_without_db_changes(
    authenticated_client,
    admin_dataset,
):
    _create_memo(admin_dataset, body="保持されるメモ")
    before = _memo_snapshot()

    response = authenticated_client.get(f"/shop-tools/memo?q={'検' * 101}")

    assert response.status_code == 400
    assert "検索文字は100文字以内で入力してください。" in (
        response.get_data(as_text=True)
    )
    assert _memo_snapshot() == before


def test_blank_search_displays_all_active_memos(
    authenticated_client,
    admin_dataset,
):
    _create_memo(admin_dataset, body="表示対象A")
    _create_memo(admin_dataset, body="表示対象B")

    response = authenticated_client.get("/shop-tools/memo?q=%20%20%20")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "表示対象A" in html
    assert "表示対象B" in html
    assert "検索結果" not in html


@pytest.mark.parametrize("operation", ["edit", "trash", "restore", "delete"])
def test_guest_cannot_mutate_admin_or_other_guest_memo_and_gets_same_404(
    flask_app,
    admin_dataset,
    csrf_token,
    operation,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    targets = [
        _create_memo(
            admin_dataset,
            body="Admin保護メモ",
            deleted=operation in {"restore", "delete"},
        ),
        _create_memo(
            guest_b,
            body="Guest B保護メモ",
            deleted=operation in {"restore", "delete"},
        ),
    ]
    guest_client = _guest_client(flask_app, guest_a)
    before = _memo_snapshot()
    payload = {"body": "越境編集"} if operation == "edit" else {}

    for target in targets:
        target_response = _post_with_csrf(
            guest_client,
            csrf_token,
            f"/shop-tools/memo/{target.id}/{operation}",
            payload,
        )
        assert target_response.status_code == 404

    missing_response = _post_with_csrf(
        guest_client,
        csrf_token,
        f"/shop-tools/memo/{targets[-1].id + 9999}/{operation}",
        payload,
    )

    assert missing_response.status_code == 404
    assert _memo_snapshot() == before


def test_external_dataset_id_is_ignored_when_creating_memo(
    flask_app,
    admin_dataset,
    csrf_token,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    guest_client = _guest_client(flask_app, guest_a)

    response = _post_with_csrf(
        guest_client,
        csrf_token,
        "/shop-tools/memo",
        {
            "body": "Guest Aメモ",
            "dataset_id": str(guest_b.id),
            "admin_dataset_id": str(admin_dataset.id),
        },
    )

    assert response.status_code == 303
    assert ShopMemo.query.one().dataset_id == guest_a.id


@pytest.mark.parametrize("operation", ["autosave", "pin", "duplicate"])
def test_guest_cannot_use_mobile_mutations_on_other_dataset_memo(
    flask_app,
    admin_dataset,
    csrf_token,
    operation,
):
    guest_a = _create_guest_dataset(minute_offset=1)
    guest_b = _create_guest_dataset(minute_offset=2)
    protected = _create_memo(guest_b, body="Guest B保護メモ")
    guest_client = _guest_client(flask_app, guest_a)
    before = _memo_snapshot()
    payload = {
        "autosave": {"body": "越境編集"},
        "pin": {"pinned": True},
        "duplicate": {},
    }[operation]

    response = _post_json_with_csrf(
        guest_client,
        csrf_token,
        f"/shop-tools/memo/{protected.id}/{operation}",
        payload,
    )
    missing = _post_json_with_csrf(
        guest_client,
        csrf_token,
        f"/shop-tools/memo/{protected.id + 9999}/{operation}",
        payload,
    )

    assert response.status_code == 404
    assert missing.status_code == 404
    assert _memo_snapshot() == before


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/shop-tools/memo/autosave", {"body": "CSRF不正新規"}),
        ("/shop-tools/memo/1/autosave", {"body": "CSRF不正編集"}),
        ("/shop-tools/memo/1/pin", {"pinned": True}),
        ("/shop-tools/memo/1/duplicate", {}),
    ],
)
def test_mobile_mutations_require_valid_csrf_without_database_changes(
    authenticated_client,
    admin_dataset,
    csrf_token,
    path,
    payload,
):
    if "/1/" in path:
        _create_memo(admin_dataset, body="CSRF保護メモ")
    valid_token = csrf_token(authenticated_client, "/shop-tools/memo")
    before = _memo_snapshot()

    missing = authenticated_client.post(path, json=payload)
    tampered = authenticated_client.post(
        path,
        json=payload,
        headers={"X-CSRFToken": _tamper_csrf_token(valid_token)},
    )

    assert missing.status_code == 400
    assert tampered.status_code == 400
    assert _memo_snapshot() == before


def test_mobile_autosave_counts_trashed_memos_toward_limit(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    db.session.add_all(
        [
            ShopMemo(
                dataset=admin_dataset,
                title=f"上限メモ{index}",
                body=f"上限メモ{index}",
                created_at=NOW,
                updated_at=NOW,
                deleted_at=NOW if index == 0 else None,
            )
            for index in range(100)
        ]
    )
    db.session.commit()

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo/autosave",
        {"body": "101件目"},
    )

    assert response.status_code == 400
    assert ShopMemo.query.count() == 100


@pytest.mark.parametrize(
    ("operation", "deleted"),
    [
        ("edit", False),
        ("trash", False),
        ("restore", True),
        ("delete", True),
    ],
)
def test_memo_writes_reject_missing_and_tampered_csrf_without_changes(
    authenticated_client,
    admin_dataset,
    csrf_token,
    operation,
    deleted,
):
    memo = _create_memo(admin_dataset, body="CSRF保護メモ", deleted=deleted)
    path = f"/shop-tools/memo/{memo.id}/{operation}"
    payload = {"body": "不正編集"} if operation == "edit" else {}
    valid_token = csrf_token(authenticated_client, "/shop-tools/memo")
    before = _memo_snapshot()

    missing_response = authenticated_client.post(path, data=payload)
    tampered_response = authenticated_client.post(
        path,
        data={**payload, "csrf_token": _tamper_csrf_token(valid_token)},
    )

    assert missing_response.status_code == 400
    assert tampered_response.status_code == 400
    assert _memo_snapshot() == before


def test_create_rejects_missing_and_tampered_csrf_without_changes(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    valid_token = csrf_token(authenticated_client, "/shop-tools/memo")

    missing_response = authenticated_client.post(
        "/shop-tools/memo",
        data={"body": "不正追加A"},
    )
    tampered_response = authenticated_client.post(
        "/shop-tools/memo",
        data={
            "body": "不正追加B",
            "csrf_token": _tamper_csrf_token(valid_token),
        },
    )

    assert missing_response.status_code == 400
    assert tampered_response.status_code == 400
    assert ShopMemo.query.count() == 0


@pytest.mark.parametrize("operation", ["edit", "trash", "restore", "delete"])
def test_memo_mutation_routes_do_not_accept_get(
    authenticated_client,
    admin_dataset,
    operation,
):
    memo = _create_memo(
        admin_dataset,
        body="GETでは変更不可",
        deleted=operation in {"restore", "delete"},
    )
    before = _memo_snapshot()

    response = authenticated_client.get(
        f"/shop-tools/memo/{memo.id}/{operation}"
    )

    assert response.status_code == 405
    assert _memo_snapshot() == before


@pytest.mark.parametrize(
    ("operation", "deleted"),
    [
        ("edit", True),
        ("trash", True),
        ("restore", False),
        ("delete", False),
    ],
)
def test_memo_mutations_reject_wrong_lifecycle_state(
    authenticated_client,
    admin_dataset,
    csrf_token,
    operation,
    deleted,
):
    memo = _create_memo(admin_dataset, body="状態保護メモ", deleted=deleted)
    before = _memo_snapshot()
    payload = {"body": "変更後"} if operation == "edit" else {}

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo.id}/{operation}",
        payload,
    )

    assert response.status_code == 404
    assert _memo_snapshot() == before


def test_invalid_edit_preserves_input_without_database_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    memo = _create_memo(admin_dataset, body="元の本文")
    before = _memo_snapshot()

    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo.id}/edit",
        {"title": "入力中のタイトル", "body": "   "},
    )

    assert response.status_code == 400
    assert _memo_snapshot() == before
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    edit_dialog = document.select_one(
        'dialog.shop-memo-edit-dialog[data-open-on-load="true"]'
    )
    assert edit_dialog is not None
    assert edit_dialog.select_one('input[name="title"]')["value"] == (
        "入力中のタイトル"
    )
    assert edit_dialog.select_one('textarea[name="body"]').text == "   "


@pytest.mark.parametrize(
    ("operation", "deleted", "expected_message"),
    [
        ("edit", False, "メモを更新できませんでした。"),
        ("trash", False, "メモをゴミ箱へ移動できませんでした。"),
        ("restore", True, "メモを復元できませんでした。"),
        ("delete", True, "メモを完全に削除できませんでした。"),
    ],
)
def test_mutation_commit_failure_rolls_back_memo_change(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
    operation,
    deleted,
    expected_message,
):
    memo = _create_memo(admin_dataset, body="rollback対象", deleted=deleted)
    token = csrf_token(authenticated_client, "/shop-tools/memo")
    before = _memo_snapshot()
    real_rollback = db.session.rollback
    rollback = Mock(wraps=real_rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test memo mutation failure after flush")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    payload = {
        "csrf_token": token,
        **(
            {"title": "更新タイトル", "body": "保存されない変更"}
            if operation == "edit"
            else {}
        ),
    }

    response = authenticated_client.post(
        f"/shop-tools/memo/{memo.id}/{operation}",
        data=payload,
    )

    assert response.status_code == 500
    assert rollback.call_count == 1
    assert expected_message in response.get_data(as_text=True)
    assert _memo_snapshot() == before


def test_create_commit_failure_rolls_back_new_memo(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
):
    token = csrf_token(authenticated_client, "/shop-tools/memo")
    real_rollback = db.session.rollback
    rollback = Mock(wraps=real_rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test memo create failure after flush")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    response = authenticated_client.post(
        "/shop-tools/memo",
        data={
            "title": "保存されないタイトル",
            "body": "保存されないメモ",
            "csrf_token": token,
        },
    )

    assert response.status_code == 500
    assert rollback.call_count == 1
    assert "メモを追加できませんでした。" in (
        response.get_data(as_text=True)
    )
    assert ShopMemo.query.count() == 0


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_mobile_autosave_commit_failure_rolls_back(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
    operation,
):
    memo = (
        _create_memo(admin_dataset, body="autosave rollback元")
        if operation == "edit"
        else None
    )
    before = _memo_snapshot()
    real_rollback = db.session.rollback
    rollback = Mock(wraps=real_rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test mobile autosave failure after flush")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    path = (
        f"/shop-tools/memo/{memo.id}/autosave"
        if memo is not None
        else "/shop-tools/memo/autosave"
    )

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        path,
        {"body": "保存されないautosave"},
    )

    assert response.status_code == 500
    assert response.json["ok"] is False
    assert rollback.call_count == 1
    assert _memo_snapshot() == before


@pytest.mark.parametrize("operation", ["pin", "duplicate"])
def test_mobile_action_commit_failure_rolls_back(
    authenticated_client,
    admin_dataset,
    csrf_token,
    monkeypatch,
    operation,
):
    memo = _create_memo(admin_dataset, body="スマホ操作rollback元")
    before = _memo_snapshot()
    real_rollback = db.session.rollback
    rollback = Mock(wraps=real_rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)

    def fail_after_flush():
        db.session.flush()
        raise SQLAlchemyError("test mobile action failure after flush")

    monkeypatch.setattr(db.session, "commit", fail_after_flush)
    payload = {"pinned": True} if operation == "pin" else {}

    response = _post_json_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo.id}/{operation}",
        payload,
    )

    assert response.status_code == 500
    assert response.json["ok"] is False
    assert rollback.call_count == 1
    assert _memo_snapshot() == before


def test_anonymous_user_cannot_view_or_create_shop_memos(
    client,
    admin_dataset,
    csrf_token,
):
    get_response = client.get("/shop-tools/memo", follow_redirects=False)
    login_token = csrf_token(client, "/login")
    post_response = client.post(
        "/shop-tools/memo",
        data={"body": "匿名メモ", "csrf_token": login_token},
        follow_redirects=False,
    )

    assert get_response.status_code == 302
    assert post_response.status_code == 302
    assert ShopMemo.query.count() == 0


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/shop-tools/memo/autosave", {"body": "匿名autosave"}),
        ("/shop-tools/memo/1/autosave", {"body": "匿名編集"}),
        ("/shop-tools/memo/1/pin", {"pinned": True}),
        ("/shop-tools/memo/1/duplicate", {}),
    ],
)
def test_anonymous_user_cannot_use_mobile_memo_mutations(
    client,
    admin_dataset,
    csrf_token,
    path,
    payload,
):
    _create_memo(admin_dataset, body="匿名操作から保護")
    token = csrf_token(client, "/login")
    before = _memo_snapshot()

    response = client.post(
        path,
        json=payload,
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert _memo_snapshot() == before


@pytest.mark.parametrize(
    "path",
    [
        "/shop-tools/memo/autosave",
        "/shop-tools/memo/1/autosave",
        "/shop-tools/memo/1/pin",
        "/shop-tools/memo/1/duplicate",
    ],
)
def test_mobile_memo_mutations_do_not_accept_get(
    authenticated_client,
    admin_dataset,
    path,
):
    _create_memo(admin_dataset, body="GETではスマホ操作不可")
    before = _memo_snapshot()

    response = authenticated_client.get(path)

    assert response.status_code == 405
    assert _memo_snapshot() == before


def test_shop_memo_input_is_html_escaped_and_sources_avoid_inner_html(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    attack = "<script>alert(1)</script>"
    assert _post_with_csrf(
        authenticated_client,
        csrf_token,
        "/shop-tools/memo",
        {"body": attack},
    ).status_code == 303

    html = authenticated_client.get("/shop-tools/memo").get_data(as_text=True)
    assert attack not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    project_root = Path(app_module.app.root_path)
    for path in [
        project_root / "templates" / "shop_tools" / "memos.html",
        project_root / "templates" / "shop_tools" / "memo_trash.html",
        project_root / "static" / "shop_memos.js",
    ]:
        source = path.read_text()
        assert "|safe" not in source
        assert "innerHTML" not in source


def test_shop_memo_list_keeps_http_urls_as_plain_text(
    authenticated_client,
    admin_dataset,
):
    body = (
        "発注先はこちら https://example.com/order です\n"
        "予備 http://example.net/backup"
    )
    _create_memo(admin_dataset, body=body)

    response = authenticated_client.get("/shop-tools/memo?q=order")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    body_element = document.select_one(".shop-memo-body")

    assert response.status_code == 200
    assert body_element.get_text() == body
    assert body_element.select("a") == []


def test_shop_memo_trash_linkifies_url_but_restored_list_is_plain_text(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    body = "確認 https://example.com/shared"
    active_memo = _create_memo(admin_dataset, body=body)
    deleted_memo = _create_memo(admin_dataset, body=body, deleted=True)

    active_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )
    trash_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo/trash").get_data(
            as_text=True
        ),
        "html.parser",
    )

    active_link = active_document.select_one(
        f'article[data-memo-id="{active_memo.id}"] .shop-memo-body a'
    )
    trash_link = trash_document.select_one(
        f'article:has(form[action$="/{deleted_memo.id}/restore"]) '
        ".shop-memo-body a"
    )
    assert active_link is None
    assert trash_link is not None
    assert trash_link.get("href") == "https://example.com/shared"
    assert trash_link.get("target") == "_blank"
    assert set(trash_link.get("rel", [])) == {"noopener", "noreferrer"}

    restore_response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{deleted_memo.id}/restore",
    )
    restored_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )

    assert restore_response.status_code == 303
    restored_bodies = restored_document.select(
        ".shop-memo-body"
    )
    assert len(restored_bodies) == 2
    assert all(body_element.select("a") == [] for body_element in restored_bodies)
    assert all(
        body_element.get_text() == body
        for body_element in restored_bodies
    )


def test_shop_memo_linkify_escapes_html_and_rejects_unsafe_schemes(
    authenticated_client,
    admin_dataset,
):
    body = (
        "<script>alert(1)</script>\n"
        "<img src=x onerror=alert(2)>\n"
        "<b>危険</b> https://example.com/safe\n"
        "javascript:alert(3) data:text/html,<svg/onload=alert(4)>\n"
        "file:///tmp/memo mailto:test@example.com www.example.com https://"
    )
    _create_memo(admin_dataset, body=body, deleted=True)

    response = authenticated_client.get("/shop-tools/memo/trash")
    html = response.get_data(as_text=True)
    document = BeautifulSoup(html, "html.parser")
    body_element = document.select_one(".shop-memo-body")
    links = body_element.select("a")

    assert body_element.get_text() == body
    assert body_element.select("script, img, b, svg") == []
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(2)&gt;" in html
    assert "&lt;b&gt;危険&lt;/b&gt;" in html
    assert [link.get("href") for link in links] == [
        "https://example.com/safe"
    ]
    assert "javascript:" not in [link.get("href") for link in links]
    assert "data:" not in [link.get("href") for link in links]


def test_shop_memo_linkify_handles_punctuation_and_attribute_injection(
    authenticated_client,
    admin_dataset,
):
    body = (
        '(https://example.com/inside), '
        "https://example.com/japanese。 "
        'https://example.com/\" onclick=\"alert(1)'
    )
    _create_memo(admin_dataset, body=body, deleted=True)

    response = authenticated_client.get("/shop-tools/memo/trash")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    body_element = document.select_one(".shop-memo-body")
    links = body_element.select("a")

    assert body_element.get_text() == body
    assert [link.get("href") for link in links] == [
        "https://example.com/inside",
        "https://example.com/japanese",
        "https://example.com/",
    ]
    assert all(set(link.attrs) == {"href", "target", "rel"} for link in links)
    assert document.select("[onclick]") == []


def test_shop_memo_edit_keeps_raw_url_text_in_textarea_and_database(
    authenticated_client,
    admin_dataset,
    csrf_token,
):
    original_body = "編集前 https://example.com/original"
    memo = _create_memo(admin_dataset, body=original_body)

    document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )
    textarea = document.select_one(f"#shop-memo-edit-{memo.id}")

    assert textarea.get_text() == original_body
    assert textarea.select("a") == []

    edited_body = "編集後 http://example.net/raw"
    response = _post_with_csrf(
        authenticated_client,
        csrf_token,
        f"/shop-tools/memo/{memo.id}/edit",
        {"body": edited_body},
    )

    assert response.status_code == 303
    db.session.expire_all()
    assert db.session.get(ShopMemo, memo.id).body == edited_body


def test_memo_ui_has_accessible_controls_and_irreversible_delete_confirmation(
    authenticated_client,
    admin_dataset,
):
    active_memo = _create_memo(admin_dataset, body="通常メモ")
    deleted_memo = _create_memo(admin_dataset, body="削除済み", deleted=True)

    active_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )
    trash_document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo/trash").get_data(
            as_text=True
        ),
        "html.parser",
    )

    assert active_document.select_one(
        f'form[action="/shop-tools/memo/{active_memo.id}/edit"] '
        'input[name="csrf_token"]'
    ) is not None
    memo_card = active_document.select_one(
        f'article[data-memo-id="{active_memo.id}"]'
    )
    row_open_button = memo_card.select_one(
        ":scope > .shop-memo-card-open"
    )
    assert row_open_button is not None
    assert row_open_button.get("type") == "button"
    assert row_open_button.get("aria-controls") == (
        f"shop-memo-edit-dialog-{active_memo.id}"
    )
    assert row_open_button.get("data-dialog-id") == (
        f"shop-memo-edit-dialog-{active_memo.id}"
    )
    assert row_open_button.select_one(".shop-memo-title") is None
    menu_button = active_document.select_one(
        f'article[data-memo-id="{active_memo.id}"] '
        f'.shop-memo-menu-button'
        f'[data-trash-url="/shop-tools/memo/{active_memo.id}/trash"]'
    )
    assert menu_button is not None
    assert menu_button.get("aria-haspopup") == "menu"
    assert menu_button.get("aria-expanded") == "false"
    assert menu_button.get("data-dialog-id") == (
        f"shop-memo-edit-dialog-{active_memo.id}"
    )
    assert active_document.select_one(
        '#shop-memo-menu-trash-form input[name="csrf_token"]'
    ) is not None
    assert trash_document.select_one(
        f'form[action="/shop-tools/memo/{deleted_memo.id}/restore"]'
    ) is not None
    permanent_delete = trash_document.select_one(
        f'form.shop-memo-delete-form'
        f'[action="/shop-tools/memo/{deleted_memo.id}/delete"]'
    )
    assert permanent_delete is not None
    assert "この操作は取り消せません" in permanent_delete.parent.get_text()

    empty_trash_button = trash_document.select_one(
        "#shop-memo-empty-trash-open"
    )
    empty_trash_dialog = trash_document.select_one(
        "#shop-memo-empty-trash-dialog"
    )
    assert empty_trash_button is not None
    assert empty_trash_button.get("aria-controls") == (
        "shop-memo-empty-trash-dialog"
    )
    assert empty_trash_dialog is not None
    assert "ゴミ箱内の1件を完全に削除します。" in (
        empty_trash_dialog.get_text()
    )
    empty_trash_form = empty_trash_dialog.select_one(
        'form[method="POST"][action="/shop-tools/memo/trash/empty"]'
    )
    assert empty_trash_form is not None
    assert empty_trash_form.select_one('input[name="csrf_token"]') is not None

    script_source = (
        Path(app_module.app.root_path) / "static" / "shop_memos.js"
    ).read_text()
    assert 'memoSearchInput.addEventListener("input"' in script_source
    assert 'memoSearchClearButton.addEventListener("click"' in script_source
    assert 'memoSearchInput.value = ""' in script_source
    assert "window.location.replace" in script_source


def test_empty_trash_control_is_hidden_when_trash_is_empty(
    authenticated_client,
    admin_dataset,
):
    document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo/trash").get_data(
            as_text=True
        ),
        "html.parser",
    )

    assert document.select_one("#shop-memo-empty-trash-open") is None
    assert document.select_one("#shop-memo-empty-trash-dialog") is None


def test_mobile_memo_ui_exposes_autosave_gesture_and_action_controls(
    authenticated_client,
    admin_dataset,
):
    memo = _create_memo(admin_dataset, body="スマホ操作対象")
    document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )

    config = document.select_one("#shop-memo-mobile-config")
    assert config is not None
    assert config.get("data-create-url") == "/shop-tools/memo/autosave"
    assert config.get("data-list-url") == "/shop-tools/memo"
    assert config.get("data-csrf-token")

    card = document.select_one(f'article[data-memo-id="{memo.id}"]')
    assert card.get("data-autosave-url") == (
        f"/shop-tools/memo/{memo.id}/autosave"
    )
    assert card.get("data-pin-url") == f"/shop-tools/memo/{memo.id}/pin"
    assert card.get("data-duplicate-url") is None
    assert card.get("data-trash-url") == (
        f"/shop-tools/memo/{memo.id}/trash"
    )
    assert card.get("data-pinned") == "false"
    assert card.select_one(".shop-memo-menu-button") is not None
    swipe_row = card.find_parent(class_="shop-memo-swipe-row")
    swipe_actions = swipe_row.select(".shop-memo-swipe-action")
    assert len(swipe_actions) == 1
    swipe_action = swipe_actions[0]
    assert swipe_action.get("data-swipe-action-layer") == "true"
    assert swipe_action.select_one('svg[data-swipe-icon="pin"]') is not None
    assert swipe_action.select_one('svg[data-swipe-icon="trash"]') is not None
    assert "📌" not in swipe_action.get_text()
    assert "🗑️" not in swipe_action.get_text()

    edit_dialog = document.select_one(f"#shop-memo-edit-dialog-{memo.id}")
    edit_mobile_header = edit_dialog.select_one(
        ".shop-memo-mobile-dialog-header"
    )
    assert edit_mobile_header is not None
    assert edit_mobile_header.get("data-scroll-state") == "no-scroll"
    edit_back = edit_mobile_header.select_one(".shop-memo-mobile-editor-back")
    assert edit_back is not None
    assert edit_back.get("aria-label") == "メモ一覧に戻る"
    assert edit_back.get_text(strip=True) == "←"
    assert edit_mobile_header.select_one(".shop-memo-dialog-close") is None
    edit_desktop_header = edit_dialog.select_one(
        ".shop-memo-desktop-dialog-header"
    )
    assert edit_desktop_header is not None
    edit_close = edit_desktop_header.select_one(".shop-memo-dialog-close")
    assert edit_close is not None
    assert edit_close.get_text(strip=True) == "×"
    edit_toolbar = edit_dialog.select_one(
        ".shop-memo-mobile-editor-toolbar[data-scroll-state]"
    )
    assert edit_toolbar is not None
    assert edit_toolbar.get("data-scroll-state") == "no-scroll"
    assert edit_toolbar.get("aria-hidden") == "true"
    assert edit_toolbar.get_text(strip=True) == ""
    assert edit_toolbar.select_one("button") is None
    assert edit_dialog.select_one('textarea[name="body"]') is not None
    assert edit_dialog.get("data-editor-mode") == "edit"
    assert edit_dialog.get("data-duplicate-url") is None

    create_dialog = document.select_one("#shop-memo-create-dialog")
    assert create_dialog.get("data-autosave-url") == (
        "/shop-tools/memo/autosave"
    )
    assert create_dialog.get("data-editor-mode") == "create"
    create_mobile_header = create_dialog.select_one(
        ".shop-memo-mobile-dialog-header"
    )
    assert create_mobile_header is not None
    assert create_mobile_header.get("data-scroll-state") == "no-scroll"
    create_back = create_mobile_header.select_one(
        ".shop-memo-mobile-editor-back"
    )
    assert create_back is not None
    assert create_back.get("aria-label") == "メモ一覧に戻る"
    assert create_back.get_text(strip=True) == "←"
    assert create_mobile_header.select_one(".shop-memo-create-close") is None
    create_desktop_header = create_dialog.select_one(
        ".shop-memo-desktop-dialog-header"
    )
    assert create_desktop_header is not None
    create_close = create_desktop_header.select_one(".shop-memo-create-close")
    assert create_close is not None
    assert create_close.get_text(strip=True) == "×"
    create_toolbar = create_dialog.select_one(
        ".shop-memo-mobile-editor-toolbar[data-scroll-state]"
    )
    assert create_toolbar is not None
    assert create_toolbar.get("data-scroll-state") == "no-scroll"
    assert not create_toolbar.has_attr("hidden")
    assert create_toolbar.get("aria-hidden") == "true"
    assert create_toolbar.get_text(strip=True) == ""
    assert create_toolbar.select_one("button") is None

    action_sheet = document.select_one("#shop-memo-mobile-actions")
    assert action_sheet is not None
    assert action_sheet.select_one(
        ".shop-memo-mobile-actions-handle[aria-hidden='true']"
    ) is not None
    assert action_sheet.select_one(
        ".shop-memo-mobile-actions-preview"
    ) is not None
    pin_action = action_sheet.select_one('[data-mobile-action="pin"]')
    assert pin_action is not None
    assert pin_action.select_one('svg[data-mobile-action-icon="pin"]')
    assert pin_action.select_one(
        '[data-mobile-action-label="pin"]'
    ).get_text(strip=True) == "ピン留め"
    copy_action = action_sheet.select_one('[data-mobile-action="copy"]')
    assert copy_action is not None
    assert copy_action.get("type") == "button"
    assert copy_action.select_one('svg[data-mobile-action-icon="copy"]')
    assert copy_action.get_text(" ", strip=True) == "コピー"
    assert action_sheet.select_one(
        '[data-mobile-action="duplicate"]'
    ) is None
    trash_action = action_sheet.select_one('[data-mobile-action="trash"]')
    assert trash_action is not None
    assert trash_action.select_one('svg[data-mobile-action-icon="trash"]')
    assert trash_action.get_text(" ", strip=True) == "削除"
    action_list = action_sheet.select_one(".shop-memo-mobile-actions-list")
    assert "📌" not in action_list.get_text()
    assert "📋" not in action_list.get_text()
    assert "🗑️" not in action_list.get_text()
    cancel_action = action_sheet.select_one(".shop-memo-mobile-actions-close")
    assert cancel_action.get_text(strip=True) == "キャンセル"

    snackbar = document.select_one("#shop-memo-snackbar")
    assert snackbar is not None
    assert snackbar.select_one("#shop-memo-snackbar-undo") is not None

    script_source = (
        Path(app_module.app.root_path) / "static" / "shop_memos.js"
    ).read_text()
    assert "LONG_PRESS_MS: 500" in script_source
    assert "MOVE_CANCEL_PX: 10" in script_source
    assert "DELETE_DISTANCE_RATIO: 0.7" in script_source
    assert "PIN_DISTANCE_RATIO: 0.45" in script_source
    assert "FAST_SWIPE_MIN_RATIO: 0.25" in script_source
    assert "FAST_SWIPE_VELOCITY: 0.65" in script_source
    assert "AUTOSAVE_DELAY_MS: 750" in script_source
    assert "SCROLL_END_TOLERANCE_PX:" in script_source
    assert "scrollHeight" in script_source
    assert "clientHeight" in script_source
    assert "scrollTop" in script_source
    assert '"no-scroll"' in script_source
    assert '"at-top"' in script_source
    assert '"middle"' in script_source
    assert '"at-bottom"' in script_source
    assert "header.dataset.scrollState = scrollState" in script_source
    assert "resizeMobileEditorTextarea" in script_source
    assert 'dialog.dataset.editorMode === "create"' in script_source
    assert "--shop-memo-swipe-progress" in script_source
    assert "row.dataset.swipeDirection" in script_source
    assert "navigator.clipboard.writeText" in script_source
    assert 'document.execCommand("copy")' in script_source
    assert "mobilePinActionLabel.textContent" in script_source
    assert "mobilePinAction.textContent" not in script_source
    assert '"コピーしました"' in script_source
    assert "postMemoJson(context.duplicateUrl)" not in script_source
    assert "innerHTML" not in script_source

    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()
    assert '.shop-memo-mobile-dialog-header[data-scroll-state="middle"]' in (
        style_source
    )
    assert '.shop-memo-mobile-editor-toolbar[data-scroll-state="at-top"]' in (
        style_source
    )
    assert "overflow-y: auto" in style_source
    assert ".shop-memo-mobile-editor-back:focus-visible" in style_source
    assert "-webkit-appearance: none" in style_source
    assert "appearance: none" in style_source

    mobile_back_rule = style_source.split(
        ".shop-memo-mobile-editor-back {",
        1,
    )[1].split("}", 1)[0]
    assert "width: 48px;" in mobile_back_rule
    assert "min-width: 48px;" in mobile_back_rule
    assert "height: 48px;" in mobile_back_rule
    assert "min-height: 48px;" in mobile_back_rule
    assert "flex: 0 0 48px;" in mobile_back_rule
    assert "border-radius: 50%;" in mobile_back_rule
    assert "-webkit-tap-highlight-color: transparent;" in mobile_back_rule
    assert "touch-action: manipulation;" in mobile_back_rule
    assert "background: transparent;" in mobile_back_rule
    assert "52px" not in mobile_back_rule

    mobile_back_circle_rule = style_source.split(
        '.shop-memo-mobile-editor-back span[aria-hidden="true"] {',
        1,
    )[1].split("}", 1)[0]
    assert "display: inline-flex;" in mobile_back_circle_rule
    assert "width: 45px;" in mobile_back_circle_rule
    assert "height: 45px;" in mobile_back_circle_rule
    assert "background: #edf1ee;" in mobile_back_circle_rule
    assert "border-radius: 50%;" in mobile_back_circle_rule
    assert (
        '.shop-memo-mobile-editor-back:active '
        'span[aria-hidden="true"]'
    ) in style_source
    assert (
        '.shop-memo-mobile-editor-back:focus-visible '
        'span[aria-hidden="true"]'
    ) in style_source


def test_memo_list_keeps_desktop_tool_header_and_svg_trash_link(
    authenticated_client,
    admin_dataset,
):
    for index in range(4):
        _create_memo(admin_dataset, body=f"通常メモ{index}")
    _create_memo(admin_dataset, body="ゴミ箱メモ", deleted=True)

    before = _memo_snapshot()
    document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )

    tool_header = document.select_one(".shop-tools-header")
    assert tool_header is not None
    assert "shop-tools-header-mobile-hidden" in tool_header.get("class", [])
    assert tool_header.select_one(".shop-tools-brand").get_text(
        " ", strip=True
    ) == "店舗メモツール"

    tool_heading = tool_header.select_one("h1")
    assert tool_heading is not None
    assert tool_heading.get_text(" ", strip=True) == "メモ"
    assert tool_heading.select_one('svg[data-icon="notebook-pen"]') is not None

    heading_row = document.select_one(".shop-memo-list-heading-row")
    controls = heading_row.select_one(".shop-memo-list-controls")
    assert controls is not None
    direct_controls = controls.find_all(recursive=False)
    assert [element.get("class", [None])[0] for element in direct_controls] == [
        "shop-memo-sort-link",
        "shop-memo-trash-link",
    ]

    trash_link = controls.select_one(".shop-memo-trash-link")
    assert trash_link is not None
    assert trash_link.get("href") == "/shop-tools/memo/trash"
    assert trash_link.get("aria-label") == "ゴミ箱を開く（1件）"
    assert trash_link.get_text(strip=True) == "ゴミ箱"
    trash_icon = trash_link.select_one('svg[data-icon="trash-2"]')
    assert trash_icon is not None
    assert trash_icon.get("aria-hidden") == "true"
    assert document.select_one(".shop-memo-count-desktop").get_text(
        strip=True
    ) == "4件のメモ"
    assert document.select_one(".shop-memo-count-mobile").get_text(
        strip=True
    ) == "(4件)"
    assert _memo_snapshot() == before


def test_mobile_css_disables_webkit_tap_highlight_globally():
    style_source = (
        Path(app_module.app.root_path) / "static" / "style.css"
    ).read_text()

    assert (
        "@media (max-width: 900px) {\n"
        "    body,\n"
        "    body * {\n"
        "        -webkit-tap-highlight-color: transparent;\n"
        "    }"
    ) in style_source
    assert ":hover" in style_source
    assert ":active" in style_source
    assert ":focus-visible" in style_source


def test_memo_search_and_shared_navigation_use_accessible_svg_icons(
    authenticated_client,
    admin_dataset,
):
    document = BeautifulSoup(
        authenticated_client.get("/shop-tools/memo").get_data(as_text=True),
        "html.parser",
    )

    search_button = document.select_one(".shop-memo-search-icon-button")
    assert search_button.get("aria-label") == "検索"
    assert search_button.get_text(strip=True) == ""
    search_icon = search_button.select_one('svg[data-icon="search"]')
    assert search_icon is not None
    assert search_icon.get("aria-hidden") == "true"

    expected_icons = {
        "orders": "shopping-cart",
        "memo": "notebook-pen",
        "tasks": "list-todo",
    }
    navigations = document.select(
        'nav[aria-label="店舗メモツールのメニュー"]'
    )
    assert len(navigations) == 2
    for navigation in navigations:
        for tool, icon_name in expected_icons.items():
            link = navigation.select_one(f'a[data-tool="{tool}"]')
            icon = link.select_one(f'svg[data-icon="{icon_name}"]')
            assert icon is not None
            assert icon.get("aria-hidden") == "true"
        navigation_text = navigation.get_text(" ", strip=True)
        assert "発注" in navigation_text
        assert "メモ" in navigation_text
        assert "タスク" in navigation_text
        assert "🛒" not in navigation_text
        assert "📝" not in navigation_text
        assert "✅" not in navigation_text
