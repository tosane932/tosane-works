"""日・週・月・年の売上サマリー。既存DailySalesを読み取り専用で集計する。"""

import calendar
import datetime

from models import DailySales, Product, db


PERIOD_OFFSETS = {"day": 366, "week": 104, "month": 120, "year": 10}


def period_bounds(period, offset, today):
    """月曜始まりの週と暦月・暦年で、選択期間の両端を返す。"""
    if period not in PERIOD_OFFSETS:
        raise ValueError("unknown period")
    if not isinstance(offset, int) or not -PERIOD_OFFSETS[period] <= offset <= 0:
        raise ValueError("invalid offset")

    if period == "day":
        start = today + datetime.timedelta(days=offset)
        end = start
    elif period == "week":
        start = today - datetime.timedelta(days=today.weekday())
        start += datetime.timedelta(weeks=offset)
        end = start + datetime.timedelta(days=6)
    elif period == "month":
        absolute_month = (today.year - 1) * 12 + today.month - 1 + offset
        year, zero_month = divmod(absolute_month, 12)
        year += 1
        month = zero_month + 1
        start = datetime.date(year, month, 1)
        end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    else:
        year = today.year + offset
        start = datetime.date(year, 1, 1)
        end = datetime.date(year, 12, 31)
    return start, end


def _date_label(start, end):
    if start == end:
        return start.strftime("%Y年%m月%d日")
    return f"{start:%Y年%m月%d日}〜{end:%m月%d日}"


def _sales_rows(dataset_id, start, end):
    amount = db.func.sum(
        db.cast(Product.price, db.BigInteger) * DailySales.quantity
    )
    quantity = db.func.sum(DailySales.quantity)
    return (
        db.session.query(DailySales.date, Product.name, quantity, amount)
        .join(Product, DailySales.product_id == Product.id)
        .filter(
            Product.dataset_id == dataset_id,
            DailySales.date >= start,
            DailySales.date <= end,
        )
        .group_by(DailySales.date, Product.name)
        .all()
    )


def _total_amount(dataset_id, start, end):
    value = (
        db.session.query(
            db.func.coalesce(
                db.func.sum(
                    db.cast(Product.price, db.BigInteger) * DailySales.quantity
                ),
                0,
            )
        )
        .select_from(Product)
        .join(DailySales, Product.id == DailySales.product_id)
        .filter(
            Product.dataset_id == dataset_id,
            DailySales.date >= start,
            DailySales.date <= end,
        )
        .scalar()
    )
    return int(value)


def sales_period_summary(dataset_id, period, offset, today):
    """Dataset別に売上金額、同期間比較、日付別推移、商品別ランキングを返す。"""
    start, end = period_bounds(period, offset, today)
    prev_start, prev_end = period_bounds(period, offset - 1, today) if (
        offset - 1 >= -PERIOD_OFFSETS[period]
    ) else (None, None)

    # 進行中の期間は本日まで。先週等とは同じ経過日数で比較する。
    current_end = min(today, end)
    if prev_start is None:
        comparison_end = None
    elif offset == 0 and period == "year":
        # 年比較はうるう年でも「前年の同月同日」までにそろえる。
        prev_day = min(today.day, calendar.monthrange(prev_start.year, today.month)[1])
        comparison_end = datetime.date(prev_start.year, today.month, prev_day)
    elif offset == 0:
        comparison_end = min(
            prev_end, prev_start + (current_end - start)
        )
    else:
        comparison_end = prev_end

    rows = _sales_rows(dataset_id, start, current_end)
    by_product = {}
    by_bucket = {}

    for sale_date, name, count, amount in rows:
        count = int(count)
        amount = int(amount)
        item = by_product.setdefault(name, {"name": name, "quantity": 0, "revenue": 0})
        item["quantity"] += count
        item["revenue"] += amount

        if period == "day":
            key = 0
        elif period == "week":
            key = (sale_date - start).days
        elif period == "month":
            key = sale_date.day - 1
        else:
            key = sale_date.month - 1
        by_bucket[key] = by_bucket.get(key, 0) + amount

    if period == "day":
        chart_labels = [str(start.day) + "日"]
    elif period == "week":
        chart_labels = ["月", "火", "水", "木", "金", "土", "日"]
    elif period == "month":
        chart_labels = [str(day) for day in range(1, end.day + 1)]
    else:
        chart_labels = [f"{month}月" for month in range(1, 13)]

    def bucket_date(index):
        if period == "day":
            return start
        if period == "week":
            return start + datetime.timedelta(days=index)
        if period == "month":
            return start + datetime.timedelta(days=index)
        return datetime.date(start.year, index + 1, 1)

    values = [
        by_bucket.get(index, 0) if bucket_date(index) <= current_end else None
        for index in range(len(chart_labels))
    ]
    total = sum(row["revenue"] for row in by_product.values())
    previous = (
        _total_amount(dataset_id, prev_start, comparison_end)
        if prev_start is not None and comparison_end is not None else None
    )
    delta = total - previous if previous is not None else None

    return {
        "period": period,
        "offset": offset,
        "label": _date_label(start, end),
        "actual_through": _date_label(start, current_end),
        "total_sales": total,
        "previous_sales": previous,
        "difference": delta,
        "comparison_rate": (
            round(delta / previous * 100, 1) if previous else None
        ),
        "comparison_label": (
            ("前期間の同じ経過日数" if offset == 0 and period != "day" else "前期間")
            if previous is not None else "比較対象なし"
        ),
        "chart_labels": chart_labels,
        "chart_values": values,
        "rankings": sorted(
            by_product.values(),
            key=lambda item: (-item["quantity"], item["name"]),
        ),
    }
