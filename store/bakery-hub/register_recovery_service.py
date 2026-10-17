"""端末会計の永続保全と、独立証拠が揃った例外的な復旧確定。"""
import datetime
import hashlib
import json

from models import (DailySales, Product, RegisterCutover, RegisterProduct, RegisterPriceHistory, RegisterRecoveryCase,
                    RegisterRecoveryEvent, RegisterRecoveryLine, RegisterTransaction,
                    RegisterTransactionLine, db)
from register_catalog_proof import verify_catalog_proof
from register_products import _lock_dataset
from register_service import (_checkout_day, _expected_prices, _normalize, TOKYO,
                              utc_now)


class RecoveryConflict(ValueError):
    pass


STATES = {"unreviewed", "investigating", "held", "saved", "cancelled", "additional"}
TRANSITIONS = {
    "unreviewed": {"investigating", "held", "saved", "additional"},
    "investigating": {"held", "saved", "cancelled", "additional"},
    "held": {"investigating", "saved", "additional"},
    "additional": {"investigating", "held", "saved"},
    "cancelled": {"investigating", "saved"},  # 誤判断が判明したら履歴を残して再調査
    "saved": set(),
}
RECEIPT_KEYS = {"submission_key", "kind", "items", "displayed_prices", "snapshots",
                "total_yen", "received_yen", "change_yen", "refund_confirmed",
                "client_business_date", "completed_at_local"}


def _receipt(value):
    if not isinstance(value, dict) or not (set(value) == RECEIPT_KEYS or
            set(value) == RECEIPT_KEYS | {"catalog_proof"}):
        raise ValueError("元の会計記録の項目が不正です。")
    key, lines, received, digest = _normalize(
        value["submission_key"], value["kind"], value["items"],
        value["received_yen"], value["refund_confirmed"])
    if value["displayed_prices"] is None or len(lines) != len(value["items"]):
        raise ValueError("表示価格または明細数量が不正です。")
    prices = _expected_prices(value["displayed_prices"], lines)
    day = _checkout_day(value["client_business_date"])
    if day is None or not isinstance(value["completed_at_local"], str) or len(value["completed_at_local"]) > 40:
        raise ValueError("端末申告日時が不正です。")
    try:
        claimed_time = datetime.datetime.fromisoformat(value["completed_at_local"].replace("Z", "+00:00"))
        if claimed_time.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError("端末申告日時が不正です。") from None
    if claimed_time.astimezone(TOKYO).date() != day:
        raise ValueError("端末申告時刻と営業日が一致しません。")
    snapshots = value["snapshots"]
    if not isinstance(snapshots, list) or len(snapshots) != len(lines):
        raise ValueError("商品スナップショットが不正です。")
    by_id = {}
    for item in snapshots:
        if (not isinstance(item, dict) or set(item) !=
                {"id", "name", "quantity", "unit_price_yen", "line_total_yen"}):
            raise ValueError("商品スナップショットが不正です。")
        product_id, name, quantity, price, amount = (item[k] for k in
            ("id", "name", "quantity", "unit_price_yen", "line_total_yen"))
        if (type(product_id) is not int or type(name) is not str or not name.strip()
                or len(name) > 100 or type(quantity) is not int or type(price) is not int
                or type(amount) is not int or product_id in by_id):
            raise ValueError("商品スナップショットが不正です。")
        by_id[product_id] = item
    if set(by_id) != {item[0] for item in lines}:
        raise ValueError("商品IDが一致しません。")
    if any(by_id[product_id]["quantity"] != quantity or
           by_id[product_id]["unit_price_yen"] != prices[product_id] or
           by_id[product_id]["line_total_yen"] != quantity * prices[product_id]
           for product_id, quantity in lines):
        raise ValueError("数量・表示単価・金額が一致しません。")
    total = sum(item["line_total_yen"] for item in snapshots)
    if type(value["total_yen"]) is not int or total != value["total_yen"] or total == 0:
        raise ValueError("会計合計が一致しません。")
    if value["kind"] == "sale":
        if (any(quantity < 0 for _, quantity in lines) or value["refund_confirmed"]
                or received is None or received < total or
                type(value["change_yen"]) is not int or value["change_yen"] != received - total):
            raise ValueError("販売と現金の整合性がありません。")
    elif (any(quantity > 0 for _, quantity in lines) or not value["refund_confirmed"]
          or received is not None or value["change_yen"] is not None):
        raise ValueError("返金の整合性がありません。")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 16_384:
        raise ValueError("会計記録が大きすぎます。")
    return key, day, lines, by_id, digest, encoded, hashlib.sha256(encoded.encode()).hexdigest()


def _server_result(owner_id, key, receipt, digest):
    saved = RegisterTransaction.query.filter_by(dataset_id=owner_id, submission_key=key).one_or_none()
    if saved is None:
        return "not_found"  # 未発見は未保存の証明ではない。
    if (saved.request_digest != digest or saved.kind != receipt["kind"] or
            saved.business_date.isoformat() != receipt["client_business_date"] or
            saved.total_yen != receipt["total_yen"] or saved.received_yen != receipt["received_yen"] or
            saved.change_yen != receipt["change_yen"]):
        return "conflict"
    lines = RegisterTransactionLine.query.filter_by(dataset_id=owner_id, transaction_id=saved.id).all()
    actual = {line.register_product_id: (line.quantity, line.unit_price_yen,
              line.product_name_snapshot, line.line_total_yen) for line in lines}
    expected = {line["id"]: (line["quantity"], line["unit_price_yen"], line["name"],
                line["line_total_yen"]) for line in receipt["snapshots"]}
    return "matching" if len(lines) == len(expected) and actual == expected else "conflict"


def _event(case, prior, status, actor, reason, server_result, *, cash_result="unknown",
           evidence_kind="none", evidence_reference="", transaction_id=None,
           adopted_business_date=None):
    receipt = json.loads(case.snapshot_json)
    prices = {item.id: item.price_yen for item in RegisterProduct.query.filter_by(
        dataset_id=case.dataset_id).all()}
    price_conflict = any(prices.get(line["id"]) != line["unit_price_yen"]
                         for line in receipt["snapshots"])
    db.session.add(RegisterRecoveryEvent(
        case_id=case.id, dataset_id=case.dataset_id, from_status=prior, to_status=status,
        actor=actor, reason=reason, server_result=server_result,
        price_conflict=price_conflict,
        day_conflict=case.client_business_date != utc_now().astimezone(TOKYO).date(),
        cash_result=cash_result, evidence_kind=evidence_kind,
        evidence_reference=evidence_reference, transaction_id=transaction_id,
        adopted_business_date=adopted_business_date))


def preserve_case(dataset, receipt, *, actor, device_status="pending"):
    """同一キー・同一内容の再送は同一案件。絶対に売上を追加しない。"""
    key, day, lines, snapshots, digest, encoded, source_digest = _receipt(receipt)
    if type(actor) is not str or not actor.strip() or len(actor) > 100:
        raise ValueError("確認者が不正です。")
    if device_status not in {"pending", "needs_review", "auth_required", "synced"}:
        raise ValueError("端末状態が不正です。")
    try:
        owner = _lock_dataset(dataset)
        if owner.kind != "admin":
            raise PermissionError("Admin専用の案件です。")
        existing = RegisterRecoveryCase.query.populate_existing().filter_by(
            dataset_id=owner.id, submission_key=key).one_or_none()
        if existing:
            if existing.source_digest != source_digest or existing.snapshot_json != encoded:
                raise RecoveryConflict("同じ会計キーに別の元記録が存在します。")
            db.session.commit()
            return existing, False
        products = RegisterProduct.query.filter(
            RegisterProduct.dataset_id == owner.id,
            RegisterProduct.id.in_([product_id for product_id, _ in lines])).all()
        if len(products) != len(lines):
            raise PermissionError("別Datasetまたは存在しない商品IDです。")
        server_result = _server_result(owner.id, key, receipt, digest)
        status = "saved" if server_result == "matching" else "unreviewed"
        case = RegisterRecoveryCase(dataset_id=owner.id, submission_key=key,
            source_digest=source_digest, snapshot_json=encoded, status=status,
            kind=receipt["kind"], client_business_date=day, total_yen=receipt["total_yen"])
        db.session.add(case)
        db.session.flush()
        for item in snapshots.values():
            db.session.add(RegisterRecoveryLine(case_id=case.id, dataset_id=owner.id,
                register_product_id=item["id"], product_name_snapshot=item["name"],
                quantity=item["quantity"], unit_price_yen=item["unit_price_yen"],
                line_total_yen=item["line_total_yen"]))
        _event(case, None, status, actor.strip(), f"端末記録を保全（{device_status}）",
               server_result)
        db.session.commit()
        return case, True
    except Exception:
        db.session.rollback()
        raise


def review_case(dataset, case_id, *, actor, target, reason, cash_result="unknown",
                evidence_kind="none", evidence_reference="", confirmed=False):
    if (target not in STATES or target == "unreviewed" or type(actor) is not str
            or not actor.strip() or len(actor) > 100 or type(reason) is not str
            or not 10 <= len(reason.strip()) <= 500 or cash_result not in
            {"unknown", "matched", "mismatch", "not_applicable"} or
            type(evidence_kind) is not str or len(evidence_kind) > 100 or
            type(evidence_reference) is not str or len(evidence_reference) > 500 or
            confirmed is not True):
        raise ValueError("明示確認、理由、現金照合結果が必要です。")
    try:
        owner = _lock_dataset(dataset)
        if owner.kind != "admin":
            raise PermissionError("Admin専用です。")
        case = RegisterRecoveryCase.query.populate_existing().filter_by(
            id=case_id, dataset_id=owner.id).one_or_none()
        if case is None:
            raise PermissionError("対象Datasetの案件ではありません。")
        if target not in TRANSITIONS[case.status]:
            raise RecoveryConflict("この状態変更は許可されません。最新の案件を確認してください。")
        receipt = json.loads(case.snapshot_json)
        _, _, _, digest = _normalize(case.submission_key, receipt["kind"], receipt["items"],
                                     receipt["received_yen"], receipt["refund_confirmed"])
        server_result = _server_result(owner.id, case.submission_key, receipt, digest)
        if target == "saved" and server_result != "matching":
            raise RecoveryConflict("元記録と一致する確定会計が確認できません。")
        if target != "saved" and server_result == "matching":
            raise RecoveryConflict("一致する確定会計があります。保存済みとして照合してください。")
        if target == "cancelled" and (server_result != "not_found" or
                cash_result == "unknown" or evidence_kind in ("", "none") or
                not evidence_reference.strip()):
            raise RecoveryConflict("取消根拠・現金照合・保存状況の確認が必要です。")
        before = case.status
        case.status = target
        _event(case, before, target, actor.strip(), reason.strip(), server_result,
               cash_result=cash_result, evidence_kind=evidence_kind.strip() or "none",
               evidence_reference=evidence_reference.strip())
        db.session.commit()
        return case
    except Exception:
        db.session.rollback()
        raise


def price_evidence(case):
    """観測済みの価格履歴だけを見せる。端末時刻では有効期間を確定しない。"""
    return {line.register_product_id: RegisterPriceHistory.query.filter_by(
        dataset_id=case.dataset_id, register_product_id=line.register_product_id
    ).order_by(RegisterPriceHistory.recorded_at, RegisterPriceHistory.id).all()
        for line in RegisterRecoveryLine.query.filter_by(
            case_id=case.id, dataset_id=case.dataset_id).all()}


def current_server_result(case):
    """表示時点のDBを読み直す。GETは状態・売上を変更しない。"""
    receipt = json.loads(case.snapshot_json)
    _, _, _, digest = _normalize(case.submission_key, receipt["kind"], receipt["items"],
                                 receipt["received_yen"], receipt["refund_confirmed"])
    return _server_result(case.dataset_id, case.submission_key, receipt, digest)


def confirm_recovery(dataset, case_id, *, actor, reason, receipt_reference,
                     cash_reference, evidence_date, confirmed=False):
    """Exceptional Admin attestation. A signed catalog alone never proves a sale date.

    The Admin must inspect the independent dated receipt and cash daybook outside
    this application. Their references and explicit attestation are durable audit
    evidence; a missing document is a hard stop, never inferred from the device.
    """
    if (confirmed is not True or any(type(v) is not str for v in
            (actor, reason, receipt_reference, cash_reference, evidence_date))
            or not 3 <= len(actor.strip()) <= 100 or not 10 <= len(reason.strip()) <= 500
            or not 6 <= len(receipt_reference.strip()) <= 120
            or not 6 <= len(cash_reference.strip()) <= 120
            or receipt_reference.strip() == cash_reference.strip()):
        raise ValueError("独立した会計控えと現金日報の参照、理由、明示承認が必要です。")
    day = _checkout_day(evidence_date)
    try:
        owner = _lock_dataset(dataset)
        if owner.kind != "admin":
            raise PermissionError("Admin専用です。")
        case = RegisterRecoveryCase.query.populate_existing().filter_by(
            id=case_id, dataset_id=owner.id).one_or_none()
        if case is None:
            raise PermissionError("対象Datasetの案件ではありません。")
        if case.status == "cancelled":
            raise RecoveryConflict("取消確認済み案件は再調査が必要です。")
        receipt = json.loads(case.snapshot_json)
        key, original_day, lines, snapshots, digest, encoded, source_digest = _receipt(receipt)
        if (day != original_day or case.source_digest != source_digest
                or case.snapshot_json != encoded or case.submission_key != key):
            raise RecoveryConflict("元記録と帳簿の営業日が一致しません。")
        result = _server_result(owner.id, key, receipt, digest)
        if result == "conflict":
            raise RecoveryConflict("同じ送信キーの確定会計が一致しません。")
        if result == "matching":
            existing = RegisterTransaction.query.filter_by(dataset_id=owner.id,
                                                             submission_key=key).one()
            if case.status != "saved":
                before = case.status
                case.status = "saved"
                _event(case, before, "saved", actor.strip(), reason.strip(), "matching",
                       cash_result="matched", evidence_kind="会計控え・現金日報",
                       evidence_reference=f"{receipt_reference.strip()} / {cash_reference.strip()}",
                       transaction_id=existing.id, adopted_business_date=day)
                db.session.commit()
            else:
                db.session.commit()
            return existing, False
        if case.status not in {"investigating", "held", "additional"}:
            raise RecoveryConflict("調査中または保留の案件だけ復旧確定できます。")
        now = utc_now()
        if day > now.astimezone(TOKYO).date():
            raise RecoveryConflict("未来の営業日は承認できません。")
        cutover = db.session.get(RegisterCutover, owner.id)
        if cutover is None or day < cutover.first_register_date:
            raise RecoveryConflict("切替日前の会計を新レジ売上に登録できません。")
        if (DailySales.query.join(Product, DailySales.product_id == Product.id)
                .filter(Product.dataset_id == owner.id, DailySales.date == day).first()):
            raise RecoveryConflict("同じ営業日に旧日次売上があります。")
        verify_catalog_proof(receipt.get("catalog_proof"), owner.id, day,
                             receipt["snapshots"])
        products = RegisterProduct.query.filter(
            RegisterProduct.dataset_id == owner.id,
            RegisterProduct.id.in_([product_id for product_id, _ in lines]))
        if db.engine.dialect.name == "postgresql":
            products = products.order_by(RegisterProduct.id).with_for_update()
        if len(products.all()) != len(lines):
            raise RecoveryConflict("商品IDが対象Datasetに存在しません。")
        # The receipt and daybook are independent references; the latter is a
        # human attestation, not a machine assertion that cash actually moved.
        references = json.dumps({"receipt": receipt_reference.strip(),
                                 "cash_daybook": cash_reference.strip()}, ensure_ascii=False)
        evidence_digest = hashlib.sha256(f"{owner.id}|{receipt_reference.strip()}".encode()).hexdigest()
        if RegisterTransaction.query.filter_by(dataset_id=owner.id,
                recovery_evidence_digest=evidence_digest).first():
            raise RecoveryConflict("この会計控えは別の復旧会計に使用されています。")
        client_time = datetime.datetime.fromisoformat(receipt["completed_at_local"].replace("Z", "+00:00"))
        transaction = RegisterTransaction(dataset_id=owner.id, submission_key=key,
            request_digest=digest, completed_at=now, business_date=day,
            kind=receipt["kind"], total_yen=receipt["total_yen"],
            received_yen=receipt["received_yen"], change_yen=receipt["change_yen"],
            save_method="recovery", recovery_case_id=case.id,
            recovery_evidence_digest=evidence_digest, client_completed_at=client_time)
        db.session.add(transaction)
        db.session.flush()
        for product_id, quantity in lines:
            item = snapshots[product_id]
            db.session.add(RegisterTransactionLine(transaction_id=transaction.id,
                dataset_id=owner.id, register_product_id=product_id,
                product_name_snapshot=item["name"], unit_price_yen=item["unit_price_yen"],
                quantity=quantity, line_total_yen=item["line_total_yen"]))
        before = case.status
        case.status = "saved"
        _event(case, before, "saved", actor.strip(), reason.strip(), "matching",
               cash_result="matched", evidence_kind="会計控え・現金日報",
               evidence_reference=references, transaction_id=transaction.id,
               adopted_business_date=day)
        db.session.commit()
        return transaction, True
    except Exception:
        db.session.rollback()
        raise
