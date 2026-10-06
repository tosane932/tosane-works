from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from models import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
PRE_MATERIAL_ORDER_REVISION = "e6b4c2d8f0a1"
PRE_SHOP_MEMO_REVISION = "d4f7a9c2e6b1"
SHOP_MEMO_REVISION = "f8c2a6d4e1b3"


def test_empty_database_upgrades_from_base_to_head(tmp_path):
    database_path = tmp_path / "alembic_migration_test.sqlite"
    database_uri = f"sqlite:///{database_path}"

    migration_app = Flask("migration_test")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    configured_url = make_url(
        migration_app.config["SQLALCHEMY_DATABASE_URI"]
    )
    assert configured_url.drivername == "sqlite"
    assert configured_url.drivername != "postgresql"
    assert Path(configured_url.database).resolve().is_relative_to(
        tmp_path.resolve()
    )
    assert Path(configured_url.database).resolve() == database_path.resolve()
    assert Path(configured_url.database).resolve() != (
        PROJECT_ROOT / "local.db"
    ).resolve()

    alembic_config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(MIGRATIONS_DIR))
    expected_head = ScriptDirectory.from_config(
        alembic_config
    ).get_current_head()

    with migration_app.app_context():
        assert db.engine.url == configured_url
        assert inspect(db.engine).get_table_names() == []

        upgrade(directory=str(MIGRATIONS_DIR), revision="head")

        inspector = inspect(db.engine)
        table_names = set(inspector.get_table_names())
        assert {
            "datasets",
            "products",
            "daily_sales",
            "guest_creation_rate_limits",
            "material_order_items",
            "shop_memos",
            "shop_tasks",
            "alembic_version",
        }.issubset(table_names)

        product_columns = {
            column["name"]
            for column in inspector.get_columns("products")
        }
        assert {
            "id",
            "year",
            "month",
            "name",
            "price",
            "is_active",
            "image_key",
        }.issubset(product_columns)
        assert next(
            column for column in inspector.get_columns("products")
            if column["name"] == "image_key"
        )["nullable"] is True

        daily_sales_columns = {
            column["name"]
            for column in inspector.get_columns("daily_sales")
        }
        assert {
            "id",
            "product_id",
            "date",
            "quantity",
        }.issubset(daily_sales_columns)

        unique_constraints = inspector.get_unique_constraints("daily_sales")
        assert any(
            set(constraint["column_names"]) == {"product_id", "date"}
            for constraint in unique_constraints
        )

        material_order_columns = {
            column["name"]: column
            for column in inspector.get_columns("material_order_items")
        }
        assert set(material_order_columns) == {
            "id",
            "dataset_id",
            "name",
            "quantity_text",
            "memo",
            "is_completed",
            "created_at",
            "completed_at",
        }
        assert material_order_columns["dataset_id"]["nullable"] is False
        assert material_order_columns["name"]["nullable"] is False
        assert material_order_columns["quantity_text"]["nullable"] is True
        assert material_order_columns["memo"]["nullable"] is True
        assert material_order_columns["is_completed"]["nullable"] is False
        assert material_order_columns["created_at"]["nullable"] is False
        assert material_order_columns["completed_at"]["nullable"] is True

        material_order_foreign_keys = inspector.get_foreign_keys(
            "material_order_items"
        )
        assert len(material_order_foreign_keys) == 1
        material_order_foreign_key = material_order_foreign_keys[0]
        assert material_order_foreign_key["name"] == (
            "fk_material_order_items_dataset_id_datasets"
        )
        assert material_order_foreign_key["constrained_columns"] == [
            "dataset_id"
        ]
        assert material_order_foreign_key["referred_table"] == "datasets"
        assert material_order_foreign_key["referred_columns"] == ["id"]
        assert (
            material_order_foreign_key["options"].get("ondelete", "").upper()
            == "CASCADE"
        )

        material_order_checks = inspector.get_check_constraints(
            "material_order_items"
        )
        assert {
            constraint["name"] for constraint in material_order_checks
        } == {"ck_material_order_items_completion_timestamp"}

        material_order_indexes = inspector.get_indexes("material_order_items")
        assert any(
            index["name"]
            == "ix_material_order_items_dataset_status_created_id"
            and index["column_names"]
            == ["dataset_id", "is_completed", "created_at", "id"]
            and not index["unique"]
            for index in material_order_indexes
        )

        shop_memo_columns = {
            column["name"]: column
            for column in inspector.get_columns("shop_memos")
        }
        assert set(shop_memo_columns) == {
            "id",
            "dataset_id",
            "title",
            "body",
            "created_at",
            "updated_at",
            "deleted_at",
            "pinned_at",
        }
        assert isinstance(shop_memo_columns["id"]["type"], sa.Integer)
        assert isinstance(shop_memo_columns["title"]["type"], sa.String)
        assert shop_memo_columns["title"]["type"].length == 100
        assert isinstance(shop_memo_columns["body"]["type"], sa.Text)
        assert isinstance(
            shop_memo_columns["created_at"]["type"],
            sa.DateTime,
        )
        assert isinstance(
            shop_memo_columns["updated_at"]["type"],
            sa.DateTime,
        )
        assert isinstance(
            shop_memo_columns["deleted_at"]["type"],
            sa.DateTime,
        )
        assert isinstance(
            shop_memo_columns["pinned_at"]["type"],
            sa.DateTime,
        )
        assert shop_memo_columns["dataset_id"]["nullable"] is False
        assert shop_memo_columns["title"]["nullable"] is False
        assert shop_memo_columns["body"]["nullable"] is False
        assert shop_memo_columns["created_at"]["nullable"] is False
        assert shop_memo_columns["updated_at"]["nullable"] is False
        assert shop_memo_columns["deleted_at"]["nullable"] is True
        assert shop_memo_columns["pinned_at"]["nullable"] is True
        assert shop_memo_columns["created_at"]["default"] is not None
        assert shop_memo_columns["updated_at"]["default"] is not None
        assert shop_memo_columns["deleted_at"]["default"] is None
        assert shop_memo_columns["pinned_at"]["default"] is None

        shop_memo_foreign_keys = inspector.get_foreign_keys("shop_memos")
        assert len(shop_memo_foreign_keys) == 1
        shop_memo_foreign_key = shop_memo_foreign_keys[0]
        assert shop_memo_foreign_key["name"] == (
            "fk_shop_memos_dataset_id_datasets"
        )
        assert shop_memo_foreign_key["constrained_columns"] == ["dataset_id"]
        assert shop_memo_foreign_key["referred_table"] == "datasets"
        assert shop_memo_foreign_key["referred_columns"] == ["id"]
        assert shop_memo_foreign_key["options"].get(
            "ondelete",
            "",
        ).upper() == "CASCADE"

        shop_memo_checks = inspector.get_check_constraints("shop_memos")
        assert {constraint["name"] for constraint in shop_memo_checks} == {
            "ck_shop_memos_body_max_length",
            "ck_shop_memos_body_nonblank",
            "ck_shop_memos_deleted_not_before_creation",
            "ck_shop_memos_title_max_length",
            "ck_shop_memos_title_nonblank",
            "ck_shop_memos_updated_not_before_creation",
            "ck_shop_memos_updated_not_before_deletion",
            "ck_shop_memos_pinned_not_before_creation",
        }

        shop_memo_indexes = inspector.get_indexes("shop_memos")
        assert any(
            index["name"]
            == "ix_shop_memos_dataset_deleted_pinned_updated_id"
            and index["column_names"] == [
                "dataset_id",
                "deleted_at",
                "pinned_at",
                "updated_at",
                "id",
            ]
            and not index["unique"]
            for index in shop_memo_indexes
        )

        current_revision = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert expected_head is not None
        assert current_revision == expected_head


def test_material_order_migration_preserves_existing_sqlite_data(tmp_path):
    database_path = tmp_path / "material_order_upgrade_test.sqlite"
    database_uri = f"sqlite:///{database_path}"
    migration_app = Flask("material_order_upgrade_test")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    with migration_app.app_context():
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=PRE_MATERIAL_ORDER_REVISION,
        )
        admin_dataset_id = db.session.execute(
            text("SELECT id FROM datasets WHERE system_key = 'admin'")
        ).scalar_one()
        guest_dataset_id = "11111111111111111111111111111111"
        db.session.execute(
            text(
                "INSERT INTO datasets "
                "(id, kind, system_key, created_at, last_activity_at, "
                "absolute_expires_at, guest_ai_usage_count) "
                "VALUES (:id, 'guest', NULL, :created_at, :activity_at, "
                ":expires_at, 2)"
            ),
            {
                "id": guest_dataset_id,
                "created_at": "2026-09-13 03:00:00",
                "activity_at": "2026-09-13 03:00:00",
                "expires_at": "2026-09-13 05:00:00",
            },
        )
        db.session.execute(
            text(
                "INSERT INTO products "
                "(id, dataset_id, year, month, name, price, is_active) "
                "VALUES (901, :dataset_id, 2026, 9, '既存商品', 250, 1)"
            ),
            {"dataset_id": admin_dataset_id},
        )
        db.session.execute(
            text(
                "INSERT INTO daily_sales "
                "(id, product_id, date, quantity) "
                "VALUES (902, 901, '2026-09-12', 7)"
            )
        )
        db.session.execute(
            text(
                "INSERT INTO guest_creation_rate_limits "
                "(client_key_hash, window_started_at, request_count, "
                "updated_at) "
                "VALUES (:client_key_hash, :started_at, 2, :updated_at)"
            ),
            {
                "client_key_hash": "b" * 64,
                "started_at": "2026-09-13 03:00:00",
                "updated_at": "2026-09-13 03:00:00",
            },
        )
        db.session.commit()

        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=PRE_SHOP_MEMO_REVISION,
        )
        db.session.execute(
            text(
                "INSERT INTO material_order_items "
                "(id, dataset_id, name, is_completed, created_at) "
                "VALUES (903, :dataset_id, '既存材料', 0, :created_at)"
            ),
            {
                "dataset_id": guest_dataset_id,
                "created_at": "2026-09-13 03:00:00",
            },
        )
        db.session.commit()

        upgrade(directory=str(MIGRATIONS_DIR), revision="head")

        assert db.session.execute(
            text("SELECT COUNT(*) FROM datasets")
        ).scalar_one() == 2
        assert db.session.execute(
            text(
                "SELECT kind, guest_ai_usage_count FROM datasets "
                "WHERE id = :id"
            ),
            {"id": guest_dataset_id},
        ).one() == ("guest", 2)
        assert db.session.execute(
            text("SELECT name, price FROM products WHERE id = 901")
        ).one() == ("既存商品", 250)
        assert db.session.execute(
            text("SELECT quantity FROM daily_sales WHERE id = 902")
        ).scalar_one() == 7
        assert db.session.execute(
            text(
                "SELECT request_count FROM guest_creation_rate_limits "
                "WHERE client_key_hash = :client_key_hash"
            ),
            {"client_key_hash": "b" * 64},
        ).scalar_one() == 2
        assert db.session.execute(
            text("SELECT COUNT(*) FROM material_order_items")
        ).scalar_one() == 1
        assert db.session.execute(
            text(
                "SELECT name FROM material_order_items WHERE id = 903"
            )
        ).scalar_one() == "既存材料"
        assert db.session.execute(
            text("SELECT COUNT(*) FROM shop_memos")
        ).scalar_one() == 0


def test_shop_memo_title_migration_backfills_existing_sqlite_data(tmp_path):
    database_path = tmp_path / "shop_memo_title_upgrade_test.sqlite"
    migration_app = Flask("shop_memo_title_upgrade_test")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    with migration_app.app_context():
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=SHOP_MEMO_REVISION,
        )
        admin_dataset_id = db.session.execute(
            text("SELECT id FROM datasets WHERE system_key = 'admin'")
        ).scalar_one()
        db.session.execute(
            text(
                "INSERT INTO shop_memos "
                "(id, dataset_id, body, created_at, updated_at) "
                "VALUES (904, :dataset_id, :body, :created_at, :updated_at)"
            ),
            {
                "dataset_id": admin_dataset_id,
                "body": "  \n  移行後のタイトル  \n本文の続き",
                "created_at": "2026-09-18 03:00:00",
                "updated_at": "2026-09-18 03:00:00",
            },
        )
        db.session.commit()

        upgrade(directory=str(MIGRATIONS_DIR), revision="head")

        assert db.session.execute(
            text(
                "SELECT title, body, pinned_at "
                "FROM shop_memos WHERE id = 904"
            )
        ).one() == (
            "移行後のタイトル",
            "  \n  移行後のタイトル  \n本文の続き",
            None,
        )

        columns = {
            column["name"]: column
            for column in inspect(db.engine).get_columns("shop_memos")
        }
        assert columns["title"]["nullable"] is False


def test_shop_memo_pin_migration_round_trip_preserves_existing_sqlite_data(
    tmp_path,
):
    database_path = tmp_path / "shop_memo_pin_round_trip.sqlite"
    migration_app = Flask("shop_memo_pin_round_trip")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    with migration_app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="c92128da7c36")
        admin_dataset_id = db.session.execute(
            text("SELECT id FROM datasets WHERE system_key = 'admin'")
        ).scalar_one()
        db.session.execute(
            text(
                "INSERT INTO shop_memos "
                "(id, dataset_id, title, body, created_at, updated_at) "
                "VALUES (905, :dataset_id, '既存タイトル', '既存本文', "
                "'2026-09-19 03:00:00', '2026-09-19 03:00:00')"
            ),
            {"dataset_id": admin_dataset_id},
        )
        db.session.commit()

        upgrade(directory=str(MIGRATIONS_DIR), revision="head")

        assert db.session.execute(
            text(
                "SELECT title, body, pinned_at "
                "FROM shop_memos WHERE id = 905"
            )
        ).one() == ("既存タイトル", "既存本文", None)
        with pytest.raises(IntegrityError):
            db.session.execute(
                text(
                    "UPDATE shop_memos "
                    "SET pinned_at = '2026-09-19 02:59:59' "
                    "WHERE id = 905"
                )
            )
            db.session.commit()
        db.session.rollback()

        downgrade(directory=str(MIGRATIONS_DIR), revision="c92128da7c36")

        inspector = inspect(db.engine)
        assert "pinned_at" not in {
            column["name"] for column in inspector.get_columns("shop_memos")
        }
        assert db.session.execute(
            text("SELECT title, body FROM shop_memos WHERE id = 905")
        ).one() == ("既存タイトル", "既存本文")
        assert any(
            index["name"] == "ix_shop_memos_dataset_deleted_updated_id"
            and index["column_names"]
            == ["dataset_id", "deleted_at", "updated_at", "id"]
            for index in inspector.get_indexes("shop_memos")
        )


def test_product_image_key_migration_round_trip_preserves_products_and_sales(tmp_path):
    database_path = tmp_path / "product_image_key_round_trip.sqlite"
    migration_app = Flask("product_image_key_round_trip")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    with migration_app.app_context():
        upgrade(directory=str(MIGRATIONS_DIR), revision="f8c1d2e3a4b5")
        admin_dataset_id = db.session.execute(
            text("SELECT id FROM datasets WHERE system_key = 'admin'")
        ).scalar_one()
        db.session.execute(
            text(
                "INSERT INTO products (id, dataset_id, year, month, name, price, is_active) "
                "VALUES (906, :dataset_id, 2025, 12, '既存商品', 250, 1)"
            ),
            {"dataset_id": admin_dataset_id},
        )
        db.session.execute(text(
            "INSERT INTO daily_sales (id, product_id, date, quantity) "
            "VALUES (907, 906, '2025-12-10', 3)"
        ))
        db.session.commit()

        def product_and_sales():
            return (
                db.session.execute(text(
                    "SELECT id, dataset_id, year, month, name, price, is_active "
                    "FROM products WHERE id = 906"
                )).one(),
                db.session.execute(text(
                    "SELECT id, product_id, date, quantity FROM daily_sales WHERE id = 907"
                )).one(),
            )

        before = product_and_sales()
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        assert db.session.execute(text(
            "SELECT image_key FROM products WHERE id = 906"
        )).scalar_one() is None
        assert product_and_sales() == before
        db.session.execute(text(
            "UPDATE products SET image_key = 'bread_01' WHERE id = 906"
        ))
        db.session.commit()

        downgrade(directory=str(MIGRATIONS_DIR), revision="f8c1d2e3a4b5")
        assert "image_key" not in {
            column["name"] for column in inspect(db.engine).get_columns("products")
        }
        assert product_and_sales() == before

        upgrade(directory=str(MIGRATIONS_DIR), revision="head")
        column = next(
            column for column in inspect(db.engine).get_columns("products")
            if column["name"] == "image_key"
        )
        assert column["nullable"] is True
        assert db.session.execute(text(
            "SELECT image_key FROM products WHERE id = 906"
        )).scalar_one() is None
        assert product_and_sales() == before
