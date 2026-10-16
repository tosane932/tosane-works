"""仕込み画面用の読取専用販売実績。製造予定・材料への書込みは行わない。"""

from models import RegisterProduct
from register_sales_aggregation import aggregate_sales


def prep_sales_reference(dataset, recipes, day, *, read_session):
    """系統と商品IDだけでレシピに対応付ける。未記録と純数量0を区別する。"""
    summary = aggregate_sales(dataset, day, day, read_session=read_session)
    cutover = summary["cutover_date"]
    register_day = cutover is not None and day >= cutover
    products = {item["key"]: item for item in summary["products"]}
    standard = {}
    if register_day:
        standard = {item.template_key: item.id for item in
                    read_session.query(RegisterProduct).filter_by(dataset_id=dataset.id).all()
                    if item.template_key is not None}
    references = {}
    for recipe in recipes:
        source = recipe.product_key
        if source.startswith("template:") and register_day:
            product_id = standard.get(source[len("template:"):])
            key = f"register:{product_id}" if product_id is not None else None
        elif source.startswith("product:") and not register_day:
            key = "legacy:" + source[len("product:"):]
        else:
            key = None
        if key is None:
            references[recipe.id] = {"status": "unlinked", "quantity": None}
        elif not summary["has_record"]:
            references[recipe.id] = {"status": "unrecorded", "quantity": None}
        else:
            references[recipe.id] = {"status": "recorded", "quantity":
                                     products[key]["quantity"] if key in products else 0}
    unmatched = []
    if register_day:
        for item in read_session.query(RegisterProduct).filter_by(dataset_id=dataset.id,
                                                                  template_key=None).all():
            sold = products.get(f"register:{item.id}")
            if sold is not None:
                unmatched.append({"name": sold["name"], "id": item.id,
                                  "quantity": sold["quantity"]})
    return {"references": references, "unmatched": unmatched,
            "has_record": summary["has_record"]}
