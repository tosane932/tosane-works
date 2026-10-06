from pathlib import Path
import datetime
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate, upgrade
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DataError, IntegrityError

from models import db
from postgresql_test_utils import get_isolated_postgresql_test_url


POSTGRESQL_TEST_DATABASE_URL = get_isolated_postgresql_test_url()

pytestmark = pytest.mark.skipif(
    not POSTGRESQL_TEST_DATABASE_URL,
    reason="isolated PostgreSQL URL is not configured",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
PRE_DATASET_REVISION = "9d3c1b7e5a42"
PRE_MATERIAL_ORDER_REVISION = "e6b4c2d8f0a1"
PRE_SHOP_MEMO_REVISION = "d4f7a9c2e6b1"
SHOP_MEMO_REVISION = "f8c2a6d4e1b3"


def _reset_public_schema(engine):
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def test_postgresql_migrations_reach_head_and_preserve_existing_data():
    reset_engine = create_engine(
        POSTGRESQL_TEST_DATABASE_URL,
        pool_pre_ping=True,
    )
    _reset_public_schema(reset_engine)

    migration_app = Flask("postgresql_migration_test")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=POSTGRESQL_TEST_DATABASE_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    alembic_config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location",
        str(MIGRATIONS_DIR),
    )
    expected_head = ScriptDirectory.from_config(
        alembic_config
    ).get_current_head()

    try:
        with migration_app.app_context():
            assert db.engine.url.get_backend_name() == "postgresql"
            assert inspect(db.engine).get_table_names() == []

            upgrade(
                directory=str(MIGRATIONS_DIR),
                revision=PRE_DATASET_REVISION,
            )
            db.session.execute(
                text(
                    "INSERT INTO products "
                    "(id, year, month, name, price, is_active) "
                    "VALUES (101, 2026, 8, '既存商品', 200, true)"
                )
            )
            db.session.execute(
                text(
                    "INSERT INTO daily_sales "
                    "(id, product_id, date, quantity) "
                    "VALUES (201, 101, '2026-08-19', 5)"
                )
            )
            db.session.commit()

            upgrade(
                directory=str(MIGRATIONS_DIR),
                revision=PRE_MATERIAL_ORDER_REVISION,
            )
            guest_dataset_id = uuid.uuid4()
            now = datetime.datetime(
                2026,
                9,
                13,
                3,
                0,
                tzinfo=datetime.timezone.utc,
            )
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
                    "created_at": now,
                    "activity_at": now,
                    "expires_at": now + datetime.timedelta(hours=2),
                },
            )
            db.session.execute(
                text(
                    "INSERT INTO guest_creation_rate_limits "
                    "(client_key_hash, window_started_at, request_count, "
                    "updated_at) "
                    "VALUES (:client_key_hash, :started_at, 2, :updated_at)"
                ),
                {
                    "client_key_hash": "a" * 64,
                    "started_at": now,
                    "updated_at": now,
                },
            )
            db.session.commit()

            upgrade(
                directory=str(MIGRATIONS_DIR),
                revision=PRE_SHOP_MEMO_REVISION,
            )
            material_order_item_id = db.session.execute(
                text(
                    "INSERT INTO material_order_items "
                    "(dataset_id, name, is_completed, created_at) "
                    "VALUES (:dataset_id, '既存材料', false, :now) "
                    "RETURNING id"
                ),
                {"dataset_id": guest_dataset_id, "now": now},
            ).scalar_one()
            db.session.commit()

            upgrade(
                directory=str(MIGRATIONS_DIR),
                revision=SHOP_MEMO_REVISION,
            )
            existing_shop_memo_id = db.session.execute(
                text(
                    "INSERT INTO shop_memos "
                    "(dataset_id, body, created_at, updated_at) "
                    "VALUES (:dataset_id, :body, :now, :now) "
                    "RETURNING id"
                ),
                {
                    "dataset_id": guest_dataset_id,
                    "body": "  \n  既存メモタイトル  \n本文の続き",
                    "now": now,
                },
            ).scalar_one()
            db.session.commit()

            upgrade(directory=str(MIGRATIONS_DIR), revision="head")

            inspector = inspect(db.engine)
            assert {
                "datasets",
                "products",
                "daily_sales",
                "guest_creation_rate_limits",
                "material_order_items",
                "shop_memos",
                "shop_tasks",
                "alembic_version",
            }.issubset(inspector.get_table_names())
            assert db.session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == expected_head

            product = db.session.execute(
                text(
                    "SELECT id, dataset_id, name, price, image_key "
                    "FROM products WHERE id = 101"
                )
            ).one()
            sale = db.session.execute(
                text(
                    "SELECT id, product_id, date, quantity "
                    "FROM daily_sales WHERE id = 201"
                )
            ).one()

            assert product.id == 101
            assert product.dataset_id is not None
            assert product.name == "既存商品"
            assert product.price == 200
            assert product.image_key is None
            assert next(
                column for column in inspector.get_columns("products")
                if column["name"] == "image_key"
            )["nullable"] is True
            assert sale.id == 201
            assert sale.product_id == 101
            assert sale.quantity == 5
            assert db.session.execute(
                text(
                    "SELECT COUNT(*) FROM datasets "
                    "WHERE id = :dataset_id "
                    "AND kind = 'admin' "
                    "AND system_key = 'admin'"
                ),
                {"dataset_id": product.dataset_id},
            ).scalar_one() == 1
            assert db.session.execute(
                text("SELECT COUNT(*) FROM products")
            ).scalar_one() == 1
            assert db.session.execute(
                text("SELECT COUNT(*) FROM daily_sales")
            ).scalar_one() == 1
            guest_dataset = db.session.execute(
                text(
                    "SELECT kind, system_key, guest_ai_usage_count "
                    "FROM datasets WHERE id = :dataset_id"
                ),
                {"dataset_id": guest_dataset_id},
            ).one()
            assert guest_dataset.kind == "guest"
            assert guest_dataset.system_key is None
            assert guest_dataset.guest_ai_usage_count == 2
            assert db.session.execute(
                text(
                    "SELECT request_count "
                    "FROM guest_creation_rate_limits "
                    "WHERE client_key_hash = :client_key_hash"
                ),
                {"client_key_hash": "a" * 64},
            ).scalar_one() == 2

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
            material_order_foreign_keys = inspector.get_foreign_keys(
                "material_order_items"
            )
            assert len(material_order_foreign_keys) == 1
            assert material_order_foreign_keys[0]["constrained_columns"] == [
                "dataset_id"
            ]
            assert material_order_foreign_keys[0]["referred_table"] == (
                "datasets"
            )
            assert (
                material_order_foreign_keys[0]["options"]["ondelete"].upper()
                == "CASCADE"
            )
            assert {
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    "material_order_items"
                )
            } == {"ck_material_order_items_completion_timestamp"}
            assert any(
                index["name"]
                == "ix_material_order_items_dataset_status_created_id"
                and index["column_names"]
                == ["dataset_id", "is_completed", "created_at", "id"]
                and not index["unique"]
                for index in inspector.get_indexes("material_order_items")
            )

            assert db.session.execute(
                text(
                    "SELECT name FROM material_order_items WHERE id = :id"
                ),
                {"id": material_order_item_id},
            ).scalar_one() == "既存材料"

            shop_task_columns = {
                column["name"]: column
                for column in inspector.get_columns("shop_tasks")
            }
            assert set(shop_task_columns) == {
                "id",
                "dataset_id",
                "title",
                "is_completed",
                "is_starred",
                "position",
                "created_at",
                "completed_at",
            }
            assert isinstance(shop_task_columns["dataset_id"]["type"], sa.Uuid)
            assert isinstance(shop_task_columns["title"]["type"], sa.String)
            assert shop_task_columns["title"]["type"].length == 100
            assert shop_task_columns["dataset_id"]["nullable"] is False
            assert shop_task_columns["title"]["nullable"] is False
            assert shop_task_columns["is_completed"]["nullable"] is False
            assert shop_task_columns["is_starred"]["nullable"] is False
            assert shop_task_columns["position"]["nullable"] is False
            assert shop_task_columns["created_at"]["nullable"] is False
            assert shop_task_columns["completed_at"]["nullable"] is True
            assert shop_task_columns["created_at"]["type"].timezone is True
            assert shop_task_columns["completed_at"]["type"].timezone is True

            shop_task_foreign_key = inspector.get_foreign_keys(
                "shop_tasks"
            )[0]
            assert shop_task_foreign_key["name"] == (
                "fk_shop_tasks_dataset_id_datasets"
            )
            assert shop_task_foreign_key["constrained_columns"] == [
                "dataset_id"
            ]
            assert shop_task_foreign_key["referred_table"] == "datasets"
            assert (
                shop_task_foreign_key["options"]["ondelete"].upper()
                == "CASCADE"
            )
            assert {
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    "shop_tasks"
                )
            } == {
                "ck_shop_tasks_title_nonblank",
                "ck_shop_tasks_title_max_length",
                "ck_shop_tasks_completion_timestamp",
                "ck_shop_tasks_position_nonnegative",
            }
            assert any(
                index["name"] == "ix_shop_tasks_dataset_status_position_id"
                and index["column_names"]
                == ["dataset_id", "is_completed", "position", "id"]
                and not index["unique"]
                for index in inspector.get_indexes("shop_tasks")
            )

            db.session.execute(
                text(
                    "INSERT INTO shop_tasks "
                    "(dataset_id, title, is_completed, created_at, completed_at) "
                    "VALUES (:dataset_id, 'PostgreSQLタスク', false, :now, NULL)"
                ),
                {"dataset_id": guest_dataset_id, "now": now},
            )
            db.session.commit()
            invalid_task_rows = (
                {"title": "   ", "completed": False, "completed_at": None},
                {"title": "タ" * 101, "completed": False, "completed_at": None},
                {"title": "不整合", "completed": True, "completed_at": None},
                {
                    "title": "逆向き不整合",
                    "completed": False,
                    "completed_at": now,
                },
            )
            for invalid_task in invalid_task_rows:
                with pytest.raises((IntegrityError, DataError)):
                    db.session.execute(
                        text(
                            "INSERT INTO shop_tasks "
                            "(dataset_id, title, is_completed, created_at, completed_at) "
                            "VALUES (:dataset_id, :title, :completed, :now, :completed_at)"
                        ),
                        {
                            "dataset_id": guest_dataset_id,
                            "now": now,
                            **invalid_task,
                        },
                    )
                    db.session.commit()
                db.session.rollback()

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
            assert isinstance(shop_memo_columns["dataset_id"]["type"], sa.Uuid)
            assert isinstance(shop_memo_columns["title"]["type"], sa.String)
            assert shop_memo_columns["title"]["type"].length == 100
            assert isinstance(shop_memo_columns["body"]["type"], sa.Text)
            for timestamp_column in (
                "created_at",
                "updated_at",
                "deleted_at",
                "pinned_at",
            ):
                column_type = shop_memo_columns[timestamp_column]["type"]
                assert isinstance(column_type, sa.DateTime)
                assert column_type.timezone is True
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
            assert shop_memo_foreign_keys[0]["name"] == (
                "fk_shop_memos_dataset_id_datasets"
            )
            assert shop_memo_foreign_keys[0]["constrained_columns"] == [
                "dataset_id"
            ]
            assert shop_memo_foreign_keys[0]["referred_table"] == "datasets"
            assert (
                shop_memo_foreign_keys[0]["options"]["ondelete"].upper()
                == "CASCADE"
            )
            assert {
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    "shop_memos"
                )
            } == {
                "ck_shop_memos_body_max_length",
                "ck_shop_memos_body_nonblank",
                "ck_shop_memos_deleted_not_before_creation",
                "ck_shop_memos_title_max_length",
                "ck_shop_memos_title_nonblank",
                "ck_shop_memos_updated_not_before_creation",
                "ck_shop_memos_updated_not_before_deletion",
                "ck_shop_memos_pinned_not_before_creation",
            }
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
                for index in inspector.get_indexes("shop_memos")
            )

            insert_shop_memo = text(
                "INSERT INTO shop_memos "
                "(dataset_id, title, body, created_at, updated_at, "
                "deleted_at) "
                "VALUES (:dataset_id, :title, :body, :created_at, "
                ":updated_at, :deleted_at) RETURNING id"
            )
            valid_memo_rows = [
                {
                    "dataset_id": guest_dataset_id,
                    "title": "通常メモ",
                    "body": "通常メモ",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
                {
                    "dataset_id": guest_dataset_id,
                    "title": "ゴミ箱メモ",
                    "body": "ゴミ箱メモ",
                    "created_at": now,
                    "updated_at": now + datetime.timedelta(minutes=1),
                    "deleted_at": now + datetime.timedelta(minutes=1),
                },
                {
                    "dataset_id": guest_dataset_id,
                    "title": "メ" * 100,
                    "body": "メ" * 2000,
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
            ]
            memo_ids = [
                db.session.execute(insert_shop_memo, row).scalar_one()
                for row in valid_memo_rows
            ]
            db.session.commit()
            assert len(memo_ids) == 3
            assert db.session.execute(
                text("SELECT title FROM shop_memos WHERE id = :id"),
                {"id": existing_shop_memo_id},
            ).scalar_one() == "既存メモタイトル"
            assert db.session.execute(
                text("SELECT pinned_at FROM shop_memos WHERE id = :id"),
                {"id": existing_shop_memo_id},
            ).scalar_one() is None
            with pytest.raises(IntegrityError):
                db.session.execute(
                    text(
                        "UPDATE shop_memos "
                        "SET pinned_at = :invalid_pinned_at "
                        "WHERE id = :id"
                    ),
                    {
                        "id": existing_shop_memo_id,
                        "invalid_pinned_at": (
                            now - datetime.timedelta(seconds=1)
                        ),
                    },
                )
                db.session.commit()
            db.session.rollback()

            invalid_memo_rows = [
                {
                    "title": "",
                    "body": "タイトル空文字",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
                {
                    "body": "",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
                {
                    "body": "   ",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
                {
                    "body": "メ" * 2001,
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                },
                {
                    "body": "更新日時違反",
                    "created_at": now,
                    "updated_at": now - datetime.timedelta(seconds=1),
                    "deleted_at": None,
                },
                {
                    "body": "削除日時違反",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": now - datetime.timedelta(seconds=1),
                },
                {
                    "body": "更新削除日時違反",
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": now + datetime.timedelta(seconds=1),
                },
            ]
            for invalid_row in invalid_memo_rows:
                with pytest.raises(IntegrityError):
                    db.session.execute(
                        insert_shop_memo,
                        {
                            "dataset_id": guest_dataset_id,
                            "title": "制約検証タイトル",
                            **invalid_row,
                        },
                    )
                    db.session.commit()
                db.session.rollback()

            with pytest.raises(DataError):
                db.session.execute(
                    insert_shop_memo,
                    {
                        "dataset_id": guest_dataset_id,
                        "title": "タ" * 101,
                        "body": "タイトル101文字",
                        "created_at": now,
                        "updated_at": now,
                        "deleted_at": None,
                    },
                )
                db.session.commit()
            db.session.rollback()

            db.session.execute(
                text("DELETE FROM datasets WHERE id = :dataset_id"),
                {"dataset_id": guest_dataset_id},
            )
            db.session.commit()

            assert db.session.execute(
                text(
                    "SELECT COUNT(*) FROM material_order_items "
                    "WHERE id = :item_id"
                ),
                {"item_id": material_order_item_id},
            ).scalar_one() == 0
            assert db.session.execute(
                text(
                    "SELECT COUNT(*) FROM shop_memos "
                    "WHERE dataset_id = :dataset_id"
                ),
                {"dataset_id": guest_dataset_id},
            ).scalar_one() == 0
            assert db.session.execute(
                text(
                    "SELECT COUNT(*) FROM shop_tasks "
                    "WHERE dataset_id = :dataset_id"
                ),
                {"dataset_id": guest_dataset_id},
            ).scalar_one() == 0
            assert db.session.execute(
                text("SELECT COUNT(*) FROM products")
            ).scalar_one() == 1
            assert db.session.execute(
                text("SELECT COUNT(*) FROM daily_sales")
            ).scalar_one() == 1

            db.session.remove()
            db.engine.dispose()
    finally:
        _reset_public_schema(reset_engine)
        reset_engine.dispose()
