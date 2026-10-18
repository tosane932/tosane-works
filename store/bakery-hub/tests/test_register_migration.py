"""隔離SQLiteで旧データ保全とv3 migrationの往復を確認する。"""

from pathlib import Path
import uuid

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text

from models import db


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
OLD_HEAD = "e2a7c4d9b105"
NEW_HEAD = "b6d2e9f4a107"
TABLES = {"register_products", "register_cutovers", "register_transactions",
          "register_transaction_lines", "register_prep_links",
          "register_price_history", "register_cutover_approvals",
          "register_recovery_cases", "register_recovery_lines", "register_recovery_events",
          "register_offline_permits"}


@pytest.fixture()
def migration_app(tmp_path):
    app = Flask("register_migration_test")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'register.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS_DIR))
    with app.app_context():
        yield app
        db.session.remove()


def migrate(revision):
    upgrade(directory=str(MIGRATIONS_DIR), revision=revision)


def table_state():
    return set(inspect(db.engine).get_table_names())


def assert_previous_rows():
    assert db.session.execute(text("SELECT id, name, price FROM products WHERE id=901")).one() == (901, "旧商品", 280)
    assert db.session.execute(text("SELECT id, product_id, quantity FROM daily_sales WHERE id=902")).one() == (902, 901, 3)


def test_empty_database_upgrades_to_single_head_without_seeding(migration_app):
    c = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    c.set_main_option("script_location", str(MIGRATIONS_DIR))
    assert ScriptDirectory.from_config(c).get_heads() == [NEW_HEAD]
    assert table_state() == set()
    migrate("head")
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD
    assert TABLES <= table_state()
    for table in TABLES:
        assert db.session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0


def test_existing_rows_and_schema_survive_upgrade_and_empty_downgrade(migration_app):
    migrate(OLD_HEAD)
    db.session.execute(text(
        "INSERT INTO products (id,dataset_id,year,month,name,price,is_active) "
        "SELECT 901,id,2026,10,'旧商品',280,true FROM datasets WHERE system_key='admin'"
    ))
    db.session.execute(text("INSERT INTO daily_sales (id,product_id,date,quantity) VALUES (902,901,'2026-10-15',3)"))
    db.session.commit()
    old_inspector = inspect(db.engine)
    old_product_columns = [x["name"] for x in old_inspector.get_columns("products")]
    old_sales_columns = [x["name"] for x in old_inspector.get_columns("daily_sales")]
    assert_previous_rows()

    migrate("head")
    inspector = inspect(db.engine)
    assert TABLES <= set(inspector.get_table_names())
    assert [x["name"] for x in inspector.get_columns("products")] == old_product_columns
    assert [x["name"] for x in inspector.get_columns("daily_sales")] == old_sales_columns
    assert_previous_rows()

    expected_columns = {
        "register_prep_links": {"dataset_id", "recipe_id", "register_product_id"},
        "register_products": {"id", "dataset_id", "template_key", "name", "price_yen", "image_key", "is_active", "display_order", "created_at", "updated_at", "creation_key", "creation_digest", "bread_kind", "product_class", "photo_webp", "revision"},
        "register_cutovers": {"dataset_id", "first_register_date"},
        "register_transactions": {"id", "dataset_id", "submission_key", "request_digest", "completed_at", "business_date", "kind", "total_yen", "received_yen", "change_yen", "save_method", "recovery_case_id", "recovery_evidence_digest", "client_completed_at", "offline_permit_id"},
        "register_transaction_lines": {"id", "transaction_id", "dataset_id", "register_product_id", "product_name_snapshot", "unit_price_yen", "quantity", "line_total_yen"},
    }
    for table, names in expected_columns.items():
        columns = {col["name"]: col for col in inspector.get_columns(table)}
        assert set(columns) == names
        assert all(not col["nullable"] for name, col in columns.items()
                   if name not in {"template_key", "image_key", "creation_key", "creation_digest", "photo_webp", "received_yen", "change_yen", "recovery_case_id", "recovery_evidence_digest", "client_completed_at", "offline_permit_id"})

    expected_fk = {
        "register_prep_links": {
            ("register_product_id", "dataset_id"): ("register_products", ("id", "dataset_id")),
            ("recipe_id", "dataset_id"): ("prep_recipes", ("id", "dataset_id")),
        },
        "register_products": {("dataset_id",): ("datasets", ("id",))},
        "register_cutovers": {("dataset_id",): ("datasets", ("id",))},
        "register_transactions": {("dataset_id",): ("datasets", ("id",)),
                                  ("recovery_case_id", "dataset_id"): ("register_recovery_cases", ("id", "dataset_id")),
                                  ("offline_permit_id", "dataset_id"): ("register_offline_permits", ("id", "dataset_id"))},
        "register_transaction_lines": {
            ("transaction_id", "dataset_id"): ("register_transactions", ("id", "dataset_id")),
            ("register_product_id", "dataset_id"): ("register_products", ("id", "dataset_id")),
        },
    }
    for table, expected in expected_fk.items():
        actual = {tuple(fk["constrained_columns"]):
                  (fk["referred_table"], tuple(fk["referred_columns"]))
                  for fk in inspector.get_foreign_keys(table)}
        assert actual == expected
        assert all(fk["options"].get("ondelete", "").upper() == "RESTRICT"
                   for fk in inspector.get_foreign_keys(table))

    for table, names in {
        "register_prep_links": {"uq_register_prep_links_dataset_product"},
        "register_products": {"uq_register_products_id_dataset", "uq_register_products_dataset_template"},
        "register_transactions": {"uq_register_transactions_id_dataset", "uq_register_transactions_submission"},
        "register_transaction_lines": {"uq_register_lines_transaction_product"},
    }.items():
        assert names <= {x["name"] for x in inspector.get_unique_constraints(table)}
    for table, names in {
        "register_products": {"ck_register_products_name", "ck_register_products_price"},
        "register_transactions": {"ck_register_transactions_kind", "ck_register_transactions_settlement"},
        "register_transaction_lines": {"ck_register_lines_quantity", "ck_register_lines_unit_price", "ck_register_lines_total"},
    }.items():
        assert names <= {x["name"] for x in inspector.get_check_constraints(table)}
    for table, name in {
        "register_products": "ix_register_products_dataset_active_order",
        "register_transactions": "ix_register_transactions_dataset_day_completed",
        "register_transaction_lines": "ix_register_lines_dataset_product",
    }.items():
        assert name in {x["name"] for x in inspector.get_indexes(table)}
    assert "ux_register_products_dataset_creation_key" in {
        x["name"] for x in inspector.get_indexes("register_products") if x["unique"]
    }

    # A future edit to the ORM without matching migration (or vice versa)
    # must be caught before either database gets the new schema.
    for table in TABLES:
        model_table = db.metadata.tables[table]
        checks = {c.name for c in model_table.constraints if isinstance(c, sa.CheckConstraint)}
        uniques = {c.name for c in model_table.constraints if isinstance(c, sa.UniqueConstraint)}
        foreign_keys = {c.name for c in model_table.constraints if isinstance(c, sa.ForeignKeyConstraint)}
        assert checks == {c["name"] for c in inspector.get_check_constraints(table)}
        assert uniques == {c["name"] for c in inspector.get_unique_constraints(table)}
        assert foreign_keys == {c["name"] for c in inspector.get_foreign_keys(table)}
        assert {i.name for i in model_table.indexes} == {i["name"] for i in inspector.get_indexes(table)}

    product_types = {c["name"]: c["type"] for c in inspector.get_columns("register_products")}
    header_types = {c["name"]: c["type"] for c in inspector.get_columns("register_transactions")}
    line_types = {c["name"]: c["type"] for c in inspector.get_columns("register_transaction_lines")}
    assert isinstance(product_types["price_yen"], sa.Integer)
    assert isinstance(product_types["name"], sa.String) and product_types["name"].length == 100
    assert isinstance(header_types["total_yen"], sa.BigInteger)
    assert isinstance(header_types["business_date"], sa.Date)
    assert isinstance(header_types["completed_at"], sa.DateTime)
    assert isinstance(line_types["quantity"], sa.Integer)

    downgrade(directory=str(MIGRATIONS_DIR), revision=OLD_HEAD)
    assert TABLES.isdisjoint(table_state())
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == OLD_HEAD
    assert_previous_rows()


@pytest.mark.parametrize("occupied", ["register_products", "register_cutovers", "register_transactions", "register_transaction_lines"])
def test_downgrade_refuses_populated_tables_before_dropping_any(migration_app, occupied):
    migrate("head")
    admin_id = db.session.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
    sale_id, key = uuid.uuid4().hex, uuid.uuid4().hex
    if occupied in {"register_products", "register_transaction_lines"}:
        db.session.execute(text("INSERT INTO register_products (id,dataset_id,name,price_yen) VALUES (11,:owner,'商品',200)"), {"owner": admin_id})
    if occupied == "register_cutovers":
        db.session.execute(text("INSERT INTO register_cutovers (dataset_id,first_register_date) VALUES (:owner,'2026-10-16')"), {"owner": admin_id})
    if occupied in {"register_transactions", "register_transaction_lines"}:
        db.session.execute(text(
            "INSERT INTO register_transactions "
            "(id,dataset_id,submission_key,request_digest,business_date,kind,total_yen,received_yen,change_yen) "
            "VALUES (:id,:owner,:key,:digest,'2026-10-16','sale',200,200,0)"
        ), {"id": sale_id, "owner": admin_id, "key": key, "digest": "a" * 64})
    if occupied == "register_transaction_lines":
        db.session.execute(text(
            "INSERT INTO register_transaction_lines "
            "(transaction_id,dataset_id,register_product_id,product_name_snapshot,unit_price_yen,quantity,line_total_yen) "
            "VALUES (:id,:owner,11,'商品',200,1,200)"
        ), {"id": sale_id, "owner": admin_id})
    db.session.commit()

    # Flask-Migrate's CLI wrapper converts migration RuntimeError to exit(1).
    with pytest.raises(SystemExit) as error:
        downgrade(directory=str(MIGRATIONS_DIR), revision=OLD_HEAD)
    assert error.value.code == 1
    db.session.rollback()
    assert TABLES <= table_state()
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD
    assert db.session.execute(text(f"SELECT COUNT(*) FROM {occupied}")).scalar_one() == 1


def test_product_creation_key_upgrade_preserves_existing_data_and_rejects_duplicate(migration_app):
    migrate("f3b7e5a9c102")
    owner = db.session.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
    db.session.execute(text("INSERT INTO register_products (id,dataset_id,name,price_yen) "
                            "VALUES (11,:owner,'既存商品',280)"), {"owner": owner})
    db.session.commit()
    migrate("head")
    assert db.session.execute(text("SELECT name,price_yen,creation_key,creation_digest "
                                   "FROM register_products WHERE id=11")).one() == (
                                       "既存商品", 280, None, None)
    key = uuid.uuid4().hex
    db.session.execute(text("INSERT INTO register_products "
                            "(id,dataset_id,name,price_yen,creation_key,creation_digest) "
                            "VALUES (12,:owner,'新商品',200,:key,:digest)"),
                       {"owner": owner, "key": key, "digest": "a" * 64})
    db.session.commit()
    with pytest.raises(sa.exc.IntegrityError):
        db.session.execute(text("INSERT INTO register_products "
                                "(id,dataset_id,name,price_yen,creation_key,creation_digest) "
                                "VALUES (13,:owner,'重複',200,:key,:digest)"),
                           {"owner": owner, "key": key, "digest": "a" * 64})
    db.session.rollback()
    with pytest.raises(SystemExit):
        downgrade(directory=str(MIGRATIONS_DIR), revision="f3b7e5a9c102")
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD
    assert db.session.execute(text("SELECT COUNT(*) FROM register_products")).scalar_one() == 2


def test_price_history_migration_does_not_invent_old_prices_and_is_append_only(migration_app):
    migrate("c9d3e7a1b5f2")
    owner = db.session.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
    db.session.execute(text("INSERT INTO register_products (id,dataset_id,name,price_yen) "
                            "VALUES (11,:owner,'以前からある商品',280)"), {"owner": owner})
    db.session.commit()
    migrate("head")
    assert db.session.execute(text("SELECT COUNT(*) FROM register_price_history")).scalar_one() == 0
    db.session.execute(text("INSERT INTO register_price_history "
                            "(dataset_id,register_product_id,event,old_price_yen,new_price_yen) "
                            "VALUES (:owner,11,'changed',280,290)"), {"owner": owner})
    db.session.commit()
    with pytest.raises(sa.exc.DBAPIError):
        db.session.execute(text("DELETE FROM register_price_history WHERE register_product_id=11"))
    db.session.rollback()
    with pytest.raises(SystemExit):
        downgrade(directory=str(MIGRATIONS_DIR), revision="c9d3e7a1b5f2")
    db.session.rollback()
    assert db.session.execute(text("SELECT COUNT(*) FROM register_price_history")).scalar_one() == 1


def test_recovery_migration_preserves_legacy_and_refuses_populated_downgrade(migration_app):
    migrate("da5c1e7f9042")
    owner = db.session.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
    db.session.execute(text("INSERT INTO products (id,dataset_id,year,month,name,price,is_active) "
                            "VALUES (901,:owner,2026,10,'旧商品',280,true)"), {"owner": owner})
    db.session.execute(text("INSERT INTO daily_sales (id,product_id,date,quantity) "
                            "VALUES (902,901,'2026-10-16',3)"))
    db.session.execute(text("INSERT INTO register_products (id,dataset_id,name,price_yen) "
                            "VALUES (11,:owner,'パン',120)"), {"owner": owner})
    db.session.commit()
    migrate("head")
    assert db.session.execute(text("SELECT review_decisions FROM register_cutover_approvals")).all() == []
    db.session.execute(text("INSERT INTO register_recovery_cases "
        "(id,dataset_id,submission_key,source_digest,snapshot_json,status,kind,client_business_date,total_yen) "
        "VALUES (:id,:owner,:key,:digest,'{}','held','sale','2026-10-16',240)"),
        {"id": uuid.uuid4().hex, "owner": owner, "key": uuid.uuid4().hex, "digest": "a" * 64})
    db.session.commit()
    with pytest.raises(SystemExit):
        downgrade(directory=str(MIGRATIONS_DIR), revision="da5c1e7f9042")
    db.session.rollback()
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW_HEAD
    assert TABLES <= table_state()
    assert_previous_rows()


def test_link_migration_rejects_populated_downgrade_and_preserves_old_recipe(migration_app):
    migrate("a4e8c2f6d901")
    owner = db.session.execute(text("SELECT id FROM datasets WHERE system_key='admin'")).scalar_one()
    db.session.execute(text("INSERT INTO register_products (id,dataset_id,name,price_yen) "
                            "VALUES (11,:owner,'独自',120)"), {"owner": owner})
    db.session.execute(text("INSERT INTO prep_recipes "
                            "(id,dataset_id,product_key,display_name,base_quantity) "
                            "VALUES (21,:owner,'product:100','旧レシピ',10)"), {"owner": owner})
    db.session.commit()
    migrate("head")
    assert db.session.execute(text("SELECT product_key FROM prep_recipes WHERE id=21")
                              ).scalar_one() == "product:100"
    db.session.execute(text("INSERT INTO register_prep_links "
                            "(dataset_id,recipe_id,register_product_id) VALUES (:owner,21,11)"),
                       {"owner": owner})
    db.session.commit()
    with pytest.raises(SystemExit):
        downgrade(directory=str(MIGRATIONS_DIR), revision="a4e8c2f6d901")
    db.session.rollback()
    assert "register_prep_links" in table_state()
    assert db.session.execute(text("SELECT register_product_id FROM register_prep_links")
                              ).scalar_one() == 11
    assert db.session.execute(text("SELECT product_key FROM prep_recipes WHERE id=21")
                              ).scalar_one() == "product:100"
