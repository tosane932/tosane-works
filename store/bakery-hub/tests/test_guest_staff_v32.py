from flask import g
from models import ShopStaff, ShopShift, db
from tests.test_guest_register_v32 import start
from tests.test_shop_staff_shifts import add_staff, add_shift


def test_guest_staff_gate_and_isolation(flask_app, admin_dataset):
    flask_app.config['REGISTER_V32_GUEST_ENABLED'] = True
    a, ca = start(flask_app)
    b, cb = start(flask_app)
    g.pop('_login_user', None)
    assert ca.get('/staff-shifts').status_code == 403
    flask_app.config['GUEST_STAFF_ENABLED'] = True
    try:
        g.pop('csrf_token', None)
        staff = add_staff(ca, '架空製造担当')
        assert staff.dataset_id == a.id
        assert add_shift(ca, staff).status_code == 303
        g.pop('_login_user', None)
        assert cb.get(f'/staff-shifts/staff/{staff.id}').status_code == 404
        g.pop('csrf_token', None)
        assert add_shift(cb, staff).status_code == 404
        assert ShopShift.query.filter_by(dataset_id=b.id).count() == 0
        assert ShopStaff.query.filter_by(dataset_id=admin_dataset.id).count() == 0
        assert ca.post('/staff-shifts/staff/new', data={}).status_code == 400
        import datetime
        import app as app_module
        from models import Dataset
        from sqlalchemy import text
        db.session.execute(text('PRAGMA foreign_keys = ON'))
        aid, bid = a.id, b.id
        a.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
        a.absolute_expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        db.session.commit()
        assert app_module._cleanup_expired_guest_datasets() == 1
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Dataset, aid) is None
        assert db.session.get(Dataset, bid) is not None
        assert db.session.get(Dataset, admin_dataset.id) is not None
        assert ShopShift.query.filter_by(dataset_id=aid).count() == 0
        assert ShopStaff.query.filter_by(dataset_id=aid).count() == 0
        assert app_module._cleanup_expired_guest_datasets() == 0
    finally:
        flask_app.config['GUEST_STAFF_ENABLED'] = False
