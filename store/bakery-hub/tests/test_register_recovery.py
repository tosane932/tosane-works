"""端末元記録の保全と判断は売上作成から独立する。"""
import datetime
import json
import uuid
from zoneinfo import ZoneInfo

import pytest
from flask import g
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from models import (Dataset, Product, RegisterCutover, RegisterCutoverApproval,
                    RegisterProduct, RegisterRecoveryCase, RegisterRecoveryEvent,
                    RegisterRecoveryLine, RegisterTransaction, db)
from register_cutover_audit import audit_cutover
from register_cutover_service import CutoverBlocked, confirm_cutover
from register_products import create_custom_product, ensure_standard_products, update_register_product
from register_recovery_service import RecoveryConflict, preserve_case, review_case
from register_recovery_service import confirm_recovery
from register_catalog_proof import issue_catalog_proof
from register_service import RecoveryReviewRequired, save_register_transaction
from tests.test_register_routes import guest


def receipt(product, *, day=None, key=None, price=None):
    day = day or datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    price = product.price_yen if price is None else price
    amount = price * 2
    return dict(submission_key=str(key or uuid.uuid4()), kind="sale",
        items=[[product.id, 2]], displayed_prices=[[product.id, price]],
        snapshots=[dict(id=product.id, name=product.name, unit_price_yen=price,
                        quantity=2, line_total_yen=amount)], total_yen=amount,
        received_yen=500, change_yen=500 - amount, refund_confirmed=False,
        client_business_date=day.isoformat(),
        completed_at_local=datetime.datetime.combine(day, datetime.time(12),
            tzinfo=ZoneInfo("Asia/Tokyo")).isoformat())


def review(owner, case, target, **kwargs):
    return review_case(owner, case.id, actor="test-admin", target=target,
                       reason="会計記録と現金帳簿を確認し調査結果を記録", confirmed=True, **kwargs)


def test_preserve_is_idempotent_but_never_posts_sales(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    original = receipt(product, day=datetime.date(2026, 10, 16))
    case, created = preserve_case(admin_dataset, original, actor="test-admin")
    assert created and case.status == "unreviewed"
    assert case.submission_key == uuid.UUID(original["submission_key"])
    assert json.loads(case.snapshot_json) == original
    assert RegisterRecoveryLine.query.one().unit_price_yen == 120
    assert RegisterTransaction.query.count() == 0
    assert preserve_case(admin_dataset, original, actor="test-admin")[1] is False
    assert RegisterRecoveryCase.query.count() == RegisterRecoveryEvent.query.count() == 1
    changed = {**original, "total_yen": 100}
    with pytest.raises(ValueError):
        preserve_case(admin_dataset, changed, actor="test-admin")
    changed = {**original, "completed_at_local": "2026-10-16T13:00:00+09:00"}
    with pytest.raises(RecoveryConflict):
        preserve_case(admin_dataset, changed, actor="test-admin")
    with pytest.raises(RecoveryConflict):
        review(admin_dataset, case, "saved")
    assert RegisterTransaction.query.count() == 0


def test_refund_and_invalid_snapshot_are_distinct_from_sales(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    original = receipt(product)
    original.update(kind="refund", items=[[product.id, -2]], total_yen=-240,
                    received_yen=None, change_yen=None, refund_confirmed=True)
    original["snapshots"][0].update(quantity=-2, line_total_yen=-240)
    case, _ = preserve_case(admin_dataset, original, actor="admin")
    assert case.kind == "refund" and case.total_yen == -240
    invalid = receipt(product)
    invalid["displayed_prices"] = None
    with pytest.raises(ValueError, match="表示価格"):
        preserve_case(admin_dataset, invalid, actor="admin")
    assert RegisterTransaction.query.count() == 0


def test_review_hold_research_cancel_and_audit_are_atomic(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    case, _ = preserve_case(admin_dataset, receipt(product), actor="test-admin")
    review(admin_dataset, case, "investigating")
    review(admin_dataset, case, "held")
    with pytest.raises(RecoveryConflict):
        review(admin_dataset, case, "cancelled", cash_result="unknown")
    assert case.status == "held" and RegisterRecoveryEvent.query.count() == 3
    review(admin_dataset, case, "investigating")
    review(admin_dataset, case, "cancelled", cash_result="matched",
           evidence_kind="取消伝票", evidence_reference="現金日報の取消記録A")
    assert case.status == "cancelled"
    assert RegisterRecoveryEvent.query.count() == 5
    assert RegisterRecoveryEvent.query.order_by(RegisterRecoveryEvent.id.desc()).first().evidence_reference == "現金日報の取消記録A"
    with pytest.raises(RecoveryConflict):
        review(admin_dataset, case, "cancelled", cash_result="matched",
               evidence_kind="取消伝票", evidence_reference="二重処理")
    with pytest.raises(DBAPIError):
        db.session.execute(text("DELETE FROM register_recovery_events WHERE case_id=:id"), {"id": case.id})
    db.session.rollback()
    assert RegisterRecoveryEvent.query.count() == 5


def test_exact_saved_reconciliation_and_price_change_conflict(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id,
                                   first_register_date=datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()))
    db.session.commit()
    original = receipt(product, day=datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date())
    save_register_transaction(admin_dataset, submission_key=original["submission_key"],
        kind="sale", items=original["items"], received_yen=500,
        displayed_prices=original["displayed_prices"],
        checkout_business_date=original["client_business_date"])
    case, _ = preserve_case(admin_dataset, original, actor="test-admin")
    assert case.status == "saved" and RegisterTransaction.query.count() == 1
    assert RegisterRecoveryEvent.query.order_by(RegisterRecoveryEvent.id.desc()).first().server_result == "matching"
    assert preserve_case(admin_dataset, original, actor="test-admin")[1] is False
    assert RegisterTransaction.query.count() == 1
    another = receipt(product, key=uuid.uuid4())
    update_register_product(admin_dataset, product.id, name="パン", price_yen=150,
                            expected_name="パン", expected_price_yen=120)
    pending, _ = preserve_case(admin_dataset, another, actor="test-admin")
    assert pending.status == "unreviewed"
    assert RegisterRecoveryEvent.query.filter_by(case_id=pending.id).one().price_conflict
    with pytest.raises(RecoveryReviewRequired, match="復旧案件"):
        save_register_transaction(admin_dataset, submission_key=another["submission_key"],
            kind="sale", items=another["items"], received_yen=500,
            displayed_prices=another["displayed_prices"],
            checkout_business_date=another["client_business_date"])
    with pytest.raises(RecoveryConflict):
        review(admin_dataset, pending, "saved")
    assert RegisterTransaction.query.count() == 1


def test_cross_dataset_fk_and_cutover_blocker(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    another = Dataset(kind="guest", absolute_expires_at=datetime.datetime.now(
        datetime.timezone.utc) + datetime.timedelta(hours=1))
    db.session.add(another)
    db.session.commit()
    with pytest.raises(PermissionError):
        preserve_case(another, receipt(product), actor="test-admin")
    case, _ = preserve_case(admin_dataset, receipt(product), actor="test-admin")
    with pytest.raises(PermissionError):
        review(another, case, "investigating")
    ensure_standard_products(admin_dataset)
    proposed = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    result = audit_cutover(admin_dataset, planned_date=proposed)
    assert result["unresolved_recovery_cases"] == 1
    assert any("未同期会計" in reason for reason in result["blockers"])
    with pytest.raises(CutoverBlocked):
        confirm_cutover(admin_dataset, date=proposed.isoformat(), approved_by="admin",
                        reason="端末と現金を確認して切替", device_inventory="端末Aを確認",
                        devices_checked=True, backup_checked=True)
    assert RegisterCutover.query.count() == 0


def test_http_admin_csrf_flag_and_guest_boundaries(flask_app, authenticated_client,
        admin_dataset, csrf_token):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    data = {"receipt": receipt(product), "device_status": "needs_review"}
    path = "/api/register/recovery/cases"
    token = csrf_token(authenticated_client, "/login")
    assert authenticated_client.post(path, json=data, headers={"X-CSRFToken": token}).status_code == 404
    flask_app.config["REGISTER_V3_RECOVERY_ENABLED"] = True
    assert authenticated_client.post(path, json=data).status_code == 400
    g.pop("_login_user", None)
    assert flask_app.test_client().get("/register/recovery").status_code != 200
    _, visitor = guest(flask_app)
    g.pop("_login_user", None)
    assert visitor.get("/register/recovery").status_code in (302, 403)
    assert visitor.post(path, json=data, headers={"X-CSRFToken": csrf_token(visitor, "/login")}).status_code in (302, 400, 403)
    assert RegisterRecoveryCase.query.count() == 0
    g.pop("_login_user", None)
    response = authenticated_client.post(path, json=data, headers={"X-CSRFToken": token})
    assert response.status_code == 201
    case_id = response.json["case_id"]
    assert authenticated_client.post(path, json=data, headers={"X-CSRFToken": token}).status_code == 200
    assert authenticated_client.get("/register/recovery").status_code == 200
    assert authenticated_client.get(f"/register/recovery/{case_id}").status_code == 200
    assert authenticated_client.post(f"/register/recovery/{case_id}/review",
        data={"target": "held"}).status_code == 400
    detail_token = csrf_token(authenticated_client, f"/register/recovery/{case_id}")
    decision = dict(target="held", reason="現金日報と元の会計時刻を追加調査する",
                    cash_result="unknown", evidence_kind="none",
                    evidence_reference="", confirmed="yes", csrf_token=detail_token)
    assert authenticated_client.post(f"/register/recovery/{case_id}/review",
        data=decision).status_code == 303
    assert RegisterRecoveryCase.query.one().status == "held"
    assert RegisterRecoveryEvent.query.count() == 2
    assert authenticated_client.post(f"/register/recovery/{case_id}/review",
        data=decision).status_code == 409
    assert RegisterRecoveryEvent.query.count() == 2
    assert RegisterTransaction.query.count() == 0


def test_audit_failure_rolls_back_case_state(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    case, _ = preserve_case(admin_dataset, receipt(product), actor="admin")
    def reject(_mapper, _connection, _target):
        if _target.from_status is not None:
            raise RuntimeError("audit failure")
    event.listen(RegisterRecoveryEvent, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="audit failure"):
            review(admin_dataset, case, "investigating")
    finally:
        event.remove(RegisterRecoveryEvent, "before_insert", reject)
    assert RegisterRecoveryCase.query.one().status == "unreviewed"
    assert RegisterRecoveryEvent.query.count() == 1


def test_cutover_review_decisions_are_required_and_recorded(flask_app, admin_dataset):
    ensure_standard_products(admin_dataset)
    proposed = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    db.session.add_all([
        Product(dataset_id=admin_dataset.id, year=proposed.year, month=proposed.month,
                name="旧高額品", price=510),
        RegisterProduct(dataset_id=admin_dataset.id, name="同名別ID", price_yen=120),
        RegisterProduct(dataset_id=admin_dataset.id, name="同名別ID", price_yen=150),
    ])
    db.session.commit()
    result = audit_cutover(admin_dataset, planned_date=proposed)
    assert set(result["review_codes"]) == {"duplicate_names", "expensive_old", "recipe_mapping"}
    params = dict(date=proposed.isoformat(), approved_by="admin", reason="全端末と現金日報を確認",
                  device_inventory="端末A：未同期0件", devices_checked=True, backup_checked=True)
    with pytest.raises(CutoverBlocked, match="要確認項目"):
        confirm_cutover(admin_dataset, **params)
    decisions = {code: f"{code} の商品IDと旧売上、販売方針を個別に確認した"
                 for code in result["review_codes"]}
    confirm_cutover(admin_dataset, **params, review_decisions=decisions)
    assert json.loads(RegisterCutoverApproval.query.one().review_decisions) == decisions


def test_past_day_recovery_requires_signed_price_and_independent_records(flask_app, admin_dataset, monkeypatch):
    from register_service import utc_now
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    day = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date() - datetime.timedelta(days=1)
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=day))
    db.session.commit()
    yesterday = datetime.datetime.combine(day, datetime.time(12), tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(datetime.timezone.utc)
    monkeypatch.setattr("register_catalog_proof.utc_now", lambda: yesterday)
    proof = issue_catalog_proof(admin_dataset, [product])
    monkeypatch.setattr("register_catalog_proof.utc_now", utc_now)
    original = receipt(product, day=day)
    original["catalog_proof"] = proof
    case, _ = preserve_case(admin_dataset, original, actor="test-admin")
    with pytest.raises(RecoveryConflict, match="調査中"):
        confirm_recovery(admin_dataset, case.id, actor="test-admin", reason="会計控えと現金日報を確認した",
            receipt_reference="伝票No.123", cash_reference="現金日報No.5",
            evidence_date=day.isoformat(), confirmed=True)
    review(admin_dataset, case, "held")
    with pytest.raises(RecoveryConflict, match="営業日"):
        confirm_recovery(admin_dataset, case.id, actor="test-admin", reason="会計控えと現金日報を確認した",
            receipt_reference="伝票No.123", cash_reference="現金日報No.5",
            evidence_date=(day + datetime.timedelta(days=1)).isoformat(), confirmed=True)
    update_register_product(admin_dataset, product.id, name="パン", price_yen=150,
                            expected_name="パン", expected_price_yen=120)
    saved, created = confirm_recovery(admin_dataset, case.id, actor="test-admin",
        reason="会計控えの日時・商品・預かりと当日の現金日報を照合し二重計上なし",
        receipt_reference="伝票No.123", cash_reference="現金日報No.5",
        evidence_date=day.isoformat(), confirmed=True)
    assert created and saved.business_date == day and saved.total_yen == 240
    assert saved.save_method == "recovery" and saved.recovery_case_id == case.id
    assert saved.completed_at > saved.client_completed_at
    assert RegisterRecoveryEvent.query.order_by(RegisterRecoveryEvent.id.desc()).first().transaction_id == saved.id
    assert RegisterRecoveryCase.query.one().status == "saved"
    assert RegisterTransaction.query.count() == 1
    assert confirm_recovery(admin_dataset, case.id, actor="test-admin",
        reason="会計控えと現金日報を再確認した", receipt_reference="伝票No.123",
        cash_reference="現金日報No.5", evidence_date=day.isoformat(), confirmed=True)[1] is False
    assert RegisterTransaction.query.count() == 1


def test_recovery_rejects_missing_or_other_dataset_catalog(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    day = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=day))
    db.session.commit()
    case, _ = preserve_case(admin_dataset, receipt(product, day=day), actor="admin")
    review(admin_dataset, case, "held")
    values = dict(actor="test-admin", reason="会計控えと現金日報を照合した",
        receipt_reference="伝票No.123", cash_reference="日報No.123",
        evidence_date=day.isoformat(), confirmed=True)
    with pytest.raises(ValueError, match="商品情報"):
        confirm_recovery(admin_dataset, case.id, **values)
    assert RegisterTransaction.query.count() == 0
    assert case.status == "held"


def test_offline_catalog_is_admin_only_and_flagged(flask_app, authenticated_client, admin_dataset):
    day = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=day))
    db.session.commit()
    flask_app.config.update(REGISTER_V3_PRODUCTION_ENABLED=True,
                            REGISTER_V3_PREVIEW_ENABLED=True, REGISTER_V3_SAVE_ENABLED=True)
    response = authenticated_client.get("/register/preview")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    from bs4 import BeautifulSoup
    configuration = json.loads(BeautifulSoup(response.data, "html.parser").find(
        id="register-config").text)
    assert configuration["catalogProof"] is None
    flask_app.config["REGISTER_V3_OFFLINE_MODE_ENABLED"] = True
    response = authenticated_client.get("/register/preview")
    configuration = json.loads(BeautifulSoup(response.data, "html.parser").find(
        id="register-config").text)
    assert isinstance(configuration["catalogProof"], str)
    assert configuration["scope"] == f"{admin_dataset.id}:admin"
    with pytest.raises(ValueError, match="Dataset"):
        from register_catalog_proof import verify_catalog_proof
        verify_catalog_proof(configuration["catalogProof"], uuid.uuid4(), day, [])


def test_recovery_rejects_tampered_price_and_rolls_back_audit(flask_app, admin_dataset):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    day = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=day))
    db.session.commit()
    original = receipt(product, day=day)
    original["catalog_proof"] = issue_catalog_proof(admin_dataset, [product])
    original["displayed_prices"] = [[product.id, 140]]
    original["snapshots"][0]["unit_price_yen"] = 140
    original["snapshots"][0]["line_total_yen"] = 280
    original.update(total_yen=280, change_yen=220)
    case, _ = preserve_case(admin_dataset, original, actor="admin")
    review(admin_dataset, case, "held")
    params = dict(actor="test-admin", reason="会計控えと現金日報を別々に照合した",
                  receipt_reference="伝票No.123", cash_reference="日報No.123",
                  evidence_date=day.isoformat(), confirmed=True)
    with pytest.raises(ValueError, match="価格"):
        confirm_recovery(admin_dataset, case.id, **params)
    assert RegisterTransaction.query.count() == 0
    assert RegisterRecoveryEvent.query.count() == 2
    original = receipt(product, day=day)
    original["catalog_proof"] = issue_catalog_proof(admin_dataset, [product])
    other, _ = preserve_case(admin_dataset, original, actor="admin")
    review(admin_dataset, other, "held")
    def fail(_mapper, _connection, target):
        if target.transaction_id:
            raise RuntimeError("audit storage failed")
    event.listen(RegisterRecoveryEvent, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="audit storage failed"):
            confirm_recovery(admin_dataset, other.id, **params)
    finally:
        event.remove(RegisterRecoveryEvent, "before_insert", fail)
    assert RegisterTransaction.query.count() == 0
    assert RegisterRecoveryCase.query.filter_by(id=other.id).one().status == "held"


def test_recovery_confirmation_http_requires_admin_flags_and_csrf(
        flask_app, authenticated_client, admin_dataset, csrf_token):
    product = create_custom_product(admin_dataset, name="パン", price_yen=120)
    day = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    db.session.add(RegisterCutover(dataset_id=admin_dataset.id, first_register_date=day))
    db.session.commit()
    original = receipt(product, day=day)
    original["catalog_proof"] = issue_catalog_proof(admin_dataset, [product])
    case, _ = preserve_case(admin_dataset, original, actor="admin")
    review(admin_dataset, case, "held")
    flask_app.config["REGISTER_V3_RECOVERY_ENABLED"] = True
    path = f"/register/recovery/{case.id}/confirm"
    form = dict(reason="会計控えと現金日報の原本を照合した", receipt_reference="伝票No.123",
                cash_reference="現金日報No.5", evidence_date=day.isoformat(), confirmed="yes")
    assert authenticated_client.post(path, data=form).status_code == 400
    token = csrf_token(authenticated_client, f"/register/recovery/{case.id}")
    form["csrf_token"] = token
    assert authenticated_client.post(path, data=form).status_code == 404
    flask_app.config.update(REGISTER_V3_OFFLINE_MODE_ENABLED=True,
                            REGISTER_V3_PRODUCTION_ENABLED=True,
                            REGISTER_V3_PREVIEW_ENABLED=True, REGISTER_V3_SAVE_ENABLED=True)
    assert authenticated_client.post(path, data=form).status_code == 303
    assert RegisterTransaction.query.count() == 1
    assert authenticated_client.post(path, data=form).status_code == 303
    assert RegisterTransaction.query.count() == 1
