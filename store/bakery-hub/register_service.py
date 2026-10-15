"""オンライン会計の保存。HTTP層は認可済みDatasetと会計ごとのUUIDを渡す。"""

import datetime
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from models import (DailySales, Product, RegisterCutover, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, db)
from register_products import _lock_dataset


TOKYO = ZoneInfo("Asia/Tokyo")
MAX_ITEMS = 50
MAX_REQUEST_BYTES = 16_384
MAX_RECEIVED_YEN = 1_000_000_000
GUEST_TRANSACTION_LIMIT = 1_000


class SubmissionConflict(ValueError):
    """同じ送信キーに異なる会計内容が使用された。"""


class GuestTransactionLimitReached(ValueError):
    """体験版の会計保存上限に達した。"""


def new_submission_key():
    """会計開始時に一度だけ発行し、通信断時にも同じ値を再送する。"""
    return uuid.uuid4()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _integer(value, label, *, maximum):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        number = int(value)
    else:
        raise ValueError(f"{label}は整数で入力してください。")
    if not 0 <= number <= maximum:
        raise ValueError(f"{label}が範囲外です。")
    return number


def _quantity(value):
    if type(value) is int:
        number = value
    elif (isinstance(value, str) and value.isascii() and len(value) <= 3
          and (value.isdigit() or (value.startswith("-") and value[1:].isdigit()))):
        number = int(value)
    else:
        raise ValueError("数量は整数で入力してください。")
    if not -50 <= number <= 50:
        raise ValueError("数量は商品ごとに±50個までです。")
    return number


def _normalize(submission_key, kind, items, received_yen, refund_confirmed):
    if isinstance(submission_key, uuid.UUID):
        key = submission_key
    elif isinstance(submission_key, str) and len(submission_key) == 36:
        try:
            key = uuid.UUID(submission_key)
        except ValueError as exc:
            raise ValueError("会計送信キーが不正です。") from exc
        if str(key) != submission_key.lower():
            raise ValueError("会計送信キーが不正です。")
    else:
        raise ValueError("会計送信キーが不正です。")
    if kind not in ("sale", "refund") or type(kind) is not str:
        raise ValueError("販売・返金区分が不正です。")
    if not isinstance(items, (tuple, list)) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("商品件数が不正です。")
    normalized = []
    seen = set()
    for entry in items:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("商品明細が不正です。")
        product_id = _integer(entry[0], "商品ID", maximum=2_147_483_647)
        if not product_id or product_id in seen:
            raise ValueError("商品IDの重複または不正な指定です。")
        seen.add(product_id)
        quantity = _quantity(entry[1])
        if quantity:
            normalized.append((product_id, quantity))
    if not normalized:
        raise ValueError("商品を選択してください。")
    if type(refund_confirmed) is not bool:
        raise ValueError("返金確認情報が不正です。")
    received = None if received_yen is None else _integer(
        received_yen, "お預かり金額", maximum=MAX_RECEIVED_YEN)
    normalized.sort()
    payload = {"kind": kind, "items": normalized, "received_yen": received,
               "refund_confirmed": refund_confirmed}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("会計データが大きすぎます。")
    return key, normalized, received, hashlib.sha256(encoded).hexdigest()


def _existing(owner_id, key, digest):
    transaction = RegisterTransaction.query.filter_by(
        dataset_id=owner_id, submission_key=key).one_or_none()
    if transaction is not None and transaction.request_digest != digest:
        raise SubmissionConflict("同じ会計送信キーに異なる内容が送られました。")
    return transaction


def _reject_zero_total(total):
    """0円会計は暫定拒否。商品マスタの0円許容とは別の可変ルール。"""
    if total == 0:
        raise ValueError("0円会計の保存は現在できません。")


def save_register_transaction(dataset, *, submission_key, kind, items,
                              received_yen=None, refund_confirmed=False):
    """商品価格をDBから再取得し、1会計と全明細を同一トランザクションで確定する。

    items は (商品ID, 数量) の列。時刻・価格・合計額はクライアントから受け取らない。
    呼出側は認可済みDatasetを渡し、同じ会計の再送には同じsubmission_keyを使う。
    """
    try:
        key, lines, received, digest = _normalize(
            submission_key, kind, items, received_yen, refund_confirmed)
        owner = _lock_dataset(dataset)
        previous = _existing(owner.id, key, digest)
        if previous is not None:
            db.session.commit()
            return previous

        now = utc_now()
        business_date = now.astimezone(TOKYO).date()
        cutover = db.session.get(RegisterCutover, owner.id)
        if cutover is None or business_date < cutover.first_register_date:
            raise ValueError("レジ会計の集計切替が未設定、または切替日前です。")
        # 切替日に旧入力が残っている場合、新レジの保存を始めず運用者に解決を促す。
        old_sales = (DailySales.query.join(Product, DailySales.product_id == Product.id)
                     .filter(Product.dataset_id == owner.id,
                             DailySales.date == business_date).first())
        if old_sales is not None:
            raise ValueError("同じ営業日に旧日次売上があります。")
        if owner.kind == "guest" and RegisterTransaction.query.filter_by(
                dataset_id=owner.id).count() >= GUEST_TRANSACTION_LIMIT:
            raise GuestTransactionLimitReached("体験版の会計保存上限に達しました。")

        if kind == "sale":
            if any(quantity < 0 for _, quantity in lines) or refund_confirmed:
                raise ValueError("販売と返金は別会計にしてください。")
            if received is None:
                raise ValueError("お預かり金額を入力してください。")
        else:
            if any(quantity > 0 for _, quantity in lines):
                raise ValueError("販売と返金は別会計にしてください。")
            if received is not None or not refund_confirmed:
                raise ValueError("返金確認が必要です。返金にはお預かり金額を指定しません。")

        products = (RegisterProduct.query.filter(
            RegisterProduct.dataset_id == owner.id,
            RegisterProduct.id.in_([product_id for product_id, _ in lines]),
            RegisterProduct.is_active.is_(True)).order_by(RegisterProduct.id))
        if db.engine.dialect.name == "postgresql":
            products = products.with_for_update()
        by_id = {product.id: product for product in products.all()}
        if len(by_id) != len(lines):
            raise PermissionError("現在のDatasetで販売可能な商品ではありません。")
        snapshots = [(product, quantity, product.price_yen * quantity)
                     for product_id, quantity in lines
                     for product in [by_id[product_id]]]
        total = sum(amount for _, _, amount in snapshots)
        _reject_zero_total(total)
        if not -MAX_RECEIVED_YEN <= total <= MAX_RECEIVED_YEN:
            raise ValueError("会計金額が範囲外です。")
        if kind == "sale":
            if received < total:
                raise ValueError("お預かり金額が不足しています。")
            change = received - total
        else:
            change = None

        transaction = RegisterTransaction(
            dataset_id=owner.id, submission_key=key, request_digest=digest,
            completed_at=now, business_date=business_date, kind=kind,
            total_yen=total, received_yen=received, change_yen=change)
        db.session.add(transaction)
        db.session.flush()
        for product, quantity, amount in snapshots:
            db.session.add(RegisterTransactionLine(
                transaction_id=transaction.id, dataset_id=owner.id,
                register_product_id=product.id, product_name_snapshot=product.name,
                unit_price_yen=product.price_yen, quantity=quantity,
                line_total_yen=amount))
        db.session.commit()
        return transaction
    except IntegrityError:
        db.session.rollback()
        # UNIQUE(dataset_id, submission_key) is the final barrier for concurrent POSTs.
        # After a race, re-read in a fresh transaction and distinguish a different DB failure.
        if 'key' in locals() and 'digest' in locals():
            try:
                owner = _lock_dataset(dataset)
                previous = _existing(owner.id, key, digest)
                db.session.commit()
                if previous is not None:
                    return previous
            except Exception:
                db.session.rollback()
                raise
        raise
    except Exception:
        db.session.rollback()
        raise
