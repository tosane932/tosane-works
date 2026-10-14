"""Login-required notices must remain useful across redirects and login."""

import datetime

from bs4 import BeautifulSoup
from flask import g

from conftest import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME
from models import Dataset, ShopStaff, db


LOGIN_WARNING = "このページを利用するにはログインが必要です。"


def page(response):
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def clear_user_cache():
    # The test fixture holds one app context across requests.
    g.pop("_login_user", None)


def test_repeated_unauthorized_requests_show_one_japanese_warning_on_login(client):
    for _ in range(2):
        clear_user_cache()
        response = client.get("/staff-shifts")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login?")
    with client.session_transaction() as session:
        assert session["_flashes"] == [("warning", LOGIN_WARNING)] * 2

    login = page(client.get("/login"))
    notices = login.select(".app-flash")
    assert len(notices) == 1
    assert notices[0].get_text(strip=True) == LOGIN_WARNING
    assert notices[0].get("role") == "alert"
    assert "app-flash--warning" in notices[0]["class"]
    with client.session_transaction() as session:
        assert not session.get("_flashes")


def test_old_english_warning_in_existing_session_is_translated(client):
    with client.session_transaction() as session:
        session["_flashes"] = [("message", "Please log in to access this page."),
                               ("warning", LOGIN_WARNING)]
    response = client.get("/login")
    notices = page(response).select(".app-flash")
    assert len(notices) == 1
    assert notices[0].get_text(strip=True) == LOGIN_WARNING
    assert "Please log in" not in response.get_data(as_text=True)


def test_successful_login_discards_only_old_login_warning(client, csrf_token, admin_dataset):
    token = csrf_token(client, "/login")
    for _ in range(2):
        clear_user_cache()
        assert client.get("/staff-shifts").status_code == 302
    with client.session_transaction() as session:
        session["_flashes"].extend([
            ("error", "保存に失敗しました。"),
            ("success", "登録しました。"),
            ("warning", "別の注意です。"),
            ("success", LOGIN_WARNING),
            ("message", "Please log in to access this page."),
        ])
        session.modified = True

    response = client.post("/login", data={
        "csrf_token": token, "username": TEST_ADMIN_USERNAME,
        "password": TEST_ADMIN_PASSWORD,
    })
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["_user_id"] == "admin"
        assert ("warning", LOGIN_WARNING) not in session.get("_flashes", [])
        assert session["_flashes"] == [
            ("error", "保存に失敗しました。"), ("success", "登録しました。"),
            ("warning", "別の注意です。"), ("success", LOGIN_WARNING),
        ]
    clear_user_cache()
    staff_page = page(client.get("/staff-shifts"))
    notices = staff_page.select(".app-flash")
    assert [(n.get_text(strip=True), n.get("role")) for n in notices] == [
        ("保存に失敗しました。", "alert"), ("登録しました。", "status"),
        ("別の注意です。", "alert"), (LOGIN_WARNING, "status"),
    ]
    assert [next(cls for cls in n["class"] if cls.startswith("app-flash--")) for n in notices] == [
        "app-flash--error", "app-flash--success", "app-flash--warning", "app-flash--success",
    ]


def test_duplicate_same_category_is_collapsed_but_other_notices_and_xss_survive(authenticated_client, admin_dataset):
    with authenticated_client.session_transaction() as session:
        session["_flashes"] = [
            ("success", "更新しました。"), ("success", "更新しました。"),
            ("warning", "更新しました。"),
            ("error", '<script>alert("x")</script>'),
            ("other", "別の通知"),
        ]
        session.modified = True
    clear_user_cache()
    response = authenticated_client.get("/staff-shifts")
    html = response.get_data(as_text=True)
    notices = page(response).select(".app-flash")
    assert [n.get_text(strip=True) for n in notices] == [
        "更新しました。", "更新しました。", '<script>alert("x")</script>', "別の通知",
    ]
    assert "app-flash--neutral" in notices[-1]["class"]
    assert "&lt;script&gt;" in html and '<script>alert("x")</script>' not in html
    assert not page(authenticated_client.get("/staff-shifts")).select(".app-flash")


def test_detail_and_existing_success_notices(authenticated_client, admin_dataset, csrf_token):
    token = csrf_token(authenticated_client, "/staff-shifts/staff/new")
    get_new = page(authenticated_client.get("/staff-shifts/staff/new"))
    submission = get_new.select_one('input[name="submission_key"]')["value"]
    response = authenticated_client.post("/staff-shifts/staff/new", data={
        "csrf_token": token, "submission_key": submission,
        "display_name": "田中", "role": "production", "hired_on": "",
    })
    assert response.status_code == 303
    result = page(authenticated_client.get(response.headers["Location"]))
    notice = result.select_one(".app-flash--success")
    assert notice and "スタッフを登録しました。" in notice.get_text()
    person = ShopStaff.query.filter_by(dataset_id=admin_dataset.id).one()
    with authenticated_client.session_transaction() as session:
        session["_flashes"] = [("warning", "詳細の注意")]
    clear_user_cache()
    detail = page(authenticated_client.get(f"/staff-shifts/staff/{person.id}"))
    assert detail.select_one(".app-flash--warning").get_text(strip=True) == "詳細の注意"


def test_guest_and_missing_csrf_still_cannot_manage_staff(flask_app, client):
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(kind="guest", system_key=None, created_at=now, last_activity_at=now,
                      absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(dataset)
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{dataset.id}"
        session["_fresh"] = True
    clear_user_cache()
    assert client.get("/staff-shifts").status_code == 403
    clear_user_cache()
    assert client.post("/staff-shifts/staff/new", data={"display_name": "侵入"}).status_code == 400
    assert ShopStaff.query.count() == 0
