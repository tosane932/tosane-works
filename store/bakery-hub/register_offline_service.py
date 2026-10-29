"""Admin-approved, bounded offline sales. Device clock is an untrusted claim.

The grant is a risk limit, not cryptographic proof that cash changed hands.
"""
import datetime
import hashlib
import json
import uuid

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from models import (DailySales, Product, RegisterCutover, RegisterOfflinePermit,
                    RegisterProduct, RegisterRecoveryCase, RegisterTransaction,
                    RegisterTransactionLine, db)
from register_catalog_proof import issue_catalog_proof, verify_catalog_proof
from register_products import _lock_dataset
from register_recovery_service import _receipt, _server_result
from register_service import TOKYO, utc_now

MAX_TRANSACTIONS_PER_GRANT = 100
MAX_ABSOLUTE_TOTAL_YEN = 50_000
SYNC_GRACE = datetime.timedelta(hours=48)
GRANT_DURATION = datetime.timedelta(hours=36)


class OfflineReviewRequired(ValueError):
    """Keep the unchanged receipt for an Admin case; never retry with a new key."""


def _signer():
    return URLSafeSerializer(current_app.secret_key, salt="register-v3-offline-permit")


def prepare_offline(dataset, device_id, *, next_day=False):
    try:
        device = uuid.UUID(str(device_id))
        if str(device) != str(device_id):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("端末識別子が不正です。") from None
    if type(next_day) is not bool:
        raise ValueError("次営業日の承認指定が不正です。")
    try:
        owner = _lock_dataset(dataset)
        if owner.kind != "admin":
            raise PermissionError("Admin専用です。")
        day = utc_now().astimezone(TOKYO).date()
        cutover = db.session.get(RegisterCutover, owner.id)
        if cutover is None or day < cutover.first_register_date:
            raise OfflineReviewRequired("切替日より前は端末を準備できません。")
        products = RegisterProduct.query.filter_by(
            dataset_id=owner.id, is_active=True).order_by(RegisterProduct.display_order, RegisterProduct.id).all()
        if not products:
            raise OfflineReviewRequired("販売可能な商品がありません。")
        proof = issue_catalog_proof(owner, products)
        now = utc_now()
        grant = RegisterOfflinePermit(
            dataset_id=owner.id, device_id=str(device), approved_by="admin", business_date=day,
            next_business_date=day + datetime.timedelta(days=1) if next_day else None,
            catalog_digest=hashlib.sha256(proof.encode()).hexdigest(),
            issued_at=now, expires_at=now + GRANT_DURATION)
        db.session.add(grant)
        db.session.flush()
        data = {"id": str(grant.id), "dataset": str(owner.id), "device": str(device),
                "day": day.isoformat(),
                "next_day": grant.next_business_date.isoformat() if next_day else None,
                "issued": now.isoformat(), "expires": grant.expires_at.isoformat(),
                "catalog_digest": grant.catalog_digest}
        token = _signer().dumps(data)
        db.session.commit()
        return grant, token, proof, products
    except Exception:
        db.session.rollback()
        raise


def revoke_offline(dataset, *, commit=True):
    """Online logout or lost-device response. Existing receipts remain intact."""
    try:
        owner = _lock_dataset(dataset)
        now = utc_now()
        for grant in RegisterOfflinePermit.query.filter_by(dataset_id=owner.id, revoked_at=None):
            grant.revoked_at = now
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:
        db.session.rollback()
        raise


def save_offline_transaction(dataset, receipt, grant_token):
    key, day, lines, snapshots, digest, _encoded, _source = _receipt(receipt)
    if type(grant_token) is not str or len(grant_token) > 4096:
        raise OfflineReviewRequired("端末のオフライン許可がありません。")
    try:
        claims = _signer().loads(grant_token)
    except (BadSignature, ValueError):
        raise OfflineReviewRequired("端末のオフライン許可を確認できません。") from None
    try:
        owner = _lock_dataset(dataset)
        if owner.kind != "admin":
            raise PermissionError("Admin専用です。")
        result = _server_result(owner.id, key, receipt, digest)
        if result == "conflict":
            raise OfflineReviewRequired("同じ送信キーの内容が一致しません。")
        if result == "matching":
            saved = RegisterTransaction.query.filter_by(dataset_id=owner.id,
                                                         submission_key=key).one()
            db.session.commit()
            return saved
        if RegisterRecoveryCase.query.filter_by(dataset_id=owner.id, submission_key=key).first():
            raise OfflineReviewRequired("保全済み案件です。管理者が確認してください。")
        grant = db.session.get(RegisterOfflinePermit, uuid.UUID(claims["id"]))
        now = utc_now()
        issued = datetime.datetime.fromisoformat(claims["issued"])
        expires = datetime.datetime.fromisoformat(claims["expires"])
        device = uuid.UUID(claims["device"])
        if (grant is None or grant.dataset_id != owner.id or grant.revoked_at is not None
                or claims["dataset"] != str(owner.id) or grant.device_id != str(device)
                or claims["day"] != grant.business_date.isoformat()
                or claims["next_day"] != (grant.next_business_date.isoformat()
                                           if grant.next_business_date else None)
                or issued != grant.issued_at.replace(tzinfo=datetime.timezone.utc)
                or expires != grant.expires_at.replace(tzinfo=datetime.timezone.utc)
                or now < issued or now > expires + SYNC_GRACE
                or day not in {grant.business_date, grant.next_business_date}
                or day > now.astimezone(TOKYO).date()
                or claims["catalog_digest"] != grant.catalog_digest
                or hashlib.sha256(receipt.get("catalog_proof", "").encode()).hexdigest()
                   != grant.catalog_digest):
            raise OfflineReviewRequired("営業日・端末・期限・商品版の許可を確認できません。")
        client_time = datetime.datetime.fromisoformat(
            receipt["completed_at_local"].replace("Z", "+00:00"))
        if not issued <= client_time <= expires or client_time > now + datetime.timedelta(minutes=5):
            raise OfflineReviewRequired("端末申告時刻が承認期間外です。")
        if abs(receipt["total_yen"]) > MAX_ABSOLUTE_TOTAL_YEN:
            raise OfflineReviewRequired("会計金額が自動受入上限を超えます。")
        if RegisterTransaction.query.filter_by(dataset_id=owner.id,
                                               offline_permit_id=grant.id).count() >= MAX_TRANSACTIONS_PER_GRANT:
            raise OfflineReviewRequired("端末許可の会計件数上限を超えます。")
        cutover = db.session.get(RegisterCutover, owner.id)
        if cutover is None or day < cutover.first_register_date:
            raise OfflineReviewRequired("切替日前の会計です。")
        if (DailySales.query.join(Product, DailySales.product_id == Product.id)
                .filter(Product.dataset_id == owner.id, DailySales.date == day).first()):
            raise OfflineReviewRequired("同じ営業日の旧日次売上が存在します。")
        verify_catalog_proof(receipt.get("catalog_proof"), owner.id, day, receipt["snapshots"])
        products = RegisterProduct.query.filter(
            RegisterProduct.dataset_id == owner.id,
            RegisterProduct.id.in_([product_id for product_id, _ in lines])
        ).order_by(RegisterProduct.id)
        if db.engine.dialect.name == "postgresql":
            products = products.with_for_update()
        if len(products.all()) != len(lines):
            raise OfflineReviewRequired("対象Datasetの商品がありません。")
        tx = RegisterTransaction(
            dataset_id=owner.id, submission_key=key, request_digest=digest,
            completed_at=now, business_date=day, kind=receipt["kind"],
            total_yen=receipt["total_yen"], received_yen=receipt["received_yen"],
            change_yen=receipt["change_yen"], save_method="offline",
            offline_permit_id=grant.id, client_completed_at=client_time)
        db.session.add(tx)
        db.session.flush()
        for product_id, quantity in lines:
            item = snapshots[product_id]
            db.session.add(RegisterTransactionLine(
                transaction_id=tx.id, dataset_id=owner.id, register_product_id=product_id,
                product_name_snapshot=item["name"], unit_price_yen=item["unit_price_yen"],
                quantity=quantity, line_total_yen=item["line_total_yen"]))
        db.session.commit()
        return tx
    except (KeyError, TypeError, ValueError) as error:
        db.session.rollback()
        if isinstance(error, OfflineReviewRequired):
            raise
        raise OfflineReviewRequired("端末許可の形式が不正です。") from error
    except Exception:
        db.session.rollback()
        raise
