"""Admin-only staff and planned shift contracts."""
import datetime
import uuid

import pytest
from bs4 import BeautifulSoup
from flask import g
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import Dataset, ShopShift, ShopStaff, db
from shop_schedules import as_jst
from shop_staff_shifts import planned_work_for_day


def _form(client, path):
    g.pop("_login_user", None)
    page = client.get(path)
    assert page.status_code == 200
    soup = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    return {
        "csrf_token": soup.select_one('input[name="csrf_token"]')["value"],
        "submission_key": (
            soup.select_one('input[name="submission_key"]')["value"]
            if soup.select_one('input[name="submission_key"]') else ""
        ),
    }


def add_staff(client, name="田中", role="production"):
    path = "/staff-shifts/staff/new"
    values = _form(client, path)
    response = client.post(path, data={
        **values, "display_name": name, "role": role, "hired_on": "2026-10-01",
    })
    assert response.status_code == 303
    return ShopStaff.query.order_by(ShopStaff.id.desc()).first()


def add_shift(client, staff, day="2026-10-14", **overrides):
    path = "/staff-shifts/shifts/new"
    data = {
        **_form(client, path + "?date=" + day),
        "business_date": day, "staff_id": str(staff.id), "status": "work",
        "start_time": "05:00", "end_time": "13:00",
        "end_next_day": "", "note": "",
        **overrides,
    }
    return client.post(path, data=data)


def guest_client(flask_app):
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(
        kind="guest", system_key=None, created_at=now, last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    db.session.add(dataset)
    db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = f"guest:{dataset.id}"
        session["_fresh"] = True
    return client, dataset


def test_empty_month_and_admin_only_navigation(authenticated_client, admin_dataset, flask_app):
    page = authenticated_client.get("/staff-shifts")
    soup = BeautifulSoup(page.get_data(as_text=True), "html.parser")
    assert page.status_code == 200
    assert "スタッフはまだ登録されていません" in page.get_data(as_text=True)
    assert "未登録" in page.get_data(as_text=True)
    assert soup.select_one(".staff-shifts-calendar table") is not None
    assert soup.select_one(".staff-shifts-days") is not None
    assert soup.select_one('a[data-page="staff-shifts"]')["aria-current"] == "page"
    assert soup.select_one('a[href="/staff-shifts/staff/new"]') is not None
    guest, _ = guest_client(flask_app)
    g.pop("_login_user", None)
    guest_page = guest.get("/home")
    assert guest_page.status_code == 200
    assert 'data-page="staff-shifts"' not in guest_page.get_data(as_text=True)


@pytest.mark.parametrize("path", [
    "/staff-shifts", "/staff-shifts/staff/new", "/staff-shifts/shifts/new",
    "/staff-shifts/staff/1/edit", "/staff-shifts/shifts/1/edit",
])
def test_guest_and_anonymous_cannot_read_staff(flask_app, client, path):
    guest, _ = guest_client(flask_app)
    g.pop("_login_user", None)
    assert guest.get(path).status_code == 403
    g.pop("_login_user", None)
    assert client.get(path).status_code in (302, 401)


@pytest.mark.parametrize("path", [
    "/staff-shifts/staff/new", "/staff-shifts/staff/1/edit",
    "/staff-shifts/staff/1/status", "/staff-shifts/shifts/new",
    "/staff-shifts/shifts/1/edit", "/staff-shifts/shifts/1/cancel",
])
def test_guest_cannot_write_staff_or_shifts(flask_app, path):
    guest, _ = guest_client(flask_app)
    g.pop("_login_user", None)
    token = _form(guest, "/home")["csrf_token"]
    g.pop("_login_user", None)
    assert guest.post(path, data={"csrf_token": token}).status_code == 403
    assert ShopStaff.query.count() == 0
    assert ShopShift.query.count() == 0


def test_staff_create_edit_retire_and_historical_shift(authenticated_client, admin_dataset):
    staff = add_staff(authenticated_client, name="<田中>")
    assert staff.display_name == "<田中>"
    assert staff.role == "production"
    assert staff.hired_on == datetime.date(2026, 10, 1)
    assert "&lt;田中&gt;" in authenticated_client.get("/staff-shifts").get_data(as_text=True)
    assert "<田中>" not in authenticated_client.get("/staff-shifts").get_data(as_text=True)

    assert add_shift(authenticated_client, staff).status_code == 303
    edit = f"/staff-shifts/staff/{staff.id}/edit"
    response = authenticated_client.post(edit, data={
        **_form(authenticated_client, edit),
        "display_name": "田中さん", "role": "both", "hired_on": "",
    })
    assert response.status_code == 303
    assert db.session.get(ShopStaff, staff.id).role == "both"

    status = f"/staff-shifts/staff/{staff.id}/status"
    response = authenticated_client.post(status, data={
        "csrf_token": _form(authenticated_client, "/staff-shifts")["csrf_token"],
        "is_active": "0",
    })
    assert response.status_code == 303
    assert not db.session.get(ShopStaff, staff.id).is_active
    assert ShopShift.query.count() == 1
    page = authenticated_client.get("/staff-shifts?month=2026-10&date=2026-10-14").get_data(as_text=True)
    assert "田中さん" in page and "退職済み" in page
    new_form = authenticated_client.get("/staff-shifts/shifts/new").get_data(as_text=True)
    options = BeautifulSoup(new_form, "html.parser").select('select[name="staff_id"] option')
    assert str(staff.id) not in [option.get("value") for option in options]
    assert add_shift(authenticated_client, staff, day="2026-10-15").status_code == 400


@pytest.mark.parametrize(("name", "role", "hired"), [
    ("   ", "sales", ""), ("x" * 81, "sales", ""),
    ("通常", "invalid", ""), ("通常", "sales", "2026-02-30"),
])
def test_staff_rejects_invalid_values(authenticated_client, admin_dataset, name, role, hired):
    path = "/staff-shifts/staff/new"
    response = authenticated_client.post(path, data={
        **_form(authenticated_client, path),
        "display_name": name, "role": role, "hired_on": hired,
    })
    assert response.status_code == 400
    assert ShopStaff.query.count() == 0


def test_staff_submit_key_prevents_repost_but_allows_same_name(authenticated_client, admin_dataset):
    path = "/staff-shifts/staff/new"
    data = {**_form(authenticated_client, path), "display_name": "同名", "role": "sales", "hired_on": ""}
    assert authenticated_client.post(path, data=data).status_code == 303
    assert authenticated_client.post(path, data=data).status_code == 409
    assert ShopStaff.query.count() == 1
    second = {**_form(authenticated_client, path), "display_name": "同名", "role": "sales", "hired_on": ""}
    assert authenticated_client.post(path, data=second).status_code == 303
    assert ShopStaff.query.count() == 2
    assert ShopStaff.query.first().id != ShopStaff.query.order_by(ShopStaff.id.desc()).first().id


def test_shift_work_off_and_not_registered_are_distinct(authenticated_client, admin_dataset):
    staff = add_staff(authenticated_client)
    assert add_shift(authenticated_client, staff, status="off", start_time="", end_time="").status_code == 303
    row = ShopShift.query.one()
    assert row.status == "off" and row.starts_at is None and row.ends_at is None
    page = authenticated_client.get("/staff-shifts?month=2026-10&date=2026-10-14").get_data(as_text=True)
    assert "休み" in page
    empty = authenticated_client.get("/staff-shifts?month=2026-10&date=2026-10-15").get_data(as_text=True)
    assert "この日のシフトは未登録です" in empty
    edit = f"/staff-shifts/shifts/{row.id}/edit"
    updated = authenticated_client.post(edit, data={
        **_form(authenticated_client, edit),
        "business_date": "2026-10-14", "staff_id": str(staff.id),
        "status": "work", "start_time": "05:00", "end_time": "13:00",
        "end_next_day": "", "note": "<script>alert(1)</script>",
    })
    assert updated.status_code == 303
    assert ShopShift.query.count() == 1
    assert ShopShift.query.one().id == row.id
    assert as_jst(ShopShift.query.one().starts_at).hour == 5
    page = authenticated_client.get("/staff-shifts?month=2026-10&date=2026-10-14").get_data(as_text=True)
    note = BeautifulSoup(page, "html.parser").select_one(".staff-shifts-note")
    assert note is not None
    assert "&lt;script&gt;" in str(note) and note.find("script") is None


def test_overnight_and_adjacent_day_overlap(authenticated_client, admin_dataset):
    staff = add_staff(authenticated_client)
    assert add_shift(
        authenticated_client, staff, start_time="22:00", end_time="02:00",
        end_next_day="1",
    ).status_code == 303
    row = ShopShift.query.one()
    assert row.business_date == datetime.date(2026, 10, 14)
    assert as_jst(row.starts_at).strftime("%Y-%m-%d %H:%M") == "2026-10-14 22:00"
    assert as_jst(row.ends_at).strftime("%Y-%m-%d %H:%M") == "2026-10-15 02:00"
    assert add_shift(authenticated_client, staff, day="2026-10-15", start_time="01:00", end_time="05:00").status_code == 400
    assert add_shift(authenticated_client, staff, day="2026-10-15", start_time="02:00", end_time="06:00").status_code == 303
    assert ShopShift.query.count() == 2
    assert planned_work_for_day(admin_dataset, datetime.date(2026, 10, 14)) == [row]


@pytest.mark.parametrize(("start", "end", "next_day"), [
    ("", "13:00", ""), ("05:00", "", ""), ("13:00", "05:00", ""),
    ("05:00", "05:00", "1"), ("25:00", "13:00", ""),
])
def test_invalid_work_time_rejected(authenticated_client, admin_dataset, start, end, next_day):
    staff = add_staff(authenticated_client)
    response = add_shift(authenticated_client, staff, start_time=start, end_time=end, end_next_day=next_day)
    assert response.status_code == 400
    assert ShopShift.query.count() == 0


def test_unique_day_cancel_and_admin_staff_isolation(authenticated_client, admin_dataset, flask_app):
    staff = add_staff(authenticated_client)
    assert add_shift(authenticated_client, staff).status_code == 303
    assert add_shift(authenticated_client, staff, status="off", start_time="", end_time="").status_code == 409
    row = ShopShift.query.one()
    csrf = _form(authenticated_client, f"/staff-shifts/shifts/{row.id}/edit")["csrf_token"]
    assert authenticated_client.post(
        f"/staff-shifts/shifts/{row.id}/cancel", data={"csrf_token": csrf}
    ).status_code == 303
    assert ShopShift.query.count() == 0
    assert db.session.get(ShopStaff, staff.id) is not None

    guest, guest_dataset = guest_client(flask_app)
    other = ShopStaff(dataset_id=guest_dataset.id, display_name="ゲスト内部", role="sales")
    db.session.add(other)
    db.session.commit()
    g.pop("_login_user", None)
    assert guest.get("/staff-shifts").status_code == 403
    assert "ゲスト内部" not in authenticated_client.get("/staff-shifts").get_data(as_text=True)
    assert add_shift(authenticated_client, other).status_code == 404


def test_csrf_and_bad_ids_dates(authenticated_client, admin_dataset):
    staff = add_staff(authenticated_client)
    assert authenticated_client.post("/staff-shifts/shifts/new", data={
        "business_date": "2026-10-14", "staff_id": str(staff.id), "status": "off",
    }).status_code == 400
    assert authenticated_client.get("/staff-shifts?month=2026-13").status_code == 400
    assert authenticated_client.get("/staff-shifts?month=2026-10&date=2026-11-01").status_code == 400
    assert authenticated_client.get("/staff-shifts/shifts/new?date=invalid").status_code == 400
    assert authenticated_client.get("/staff-shifts/staff/999/edit").status_code == 404
    assert authenticated_client.get("/staff-shifts/shifts/999/edit").status_code == 404


def test_db_constraints_prevent_duplicate_and_cross_dataset_staff(flask_app, admin_dataset):
    staff = ShopStaff(dataset_id=admin_dataset.id, display_name="A", role="sales")
    guest, guest_dataset = guest_client(flask_app)
    other = ShopStaff(dataset_id=guest_dataset.id, display_name="B", role="sales")
    db.session.add_all([staff, other])
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    day = datetime.date(2026, 10, 14)
    db.session.add_all([
        ShopShift(dataset_id=admin_dataset.id, staff_id=staff.id, business_date=day, status="off"),
        ShopShift(dataset_id=admin_dataset.id, staff_id=staff.id, business_date=day, status="off"),
    ])
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    db.session.add(ShopShift(dataset_id=admin_dataset.id, staff_id=other.id, business_date=day, status="off"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert ShopShift.query.count() == 0


def test_failed_save_rolls_back(authenticated_client, admin_dataset, monkeypatch):
    staff = add_staff(authenticated_client)
    def fail():
        raise SQLAlchemyError("test failure")
    monkeypatch.setattr(db.session, "commit", fail)
    assert add_shift(authenticated_client, staff).status_code == 503
    assert ShopShift.query.count() == 0
