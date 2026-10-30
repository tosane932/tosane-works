"""Adminと同じ保存契約を正規Guest開始で検証する。"""
import pytest
from flask import g
from models import ShopStaff, ShopShift
from tests.test_guest_register_v32 import start
from tests import test_shop_staff_shifts as contracts


@pytest.fixture
def guest_staff(flask_app, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'REGISTER_V32_GUEST_ENABLED', True)
    monkeypatch.setitem(flask_app.config, 'GUEST_STAFF_ENABLED', True)
    owner, client = start(flask_app)
    return owner, client


@pytest.mark.parametrize('contract', [
    'test_staff_create_edit_retire_and_historical_shift',
    'test_all_day_edit_off_and_confirm_cancel',
    'test_staff_submit_key_prevents_repost_but_allows_same_name',
    'test_shift_work_off_and_not_registered_are_distinct',
])
def test_guest_reuses_staff_and_shift_contracts(guest_staff, contract):
    owner, client = guest_staff
    getattr(contracts, contract)(client, owner)


def test_guest_save_failure_rolls_back(guest_staff, monkeypatch):
    owner, client = guest_staff
    from models import db
    from sqlalchemy.exc import SQLAlchemyError
    staff = contracts.add_staff(client)
    path = '/staff-shifts/shifts/new'
    data = {**contracts._form(client, path), 'business_date': '2026-10-14',
            'staff_id': str(staff.id), 'status': 'all_day', 'start_time': '', 'end_time': '', 'note': ''}
    original = db.session.commit
    def fail_shift_only():
        if any(isinstance(row, ShopShift) for row in db.session.new):
            raise SQLAlchemyError('synthetic shift save failure')
        return original()
    monkeypatch.setattr(db.session, 'commit', fail_shift_only)
    assert client.post(path, data=data).status_code == 503
    assert ShopShift.query.filter_by(dataset_id=owner.id).count() == 0
    assert ShopStaff.query.filter_by(dataset_id=owner.id).count() == 1


@pytest.mark.parametrize('flag', ['GUEST_STAFF_ENABLED', 'REGISTER_V32_GUEST_ENABLED'])
def test_gate_rejects_read_and_write(guest_staff, flask_app, monkeypatch, flag):
    owner, client = guest_staff
    token = contracts._form(client, '/staff-shifts')['csrf_token']
    monkeypatch.setitem(flask_app.config, flag, False)
    assert client.get('/staff-shifts').status_code == 403
    assert client.post('/staff-shifts/staff/new', data={'csrf_token': token}).status_code == 403
    assert ShopStaff.query.filter_by(dataset_id=owner.id).count() == 0


def test_foreign_staff_and_shift_get_post_do_not_mutate(guest_staff, flask_app):
    a, ca = guest_staff
    b, cb = start(flask_app)
    g.pop('csrf_token', None)
    staff = contracts.add_staff(cb, '架空B専用')
    assert contracts.add_shift(cb, staff).status_code == 303
    shift = ShopShift.query.filter_by(dataset_id=b.id).one()
    g.pop('_login_user', None)
    g.pop('csrf_token', None)
    token = contracts._form(ca, '/staff-shifts')['csrf_token']
    for path in [f'/staff-shifts/staff/{staff.id}/edit', f'/staff-shifts/shifts/{shift.id}/edit']:
        assert ca.get(path).status_code == 404
        assert ca.post(path, data={'csrf_token': token}).status_code == 404
    assert ca.post(f'/staff-shifts/shifts/{shift.id}/cancel', data={'csrf_token': token}).status_code == 404
    assert ShopShift.query.filter_by(dataset_id=b.id).count() == 1
    assert ShopStaff.query.filter_by(dataset_id=b.id).one().display_name == '架空B専用'


def test_admin_records_reject_guest_get_post(authenticated_client, admin_dataset, guest_staff):
    a, ca = guest_staff
    g.pop('csrf_token', None)
    staff = contracts.add_staff(authenticated_client, '架空Admin専用')
    assert contracts.add_shift(authenticated_client, staff).status_code == 303
    shift = ShopShift.query.filter_by(dataset_id=admin_dataset.id).one()
    g.pop('csrf_token', None)
    token = contracts._form(ca, '/staff-shifts')['csrf_token']
    for path in [f'/staff-shifts/staff/{staff.id}/edit', f'/staff-shifts/shifts/{shift.id}/edit']:
        assert ca.get(path).status_code == 404
        assert ca.post(path, data={'csrf_token': token}).status_code == 404
    assert ca.post(f'/staff-shifts/shifts/{shift.id}/cancel', data={'csrf_token': token}).status_code == 404
    assert ShopShift.query.filter_by(dataset_id=admin_dataset.id).count() == 1
    assert ShopStaff.query.filter_by(dataset_id=admin_dataset.id).one().display_name == '架空Admin専用'


@pytest.mark.parametrize('expiry', ['idle', 'absolute'])
def test_expired_guest_cannot_read_or_write(guest_staff, expiry):
    import datetime
    from models import db
    owner, client = guest_staff
    staff = contracts.add_staff(client)
    token = contracts._form(client, '/staff-shifts')['csrf_token']
    now = datetime.datetime.now(datetime.timezone.utc)
    owner.created_at = now - datetime.timedelta(hours=3)
    if expiry == 'idle':
        owner.last_activity_at = now - datetime.timedelta(minutes=31)
    else:
        owner.absolute_expires_at = now - datetime.timedelta(seconds=1)
    db.session.commit()
    g.pop('_login_user', None)
    assert client.get('/staff-shifts').status_code == 302
    assert client.post(f'/staff-shifts/staff/{staff.id}/status', data={
        'csrf_token': token, 'is_active': '0'}).status_code in (302, 400)
    assert db.session.get(ShopStaff, staff.id).is_active
