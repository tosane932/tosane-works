"""Bounded automatic receipt ingestion; device date and price are never sufficient alone."""
import datetime
import uuid
from zoneinfo import ZoneInfo

import pytest

from models import (DailySales, Product, RegisterCutover, RegisterOfflinePermit,
                    RegisterRecoveryCase, RegisterTransaction, RegisterTransactionLine, db)
from register_offline_service import (OfflineReviewRequired, prepare_offline,
                                      revoke_offline, save_offline_transaction)
from register_products import create_custom_product, update_register_product
from register_service import utc_now
from tests.test_register_recovery import receipt


def setup_grant(owner, *, next_day=True):
    product = create_custom_product(owner, name="パン", price_yen=120)
    day = utc_now().astimezone(ZoneInfo("Asia/Tokyo")).date()
    db.session.add(RegisterCutover(dataset_id=owner.id, first_register_date=day))
    db.session.commit()
    grant, token, proof, _ = prepare_offline(owner, str(uuid.uuid4()), next_day=next_day)
    return product, grant, token, proof, day


def original_for(product, proof, day, *, when=None):
    data = receipt(product, day=day)
    data["catalog_proof"] = proof
    if when:
        data["completed_at_local"] = when.isoformat()
    return data


def test_next_day_sync_keeps_original_day_and_price(flask_app, admin_dataset, monkeypatch):
    product, grant, token, proof, day = setup_grant(admin_dataset)
    recorded = grant.issued_at.astimezone(ZoneInfo("Asia/Tokyo"))
    original = original_for(product, proof, day, when=recorded)
    update_register_product(admin_dataset, product.id, name="パン", price_yen=140,
                            expected_name="パン", expected_price_yen=120)
    next_server_time = grant.issued_at.replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)
    assert next_server_time.astimezone(ZoneInfo("Asia/Tokyo")).date() == day + datetime.timedelta(days=1)
    monkeypatch.setattr("register_offline_service.utc_now",
                        lambda: next_server_time)
    saved = save_offline_transaction(admin_dataset, original, token)
    assert saved.business_date == day and saved.save_method == "offline"
    assert saved.total_yen == 240 and saved.offline_permit_id == grant.id
    assert saved.completed_at > saved.client_completed_at
    assert RegisterTransactionLine.query.one().unit_price_yen == 120
    assert save_offline_transaction(admin_dataset, original, token).id == saved.id
    assert RegisterTransaction.query.count() == 1 and RegisterRecoveryCase.query.count() == 0


def test_rejected_grants_and_conflicting_keys_keep_sales_untouched(flask_app, admin_dataset):
    product, grant, token, proof, day = setup_grant(admin_dataset, next_day=False)
    original = original_for(product, proof, day, when=grant.issued_at.astimezone(ZoneInfo("Asia/Tokyo")))
    other = original_for(product, proof, day + datetime.timedelta(days=1))
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, other, token)
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, original, token + "bad")
    revoke_offline(admin_dataset)
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, original, token)
    assert RegisterTransaction.query.count() == 0


def test_old_daily_sales_blocks_offline_sync(flask_app, admin_dataset):
    product, grant, token, proof, day = setup_grant(admin_dataset)
    original = original_for(product, proof, day, when=grant.issued_at.astimezone(ZoneInfo("Asia/Tokyo")))
    old = Product(dataset_id=admin_dataset.id, year=day.year, month=day.month,
                  name="旧商品", price=280)
    db.session.add(old)
    db.session.flush()
    db.session.add(DailySales(product_id=old.id, date=day, quantity=1))
    db.session.commit()
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, original, token)
    assert RegisterTransaction.query.count() == 0


def test_offline_http_admin_csrf_and_dataset_boundary(flask_app, authenticated_client,
                                                       admin_dataset):
    from bs4 import BeautifulSoup
    from flask import g
    from tests.test_register_routes import guest

    product, grant, token, proof, day = setup_grant(admin_dataset)
    flask_app.config.update(REGISTER_V3_PRODUCTION_ENABLED=True,
                            REGISTER_V3_PREVIEW_ENABLED=True,
                            REGISTER_V3_SAVE_ENABLED=True,
                            REGISTER_V3_OFFLINE_MODE_ENABLED=True)
    g.pop("_login_user", None)
    page = authenticated_client.get("/register/preview")
    csrf = BeautifulSoup(page.data, "html.parser").select_one('meta[name="csrf-token"]')["content"]
    assert page.status_code == 200
    shell = authenticated_client.get("/register/offline")
    assert shell.status_code == 200 and shell.headers["X-Register-Offline-Shell"] == "1"
    assert str(admin_dataset.id).encode() not in shell.data
    assert authenticated_client.post("/api/register/offline/prepare", json={}).status_code == 400
    data = {"device_id": str(uuid.uuid4()), "next_day": True}
    prepared = authenticated_client.post("/api/register/offline/prepare", json=data,
                                         headers={"X-CSRFToken": csrf})
    assert prepared.status_code == 200 and prepared.json["scope"] == f"{admin_dataset.id}:admin"
    session = authenticated_client.get("/api/register/offline/session",
                                       query_string={"grant_id": prepared.json["grant_id"]})
    assert session.status_code == 200 and session.json["csrf_token"]
    original = original_for(product, prepared.json["catalog_proof"], day,
                            when=utc_now())
    g.pop("_login_user", None)
    response = authenticated_client.post("/api/register/offline/sync",
        json={"receipt": original, "grant_token": prepared.json["token"]},
        headers={"X-CSRFToken": csrf})
    assert response.status_code == 200 and response.json["business_date"] == day.isoformat()
    assert authenticated_client.post("/api/register/offline/sync",
        json={"receipt": original, "grant_token": prepared.json["token"]},
        headers={"X-CSRFToken": csrf}).json["transaction_id"] == response.json["transaction_id"]
    outsider, other_client = guest(flask_app)
    g.pop("_login_user", None)
    assert other_client.get("/register/offline").status_code in (302, 403, 404)
    assert other_client.get("/api/register/offline/session",
                            query_string={"grant_id": prepared.json["grant_id"]}).status_code != 200
    assert RegisterTransaction.query.count() == 1


def test_offline_preparation_is_default_disabled(flask_app, authenticated_client, admin_dataset):
    setup_grant(admin_dataset)
    assert not flask_app.config["REGISTER_V3_OFFLINE_MODE_ENABLED"]
    assert authenticated_client.get("/register/offline").status_code == 404
    assert authenticated_client.get("/api/register/offline/session",
                                    query_string={"grant_id": str(uuid.uuid4())}).status_code == 404


def test_offline_receipts_bounds_price_and_recovery_case(flask_app, admin_dataset):
    from register_recovery_service import preserve_case
    product, grant, token, proof, day = setup_grant(admin_dataset)
    original = original_for(product, proof, day,
                            when=grant.issued_at.replace(tzinfo=datetime.timezone.utc))
    bad = dict(original, catalog_proof=proof + "changed")
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, bad, token)
    bad = dict(original, total_yen=120)
    with pytest.raises(ValueError):
        save_offline_transaction(admin_dataset, bad, token)
    bad = dict(original, submission_key=str(uuid.uuid4()))
    bad["snapshots"] = [dict(bad["snapshots"][0], unit_price_yen=140,
                             line_total_yen=280)]
    bad["displayed_prices"] = [[product.id, 140]]
    bad["total_yen"] = 280
    bad["change_yen"] = bad["received_yen"] - 280
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, bad, token)
    preserve_case(admin_dataset, original, actor="admin", device_status="needs_review")
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, original, token)
    assert RegisterTransaction.query.count() == 0 and RegisterRecoveryCase.query.count() == 1


def test_offline_auto_accept_limit_and_key_collision(flask_app, admin_dataset):
    product, grant, token, proof, day = setup_grant(admin_dataset)
    original = original_for(product, proof, day,
                            when=grant.issued_at.replace(tzinfo=datetime.timezone.utc))
    saved = save_offline_transaction(admin_dataset, original, token)
    changed = dict(original, received_yen=600, change_yen=360)
    with pytest.raises(OfflineReviewRequired):
        save_offline_transaction(admin_dataset, changed, token)
    huge = original_for(product, proof, day,
                        when=grant.issued_at.replace(tzinfo=datetime.timezone.utc))
    huge["submission_key"] = str(uuid.uuid4())
    huge["items"] = [[product.id, 50]]
    huge["snapshots"] = [dict(huge["snapshots"][0], quantity=50, line_total_yen=6000)]
    huge["total_yen"] = 6000
    huge["received_yen"] = 6000
    huge["change_yen"] = 0
    # An invalid amount can never create half a transaction.
    huge["received_yen"] = 100
    with pytest.raises(ValueError):
        save_offline_transaction(admin_dataset, huge, token)
    assert RegisterTransaction.query.count() == 1
    assert RegisterTransactionLine.query.count() == 1
    assert saved.id == RegisterTransaction.query.one().id


def test_fifty_offline_sales_are_independent(flask_app, admin_dataset):
    product, grant, token, proof, day = setup_grant(admin_dataset)
    when = grant.issued_at.replace(tzinfo=datetime.timezone.utc)
    keys = []
    for _ in range(50):
        original = original_for(product, proof, day, when=when)
        keys.append(original["submission_key"])
        saved = save_offline_transaction(admin_dataset, original, token)
        assert saved.submission_key == uuid.UUID(original["submission_key"])
    assert len(set(keys)) == 50
    assert RegisterTransaction.query.count() == RegisterTransactionLine.query.count() == 50
    assert RegisterRecoveryCase.query.count() == 0


def test_admin_logout_revokes_issued_grants(flask_app, authenticated_client, admin_dataset):
    from bs4 import BeautifulSoup
    from flask import g
    _, grant, token, proof, day = setup_grant(admin_dataset)
    flask_app.config.update(REGISTER_V3_OFFLINE_MODE_ENABLED=True)
    g.pop("_login_user", None)
    csrf = BeautifulSoup(authenticated_client.get("/home").data,
                         "html.parser").select_one('form[action="/logout"] input[name="csrf_token"]')["value"]
    response = authenticated_client.post("/logout", data={"csrf_token": csrf})
    assert response.status_code == 302
    db.session.refresh(grant)
    assert grant.revoked_at is not None
