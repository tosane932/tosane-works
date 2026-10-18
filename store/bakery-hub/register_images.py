"""承認済みレジ商品画像の安全なURL解決。標準品は軽量WebPだけを使う。"""

from pathlib import Path

from flask import url_for

from product_images import (PRODUCT_IMAGE_OPTIONS, _safe_image_path,
                            available_product_image_assets)


MAX_REGISTER_ADMIN_IMAGE_BYTES = 500 * 1024


def register_image_choices(static_folder, *, is_guest, include_urls=True):
    """キー、表示名、URLの対応を返す。カタログ外・欠損・過大な画像は返さない。"""
    root = Path(static_folder)
    original_root = (root / "product_images").resolve()
    register_root = (root / "register_images").resolve()
    approved = available_product_image_assets(static_folder, is_guest=is_guest)
    choices = {}
    for key, option in PRODUCT_IMAGE_OPTIONS.items():
        if key not in approved:
            continue
        image = _safe_image_path(register_root, f"{key}.webp")
        if image is not None:
            choices[key] = {"label": option["label"],
                            "url": url_for("static", filename=f"register_images/{key}.webp") if include_urls else None}
    for key, option in approved.items():
        if key in PRODUCT_IMAGE_OPTIONS:
            continue
        filename = option.get("filename")
        image = _safe_image_path(original_root, filename)
        if (image is not None and image.suffix.lower() == ".webp"
                and image.stat().st_size <= MAX_REGISTER_ADMIN_IMAGE_BYTES):
            choices[key] = {"label": option["label"],
                            "url": url_for("static", filename=f"product_images/{filename}") if include_urls else None}
    return choices


def product_image_url(product, choices, *, embedded=False):
    """同じ商品写真を一覧・レジで共用。オフライン時だけ暗号化catalog内へ埋め込む。"""
    if product.photo_webp:
        import base64
        import hashlib
        if embedded:
            return "data:image/webp;base64," + base64.b64encode(product.photo_webp).decode("ascii")
        return url_for("register_products.photo", product_id=product.id,
                       v=hashlib.sha256(product.photo_webp).hexdigest())
    return choices.get(product.image_key, {}).get("url")
