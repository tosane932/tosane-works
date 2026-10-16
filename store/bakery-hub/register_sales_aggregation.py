"""Dataset・営業日で旧日次売上と確定済みレジ会計を排他的に読む。

公開画面への接続はPhase 3B。呼び出し元は認証済みDatasetを渡す。
切替規則に反する行が対象期間にあれば、部分的な正常値を返さない。
"""

import calendar
import datetime
from decimal import Decimal, ROUND_HALF_UP

from models import (DailySales, Dataset, Product, RegisterCutover,
                    RegisterTransaction, RegisterTransactionLine, db)
from sales_period_summary import PERIOD_OFFSETS, period_bounds


class SalesCutoverIntegrityError(RuntimeError):
    """集計対象期間に切替規則と矛盾する保存済みデータがある。"""

    def __init__(self, legacy_count, register_count):
        self.legacy_count = legacy_count
        self.register_count = register_count
        super().__init__(f"切替条件違反: 旧売上{legacy_count}行、レジ会計{register_count}件")


class SalesAggregationConsistencyError(RuntimeError):
    """会計ヘッダーと明細、または複数読取間の合計が一致しない。"""


def _dataset_id(dataset):
    if not isinstance(dataset, Dataset) or dataset.id is None:
        raise PermissionError("認可済みDatasetが必要です")
    owner = db.session.get(Dataset, dataset.id)
    if owner is None or owner.kind != dataset.kind:
        raise PermissionError("Datasetが利用できません")
    if owner.kind == "guest":
        from app import _guest_dataset_is_expired
        if _guest_dataset_is_expired(owner):
            raise PermissionError("Guestの利用期限が終了しています")
    return owner.id


def _valid_dates(start, end):
    if type(start) is not datetime.date or type(end) is not datetime.date or start > end:
        raise ValueError("有効な営業日の範囲を指定してください")


def _filters(dataset_id, start, end, cutover):
    old = [Product.dataset_id == dataset_id, DailySales.date >= start,
           DailySales.date <= end]
    new = [RegisterTransaction.dataset_id == dataset_id,
           RegisterTransaction.business_date >= start,
           RegisterTransaction.business_date <= end]
    if cutover is not None:
        old.append(DailySales.date < cutover)
        new.append(RegisterTransaction.business_date >= cutover)
    return old, new


def _check_cutover(dataset_id, start, end, cutover):
    old_bad = 0
    if cutover is not None:
        old_bad = (db.session.query(db.func.count(DailySales.id))
                   .join(Product, Product.id == DailySales.product_id)
                   .filter(Product.dataset_id == dataset_id,
                           DailySales.date >= start, DailySales.date <= end,
                           DailySales.date >= cutover).scalar())
    new_bad = (db.session.query(db.func.count(RegisterTransaction.id))
               .filter(RegisterTransaction.dataset_id == dataset_id,
                       RegisterTransaction.business_date >= start,
                       RegisterTransaction.business_date <= end,
                       *((RegisterTransaction.business_date < cutover,)
                         if cutover is not None else ())).scalar())
    if old_bad or new_bad:
        raise SalesCutoverIntegrityError(int(old_bad), int(new_bad))


def aggregate_sales(dataset, start, end):
    """両端を含む営業日範囲を集計。未記録の日はdaysに含めない。

    productsは系統＋IDをキーにし、レジ改名前後の名称はsnapshotsに保持。
    transaction_count=Noneは旧データのみで件数を算出できないことを表す。
    """
    _valid_dates(start, end)
    dataset_id = _dataset_id(dataset)
    cutover_row = db.session.get(RegisterCutover, dataset_id)
    cutover = cutover_row.first_register_date if cutover_row else None
    _check_cutover(dataset_id, start, end, cutover)
    old, new = _filters(dataset_id, start, end, cutover)

    days = {}
    products = {}
    old_amount = register_amount = old_records = transaction_count = sales = refunds = 0
    old_total = db.func.sum(db.cast(Product.price, db.BigInteger) * DailySales.quantity)
    for day, amount, count in (db.session.query(DailySales.date, old_total,
                                                db.func.count(DailySales.id))
                               .join(Product, Product.id == DailySales.product_id)
                               .filter(*old).group_by(DailySales.date).all()):
        days[day] = {"date": day, "total_yen": int(amount),
                     "has_record": True, "legacy_records": int(count),
                     "transaction_count": 0}
        old_amount += int(amount)
        old_records += int(count)

    sale_count = db.func.sum(db.case((RegisterTransaction.kind == "sale", 1), else_=0))
    refund_count = db.func.sum(db.case((RegisterTransaction.kind == "refund", 1), else_=0))
    for day, amount, count, s_count, r_count in (
        db.session.query(RegisterTransaction.business_date,
                         db.func.sum(RegisterTransaction.total_yen),
                         db.func.count(RegisterTransaction.id), sale_count, refund_count)
        .filter(*new).group_by(RegisterTransaction.business_date).all()
    ):
        # ヘッダーだけを集約し、明細とのJOINによる件数・金額の増幅を防ぐ。
        if day in days:
            raise SalesCutoverIntegrityError(days[day]["legacy_records"], int(count))
        days[day] = {"date": day, "total_yen": int(amount),
                     "has_record": True, "legacy_records": 0,
                     "transaction_count": int(count)}
        register_amount += int(amount)
        transaction_count += int(count)
        sales += int(s_count)
        refunds += int(r_count)

    old_product_total = db.func.sum(db.cast(Product.price, db.BigInteger) * DailySales.quantity)
    for product_id, name, quantity, amount in (
        db.session.query(Product.id, Product.name, db.func.sum(DailySales.quantity),
                         old_product_total)
        .join(DailySales, DailySales.product_id == Product.id)
        .filter(*old).group_by(Product.id, Product.name).all()
    ):
        products[f"legacy:{product_id}"] = {
            "key": f"legacy:{product_id}", "source": "legacy", "product_id": product_id,
            "name": name, "quantity": int(quantity), "revenue_yen": int(amount),
            "snapshots": [],
        }

    # 商品マスタの現行価格・名称ではなく明細スナップショットで集計する。
    for product_id, name, quantity, amount in (
        db.session.query(RegisterTransactionLine.register_product_id,
                         RegisterTransactionLine.product_name_snapshot,
                         db.func.sum(RegisterTransactionLine.quantity),
                         db.func.sum(RegisterTransactionLine.line_total_yen))
        .join(RegisterTransaction,
              db.and_(RegisterTransaction.id == RegisterTransactionLine.transaction_id,
                      RegisterTransaction.dataset_id == RegisterTransactionLine.dataset_id))
        .filter(*new, RegisterTransactionLine.dataset_id == dataset_id)
        .group_by(RegisterTransactionLine.register_product_id,
                  RegisterTransactionLine.product_name_snapshot).all()
    ):
        key = f"register:{product_id}"
        item = products.setdefault(key, {
            "key": key, "source": "register", "product_id": product_id,
            "name": name, "quantity": 0, "revenue_yen": 0, "snapshots": [],
        })
        item["quantity"] += int(quantity)
        item["revenue_yen"] += int(amount)
        item["snapshots"].append({"name": name, "quantity": int(quantity),
                                  "revenue_yen": int(amount)})

    for item in products.values():
        item["snapshots"].sort(key=lambda row: row["name"])
        if item["source"] == "register":
            item["name"] = item["snapshots"][0]["name"]

    if (sum(row["revenue_yen"] for row in products.values() if row["source"] == "legacy") != old_amount
        or sum(row["revenue_yen"] for row in products.values() if row["source"] == "register") != register_amount):
        raise SalesAggregationConsistencyError("日別売上と商品別売上が一致しません")
    # 読み取り途中に切替違反行が確定した場合も、可能な範囲で検出する。
    _check_cutover(dataset_id, start, end, cutover)

    average = (Decimal(register_amount) / Decimal(transaction_count)
               if transaction_count else None)
    return {
        "start": start, "end": end, "cutover_date": cutover,
        "total_yen": old_amount + register_amount, "has_record": bool(days),
        "legacy_total_yen": old_amount, "legacy_record_count": old_records,
        "register_total_yen": register_amount,
        "transaction_count": transaction_count if cutover is not None and
                             end >= cutover else None,
        "sale_count": sales if cutover is not None and end >= cutover else None,
        "refund_count": refunds if cutover is not None and end >= cutover else None,
        "average_yen": average,
        "average_display_yen": int(average.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                               if average is not None else None,
        "days": [days[day] for day in sorted(days)],
        "products": sorted(products.values(), key=lambda row: row["key"]),
    }


def _previous_bounds(period, offset, today, start, end):
    if offset - 1 < -PERIOD_OFFSETS[period]:
        return None, None
    prev_start, prev_end = period_bounds(period, offset - 1, today)
    if offset == 0 and period == "year":
        day = min(today.day, calendar.monthrange(prev_start.year, today.month)[1])
        return prev_start, datetime.date(prev_start.year, today.month, day)
    if offset == 0:
        return prev_start, min(prev_end, prev_start + (min(today, end) - start))
    return prev_start, prev_end


def aggregate_period(dataset, period, offset, today):
    """既存period_boundsの暦・進行中期間規則を用いる比較契約。"""
    if type(today) is not datetime.date:
        raise ValueError("有効な営業日が必要です")
    start, end = period_bounds(period, offset, today)
    current = aggregate_sales(dataset, start, min(today, end))
    prev_start, prev_end = _previous_bounds(period, offset, today, start, end)
    previous = (aggregate_sales(dataset, prev_start, prev_end)
                if prev_start is not None else None)
    previous_amount = (previous["total_yen"] if previous and previous["has_record"]
                       else None)
    difference = current["total_yen"] - previous_amount if previous_amount is not None else None
    rate = (Decimal(difference) * 100 / Decimal(previous_amount)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP) if previous_amount is not None and previous_amount > 0 else None
    return {"period": period, "offset": offset, "current": current,
            "previous": previous, "previous_yen": previous_amount,
            "difference_yen": difference, "comparison_rate_percent": rate}


def sales_year_months(dataset, start, end):
    """記録がある年・年月だけを返す。0円の日も候補に含める。"""
    _valid_dates(start, end)
    dataset_id = _dataset_id(dataset)
    cutover_row = db.session.get(RegisterCutover, dataset_id)
    cutover = cutover_row.first_register_date if cutover_row else None
    _check_cutover(dataset_id, start, end, cutover)
    old, new = _filters(dataset_id, start, end, cutover)
    old_dates = (db.session.query(DailySales.date).join(Product, Product.id == DailySales.product_id)
                 .filter(*old).distinct().all())
    new_dates = (db.session.query(RegisterTransaction.business_date)
                 .filter(*new).distinct().all())
    dates = [row[0] for row in old_dates + new_dates]
    return {"years": sorted({day.year for day in dates}, reverse=True),
            "year_months": sorted({(day.year, day.month) for day in dates}, reverse=True)}
