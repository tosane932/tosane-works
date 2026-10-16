"""承認済みレジ商品画像の安全なURL解決。標準品は軽量WebPだけを使う。"""

from pathlib import Path

from flask import url_for

from product_images import (PRODUCT_IMAGE_OPTIONS, _safe_image_path,
                            available_product_image_assets)


MAX_REGISTER_ADMIN_IMAGE_BYTES = 500 * 1024


def register_image_choices(static_folder, *, is_guest):
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
                            "url": url_for("static", filename=f"register_images/{key}.webp")}
    for key, option in approved.items():
        if key in PRODUCT_IMAGE_OPTIONS:
            continue
        filename = option.get("filename")
        image = _safe_image_path(original_root, filename)
        if (image is not None and image.suffix.lower() == ".webp"
                and image.stat().st_size <= MAX_REGISTER_ADMIN_IMAGE_BYTES):
            choices[key] = {"label": option["label"],
                            "url": url_for("static", filename=f"product_images/{filename}")}
    return choices
