"""Admin-only planned staff on the existing store home."""
import datetime

from bs4 import BeautifulSoup
from flask import g
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

import app as app_module
from models import Dataset, ShopShift, ShopStaff, db
from shop_schedules import JST, UTC

DAY = datetime.date(2026, 10, 14)


def staff(dataset, name):
    row = ShopStaff(dataset_id=dataset.id, display_name=name, role="production")
    db.session.add(row)
    db.session.flush()
    return row


def shift(dataset, person, day=DAY, status="work", start=(6, 0), end=(14, 0), next_day=False):
    starts = ends = None
    if status == "work":
        starts = datetime.datetime.combine(day, datetime.time(*start), JST).astimezone(UTC)
        ends = datetime.datetime.combine(
            day + datetime.timedelta(days=1 if next_day else 0), datetime.time(*end), JST
        ).astimezone(UTC)
    row = ShopShift(dataset_id=dataset.id, staff_id=person.id, business_date=day,
                    status=status, starts_at=starts, ends_at=ends)
    db.session.add(row)
    return row


def guest_client(app):
    now = datetime.datetime.now(UTC)
    dataset = Dataset(kind="guest", system_key=None, created_at=now, last_activity_at=now,
                      absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(dataset)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{dataset.id}"
        session["_fresh"] = True
    return client, dataset


def home(client):
    g.pop("_login_user", None)
    response = client.get("/home")
    assert response.status_code == 200
    return BeautifulSoup(response.data, "html.parser"), response.get_data(as_text=True)


def test_admin_home_uses_only_today_work_and_all_day(authenticated_client, admin_dataset, flask_app, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: DAY)
    first = staff(admin_dataset, "<田中>")
    second = staff(admin_dataset, "佐藤")
    off = staff(admin_dataset, "休み担当")
    old = staff(admin_dataset, "前日担当")
    future = staff(admin_dataset, "翌日担当")
    shift(admin_dataset, first, start=(22, 0), end=(2, 0), next_day=True)
    shift(admin_dataset, second, status="all_day")
    shift(admin_dataset, off, status="off")
    shift(admin_dataset, old, day=DAY - datetime.timedelta(days=1))
    shift(admin_dataset, future, day=DAY + datetime.timedelta(days=1))
    guest, guest_dataset = guest_client(flask_app)
    outsider = staff(guest_dataset, "別Dataset")
    shift(guest_dataset, outsider)
    db.session.commit()

    page, html = home(authenticated_client)
    card = page.select_one("#today-staff")
    assert card is not None
    assert "今日の出勤予定" in card.get_text()
    assert "10月14日（水）" in card.get_text()
    assert "出勤予定 2人" in card.get_text()
    assert len(card.select("li")) == 2
    assert "&lt;田中&gt;" in html and "<田中>" not in html
    assert "22:00〜02:00" in card.get_text() and "翌日終了" in card.get_text()
    assert "終日（時刻未指定）" in card.get_text()
    assert all(name not in card.get_text() for name in ("休み担当", "前日担当", "翌日担当", "別Dataset"))
    assert card.select_one('a[href="/staff-shifts"]') is not None
    ids = [item.get("id") for item in page.select(".operations-grid > *")]
    assert ids.index("today-sales") < ids.index("today-staff") < ids.index("bakery-news")
    assert page.select_one(".operations-heading h1").get_text(strip=True) == "パン工房 ひだまり"
    assert page.select_one('.hidamari-banner img[alt="パン工房 ひだまり"]') is not None
    assert page.select_one("#weather") and page.select_one("#schedule")
    assert not page.select_one("#puoppo") and page.select_one("#today-sales")
    assert "勤務中" not in card.get_text() and "出勤済み" not in card.get_text()


def test_empty_states_and_get_does_not_commit(authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: DAY)
    page, _ = home(authenticated_client)
    card = page.select_one("#today-staff")
    assert "スタッフはまだ登録されていません" in card.get_text()
    assert card.select_one('a[href="/staff-shifts/staff/new"]')

    person = staff(admin_dataset, "田中")
    db.session.commit()
    page, _ = home(authenticated_client)
    assert "今日の出勤予定は登録されていません" in page.select_one("#today-staff").get_text()
    assert "出勤予定 0人" in page.select_one("#today-staff").get_text()
    shift(admin_dataset, person, status="off")
    db.session.commit()

    def no_commit():
        raise AssertionError("GET must not write to the database")
    monkeypatch.setattr(db.session, "commit", no_commit)
    page, _ = home(authenticated_client)
    assert "今日の出勤予定は登録されていません" in page.select_one("#today-staff").get_text()
    assert ShopShift.query.count() == 1


def test_more_than_four_people_are_summarized(authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: DAY)
    for index in range(5):
        shift(admin_dataset, staff(admin_dataset, f"スタッフ{index}"), status="all_day")
    db.session.commit()
    page, _ = home(authenticated_client)
    card = page.select_one("#today-staff")
    assert "出勤予定 5人" in card.get_text()
    assert len(card.select("li")) == 4
    assert "ほか1人" in card.get_text()


def test_guest_and_anonymous_never_read_staff(authenticated_client, admin_dataset, client, flask_app, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: DAY)
    shift(admin_dataset, staff(admin_dataset, "非公開スタッフ"))
    db.session.commit()
    guest, _ = guest_client(flask_app)
    statements = []
    def collect(conn, cursor, statement, parameters, context, executemany):
        if "shop_staff" in statement.lower() or "shop_shifts" in statement.lower():
            statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", collect)
    try:
        page, html = home(guest)
    finally:
        event.remove(db.engine, "before_cursor_execute", collect)
    assert page.select_one(".operations-heading h1").get_text(strip=True) == "パン工房 ひだまり"
    assert page.select_one(".hidamari-banner img") is not None
    assert page.select_one("#today-staff") is None
    assert "非公開スタッフ" not in html and 'href="/staff-shifts"' not in html
    assert not statements
    assert page.select_one("#weather") and page.select_one("#today-sales")
    assert page.select_one("#bakery-news") and not page.select_one("#puoppo")
    g.pop("_login_user", None)
    assert client.get("/home").status_code == 302
    g.pop("_login_user", None)
    assert guest.get("/staff-shifts").status_code == 403


def test_shift_read_failure_is_distinct_from_zero_and_keeps_home(authenticated_client, admin_dataset, monkeypatch):
    monkeypatch.setattr(app_module, "business_today", lambda: DAY)
    staff(admin_dataset, "田中")
    db.session.commit()
    calls = []
    def broken(*args):
        raise SQLAlchemyError("test read failure")
    original_rollback = db.session.rollback
    def rollback():
        calls.append(True)
        original_rollback()
    monkeypatch.setattr(app_module, "planned_work_for_day", broken, raising=False)
    monkeypatch.setattr(db.session, "rollback", rollback)
    page, _ = home(authenticated_client)
    card = page.select_one("#today-staff")
    assert calls
    assert "取得できません" in card.get_text()
    assert "出勤予定 0人" not in card.get_text()
    assert page.select_one("#today-sales") and page.select_one("#weather")
    assert page.select_one("#bakery-news") and not page.select_one("#puoppo")
