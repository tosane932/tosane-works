"""レジ用の継続商品マスタ。HTTPからは認可済みDatasetだけを渡す。"""

from pathlib import Path
import hashlib
import json
import uuid

from flask import current_app
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models import Dataset, Product, RegisterPriceHistory, RegisterProduct, db
import product_images
from register_images import register_image_choices
from product_rules import BREAD_KINDS, PRODUCT_CLASSES, MAX_PRODUCT_PRICE_YEN


GUEST_CUSTOM_PRODUCT_LIMIT = 30  # 旧月次Productの30件とは独立した枠。


class CustomProductLimitReached(ValueError):
    pass


class ProductSubmissionConflict(ValueError):
    pass


def _submission(owner, request_key, operation, name, price, source_id=None, metadata=None):
    """Datasetロック後に同じ送信の既存結果を返す。変更後もdigestは不変。"""
    if request_key is None:
        return None, None, None
    try:
        key = uuid.UUID(str(request_key))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("送信キーが不正です。") from None
    payload = [operation, source_id, name, price]
    if metadata is not None:
        payload.append(metadata)
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False,
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
        if len(normalized) > 6:
            raise ValueError("価格は0〜100,000円です。")
        result = int(normalized)
    else:
        raise ValueError("価格は整数で入力してください。")
    if not 0 <= result <= MAX_PRODUCT_PRICE_YEN:
        raise ValueError("価格は0〜100,000円です。")
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


def ensure_standard_products(dataset, *, commit=True):
    """不足する標準品だけを一括追加。既存の価格・名称・画像・状態には触れない。"""
    try:
        rows = _standard_rows(current_app.static_folder)
        owner = _lock_dataset(dataset)
        if owner.kind == "guest":
            rows = [row for row in rows if product_images.PRODUCT_IMAGE_OPTIONS[
                row["template_key"]].get("guest_allowed") is True]
        before = ({item.id for item in RegisterProduct.query.filter_by(dataset_id=owner.id).all()}
                  if owner.kind == "admin" else set())
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
        # Datasetロックの下で追加分だけを記録する。既存行の過去価格は補完しない。
        if owner.kind == "admin":
            for item in RegisterProduct.query.filter_by(dataset_id=owner.id).all():
                if item.id not in before:
                    db.session.add(RegisterPriceHistory(
                        dataset_id=owner.id, register_product_id=item.id,
                        event="created", new_price_yen=item.price_yen))
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return count
    except Exception:
        db.session.rollback()
        raise


def _add_custom(owner, name, price, *, creation_key=None, creation_digest=None, **attributes):
    if owner.kind == "guest":
        used = RegisterProduct.query.filter_by(dataset_id=owner.id, template_key=None).count()
        if used >= GUEST_CUSTOM_PRODUCT_LIMIT:
            raise CustomProductLimitReached("ゲストのレジ独自商品は30件までです。")
    order = db.session.query(sa.func.max(RegisterProduct.display_order)).filter_by(dataset_id=owner.id).scalar()
    item = RegisterProduct(dataset_id=owner.id, template_key=None, name=name,
                           price_yen=price, is_active=True, display_order=0 if order is None else order + 1,
                           creation_key=creation_key, creation_digest=creation_digest, **attributes)
    db.session.add(item)
    db.session.flush()
    if owner.kind == "admin":
        db.session.add(RegisterPriceHistory(dataset_id=owner.id, register_product_id=item.id,
                                            event="created", new_price_yen=price))
    db.session.commit()
    return item


def _attributes(*, bread_kind, product_class, image_key, photo_webp, is_guest, validate_image_key=True):
    if (type(bread_kind) is not str or type(product_class) is not str
            or bread_kind not in BREAD_KINDS or product_class not in PRODUCT_CLASSES):
        raise ValueError("パンの種類と商品分類を選択してください。")
    if validate_image_key and image_key is not None and (type(image_key) is not str or image_key not in
            register_image_choices(current_app.static_folder, is_guest=is_guest, include_urls=False)):
        raise ValueError("利用できる承認済み画像を選択してください。")
    if photo_webp is not None and ((is_guest and not current_app.config.get("REGISTER_V32_GUEST_ENABLED")) or type(photo_webp) is not bytes
            or not 1 <= len(photo_webp) <= 512000 or image_key is not None):
        raise ValueError("写真の指定が不正です。")
    return dict(bread_kind=bread_kind, product_class=product_class,
                image_key=image_key, photo_webp=photo_webp)


def create_custom_product(dataset, *, name, price_yen, request_key=None,
                          bread_kind="other", product_class="regular",
                          image_key=None, photo_webp=None):
    """共通商品を一度だけ登録する。写真は検証・再符号化済みのbytesだけを渡す。"""
    try:
        valid_name, valid_price = _name(name), _price(price_yen)
        owner = _lock_dataset(dataset)
        attrs = _attributes(bread_kind=bread_kind, product_class=product_class,
                            image_key=image_key, photo_webp=photo_webp, is_guest=owner.kind == "guest")
        metadata = None if (bread_kind, product_class, image_key, photo_webp) == (
            "other", "regular", None, None) else [bread_kind, product_class, image_key,
                hashlib.sha256(photo_webp).hexdigest() if photo_webp else None]
        key, digest, existing = _submission(owner, request_key, "create", valid_name,
                                             valid_price, metadata=metadata)
        if existing is not None:
            db.session.rollback()
            return existing
        return _add_custom(owner, valid_name, valid_price,
                           creation_key=key, creation_digest=digest, **attrs)
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
                            expected_name=None, expected_price_yen=None,
                            expected_revision=None, bread_kind=None, product_class=None,
                            image_key="__keep__", photo_webp=None, is_active=None):
    """同一IDの将来情報だけを更新。版番号で画像・分類・状態を含む競合を防ぐ。"""
    try:
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("商品IDが不正です。")
        valid_name, valid_price = _name(name), _price(price_yen)
        owner = _lock_dataset(dataset)
        item = (RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id)
                .populate_existing().one_or_none())
        if item is None:
            raise PermissionError("このDatasetの商品ではありません。")
        attrs = _attributes(bread_kind=bread_kind if bread_kind is not None else item.bread_kind,
            product_class=product_class if product_class is not None else item.product_class,
            image_key=item.image_key if image_key == "__keep__" and photo_webp is None else
                      (None if photo_webp is not None else image_key),
            photo_webp=photo_webp if photo_webp is not None else
                      (item.photo_webp if image_key == "__keep__" else None),
            is_guest=owner.kind == "guest", validate_image_key=image_key != "__keep__")
        # 既存の承認画像が後で欠損しても、状態や価格の変更は可能にする。
        if is_active is not None and type(is_active) is not bool:
            raise ValueError("販売状態が不正です。")
        desired = dict(attrs, name=valid_name, price_yen=valid_price,
                       is_active=item.is_active if is_active is None else is_active)
        same = all(getattr(item, key) == value for key, value in desired.items())
        if expected_revision is not None:
            if type(expected_revision) is not int or (item.revision != expected_revision and
                    not (same and item.revision == expected_revision + 1)):
                raise ProductSubmissionConflict("商品情報が変更されています。再読み込みしてください。")
        elif expected_name is not None and expected_price_yen is not None:
            if (item.name, item.price_yen) != (expected_name, expected_price_yen) and not same:
                raise ProductSubmissionConflict("商品情報が変更されています。再読み込みしてください。")
        if owner.kind == "admin" and item.price_yen != valid_price:
            db.session.add(RegisterPriceHistory(dataset_id=owner.id, register_product_id=item.id,
                event="changed", old_price_yen=item.price_yen, new_price_yen=valid_price))
        if not same:
            for key, value in desired.items():
                setattr(item, key, value)
            item.revision += 1
        db.session.commit()
        return item
    except Exception:
        db.session.rollback()
        raise


def set_product_active(dataset, product_id, *, is_active, expected_revision):
    """ロック取得後の情報を保ち、状態だけを切り替える。"""
    try:
        owner = _lock_dataset(dataset)
        item = (RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id)
                .populate_existing().one_or_none())
        if item is None:
            raise PermissionError("このDatasetの商品ではありません。")
        return update_register_product(owner, product_id, name=item.name, price_yen=item.price_yen,
                                       is_active=is_active, expected_revision=expected_revision)
    except Exception:
        db.session.rollback()
        raise


def update_register_product_image(dataset, product_id, *, image_key,
                                  expected_name, expected_price_yen, expected_image_key,
                                  expected_revision=None):
    """承認済み画像だけを選び、別画面の名前・価格・画像更新を上書きしない。"""
    try:
        if type(product_id) is not int or product_id <= 0:
            raise ValueError("レジ商品IDが不正です。")
        if image_key is not None and (type(image_key) is not str or len(image_key) > 100):
            raise ValueError("画像の指定が不正です。")
        owner = _lock_dataset(dataset)
        product = (RegisterProduct.query.filter_by(id=product_id, dataset_id=owner.id)
                   .populate_existing().one_or_none())
        if product is None:
            raise PermissionError("このDatasetの商品ではありません。")
        if image_key is not None and image_key not in register_image_choices(
                current_app.static_folder, is_guest=owner.kind == "guest", include_urls=False):
            raise ValueError("利用できる承認済み画像を選択してください。")
        if (product.name, product.price_yen) != (expected_name, expected_price_yen):
            raise ProductSubmissionConflict("商品情報が変更されています。再読み込みしてください。")
        if product.image_key != expected_image_key and product.image_key != image_key:
            raise ProductSubmissionConflict("画像が変更されています。再読み込みしてください。")
        if expected_revision is not None and product.revision != expected_revision and not (
                product.revision == expected_revision + 1 and product.image_key == image_key
                and product.photo_webp is None):
            raise ProductSubmissionConflict("商品情報が変更されています。再読み込みしてください。")
        if product.image_key != image_key or product.photo_webp is not None:
            product.image_key = image_key
            product.photo_webp = None
            product.revision += 1
        db.session.commit()
        return product
    except Exception:
        db.session.rollback()
        raise
