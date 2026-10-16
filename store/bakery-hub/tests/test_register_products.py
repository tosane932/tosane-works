"""継続商品マスタの初期化、独自枠、旧データ保全とDataset境界。"""

import datetime
import concurrent.futures
from pathlib import Path
import threading
import uuid

from flask import Flask
import pytest

import app as app_module
from models import DailySales, Dataset, PrepRecipe, Product, RegisterProduct, db
import product_images
from register_products import (
    CustomProductLimitReached, GUEST_CUSTOM_PRODUCT_LIMIT, ProductSubmissionConflict,
    copy_legacy_product, create_custom_product, ensure_standard_products,
    update_register_product,
)


def owner(kind="guest", *, expired=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(kind=kind, system_key="admin" if kind == "admin" else None,
                      created_at=now - datetime.timedelta(hours=1),
                      last_activity_at=now - datetime.timedelta(minutes=31 if expired else 1),
                      absolute_expires_at=(now + datetime.timedelta(hours=1)) if kind == "guest" else None)
    db.session.add(dataset)
    db.session.commit()
    return dataset


def test_standard_catalog_and_assets_match_all_eighteen(flask_app):
    options = product_images.PRODUCT_IMAGE_OPTIONS
    assert len(options) == 18
    assert all(type(v["default_price"]) is int and 0 <= v["default_price"] <= 500
               for v in options.values())
    assert all((Path(flask_app.static_folder) / "product_images" / v["filename"]).is_file()
               for v in options.values())
    admin, guest_a, guest_b = owner("admin"), owner(), owner()
    for dataset in (admin, guest_a, guest_b):
        assert ensure_standard_products(dataset) == 18
        saved = RegisterProduct.query.filter_by(dataset_id=dataset.id).order_by(RegisterProduct.display_order).all()
        assert [(p.template_key, p.name, p.price_yen, p.image_key, p.display_order, p.is_active)
                for p in saved] == [
            (key, option["label"], option["default_price"], key, order, True)
            for order, (key, option) in enumerate(options.items())]
        assert ensure_standard_products(dataset) == 0
        assert ensure_standard_products(dataset) == 0
    assert RegisterProduct.query.count() == 54


def test_partial_seed_preserves_customizations_and_survives_month_change(flask_app):
    dataset = owner()
    melon = RegisterProduct(dataset_id=dataset.id, template_key="melonpan", name="店長のメロンパン",
                            price_yen=150, image_key=None, display_order=99, is_active=False)
    db.session.add(melon)
    db.session.commit()
    assert ensure_standard_products(dataset) == 17
    db.session.expire_all()
    saved = RegisterProduct.query.filter_by(dataset_id=dataset.id, template_key="melonpan").one()
    assert (saved.id, saved.name, saved.price_yen, saved.image_key, saved.display_order, saved.is_active) == (
        melon.id, "店長のメロンパン", 150, None, 99, False)
    # 年月を引数に取らない継続マスタ。月次Productが別月に増えても変わらない。
    db.session.add(Product(dataset_id=dataset.id, year=2026, month=11,
                           name="月次のパン", price=200))
    db.session.commit()
    assert ensure_standard_products(dataset) == 0
    assert RegisterProduct.query.filter_by(dataset_id=dataset.id).count() == 18


def test_missing_image_falls_back_without_losing_standard_product(flask_app, monkeypatch, tmp_path):
    dataset = owner()
    monkeypatch.setattr(flask_app, "static_folder", str(tmp_path))
    assert ensure_standard_products(dataset) == 18
    assert RegisterProduct.query.filter_by(dataset_id=dataset.id, image_key=None).count() == 18


def test_invalid_template_fails_atomically(flask_app, monkeypatch):
    dataset = owner()
    changed = dict(product_images.PRODUCT_IMAGE_OPTIONS)
    last_key = list(changed)[-1]
    changed[last_key] = dict(changed[last_key], default_price=501)
    monkeypatch.setattr(product_images, "PRODUCT_IMAGE_OPTIONS", changed)
    with pytest.raises(ValueError):
        ensure_standard_products(dataset)
    assert RegisterProduct.query.count() == 0


def test_seed_rolls_back_every_insert_if_commit_fails(flask_app, monkeypatch):
    dataset = owner()
    original_commit = db.session.commit

    def reject_commit():
        raise RuntimeError("保存障害")

    monkeypatch.setattr(db.session, "commit", reject_commit)
    with pytest.raises(RuntimeError, match="保存障害"):
        ensure_standard_products(dataset)
    monkeypatch.setattr(db.session, "commit", original_commit)
    assert RegisterProduct.query.filter_by(dataset_id=dataset.id).count() == 0


@pytest.mark.parametrize("name,price", [
    (" ", 200), ("a" * 101, 200), (None, 200), ("パン", -1),
    ("パン", 501), ("パン", 20.5), ("パン", "20.5"),
    ("パン", "パン"), ("パン", True),
])
def test_custom_validation_rejects_invalid_values(flask_app, name, price):
    dataset = owner()
    with pytest.raises(ValueError):
        create_custom_product(dataset, name=name, price_yen=price)
    assert RegisterProduct.query.count() == 0


def test_custom_accepts_zero_non_ten_step_and_five_hundred_without_images(flask_app):
    dataset = owner("admin")
    saved = [create_custom_product(dataset, name=f" パン{i} ", price_yen=price)
             for i, price in enumerate((0, "123", 500))]
    assert [(p.name, p.price_yen, p.image_key, p.template_key, p.is_active)
            for p in saved] == [(f"パン{i}", price, None, None, True)
                             for i, price in enumerate((0, 123, 500))]
    assert Product.query.count() == DailySales.query.count() == 0


def test_guest_custom_limit_is_independent_of_standard_and_legacy(flask_app):
    guest = owner()
    assert ensure_standard_products(guest) == 18
    for i in range(5):
        db.session.add(Product(dataset_id=guest.id, year=2026, month=10,
                               name=f"旧商品{i}", price=200))
    db.session.commit()
    for i in range(GUEST_CUSTOM_PRODUCT_LIMIT):
        create_custom_product(guest, name=f"独自{i}", price_yen=200)
    with pytest.raises(CustomProductLimitReached):
        create_custom_product(guest, name="31件目", price_yen=200)
    assert RegisterProduct.query.filter_by(dataset_id=guest.id, template_key=None).count() == 30
    assert Product.query.filter_by(dataset_id=guest.id).count() == 5
    assert ensure_standard_products(guest) == 0


def test_expired_guest_and_dataset_scoped_update(flask_app):
    admin, guest_a, guest_b, expired = owner("admin"), owner(), owner(), owner(expired=True)
    a = create_custom_product(guest_a, name="A", price_yen=100)
    b = create_custom_product(guest_b, name="B", price_yen=200)
    parent = create_custom_product(admin, name="管理", price_yen=300)
    with pytest.raises(PermissionError):
        ensure_standard_products(expired)
    with pytest.raises(PermissionError):
        create_custom_product(expired, name="期限切れ", price_yen=100)
    for forbidden in (b, parent):
        with pytest.raises(PermissionError):
            update_register_product(guest_a, forbidden.id, name="越境", price_yen=1)
    with pytest.raises(PermissionError):
        update_register_product(expired, a.id, name="期限切れ", price_yen=1)
    assert update_register_product(guest_a, a.id, name="改名", price_yen=123).price_yen == 123
    assert (db.session.get(RegisterProduct, b.id).name, db.session.get(RegisterProduct, parent.id).name) == ("B", "管理")


def test_legacy_copy_requires_confirmation_and_never_changes_old_data(flask_app):
    admin, guest = owner("admin"), owner()
    legacy = [Product(dataset_id=admin.id, year=2026, month=10, name="くるみパン",
                      price=price, image_key="croissant", is_active=active)
              for price, active in ((260, True), (290, True), (510, True), (200, False))]
    db.session.add_all(legacy)
    db.session.commit()
    sale = DailySales(product_id=legacy[0].id, date=datetime.date(2026, 10, 15), quantity=2)
    recipe = PrepRecipe(dataset_id=admin.id, product_key=f"product:{legacy[0].id}",
                        display_name="くるみパン", base_quantity=10)
    db.session.add_all([sale, recipe])
    db.session.commit()
    initial = [(p.id, p.name, p.price, p.image_key, p.is_active) for p in legacy]
    for row in legacy[:2]:
        with pytest.raises(PermissionError):
            copy_legacy_product(guest, row.id, confirmed_name=row.name, confirmed_price_yen=row.price)
        copy = copy_legacy_product(admin, row.id, confirmed_name=row.name, confirmed_price_yen=row.price)
        assert copy.id != row.id or copy.__tablename__ != row.__tablename__
        assert (copy.name, copy.price_yen, copy.image_key) == ("くるみパン", row.price, None)
    with pytest.raises(ValueError):
        copy_legacy_product(admin, legacy[2].id, confirmed_name="くるみパン", confirmed_price_yen=510)
    with pytest.raises(ValueError):
        copy_legacy_product(admin, legacy[3].id, confirmed_name="くるみパン", confirmed_price_yen=200)
    with pytest.raises(ValueError):
        copy_legacy_product(admin, legacy[0].id, confirmed_name="古い表示", confirmed_price_yen=260)
    assert [(p.id, p.name, p.price, p.image_key, p.is_active) for p in legacy] == initial
    assert (Product.query.count(), DailySales.query.count(), PrepRecipe.query.count()) == (4, 1, 1)
    assert db.session.get(PrepRecipe, recipe.id).product_key == f"product:{legacy[0].id}"
    assert RegisterProduct.query.filter_by(dataset_id=admin.id, template_key=None).count() == 2


def test_expired_guest_cleanup_deletes_only_target_products(flask_app):
    expired, active, admin = owner(), owner(), owner("admin")
    expired_id = expired.id
    for dataset in (expired, active, admin):
        ensure_standard_products(dataset)
        create_custom_product(dataset, name="独自商品", price_yen=200)
    expired.last_activity_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=31)
    db.session.commit()
    assert app_module._cleanup_expired_guest_datasets() == 1
    db.session.commit()
    assert db.session.get(Dataset, expired_id) is None
    assert RegisterProduct.query.filter_by(dataset_id=expired_id).count() == 0
    for dataset in (active, admin):
        assert RegisterProduct.query.filter_by(dataset_id=dataset.id).count() == 19


def test_sqlite_parallel_seed_and_guest_limit_use_one_dataset_writer(tmp_path):
    app = Flask("register_products_sqlite_concurrency")
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'register.sqlite'}",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      static_folder=app_module.app.static_folder)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        guest = owner()
        guest_id = guest.id

    def race(action):
        barrier = threading.Barrier(2)

        def worker():
            with app.app_context():
                dataset = db.session.get(Dataset, guest_id)
                barrier.wait(timeout=15)
                try:
                    return action(dataset)
                finally:
                    db.session.remove()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for _ in range(2)]
            return [future.result(timeout=20) for future in futures]

    assert sorted(race(ensure_standard_products)) == [0, 18]
    with app.app_context():
        for i in range(29):
            create_custom_product(db.session.get(Dataset, guest_id), name=f"独自{i}", price_yen=200)

    def try_create(dataset):
        try:
            create_custom_product(dataset, name="同時追加", price_yen=200)
            return "saved"
        except CustomProductLimitReached:
            return "full"

    assert sorted(race(try_create)) == ["full", "saved"]
    with app.app_context():
        assert RegisterProduct.query.filter_by(dataset_id=guest_id).count() == 48
        db.session.remove()
        db.drop_all()


def test_sqlite_parallel_identical_product_submission_creates_one_row(tmp_path):
    app = Flask("register_products_idempotency_sqlite")
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'products.sqlite'}",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        dataset = owner()
        dataset_id = dataset.id
    key = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            dataset = db.session.get(Dataset, dataset_id)
            barrier.wait(timeout=15)
            try:
                return create_custom_product(dataset, name="一度だけ", price_yen=500,
                                             request_key=key).id
            finally:
                db.session.remove()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        assert futures[0].result(timeout=20) == futures[1].result(timeout=20)
    with app.app_context():
        dataset = db.session.get(Dataset, dataset_id)
        with pytest.raises(ProductSubmissionConflict):
            create_custom_product(dataset, name="異なる商品", price_yen=500,
                                  request_key=key)
        assert RegisterProduct.query.filter_by(dataset_id=dataset_id).count() == 1
        db.session.remove()
        db.drop_all()
