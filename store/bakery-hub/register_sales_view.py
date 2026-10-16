"""Phase 3Aの集計契約を既存ダッシュボードの表示契約へ変換する。"""

import calendar
import datetime

from register_sales_aggregation import aggregate_period, aggregate_sales, sales_year_months
from sales_period_summary import _date_label, period_bounds


def selected_dates(year, month):
    if year is None:
        return datetime.date.min, datetime.date.max
    if month is None:
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    return (datetime.date(year, month, 1),
            datetime.date(year, month, calendar.monthrange(year, month)[1]))


def ranked_products(summary):
    """同名異IDは画面・AIでも混同しない。従来の[name, quantity]形を保つ。"""
    products = summary["products"]
    collisions = {}
    for item in products:
        collisions[item["name"]] = collisions.get(item["name"], 0) + 1
    rows = []
    for item in products:
        name = item["name"]
        if collisions[name] > 1:
            source = "旧商品" if item["source"] == "legacy" else "レジ商品"
            name = f"{name}（{source} #{item['product_id']}）"
        rows.append({"key": item["key"], "name": name,
                     "quantity": item["quantity"], "revenue": item["revenue_yen"]})
    return sorted(rows, key=lambda item: (-item["quantity"], item["name"]))


def period_payload(dataset, period, offset, today, read_session):
    data = aggregate_period(dataset, period, offset, today, read_session=read_session)
    current = data["current"]
    start, end = period_bounds(period, offset, today)
    current_end = min(today, end)
    daily = {row["date"]: row["total_yen"] for row in current["days"]}
    if period == "day":
        labels = [f"{start.day}日"]
    elif period == "week":
        labels = ["月", "火", "水", "木", "金", "土", "日"]
    elif period == "month":
        labels = [str(day) for day in range(1, end.day + 1)]
    else:
        labels = [f"{month}月" for month in range(1, 13)]
    values = []
    for index in range(len(labels)):
        if period == "year":
            bucket_start = datetime.date(start.year, index + 1, 1)
            bucket_end = datetime.date(start.year, index + 1,
                                       calendar.monthrange(start.year, index + 1)[1])
            value = sum(amount for day, amount in daily.items()
                        if bucket_start <= day <= bucket_end)
        else:
            bucket_start = start + datetime.timedelta(days=index)
            value = daily.get(bucket_start, 0)
        values.append(value if bucket_start <= current_end else None)
    rankings = ranked_products(current)
    return {
        "period": period, "offset": offset,
        "label": _date_label(start, end),
        "actual_through": _date_label(start, current_end),
        "total_sales": current["total_yen"],
        "previous_sales": data["previous_yen"],
        "difference": data["difference_yen"],
        "comparison_rate": float(data["comparison_rate_percent"])
                           if data["comparison_rate_percent"] is not None else None,
        "comparison_label": ("比較対象なし" if data["previous_yen"] is None else
                             "前期間の同じ経過日数" if offset == 0 and period != "day"
                             else "前期間"),
        "chart_labels": labels, "chart_values": values,
        "rankings": rankings,
        "has_record": current["has_record"],
        "transaction_count": current["transaction_count"],
        "sale_count": current["sale_count"],
        "refund_count": current["refund_count"],
        "average_display_yen": current["average_display_yen"],
        "register_total_yen": current["register_total_yen"],
        "legacy_total_yen": current["legacy_total_yen"],
        "legacy_record_count": current["legacy_record_count"],
        "register_scope_start": current["cutover_date"].isoformat()
                                if current["cutover_date"] else None,
    }


def dashboard_payload(dataset, today, year, month, read_session):
    start, end = selected_dates(year, month)
    selected = aggregate_sales(dataset, start, end, read_session=read_session)
    current = aggregate_sales(dataset, today, today, read_session=read_session)
    candidates = sales_year_months(dataset, datetime.date.min, datetime.date.max,
                                   read_session=read_session)
    ranked = ranked_products(selected)
    years = {today.year, *(candidates["years"])}
    if year is not None:
        years.add(year)
    return {"selected": selected, "today": current, "ranked": ranked,
            "years": sorted(years),
            "months": sorted(month for y, month in candidates["year_months"] if y == year)
                      if year is not None else []}
