"""レジ用の継続商品マスタ。HTTPからは認可済みDatasetだけを渡す。"""

from pathlib import Path
import hashlib
import json
import uuid

from flask import current_app
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models import Dataset, Product, RegisterProduct, db
import product_images


GUEST_CUSTOM_PRODUCT_LIMIT = 30  # 旧月次Productの30件とは独立した枠。


class CustomProductLimitReached(ValueError):
    pass


class ProductSubmissionConflict(ValueError):
    pass


def _submission(owner, request_key, operation, name, price, source_id=None):
    """Datasetロック後に同じ送信の既存結果を返す。変更後もdigestは不変。"""
    if request_key is None:
        return None, None, None
    try:
        key = uuid.UUID(str(request_key))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("送信キーが不正です。") from None
    digest = hashlib.sha256(json.dumps(
        [operation, source_id, name, price], ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    existing = RegisterProduct.query.filter_by(dataset_id=owner.id, creation_key=key).one_or_none()
    if existing is not None and existing.creation_digest != digest:
        raise ProductSubmissionConflict("同じ送信キーで異なる商品を登録できません。")
    return key, digest, existing


def _lock_dataset(dataset):
    """書込とcleanupをDataset単位で直列化し、Guest期限をロック後に確認。"""
    if not isinstance(dataset, Dataset) or dataset.id is None:
        raise PermissionError("認可済みDatasetが必要です。")
    query = Dataset.query.filter(Dataset.id == dataset.id)
    if db.engine.dialect.name == "postgresql":
        owner = query.populate_existing().with_for_update().one_or_none()
    else:
        # SQLiteのFOR UPDATEは無効。値を変えないUPDATEで書込ロックを先に取る。
        db.session.execute(
            sa.update(Dataset).where(Dataset.id == dataset.id)
            .values(last_activity_at=Dataset.last_activity_at)
        )
        owner = query.populate_existing().one_or_none()
    if owner is None or owner.kind not in {"admin", "guest"}:
        raise PermissionError("Datasetを利用できません。")
    if owner.kind == "guest":
        # 現行の絶対期限・無操作30分の判定と同じ関数を使う。
        from app import _guest_dataset_is_expired
        if _guest_dataset_is_expired(owner):
            raise PermissionError("期限切れのGuestです。")
    return owner


def _price(value):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        normalized = value.lstrip("0") or "0"
        if len(normalized) > 3:
            raise ValueError("価格は0〜500円です。")
        result = int(normalized)
    else:
        raise ValueError("価格は整数で入力してください。")
    if not 0 <= result <= 500:
        raise ValueError("価格は0〜500円です。")
    return result


def _name(value):
    if not isinstance(value, str):
        raise ValueError("商品名を入力してください。")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 100:
        raise ValueError("商品名は1〜100文字です。")
    return normalized


def _standard_rows(static_folder):
    """承認カタログを混ぜず、18品の正本だけを使用。画像欠損はNULLへ。"""
    directory = (Path(static_folder) / "product_images").resolve()
    rows = []
    for order, (key, option) in enumerate(product_images.PRODUCT_IMAGE_OPTIONS.items()):
        if not isinstance(key, str) or not product_images._KEY_PATTERN.fullmatch(key):
            raise ValueError("標準商品のキーが不正です。")
        if not isinstance(option, dict):
            raise ValueError("標準商品の定義が不正です。")
        name = _name(option.get("label"))
        price = _price(option.get("default_price"))
        filename = option.get("filename")
        image_key = key if product_images._safe_image_path(directory, filename) else None
        rows.append(dict(template_key=key, name=name, price_yen=price,
                         image_key=image_key, display_order=order, is_active=True))
    return rows


def ensure_standard_products(dataset):
    """不足する標準品だけを一括追加。既存の価格・名称・画像・状態には触れない。"""
    try:
        rows = _standard_rows(current_app.static_folder)
        owner = _lock_dataset(dataset)
        values = [dict(row, dataset_id=owner.id) for row in rows]
        dialect = db.engine.dialect.name
        if dialect == "postgresql":
            statement = pg_insert(RegisterProduct)
        elif dialect == "sqlite":
            statement = sqlite_insert(RegisterProduct)
        else:
            raise RuntimeError("未対応のDBです。")
        count = 0
        if values:
            result = db.session.execute(statement.values(values).on_conflict_do_nothing(
                index_elements=["dataset_id", "template_key"]
            ))
            count = result.rowcount
        db.session.commit()
        return count
    except Exception:
        db.session.rollback()
        raise


def _add_custom(owner, name, price, *, creation_key=None, creation_digest=None):
    if owner.kind == "guest":
        used = RegisterProduct.query.filter_by(dataset_id=owner.id, template_key=None).count()
        if used >= GUEST_CUSTOM_PRODUCT_LIMIT:
            raise CustomProductLimitReached("ゲストのレジ独自商品は30件までです。")
    item = RegisterProduct(dataset_id=owner.id, template_key=None, name=name,
                           price_yen=price, image_key=None, is_active=True,
                           creation_key=creation_key, creation_digest=creation_digest)
    db.session.add(item)
    db.session.commit()
    return item


def create_custom_product(dataset, *, name, price_yen, request_key=None):
    """名前・価格だけで独自商品を作成。旧月次商品の枠は消費しない。"""
    try:
        valid_name, valid_price = _name(name), _price(price_yen)
        owner = _lock_dataset(dataset)
        key, digest, existing = _submission(owner, request_key, "create", valid_name, valid_price)
        if existing is not None:
            db.session.rollback()
            return existing
        return _add_custom(owner, valid_name, valid_price,
                           creation_key=key, creation_digest=digest)
    except Exception:
        db.session.rollback()
        raise


def copy_legacy_product(dataset, product_id, *, confirmed_name, confirmed_price_yen,
                        request_key=None):
    """明示選択した旧商品を別IDの新商品へコピー。元データは不変。"""
    try:
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("旧商品IDが不正です。")
        owner = _lock_dataset(dataset)
        valid_name, valid_price = _name(confirmed_name), _price(confirmed_price_yen)
        key, digest, existing = _submission(owner, request_key, "copy", valid_name,
                                            valid_price, product_id)
        if existing is not None:
            db.session.rollback()
            return existing
        source = Product.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if source is None:
            raise PermissionError("このDatasetの旧商品ではありません。")
        if not source.is_active:
            raise ValueError("販売終了した旧商品はコピーできません。")
        if valid_name != source.name or valid_price != source.price:
            raise ValueError("確認した旧商品の名前または価格が現在の値と異なります。")
        return _add_custom(owner, source.name, source.price,
                           creation_key=key, creation_digest=digest)
    except Exception:
        db.session.rollback()
        raise


def update_register_product(dataset, product_id, *, name, price_yen,
                            expected_name=None, expected_price_yen=None):
    """将来の編集用。必ず所有Dataset内の1件だけを変更する。"""
    try:
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("レジ商品IDが不正です。")
        valid_name, valid_price = _name(name), _price(price_yen)
        owner = _lock_dataset(dataset)
        item = RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id).one_or_none()
        if item is None:
            raise PermissionError("このDatasetの商品ではありません。")
        if expected_name is not None and expected_price_yen is not None:
            # 同じフォームの再送は安全。別画面の編集と衝突したら上書きしない。
            if (item.name, item.price_yen) != (expected_name, expected_price_yen) and (
                    item.name, item.price_yen) != (valid_name, valid_price):
                raise ProductSubmissionConflict("商品情報が変更されています。再読み込みしてください。")
        item.name, item.price_yen = valid_name, valid_price
        db.session.commit()
        return item
    except Exception:
        db.session.rollback()
        raise
