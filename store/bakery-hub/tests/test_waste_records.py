"""Actual production/disposal records remain distinct from plans and sales."""

import datetime

import pytest
from flask import g
from sqlalchemy.exc import SQLAlchemyError

from models import (DailySales, Dataset, PrepPlan, PrepRecipe, Product,
                    RegisterProduct, WasteRecord, db)


DAY = "2026-10-23"
PATH = "/prep/waste"


def add_products(dataset):
    register = RegisterProduct(dataset_id=dataset.id, name="クロワッサン", price_yen=280)
    legacy = Product(dataset_id=dataset.id, year=2026, month=10,
                     name="クロワッサン", price=250)
    db.session.add_all((register, legacy))
    db.session.commit()
    return register, legacy


def save(client, csrf_post, key, **changes):
    data = {"business_date": DAY, "product_key": key, "made_quantity": "50",
            "discarded_quantity": "3", "note": ""}
    data.update(changes)
    return csrf_post(client, PATH, data)


def test_record_and_edit_keep_zero_distinct_from_missing(authenticated_client, admin_dataset, csrf_post):
    register, legacy = add_products(admin_dataset)
    recipe = PrepRecipe(dataset_id=admin_dataset.id, product_key="template:test",
                        display_name="試作", base_quantity=10)
    db.session.add(recipe)
    db.session.commit()
    db.session.add_all((PrepPlan(dataset_id=admin_dataset.id, recipe_id=recipe.id,
                                 business_date=datetime.date.fromisoformat(DAY), quantity=60),
                        DailySales(product_id=legacy.id,
                                   date=datetime.date.fromisoformat(DAY), quantity=40)))
    db.session.commit()
    page = authenticated_client.get(PATH + "?date=" + DAY)
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "未入力" in html
    assert f'value="register:{register.id}"' in html
    assert f'value="legacy:{legacy.id}"' in html
    assert "旧月次商品（年月別）" in html
    assert "製造実績・廃棄ロスを記録する" in authenticated_client.get("/prep?date=" + DAY).get_data(as_text=True)

    assert save(authenticated_client, csrf_post, f"register:{register.id}",
                made_quantity="", discarded_quantity="0").status_code == 303
    row = WasteRecord.query.one()
    assert (row.made_quantity, row.discarded_quantity) == (None, 0)
    assert row.product_label == "継続商品｜クロワッサン"
    assert "未入力" in authenticated_client.get(PATH + "?date=" + DAY).get_data(as_text=True)
    assert save(authenticated_client, csrf_post, f"register:{register.id}",
                made_quantity="50", discarded_quantity="3", note="品質不良").status_code == 303
    assert WasteRecord.query.count() == 1
    db.session.refresh(row)
    assert (row.made_quantity, row.discarded_quantity, row.note) == (50, 3, "品質不良")
    register.name = "名前変更後"
    register.is_active = False
    db.session.commit()
    html = authenticated_client.get(PATH + "?date=" + DAY).get_data(as_text=True)
    assert "継続商品｜クロワッサン" in html and "名前変更後" in html
    assert PrepPlan.query.one().quantity == 60
    assert DailySales.query.one().quantity == 40
    assert Product.query.one().price == 250


def test_legacy_month_and_date_are_independent(authenticated_client, admin_dataset, csrf_post):
    _, legacy = add_products(admin_dataset)
    other = Product(dataset_id=admin_dataset.id, year=2026, month=11,
                    name=legacy.name, price=300)
    db.session.add(other)
    db.session.commit()
    assert save(authenticated_client, csrf_post, f"legacy:{legacy.id}").status_code == 303
    assert save(authenticated_client, csrf_post, f"legacy:{other.id}",
                discarded_quantity="0").status_code == 303
    assert WasteRecord.query.count() == 2
    assert {row.product_label for row in WasteRecord.query.all()} == {
        "旧商品｜2026年10月｜クロワッサン", "旧商品｜2026年11月｜クロワッサン"}
    assert save(authenticated_client, csrf_post, f"legacy:{legacy.id}",
                business_date="2026-10-24").status_code == 303
    assert WasteRecord.query.count() == 3
    assert "11月" not in authenticated_client.get(PATH + "?date=2026-10-24").get_data(as_text=True).split("保存済み記録", 1)[1]


def test_invalid_input_does_not_create_or_partially_edit(authenticated_client, admin_dataset, csrf_post):
    register, _ = add_products(admin_dataset)
    key = f"register:{register.id}"
    for invalid in ("-1", "1.5", "abc", "01", "100001", "1e3"):
        assert save(authenticated_client, csrf_post, key, made_quantity=invalid).status_code == 400
    assert save(authenticated_client, csrf_post, key,
                made_quantity="", discarded_quantity="").status_code == 400
    assert save(authenticated_client, csrf_post, key, note="x" * 501).status_code == 400
    assert WasteRecord.query.count() == 0
    assert save(authenticated_client, csrf_post, key,
                made_quantity="0", discarded_quantity="1", note="前日持ち越し").status_code == 303
    assert WasteRecord.query.one().discarded_quantity == 1  # More than today's production is allowed.
    assert save(authenticated_client, csrf_post, key,
                made_quantity="25", discarded_quantity="bad").status_code == 400
    assert WasteRecord.query.one().made_quantity == 0
    assert save(authenticated_client, csrf_post, key, note="<script>alert(1)</script>").status_code == 303
    html = authenticated_client.get(PATH + "?date=" + DAY).get_data(as_text=True)
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    for day in ("2026-02-30", "not-a-date", "2026-10-23<script>"):
        assert save(authenticated_client, csrf_post, key, business_date=day).status_code == 400
        assert authenticated_client.get(PATH + "?date=" + day).status_code == 400


def test_admin_only_dataset_scope_and_csrf(authenticated_client, client, flask_app, admin_dataset, csrf_post, csrf_token):
    g.pop("_login_user", None)
    assert client.get(PATH).status_code == 302
    guest_dataset = Dataset(kind="guest", absolute_expires_at=(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)))
    db.session.add(guest_dataset)
    db.session.commit()
    guest = flask_app.test_client()
    with guest.session_transaction() as session:
        session["_user_id"] = f"guest:{guest_dataset.id}"
        session["_fresh"] = True
    g.pop("_login_user", None)
    assert guest.get(PATH).status_code == 403
    g.pop("csrf_token", None)  # Separate clients share the fixture app context.
    assert guest.post(PATH, data={"business_date": DAY,
                                  "csrf_token": csrf_token(guest, "/login")}).status_code == 403
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    foreign = RegisterProduct(dataset_id=guest_dataset.id, name="秘密商品", price_yen=100)
    foreign_legacy = Product(dataset_id=guest_dataset.id, year=2026, month=10,
                             name="秘密旧商品", price=100)
    db.session.add_all((foreign, foreign_legacy))
    db.session.commit()
    for key in (f"register:{foreign.id}", f"legacy:{foreign_legacy.id}",
                "register:99999", "legacy:99999", "other:1"):
        assert save(authenticated_client, csrf_post, key).status_code == 404
    assert WasteRecord.query.count() == 0
    assert authenticated_client.post(PATH, data={"business_date": DAY,
        "product_key": f"register:{foreign.id}", "made_quantity": "1"}).status_code == 400
    assert "秘密商品" not in authenticated_client.get(PATH + "?date=" + DAY).get_data(as_text=True)


def test_db_failure_rolls_back_and_reports_unsaved(authenticated_client, admin_dataset, csrf_post, monkeypatch):
    register, _ = add_products(admin_dataset)
    key = f"register:{register.id}"
    assert save(authenticated_client, csrf_post, key).status_code == 303
    original_commit = db.session.commit
    def fail_once():
        monkeypatch.setattr(db.session, "commit", original_commit)
        raise SQLAlchemyError("synthetic commit failure")
    monkeypatch.setattr(db.session, "commit", fail_once)
    response = save(authenticated_client, csrf_post, key, made_quantity="99")
    assert response.status_code == 500
    assert "保存できませんでした" in response.get_data(as_text=True)
    assert WasteRecord.query.one().made_quantity == 50


def test_database_constraints_keep_dataset_and_unique_boundary(flask_app, admin_dataset):
    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError
    register, _ = add_products(admin_dataset)
    other = Dataset(kind="guest", absolute_expires_at=(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)))
    db.session.add(other)
    db.session.commit()
    other_id, product_id = other.id, register.id
    day = datetime.date.fromisoformat(DAY)
    # SQLite's test fixture does not enable FK enforcement by default. Turn it
    # on on this isolated connection; PostgreSQL enforces it unconditionally.
    db.session.remove()
    with db.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        with pytest.raises(IntegrityError):
            with connection.begin():
                connection.execute(WasteRecord.__table__.insert().values(
                    dataset_id=other_id, business_date=day,
                    register_product_id=product_id, product_label="不正", made_quantity=1))
    assert any(fk["constrained_columns"] == ["register_product_id", "dataset_id"]
               for fk in inspect(db.engine).get_foreign_keys("waste_records"))
