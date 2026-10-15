"""会計保存の整合性・再送・Dataset境界をSQLiteで検証。"""

import datetime
import uuid

import pytest

import app as app_module
from models import (DailySales, Dataset, Product, RegisterCutover, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, db)
import register_service as service


def _dataset(kind="guest", *, expired=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    value = Dataset(kind=kind, system_key="admin" if kind == "admin" else None,
                    created_at=now - datetime.timedelta(hours=1),
                    last_activity_at=now - datetime.timedelta(minutes=31 if expired else 1),
                    absolute_expires_at=now + datetime.timedelta(hours=1) if kind == "guest" else None)
    db.session.add(value)
    db.session.commit()
    return value


def _ready(owner, *prices):
    db.session.add(RegisterCutover(dataset_id=owner.id,
                                   first_register_date=datetime.date(2020, 1, 1)))
    products = [RegisterProduct(dataset_id=owner.id, name=f"パン{i}", price_yen=price)
                for i, price in enumerate(prices)]
    db.session.add_all(products)
    db.session.commit()
    return products


def _save(owner, product, **changes):
    kwargs = dict(submission_key=uuid.uuid4(), kind="sale", items=[(product.id, 2)],
                  received_yen=500)
    kwargs.update(changes)
    return service.save_register_transaction(owner, **kwargs)


def test_sales_multiple_lines_boundaries_and_business_day(flask_app, monkeypatch):
    owner = _dataset("admin")
    a, b = _ready(owner, 120, 140)
    monkeypatch.setattr(service, "utc_now", lambda: datetime.datetime(
        2026, 10, 15, 16, 0, tzinfo=datetime.timezone.utc))
    saved = _save(owner, a, items=[(a.id, 2), (b.id, 1)])
    lines = RegisterTransactionLine.query.filter_by(transaction_id=saved.id).all()
    assert saved.id and saved.kind == "sale"
    assert (saved.business_date, saved.total_yen, saved.received_yen, saved.change_yen) == (
        datetime.date(2026, 10, 16), 380, 500, 120)
    assert sorted((line.product_name_snapshot, line.unit_price_yen, line.quantity,
                   line.line_total_yen) for line in lines) == [
        ("パン0", 120, 2, 240), ("パン1", 140, 1, 140)]
    b.price_yen, b.name, b.is_active = 150, "改名", False
    db.session.commit()
    assert (lines[1].product_name_snapshot, lines[1].unit_price_yen) == ("パン1", 140)


def test_plus_fifty_and_price_five_hundred(flask_app):
    owner = _dataset("admin")
    (item,) = _ready(owner, 500)
    record = _save(owner, item, items=[(item.id, 50)], received_yen=25_000)
    assert record.total_yen == 25_000
    assert RegisterTransactionLine.query.one().line_total_yen == 25_000


def test_refund_and_negative_fifty(flask_app):
    owner = _dataset("admin")
    (item,) = _ready(owner, 220)
    for quantity in (-1, -50):
        record = _save(owner, item, kind="refund", items=[(item.id, quantity)],
                       received_yen=None, refund_confirmed=True)
        assert (record.kind, record.total_yen, record.received_yen, record.change_yen) == (
            "refund", 220 * quantity, None, None)
    assert RegisterTransaction.query.count() == 2


@pytest.mark.parametrize("changes, error", [
    ({"items": []}, ValueError), ({"items": [(1, 0)]}, ValueError),
    ({"items": [(1, 51)]}, ValueError), ({"items": [(1, -51)]}, ValueError),
    ({"items": [(1, 1.5)]}, ValueError), ({"items": [(1, True)]}, ValueError),
    ({"items": [(1, 1), (1, 1)]}, ValueError),
    ({"items": [(1, 1)] * 51}, ValueError),
    ({"items": [(999, 1)]}, PermissionError),
    ({"received_yen": 10}, ValueError), ({"received_yen": -1}, ValueError),
    ({"received_yen": "wrong"}, ValueError),
    ({"received_yen": 1_000_000_001}, ValueError),
    ({"kind": "other"}, ValueError),
    ({"kind": "refund", "items": [(1, -1)], "received_yen": None}, ValueError),
    ({"kind": "refund", "items": [(1, -1)], "received_yen": 500,
      "refund_confirmed": True}, ValueError),
    ({"kind": "refund", "items": [(1, -1), (2, 1)], "received_yen": None,
      "refund_confirmed": True}, ValueError),
])
def test_invalid_input_does_not_save(flask_app, changes, error):
    owner = _dataset("admin")
    a, b = _ready(owner, 120, 140)
    with pytest.raises(error):
        _save(owner, a, **changes)
    assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 0


def test_zero_price_and_zero_only_lines_temporarily_rejected(flask_app):
    owner = _dataset("admin")
    (free,) = _ready(owner, 0)
    with pytest.raises(ValueError, match="0円会計"):
        _save(owner, free)
    with pytest.raises(ValueError):
        _save(owner, free, items=[(free.id, 0)])


def test_replay_after_product_changes_and_order_reversal(flask_app):
    owner = _dataset("admin")
    a, b = _ready(owner, 120, 140)
    key = uuid.uuid4()
    first = _save(owner, a, submission_key=key,
                  items=[(b.id, 1), (a.id, 2)])
    a.price_yen, a.is_active = 500, False
    db.session.commit()
    second = _save(owner, a, submission_key=key,
                   items=[(a.id, 2), (b.id, 1)])
    assert second.id == first.id and second.total_yen == 380
    with pytest.raises(service.SubmissionConflict):
        _save(owner, a, submission_key=key, items=[(b.id, 2)])
    assert RegisterTransaction.query.count() == 1


def test_unique_race_reloads_committed_result(flask_app, monkeypatch):
    owner = _dataset("admin")
    (item,) = _ready(owner, 120)
    key = uuid.uuid4()
    first = _save(owner, item, submission_key=key)
    original = service._existing
    calls = 0

    def miss_once(owner_id, submission_key, digest):
        nonlocal calls
        calls += 1
        return None if calls == 1 else original(owner_id, submission_key, digest)

    monkeypatch.setattr(service, "_existing", miss_once)
    duplicate = _save(owner, item, submission_key=key)
    assert duplicate.id == first.id and calls == 2
    assert RegisterTransaction.query.count() == 1


def test_lost_response_reuses_same_key(flask_app):
    owner = _dataset("admin")
    (item,) = _ready(owner, 120)
    key = service.new_submission_key()

    def lost_response():
        _save(owner, item, submission_key=key)
        raise ConnectionError("保存後に応答だけが失われた")

    with pytest.raises(ConnectionError):
        lost_response()
    recovered = _save(owner, item, submission_key=key)
    assert recovered.submission_key == key
    assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 1


def test_new_key_requires_active_same_dataset_product(flask_app):
    admin, guest_a, guest_b = _dataset("admin"), _dataset(), _dataset()
    admin_product = _ready(admin, 100)[0]
    guest_product = _ready(guest_a, 200)[0]
    _ready(guest_b, 300)
    for owner, foreign in [(guest_a, admin_product), (guest_b, guest_product),
                           (admin, guest_product)]:
        with pytest.raises(PermissionError):
            _save(owner, foreign)
    guest_product.is_active = False
    db.session.commit()
    with pytest.raises(PermissionError):
        _save(guest_a, guest_product)
    assert RegisterTransaction.query.count() == 0


def test_cutover_old_sales_and_expiry_do_not_mutate_legacy(flask_app):
    owner = _dataset("admin")
    product = RegisterProduct(dataset_id=owner.id, name="レジ商品", price_yen=100)
    old = Product(dataset_id=owner.id, year=2026, month=10, name="旧商品", price=200)
    db.session.add_all([product, old])
    db.session.commit()
    with pytest.raises(ValueError, match="切替"):
        _save(owner, product)
    db.session.add(RegisterCutover(dataset_id=owner.id,
                                   first_register_date=datetime.date(9999, 1, 1)))
    db.session.commit()
    with pytest.raises(ValueError, match="切替"):
        _save(owner, product)
    db.session.get(RegisterCutover, owner.id).first_register_date = datetime.date(2020, 1, 1)
    db.session.add(DailySales(product_id=old.id, date=service.utc_now().astimezone(
        service.TOKYO).date(), quantity=2))
    db.session.commit()
    with pytest.raises(ValueError, match="旧日次売上"):
        _save(owner, product)
    assert (old.price, DailySales.query.one().quantity) == (200, 2)
    assert RegisterTransaction.query.count() == 0
    expired = _dataset(expired=True)
    (expired_product,) = _ready(expired, 100)
    with pytest.raises(PermissionError, match="期限切れ"):
        _save(expired, expired_product)


def test_rollback_on_line_or_commit_failure(flask_app, monkeypatch):
    owner = _dataset("admin")
    (item,) = _ready(owner, 100)
    original_commit = db.session.commit

    def fail_commit():
        raise RuntimeError("通信前のDB障害")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    with pytest.raises(RuntimeError):
        _save(owner, item)
    monkeypatch.setattr(db.session, "commit", original_commit)
    assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 0
    original_add = db.session.add

    def fail_line(value):
        if isinstance(value, RegisterTransactionLine):
            raise RuntimeError("明細の途中で失敗")
        return original_add(value)

    monkeypatch.setattr(db.session, "add", fail_line)
    with pytest.raises(RuntimeError):
        _save(owner, item)
    monkeypatch.setattr(db.session, "add", original_add)
    assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 0
    assert _save(owner, item).total_yen == 200


def test_second_line_failure_rolls_back_header_and_first_line(flask_app, monkeypatch):
    owner = _dataset("admin")
    a, b = _ready(owner, 100, 200)
    original = db.session.add
    count = 0

    def fail_second_line(value):
        nonlocal count
        if isinstance(value, RegisterTransactionLine):
            count += 1
            if count == 2:
                raise RuntimeError("2行目の保存障害")
        return original(value)

    monkeypatch.setattr(db.session, "add", fail_second_line)
    with pytest.raises(RuntimeError):
        _save(owner, a, items=[(a.id, 1), (b.id, 1)])
    monkeypatch.setattr(db.session, "add", original)
    assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 0


def test_guest_limit_and_cleanup_preserve_others(flask_app, monkeypatch):
    admin, expired, active = _dataset("admin"), _dataset(), _dataset()
    admin_item, expired_item, active_item = (_ready(owner, 100)[0]
                                            for owner in (admin, expired, active))
    for owner, item in ((admin, admin_item), (expired, expired_item), (active, active_item)):
        _save(owner, item)
    monkeypatch.setattr(service, "GUEST_TRANSACTION_LIMIT", 1)
    with pytest.raises(service.GuestTransactionLimitReached):
        _save(active, active_item)
    expired.last_activity_at = service.utc_now() - datetime.timedelta(minutes=31)
    db.session.commit()
    expired_id, admin_id, active_id = expired.id, admin.id, active.id
    assert app_module._cleanup_expired_guest_datasets() == 1
    db.session.commit()
    assert db.session.get(Dataset, expired_id) is None
    assert {row.dataset_id for row in RegisterTransaction.query.all()} == {admin_id, active_id}
