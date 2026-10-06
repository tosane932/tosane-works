"""Adminカタログ変更を単一Git commitとPRにまとめる。mainは直接更新しない。"""

import base64
import io
import json
import os
import uuid

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from product_images import PRODUCT_IMAGE_OPTIONS


REPOSITORY = "tosane932/tosane-works"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
CATALOG_PATH = "store/bakery-hub/product_catalog/admin_products.json"
IMAGE_DIRECTORY = "store/bakery-hub/static/product_images/admin"
MAX_IMAGE_BYTES = 200 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
IMAGE_FORMATS = {
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
    ".webp": ("WEBP", "image/webp"),
}


class InvalidCatalogImage(ValueError):
    pass


class CatalogPublishError(RuntimeError):
    pass


def validate_image(file):
    """実データを検証し、メタデータを除いたWebPへ再符号化する。"""
    filename = file.filename or ""
    if not filename or filename != filename.rsplit("/", 1)[-1] or "\\" in filename:
        raise InvalidCatalogImage("安全でないファイル名です。")
    suffix = os.path.splitext(filename)[1].lower()
    expected = IMAGE_FORMATS.get(suffix)
    if expected is None or file.mimetype != expected[1]:
        raise InvalidCatalogImage("JPEG・PNG・WebP画像を選択してください。")
    raw = file.stream.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise InvalidCatalogImage("画像は200KB以下にしてください。")
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.format != expected[0] or getattr(original, "n_frames", 1) != 1:
                raise InvalidCatalogImage("画像形式が一致しません。")
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise InvalidCatalogImage("画像の縦横サイズが大きすぎます。")
            original.verify()
        with Image.open(io.BytesIO(raw)) as decoded:
            image = ImageOps.exif_transpose(decoded)
            image.load()
            result = io.BytesIO()
            mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
            image.convert(mode).save(result, format="WEBP", quality=82)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise InvalidCatalogImage("正しい画像を選択してください。") from error
    encoded = result.getvalue()
    if not encoded or len(encoded) > MAX_IMAGE_BYTES:
        raise InvalidCatalogImage("変換後の画像が200KBを超えました。")
    return "webp", encoded


def _api(method, path, token, *, expected, payload=None, params=None):
    try:
        response = requests.request(
            method,
            f"{API_ROOT}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload,
            params=params,
            timeout=10,
            allow_redirects=False,
        )
    except requests.RequestException as error:
        raise CatalogPublishError("GitHubとの通信に失敗しました。") from error
    if response.status_code not in expected:
        raise CatalogPublishError("GitHubへ商品カタログを保存できませんでした。")
    if response.status_code == 204:
        return {}
    try:
        return response.json()
    except ValueError as error:
        raise CatalogPublishError("GitHubの応答を確認できませんでした。") from error


def publish_catalog_change(*, label, default_price, reading, image,
                           edit_key=None, guest_allowed=False):
    """変更一式を新規branchの1 commitへ保存し、レビュー用PRを作る。"""
    token = os.environ.get("GITHUB_CATALOG_TOKEN")
    if not token:
        raise CatalogPublishError("カタログ保存の設定がありません。")

    main = _api("GET", "git/ref/heads/main", token, expected=(200,))
    base_sha = main["object"]["sha"]
    base_commit = _api("GET", f"git/commits/{base_sha}", token, expected=(200,))
    remote = _api("GET", f"contents/{CATALOG_PATH}", token,
                  expected=(200,), params={"ref": base_sha})
    try:
        catalog = json.loads(base64.b64decode(remote["content"]))
        products = catalog["products"]
        images = catalog["images"]
        if not isinstance(products, dict) or not isinstance(images, dict):
            raise ValueError("Invalid catalog")
    except (KeyError, ValueError, TypeError) as error:
        raise CatalogPublishError("既存の商品カタログを読み取れませんでした。") from error

    if edit_key:
        if not edit_key.startswith("admin_") or edit_key not in products:
            raise ValueError("更新対象の商品が見つかりません。")
        key = edit_key
        original = products[key]
    else:
        key = f"admin_{uuid.uuid4().hex}"
        if key in products or key in PRODUCT_IMAGE_OPTIONS:
            raise CatalogPublishError("商品キーが重複しました。")
        original = {"filename": None, "image_key": None}

    names = [entry.get("label", "").casefold() for candidate, entry in
             {**PRODUCT_IMAGE_OPTIONS, **products}.items() if candidate != key]
    if label.casefold() in names:
        raise ValueError("同じ商品名はすでにカタログにあります。")

    filename = original.get("filename")
    image_key = original.get("image_key")
    tree = []
    if image is not None:
        extension, data = image
        if extension != "webp" or not isinstance(data, bytes):
            raise ValueError("商品画像が正しくありません。")
        asset_id = uuid.uuid4().hex
        image_key = f"admin_image_{asset_id}"
        filename = f"admin/{asset_id}.webp"
        blob = _api("POST", "git/blobs", token, expected=(201,), payload={
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        })
        images[image_key] = {
            "filename": filename, "label": label, "product_key": key,
        }
        tree.append({
            "path": f"{IMAGE_DIRECTORY}/{asset_id}.webp",
            "mode": "100644", "type": "blob", "sha": blob["sha"],
        })

    products[key] = {
        "label": label,
        "default_price": default_price,
        "reading": reading,
        "filename": filename,
        "image_key": image_key,
        "guest_allowed": guest_allowed,
    }
    tree.append({
        "path": CATALOG_PATH, "mode": "100644", "type": "blob",
        "content": json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    })
    new_tree = _api("POST", "git/trees", token, expected=(201,), payload={
        "base_tree": base_commit["tree"]["sha"], "tree": tree,
    })
    commit = _api("POST", "git/commits", token, expected=(201,), payload={
        "message": f"catalog: {'update' if edit_key else 'add'} {label}",
        "tree": new_tree["sha"], "parents": [base_sha],
    })
    branch = f"catalog/admin-{uuid.uuid4().hex}"
    _api("POST", "git/refs", token, expected=(201,), payload={
        "ref": f"refs/heads/{branch}", "sha": commit["sha"],
    })
    try:
        pull = _api("POST", "pulls", token, expected=(201,), payload={
            "title": f"商品カタログ: {label}",
            "head": branch,
            "base": "main",
            "body": "Adminの商品カタログ申請です。商品情報と画像を確認してからmergeしてください。",
        })
        url = pull["html_url"]
        if not url.startswith(f"https://github.com/{REPOSITORY}/pull/"):
            raise CatalogPublishError("作成したPRを確認できませんでした。")
    except (CatalogPublishError, KeyError):
        try:
            _api("DELETE", f"git/refs/heads/{branch}", token, expected=(204,))
        except CatalogPublishError:
            pass  # 固有branchが残った場合は管理画面で調査する。
        raise CatalogPublishError("PRを作成できませんでした。")
    return url
