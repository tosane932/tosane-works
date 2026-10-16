"""認可済みDatasetの切替前監査。ここから切替行・売上を保存しない。"""

import datetime
import re
from zoneinfo import ZoneInfo

from sqlalchemy import func

from models import (DailySales, PrepRecipe, Product, RegisterCutover,
                    RegisterProduct, RegisterTransaction, RegisterTransactionLine,
                    RegisterPrepLink, RegisterRecoveryCase, db)
from product_images import PRODUCT_IMAGE_OPTIONS
from register_sales_aggregation import _dataset_id


def planned_business_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("切替予定日はYYYY-MM-DDで指定してください。")
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("切替予定日が不正です。") from None
    if not 2000 <= day.year <= 2100:
        raise ValueError("切替予定日は2000～2100年の範囲で指定してください。")
    return day


def audit_cutover(dataset, *, planned_date=None, read_session=None, today=None):
    """DB上の成立条件のみ判定。端末内の未同期件数は取得できない。"""
    if planned_date is not None and type(planned_date) is not datetime.date:
        raise ValueError("切替予定日が不正です。")
    if today is None:
        today = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).date()
    session = read_session if read_session is not None else db.session
    owner_id = _dataset_id(dataset, session)
    old_rows = (session.query(DailySales.date.label("day"))
                .join(Product, DailySales.product_id == Product.id)
                .filter(Product.dataset_id == owner_id).subquery())
    old_count, first_day, last_day = session.query(
        func.count(old_rows.c.day), func.min(old_rows.c.day), func.max(old_rows.c.day)).one()
    new_count = session.query(func.count(RegisterTransaction.id)).filter_by(dataset_id=owner_id).scalar()
    lines_count = session.query(func.count(RegisterTransactionLine.id)).filter_by(dataset_id=owner_id).scalar()
    products = session.query(RegisterProduct).filter_by(dataset_id=owner_id).all()
    old_products = session.query(Product).filter_by(dataset_id=owner_id).all()
    recipes = session.query(PrepRecipe).filter_by(dataset_id=owner_id).all()
    links = session.query(RegisterPrepLink).filter_by(dataset_id=owner_id).all()
    cutover_row = session.get(RegisterCutover, owner_id)
    cutover = cutover_row.first_register_date if cutover_row else None
    old_days = session.query(old_rows.c.day).distinct().subquery()
    new_days = (session.query(RegisterTransaction.business_date.label("day"))
                .filter_by(dataset_id=owner_id).distinct().subquery())
    overlap = [row[0] for row in session.query(old_days.c.day).join(
        new_days, old_days.c.day == new_days.c.day).order_by(old_days.c.day).all()]

    # 明細なし・合計違いを検出し、正常な売上として扱わない。
    mismatch = (session.query(RegisterTransaction.id)
                .outerjoin(RegisterTransactionLine,
                           (RegisterTransactionLine.transaction_id == RegisterTransaction.id) &
                           (RegisterTransactionLine.dataset_id == RegisterTransaction.dataset_id))
                .filter(RegisterTransaction.dataset_id == owner_id)
                .group_by(RegisterTransaction.id, RegisterTransaction.total_yen)
                .having((func.count(RegisterTransactionLine.id) == 0) |
                        (func.coalesce(func.sum(RegisterTransactionLine.line_total_yen), 0)
                         != RegisterTransaction.total_yen)).count())
    boundary = cutover or planned_date
    old_after = new_before = 0
    if boundary is not None:
        old_after = session.query(func.count(old_rows.c.day)).filter(old_rows.c.day >= boundary).scalar()
        new_before = session.query(func.count(RegisterTransaction.id)).filter(
            RegisterTransaction.dataset_id == owner_id,
            RegisterTransaction.business_date < boundary).scalar()
    templates = {item.template_key: item for item in products if item.template_key is not None}
    standards_missing = sorted(set(PRODUCT_IMAGE_OPTIONS) - set(templates))
    standards_inactive = sorted(key for key in PRODUCT_IMAGE_OPTIONS if key in templates
                                and not templates[key].is_active)
    standard_duplicates = sorted(key for key in PRODUCT_IMAGE_OPTIONS
                                 if sum(item.template_key == key for item in products) > 1)
    inactive_register = [{"id": item.id, "name": item.name} for item in products
                         if not item.is_active]
    by_name = {}
    for item in products:
        by_name.setdefault(item.name, []).append(item.id)
    duplicate_names = [{"name": name, "ids": ids} for name, ids in by_name.items()
                       if len(ids) > 1]
    recipe_keys = {recipe.product_key for recipe in recipes}
    linked_custom = {link.register_product_id for link in links}
    unmapped_custom = [{"id": item.id, "name": item.name} for item in products
                       if item.template_key is None and item.id not in linked_custom]
    unmapped_old = [{"id": item.id, "name": item.name} for item in old_products
                    if f"product:{item.id}" not in recipe_keys]
    expensive_old = [{"id": item.id, "name": item.name, "price": item.price}
                     for item in old_products if item.price > 500]
    unresolved = session.query(func.count(RegisterRecoveryCase.id)).filter(
        RegisterRecoveryCase.dataset_id == owner_id,
        RegisterRecoveryCase.status.notin_(("saved", "cancelled"))).scalar()
    blockers = []
    reviews = []
    review_codes = []
    if overlap:
        blockers.append(f"旧売上と新会計が同じ営業日に存在します（{len(overlap)}日）。")
    if mismatch:
        blockers.append(f"会計ヘッダーと明細の合計が一致しない会計が{mismatch}件あります。")
    if standard_duplicates:
        blockers.append("標準商品キーが重複しています。")
    if old_after:
        blockers.append(f"切替日以降の旧売上が{old_after}行あります。")
    if new_before:
        blockers.append(f"切替日前の新会計が{new_before}件あります。")
    if cutover is None and new_count:
        blockers.append("切替日未設定のまま新会計が存在します。")
    if cutover and planned_date and cutover != planned_date:
        blockers.append("既存の切替日と予定日が異なります。自動変更しません。")
    if planned_date and cutover is None and planned_date < today:
        blockers.append("過去の営業日は新しい切替予定日に指定できません。")
    if standards_missing or standards_inactive:
        blockers.append(f"標準18商品の不足{len(standards_missing)}件・無効{len(standards_inactive)}件を解消してください。")
    if inactive_register:
        reviews.append(f"無効なレジ商品{len(inactive_register)}件を確認してください。")
        review_codes.append("inactive_products")
    if duplicate_names:
        reviews.append(f"同名の異なるレジ商品{len(duplicate_names)}組をIDで確認してください。")
        review_codes.append("duplicate_names")
    if not any(item.is_active and item.price_yen > 0 for item in products):
        blockers.append("販売可能な0円超の商品がありません。")
    if expensive_old:
        reviews.append(f"旧商品のうち{len(expensive_old)}件は新レジの500円上限を超えます。")
        review_codes.append("expensive_old")
    if unmapped_old or unmapped_custom:
        reviews.append(f"レシピとの対応未設定：旧商品{len(unmapped_old)}件、独自商品{len(unmapped_custom)}件。")
        review_codes.append("recipe_mapping")
    if unresolved:
        blockers.append(f"未解決の未同期会計復旧案件が{unresolved}件あります。")
    if planned_date is None and cutover is None:
        reviews.append("切替日が未設定です。予定日を入力して事前検証してください。")
    return {
        "status": "blocked" if blockers else "review" if reviews else "ready",
        "blockers": blockers, "reviews": reviews,
        "review_codes": review_codes, "unresolved_recovery_cases": int(unresolved),
        "counts": {"old_products": len(old_products), "old_sales": int(old_count),
                   "register_products": len(products), "transactions": int(new_count),
                   "lines": int(lines_count)},
        "old_first_day": first_day, "old_last_day": last_day,
        "cutover": cutover, "planned_date": planned_date,
        "overlap_days": overlap, "header_line_mismatch": mismatch,
        "old_after": int(old_after), "new_before": int(new_before),
        "missing_standard": standards_missing, "inactive_standard": standards_inactive,
        "duplicate_standard": standard_duplicates, "inactive_register": inactive_register,
        "duplicate_names": duplicate_names,
        "expensive_old": expensive_old, "unmapped_old": unmapped_old,
        "unmapped_custom": unmapped_custom,
        "device_queue_known": False,
    }
