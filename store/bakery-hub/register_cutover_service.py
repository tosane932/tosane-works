"""Adminが確認した切替日をDatasetの書込ロック下で確定する。"""

import datetime
import json
from zoneinfo import ZoneInfo

from models import RegisterCutover, RegisterCutoverApproval, db
from register_cutover_audit import audit_cutover, planned_business_day
from register_products import _lock_dataset


class CutoverBlocked(ValueError):
    pass


def confirm_cutover(dataset, *, date, approved_by, reason, device_inventory,
                    devices_checked=False, backup_checked=False, review_decisions=None):
    """GETの結果に依存せず、全書込経路と同じDatasetロックを取得して再監査する。

    全端末の実態とバックアップはDBでは証明できない。管理者の確認記録を残す。
    """
    day = planned_business_day(date)
    if (type(approved_by) is not str or not approved_by.strip() or len(approved_by) > 100
            or type(reason) is not str or not 5 <= len(reason.strip()) <= 500
            or type(device_inventory) is not str or not 5 <= len(device_inventory.strip()) <= 1000
            or devices_checked is not True or backup_checked is not True):
        raise ValueError("管理者・理由・全端末の確認記録・バックアップ確認が必要です。")
    try:
        owner = _lock_dataset(dataset)
        if owner.kind != "admin":
            raise PermissionError("Admin専用の切替操作です。")
        existing = db.session.get(RegisterCutover, owner.id)
        if existing is not None:
            if existing.first_register_date != day:
                raise CutoverBlocked("既存の切替日を上書きできません。")
            db.session.rollback()
            return existing, False
        # ロック取得後の同一transactionで、他の旧POST・新POSTが確定した結果を読む。
        result = audit_cutover(owner, planned_date=day, today=datetime.datetime.now(
            ZoneInfo("Asia/Tokyo")).date())
        if result["blockers"]:
            raise CutoverBlocked("切替監査で重大な問題があります：" + "／".join(result["blockers"]))
        decisions = review_decisions if review_decisions is not None else {}
        if (type(decisions) is not dict or set(decisions) != set(result["review_codes"])
                or any(type(value) is not str or not 10 <= len(value.strip()) <= 500
                       for value in decisions.values())):
            raise CutoverBlocked("要確認項目ごとの管理者判断と根拠を記録してください。")
        cutover = RegisterCutover(dataset_id=owner.id, first_register_date=day)
        db.session.add(cutover)
        db.session.flush()
        db.session.add(RegisterCutoverApproval(
            dataset_id=owner.id, first_register_date=day,
            approved_by=approved_by.strip(), reason=reason.strip(),
            device_inventory=device_inventory.strip(),
            review_decisions=json.dumps({key: decisions[key].strip() for key in sorted(decisions)},
                                        ensure_ascii=False, sort_keys=True)))
        db.session.commit()
        return cutover, True
    except Exception:
        db.session.rollback()
        raise
