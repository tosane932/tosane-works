"""隔離PostgreSQLで新旧排他・複数明細・返金の集計契約を確認。"""

import datetime
from pathlib import Path
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, text

from models import (DailySales, Dataset, Product, RegisterCutover, RegisterProduct,
                    RegisterTransaction, RegisterTransactionLine, db)
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_sales_aggregation import SalesCutoverIntegrityError, aggregate_sales


TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not TEST_URL, reason="隔離PostgreSQLの設定なし")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture()
def pg_app():
    reset = create_engine(TEST_URL, pool_pre_ping=True)
    try:
        with reset.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        reset.dispose()
    app = Flask("register_sales_aggregation_postgresql")
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_URL, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        yield app
        db.session.remove()
        db.engine.dispose()


def test_cutover_refund_and_two_lines_do_not_multiply_header(pg_app):
    with pg_app.app_context():
        day_before, cutover = datetime.date(2026, 10, 15), datetime.date(2026, 10, 16)
        # 既存migrationが管理者Datasetを初期生成するので、その行を使う。
        owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
        old = Product(dataset_id=owner.id, year=2026, month=10, name="パン", price=100)
        first = RegisterProduct(dataset_id=owner.id, name="パン", price_yen=120)
        second = RegisterProduct(dataset_id=owner.id, name="塩パン", price_yen=140)
        db.session.add_all([old, first, second, RegisterCutover(
            dataset_id=owner.id, first_register_date=cutover)])
        db.session.flush()
        db.session.add(DailySales(product_id=old.id, date=day_before, quantity=2))
        for kind, amount, lines in [
            ("sale", 380, [(first, 120, 2), (second, 140, 1)]),
            ("refund", -120, [(first, 120, -1)]),
        ]:
            header = RegisterTransaction(dataset_id=owner.id, business_date=cutover,
                submission_key=uuid.uuid4(), request_digest="a" * 64, kind=kind,
                total_yen=amount, received_yen=amount if kind == "sale" else None,
                change_yen=0 if kind == "sale" else None)
            db.session.add(header)
            db.session.flush()
            for product, unit, quantity in lines:
                db.session.add(RegisterTransactionLine(
                    transaction_id=header.id, dataset_id=owner.id,
                    register_product_id=product.id, product_name_snapshot=product.name,
                    unit_price_yen=unit, quantity=quantity, line_total_yen=unit * quantity))
        db.session.commit()
        result = aggregate_sales(owner, day_before, cutover)
        assert (result["total_yen"], result["register_total_yen"],
                result["transaction_count"], result["refund_count"]) == (460, 260, 2, 1)
        assert result["products"][1]["quantity"] == 1
        db.session.add(DailySales(product_id=old.id, date=cutover, quantity=1))
        db.session.commit()
        with pytest.raises(SalesCutoverIntegrityError) as error:
            aggregate_sales(owner, day_before, cutover)
        assert error.value.legacy_count == 1
