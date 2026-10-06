"""承認済みのローカル商品画像だけを商品登録とCatalogへ公開する。"""

from pathlib import Path
import re


# 画像素材が承認・配置されたら、ここへ key: filename / label / guest_allowed を追加する。
# 例: "bread_01": {"filename": "bread_01.webp", "label": "食パン", "guest_allowed": True}
PRODUCT_IMAGE_OPTIONS = {
    "candied_sweet_potato_croissant": {
        "filename": "candied_sweet_potato_croissant.png",
        "label": "大学いもクロワッサン",
            "default_price": 250,
        "reading": "だいがくいもくろわっさん",
        "guest_allowed": True,
    },
    "caramel_apple_danish": {
        "filename": "caramel_apple_danish.png",
        "label": "キャラメルりんごデニッシュ",
            "default_price": 240,
        "reading": "きゃらめるりんごでにっしゅ",
        "guest_allowed": True,
    },
    "chestnut_danish_with_innerskin": {
        "filename": "chestnut_danish_with_innerskin.png",
        "label": "渋皮マロンデニッシュ",
            "default_price": 260,
        "reading": "しぶかわまろんでにっしゅ",
        "guest_allowed": True,
    },
    "chestnut_paste_and_butter": {
        "filename": "chestnut_paste_and_butter.png",
        "label": "栗あんバター",
            "default_price": 220,
        "reading": "くりあんばたー",
        "guest_allowed": True,
    },
    "cinnamon_baked_apple_bread": {
        "filename": "cinnamon_baked_apple_bread.png",
        "label": "シナモン香る焼きりんごパン",
            "default_price": 220,
        "reading": "しなもんかおるやきりんごぱん",
        "guest_allowed": True,
    },
    "cornet": {
        "filename": "cornet.png",
        "label": "コロネ",
            "default_price": 150,
        "reading": "ころね",
        "guest_allowed": True,
    },
    "croissant": {
        "filename": "croissant.png",
        "label": "クロワッサン",
            "default_price": 120,
        "reading": "くろわっさん",
        "guest_allowed": True,
    },
    "curry_bread": {
        "filename": "curry_bread.png",
        "label": "カレーパン",
            "default_price": 220,
        "reading": "かれーぱん",
        "guest_allowed": True,
    },
    "fluffy_pumpkin_bread": {
        "filename": "fluffy_pumpkin_bread.png",
        "label": "かぼちゃのふんわりパン",
            "default_price": 180,
        "reading": "かぼちゃのふんわりぱん",
        "guest_allowed": True,
    },
    "ham_cheese_deli_sandwich": {
        "filename": "ham_cheese_deli_sandwich.png",
        "label": "ハムチーズデリサンド",
            "default_price": 320,
        "reading": "はむちーずでりさんど",
        "guest_allowed": True,
    },
    "hotdog": {
        "filename": "hotdog.png",
        "label": "ホットドッグ",
            "default_price": 220,
        "reading": "ほっとどっぐ",
        "guest_allowed": True,
    },
    "loafbread": {
        "filename": "loafbread.png",
        "label": "食パン",
            "default_price": 200,
        "reading": "しょくぱん",
        "guest_allowed": True,
    },
    "melonpan": {
        "filename": "melonpan.png",
        "label": "メロンパン",
            "default_price": 140,
        "reading": "めろんぱん",
        "guest_allowed": True,
    },
    "mushroom_and_bacon_gratin_bread": {
        "filename": "mushroom_and_bacon_gratin_bread.png",
        "label": "きのことベーコンのグラタンパン",
            "default_price": 260,
        "reading": "きのことべーこんのぐらたんぱん",
        "guest_allowed": True,
    },
    "mushroom_teriyaki_chicken": {
        "filename": "mushroom_teriyaki_chicken.png",
        "label": "きのこ照り焼きチキン",
            "default_price": 280,
        "reading": "きのこてりやきちきん",
        "guest_allowed": True,
    },
    "purple_sweetpotato_anpan": {
        "filename": "purple_sweetpotato_anpan.png",
        "label": "紫芋あんぱん",
            "default_price": 180,
        "reading": "むらさきいもあんぱん",
        "guest_allowed": True,
    },
    "roasted_sweetpotato_creambun": {
        "filename": "roasted_sweetpotato_creambun.png",
        "label": "焼きいもクリームパン",
            "default_price": 200,
        "reading": "やきいもくりーむぱん",
        "guest_allowed": True,
    },
    "sweetpotato_and_cheese_frenchbread": {
        "filename": "sweetpotato_and_cheese_frenchbread.png",
        "label": "さつまいもとチーズフランス",
            "default_price": 240,
        "reading": "さつまいもとちーずふらんす",
        "guest_allowed": True,
    },
}

_KEY_PATTERN = re.compile(r"[a-z0-9_]{1,100}\Z")
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _product_option_sort_key(item):
    """日本語テンプレートを読み順、その後に英字名をA-Z順で並べる。"""
    key, option = item
    label = str(option.get("label", ""))
    reading = str(option.get("reading", "")).strip()

    if reading:
        return (0, reading, label.casefold(), key)

    return (1, label.casefold(), key)


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
    return dict(sorted(available.items(), key=_product_option_sort_key))
