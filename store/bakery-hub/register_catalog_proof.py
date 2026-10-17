"""Server-signed observation of the Admin catalog. It does not prove a sale time."""
import datetime

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from register_service import TOKYO, utc_now


def _signer():
    return URLSafeSerializer(current_app.secret_key, salt="register-v3-catalog-observation")


def issue_catalog_proof(dataset, products):
    now = utc_now()
    payload = {"dataset": str(dataset.id), "issued": now.isoformat(),
               "expires": (now + datetime.timedelta(hours=36)).isoformat(),
               "products": [[p.id, p.name, p.price_yen] for p in products]}
    return _signer().dumps(payload)


def verify_catalog_proof(token, dataset_id, day, snapshots):
    """Proves what the server offered, not when cash changed hands."""
    if not isinstance(token, str) or not 20 <= len(token) <= 12000:
        raise ValueError("サーバー発行の商品情報がありません。")
    try:
        payload = _signer().loads(token)
        issued = datetime.datetime.fromisoformat(payload["issued"])
        expires = datetime.datetime.fromisoformat(payload["expires"])
        if (payload["dataset"] != str(dataset_id) or issued.tzinfo is None
                or expires.tzinfo is None or issued > utc_now()
                or expires - issued != datetime.timedelta(hours=36)
                or not issued.astimezone(TOKYO).date() <= day <= expires.astimezone(TOKYO).date()):
            raise ValueError()
        listed = payload["products"]
        if (not isinstance(listed, list) or len({row[0] for row in listed}) != len(listed)
                or any(not isinstance(row, list) or len(row) != 3 for row in listed)):
            raise ValueError()
        offered = {row[0]: (row[1], row[2]) for row in listed}
        if any(offered.get(line["id"]) != (line["name"], line["unit_price_yen"])
               for line in snapshots):
            raise ValueError()
    except (BadSignature, KeyError, TypeError, ValueError, IndexError) as error:
        raise ValueError("商品情報の署名・Dataset・価格・有効期間を検証できません。") from error
    return issued, expires
