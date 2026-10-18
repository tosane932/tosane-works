"""隔離PostgreSQLでschema往復・履歴保全・実並行更新を確認。"""
import datetime
import io
import uuid

from flask_migrate import downgrade, upgrade
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models import (Dataset, RegisterCutover, RegisterPriceHistory, RegisterProduct,
                    RegisterTransactionLine, db)
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_products import (ProductSubmissionConflict, create_custom_product,
                               set_product_active, update_register_product)
from register_service import save_register_transaction
from tests.test_register_products_postgresql import pg_app, _race, MIGRATIONS_DIR  # noqa: F401

pytestmark = pytest.mark.skipif(not get_isolated_postgresql_test_url(), reason="隔離PostgreSQLの設定なし")
NEW = "b6d2e9f4a107"
OLD = "a3c9d7e5f102"


def test_upgrade_downgrade_and_preexisting_business_rows(pg_app):
    app, _ = pg_app
    with app.app_context():
        downgrade(directory=str(MIGRATIONS_DIR), revision=OLD)
        # Old models cannot be selected through the new ORM before upgrading.
        db.session.execute(text("INSERT INTO products (id,dataset_id,year,month,name,price,is_active) "
            "SELECT 901,id,2026,10,'旧商品',280,true FROM datasets WHERE system_key='admin'"))
        db.session.execute(text("INSERT INTO daily_sales (id,product_id,date,quantity) VALUES (902,901,'2026-10-01',3)"))
        db.session.execute(text("INSERT INTO register_products (id,dataset_id,name,price_yen,image_key) "
            "SELECT 903,id,'継続パン',500,'croissant' FROM datasets WHERE system_key='admin'"))
        db.session.commit()
        transaction_id, submission_key = str(uuid.uuid4()), str(uuid.uuid4())
        db.session.execute(text("INSERT INTO register_transactions "
            "(id,dataset_id,submission_key,request_digest,business_date,kind,total_yen,received_yen,change_yen) "
            "SELECT :id,id,:key,:digest,'2026-10-02','sale',500,500,0 FROM datasets WHERE system_key='admin'"),
            {"id":transaction_id,"key":submission_key,"digest":"a"*64})
        db.session.execute(text("INSERT INTO register_transaction_lines "
            "(id,transaction_id,dataset_id,register_product_id,product_name_snapshot,unit_price_yen,quantity,line_total_yen) "
            "SELECT 904,:id,id,903,'販売時のパン',500,1,500 FROM datasets WHERE system_key='admin'"),
            {"id":transaction_id})
        db.session.commit()
        upgrade(directory=str(MIGRATIONS_DIR), revision=NEW)
        item = db.session.get(RegisterProduct, 903)
        assert (item.name, item.price_yen, item.image_key, item.bread_kind, item.product_class, item.revision) == (
            "継続パン", 500, "croissant", "other", "regular", 0)
        assert db.session.execute(text("SELECT id,product_id,quantity FROM daily_sales")).one() == (902,901,3)
        assert db.session.execute(text("SELECT id,register_product_id,product_name_snapshot,unit_price_yen,quantity,line_total_yen "
            "FROM register_transaction_lines")).one() == (904,903,"販売時のパン",500,1,500)
        assert db.session.execute(text("SELECT total_yen FROM register_transactions")).scalar_one() == 500
        with pytest.raises(RuntimeError, match="populated"):
            downgrade(directory=str(MIGRATIONS_DIR), revision=OLD)
        assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW
        assert db.session.get(RegisterProduct, 903).name == "継続パン"


def test_large_price_photo_constraints_history_and_refund(pg_app):
    app, _ = pg_app
    with app.app_context():
        owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
        buf = io.BytesIO(); Image.new("RGB",(20,20),"red").save(buf,"WEBP")
        item = create_custom_product(owner, name="写真パン", price_yen=100000,
                                     bread_kind="sweet", product_class="seasonal", photo_webp=buf.getvalue())
        db.session.add(RegisterCutover(dataset_id=owner.id, first_register_date=datetime.date(2020,1,1)))
        db.session.commit()
        original_id = item.id
        for kind, qty in (("sale",2),("refund",-1)):
            transaction = save_register_transaction(owner, submission_key=uuid.uuid4(), kind=kind,
                items=[(item.id,qty)], received_yen=200000 if qty>0 else None,
                refund_confirmed=qty<0, displayed_prices=[(item.id,100000)])
            assert transaction.total_yen == qty * 100000
        update_register_product(owner,item.id,name=item.name,price_yen=850,
                                expected_revision=0,product_class="special")
        set_product_active(owner,item.id,is_active=False,expected_revision=1)
        set_product_active(owner,item.id,is_active=True,expected_revision=2)
        assert item.id == original_id and item.photo_webp == buf.getvalue()
        assert [line.unit_price_yen for line in RegisterTransactionLine.query.all()] == [100000,100000]
        assert [(r.old_price_yen,r.new_price_yen) for r in RegisterPriceHistory.query.order_by(RegisterPriceHistory.id)] == [(None,100000),(100000,850)]
        with pytest.raises(IntegrityError):
            db.session.execute(text("UPDATE register_price_history SET new_price_yen=1"))
        db.session.rollback()
        for column, value in (("price_yen",100001),("bread_kind","unknown"),("product_class","unknown"),
                              ("photo_webp",b"x"*512001),("revision",-1)):
            with pytest.raises(IntegrityError):
                db.session.execute(text(f"UPDATE register_products SET {column}=:value WHERE id=:id"),
                                   {"value":value,"id":original_id})
                db.session.commit()
            db.session.rollback()


def test_parallel_same_state_is_idempotent_and_stale_edits_conflict(pg_app):
    app, owner_id = pg_app
    with app.app_context():
        owner = db.session.get(Dataset,owner_id)
        item_id = create_custom_product(owner,name="継続",price_yen=850).id
    assert _race(app,owner_id,lambda owner:set_product_active(owner,item_id,
                                is_active=False,expected_revision=0).revision) == [1,1]
    with app.app_context():
        set_product_active(db.session.get(Dataset,owner_id),item_id,is_active=True,expected_revision=1)
    def edit(owner):
        try:
            update_register_product(owner,item_id,name="新名称",price_yen=900,expected_revision=0,
                                    bread_kind="hard",product_class="special")
            return "unexpected"
        except ProductSubmissionConflict:
            return "conflict"
    assert _race(app,owner_id,edit) == ["conflict","conflict"]
    with app.app_context():
        item = db.session.get(RegisterProduct,item_id)
        assert (item.name,item.price_yen,item.is_active,item.revision) == ("継続",850,True,2)

    def competing_edit(owner):
        try:
            update_register_product(owner,item_id,name=f"編集{uuid.uuid4().hex}",
                                    price_yen=900,expected_revision=2)
            return "saved"
        except ProductSubmissionConflict:
            return "conflict"
    assert sorted(_race(app,owner_id,competing_edit)) == ["conflict","saved"]
    with app.app_context():
        assert db.session.get(RegisterProduct,item_id).revision == 3
