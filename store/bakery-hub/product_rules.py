"""共通商品マスタの入力範囲。旧月次Productの制約とは分離する。"""
MAX_PRODUCT_PRICE_YEN = 100_000
BREAD_KINDS = {
    "savory": "惣菜パン", "sweet": "菓子パン", "loaf": "食パン・食事パン",
    "hard": "ハード系", "sandwich": "サンドイッチ", "fried": "ドーナツ・揚げパン",
    "baked": "焼き菓子", "other": "その他",
}
PRODUCT_CLASSES = {"regular": "定番", "seasonal": "季節もの", "special": "特別商品"}
