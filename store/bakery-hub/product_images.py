"""承認済みのローカル商品画像だけを商品登録とCatalogへ公開する。"""

from pathlib import Path
import re


# 画像素材が承認・配置されたら、ここへ key: filename / label / guest_allowed を追加する。
# 例: "bread_01": {"filename": "bread_01.webp", "label": "食パン", "guest_allowed": True}
PRODUCT_IMAGE_OPTIONS = {}

_KEY_PATTERN = re.compile(r"[a-z0-9_]{1,100}\Z")
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def available_product_images(static_folder, *, is_guest):
    """実在する安全なファイルだけを権限別に返す。"""
    directory = (Path(static_folder) / "product_images").resolve()
    available = {}
    for key, option in PRODUCT_IMAGE_OPTIONS.items():
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            continue
        if is_guest and not option["guest_allowed"]:
            continue
        filename = option["filename"]
        if not isinstance(filename, str) or Path(filename).name != filename:
            continue
        if Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            continue
        available[key] = option
    return available
