"""管理者の明示操作だけで独自レジ商品と仕込みレシピを結ぶ。"""

from models import PrepRecipe, RegisterPrepLink, RegisterProduct, db
from register_products import _lock_dataset


class MappingConflict(ValueError):
    pass


def set_recipe_link(dataset, recipe_id, product_id, *, expected_product_id):
    """Datasetロック下で現行対応とフォーム表示時の対応を比較する。"""
    if dataset.kind != "admin":
        raise PermissionError("管理者だけが対応を設定できます。")
    if type(recipe_id) is not int or recipe_id <= 0 or (product_id is not None and
            (type(product_id) is not int or product_id <= 0)):
        raise ValueError("商品またはレシピの指定が不正です。")
    try:
        owner = _lock_dataset(dataset)
        recipe = db.session.query(PrepRecipe).filter_by(
            id=recipe_id, dataset_id=owner.id).one_or_none()
        if recipe is None or recipe.product_key.startswith("template:"):
            raise ValueError("このレシピには独自レジ商品を対応できません。")
        existing = db.session.get(RegisterPrepLink, (owner.id, recipe_id))
        prior_id = existing.register_product_id if existing else None
        if prior_id != expected_product_id:
            raise MappingConflict("対応が別画面で変更されています。読み直してください。")
        if product_id is not None:
            product = db.session.query(RegisterProduct).filter_by(
                id=product_id, dataset_id=owner.id, template_key=None).one_or_none()
            if product is None:
                raise ValueError("同じDatasetの独自レジ商品を選んでください。")
            other = db.session.query(RegisterPrepLink).filter_by(
                dataset_id=owner.id, register_product_id=product_id).one_or_none()
            if other is not None and other.recipe_id != recipe_id:
                raise MappingConflict("この商品は別のレシピと対応済みです。")
        if product_id is None and existing is not None:
            db.session.delete(existing)
        elif product_id is not None and existing is None:
            db.session.add(RegisterPrepLink(dataset_id=owner.id, recipe_id=recipe_id,
                                            register_product_id=product_id))
        elif existing is not None:
            existing.register_product_id = product_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
