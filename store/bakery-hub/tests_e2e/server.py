"""Loopback-only, throwaway Bakery Hub for Chromium E2E. Never use a real DB."""
import datetime
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash


directory = Path(os.environ["E2E_STATE_DIR"])
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{directory / 'register.sqlite'}"
os.environ.pop("LOCAL_DEVELOPMENT", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app  # noqa: E402  (database URL must precede app import)
import app as app_module  # noqa: E402
from flask import abort, jsonify, request  # noqa: E402
from models import (Dataset, RegisterCutover, RegisterProduct, RegisterTransaction,
                    RegisterTransactionLine, db)  # noqa: E402
from register_products import update_register_product  # noqa: E402
import register_catalog_proof  # noqa: E402
import register_offline_service  # noqa: E402
import register_service  # noqa: E402

# Navigation fixtures are synthetic; E2E must not fetch an external feed.
app_module.get_news = lambda limit=3: []


username = "e2e-admin"
password = secrets.token_urlsafe(24)
control = secrets.token_urlsafe(32)
app.secret_key = secrets.token_urlsafe(32)
app.config.update(
    TESTING=True, SESSION_COOKIE_SECURE=False,
    ADMIN_USERNAME=username, ADMIN_PASSWORD_HASH=generate_password_hash(password),
    GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS=100,
    GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS=60,
    GUEST_CREATION_RATE_LIMIT_TEST_CLIENT_IP="127.0.0.1",
    REGISTER_V3_PRODUCTS_PREVIEW_ENABLED=True, REGISTER_V3_PRODUCTS_WRITE_ENABLED=True,
    REGISTER_V3_PUBLIC_DEMO_ENABLED=True,
    REGISTER_V3_PRODUCTION_ENABLED=True, REGISTER_V3_PREVIEW_ENABLED=True,
    REGISTER_V3_SAVE_ENABLED=True, REGISTER_V3_AGGREGATION_ENABLED=True,
    REGISTER_V3_OFFLINE_MODE_ENABLED=True, REGISTER_V3_RECOVERY_ENABLED=True,
)

with app.app_context():
    db.create_all()
    owner = Dataset(kind="admin", system_key="admin", absolute_expires_at=None)
    db.session.add(owner)
    db.session.flush()
    day = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("Asia/Tokyo")).date()
    db.session.add(RegisterCutover(dataset_id=owner.id, first_register_date=day))
    db.session.add_all([
        RegisterProduct(dataset_id=owner.id, name="検証クロワッサン", price_yen=120),
        RegisterProduct(dataset_id=owner.id, name="検証食パン", price_yen=300),
    ])
    db.session.commit()

auth_path = directory / "auth.json"
auth_path.write_text(json.dumps({"username": username, "password": password,
                                 "control": control, "day": day.isoformat()}))
auth_path.chmod(0o600)

offset = datetime.timedelta()
real_now = register_service.utc_now


def test_now():
    return real_now() + offset


register_service.utc_now = test_now
register_catalog_proof.utc_now = test_now
register_offline_service.utc_now = test_now


@app.before_request
def protect_control():
    if request.path.startswith("/__e2e/") and request.headers.get("X-E2E-Control") != control:
        abort(404)


@app.get("/__e2e/state")
def e2e_state():
    rows = RegisterTransaction.query.order_by(RegisterTransaction.completed_at,
                                               RegisterTransaction.id).all()
    return jsonify([dict(key=str(row.submission_key), id=str(row.id),
                         day=row.business_date.isoformat(), total=row.total_yen,
                         method=row.save_method,
                         lines=[dict(name=line.product_name_snapshot,
                                     price=line.unit_price_yen, quantity=line.quantity)
                                for line in RegisterTransactionLine.query.filter_by(
                                    transaction_id=row.id).all()]) for row in rows])


@app.get("/__e2e/next-day")
def e2e_next_day():
    global offset
    offset = datetime.timedelta(days=1)
    return jsonify(day=test_now().astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat())


@app.get("/__e2e/change-price")
def e2e_change_price():
    owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
    product = RegisterProduct.query.filter_by(dataset_id=owner.id, name="検証クロワッサン").one()
    update_register_product(owner, product.id, name=product.name, price_yen=140,
                            expected_name=product.name, expected_price_yen=120)
    return jsonify(price=140)


@app.get("/__e2e/reset-scenario")
def e2e_reset_scenario():
    global offset
    offset = datetime.timedelta()
    owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
    product = RegisterProduct.query.filter_by(dataset_id=owner.id, name="検証クロワッサン").one()
    if product.price_yen != 120:
        update_register_product(owner, product.id, name=product.name, price_yen=120,
                                expected_name=product.name, expected_price_yen=product.price_yen)
    return jsonify(day=test_now().astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat())


@app.get("/__e2e/pager-catalog")
def e2e_pager_catalog():
    count = int(request.args.get("count", "0"))
    if not 0 <= count <= 50:
        abort(400)
    owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
    rows = RegisterProduct.query.filter_by(dataset_id=owner.id).all()
    for item in rows:
        item.is_active = False
    for index in range(count):
        name = f"ページ検証{index + 1:02}"
        item = next((p for p in rows if p.name == name), None)
        if item is None:
            item = RegisterProduct(dataset_id=owner.id, name=name, price_yen=1000,
                                   display_order=100 + index)
            db.session.add(item)
        item.is_active = True
    if request.args.get("restore") == "1":
        for item in rows:
            item.is_active = item.name in {"検証クロワッサン", "検証食パン"}
    db.session.commit()
    return jsonify(count=count)


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host="127.0.0.1", port=int(os.environ.get("E2E_PORT", "8765")),
            use_reloader=False, threaded=True)
