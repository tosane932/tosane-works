import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from flask import Flask
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from models import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
PREVIOUS_REVISION = "9d3c1b7e5a42"
DATASET_REVISION = "c7a1d9e4f2b6"
NOT_NULL_REVISION = "f2b6c8d4e1a9"
HEAD_REVISION = "c4e8a1d7b2f6"


def _migration_app(tmp_path, name):
    database_path = tmp_path / f"{name}.sqlite"
    migration_app = Flask(name)
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))
    return migration_app


def _assert_product_dataset_schema(inspector, *, nullable):
    product_columns = {
        column["name"]: column
        for column in inspector.get_columns("products")
    }
    assert product_columns["dataset_id"]["nullable"] is nullable

    dataset_foreign_keys = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys("products")
        if foreign_key["constrained_columns"] == ["dataset_id"]
    ]
    assert len(dataset_foreign_keys) == 1
    assert dataset_foreign_keys[0]["referred_table"] == "datasets"
    assert dataset_foreign_keys[0]["referred_columns"] == ["id"]
    assert (
        dataset_foreign_keys[0].get("options", {}).get("ondelete")
        == "CASCADE"
    )

    product_indexes = inspector.get_indexes("products")
    assert any(
        index["name"] == "ix_products_dataset_id"
        and index["column_names"] == ["dataset_id"]
        for index in product_indexes
    )


def _assert_dataset_constraints(inspector):
    dataset_check_names = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("datasets")
    }
    assert {
        "ck_datasets_kind",
        "ck_datasets_system_key_by_kind",
        "ck_datasets_absolute_expiry_by_kind",
        "ck_datasets_activity_not_before_creation",
        "ck_datasets_expiry_after_creation",
    }.issubset(dataset_check_names)
    assert any(
        constraint["name"] == "uq_datasets_system_key"
        and constraint["column_names"] == ["system_key"]
        for constraint in inspector.get_unique_constraints("datasets")
    )


def test_empty_database_reaches_not_null_head_with_dataset_schema(tmp_path):
    migration_app = _migration_app(tmp_path, "empty_not_null_migration")

    with migration_app.app_context():
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=DATASET_REVISION,
        )
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=NOT_NULL_REVISION,
        )
        upgrade(directory=str(MIGRATIONS_DIR), revision="head")

        inspector = inspect(db.engine)
        assert "datasets" in inspector.get_table_names()
        assert db.session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM datasets
                WHERE kind = 'admin' AND system_key = 'admin'
                """
            )
        ).scalar_one() == 1
        _assert_product_dataset_schema(inspector, nullable=False)
        _assert_dataset_constraints(inspector)
        assert db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == HEAD_REVISION


def test_existing_products_are_backfilled_to_admin_dataset(tmp_path):
    database_path = tmp_path / "dataset_migration_test.sqlite"
    database_uri = f"sqlite:///{database_path}"

    migration_app = Flask("dataset_migration_test")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(migration_app)
    Migrate(migration_app, db, directory=str(MIGRATIONS_DIR))

    with migration_app.app_context():
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=PREVIOUS_REVISION,
        )

        db.session.execute(
            text(
                """
                INSERT INTO products
                    (id, year, month, name, price, is_active)
                VALUES
                    (101, 2026, 8, '既存商品A', 200, 1),
                    (102, 2026, 8, '既存商品B', 300, 1)
                """
            )
        )
        db.session.execute(
            text(
                """
                INSERT INTO daily_sales
                    (id, product_id, date, quantity)
                VALUES
                    (201, 101, '2026-08-19', 5)
                """
            )
        )
        db.session.commit()

        product_ids_before = db.session.execute(
            text("SELECT id FROM products ORDER BY id")
        ).scalars().all()
        product_count_before = db.session.execute(
            text("SELECT COUNT(*) FROM products")
        ).scalar_one()
        sales_count_before = db.session.execute(
            text("SELECT COUNT(*) FROM daily_sales")
        ).scalar_one()
        db.session.remove()

        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=DATASET_REVISION,
        )
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=NOT_NULL_REVISION,
        )

        product_rows = db.session.execute(
            text(
                """
                SELECT id, dataset_id
                FROM products
                ORDER BY id
                """
            )
        ).all()
        admin_rows = db.session.execute(
            text(
                """
                SELECT id, created_at, last_activity_at, absolute_expires_at
                FROM datasets
                WHERE kind = 'admin' AND system_key = 'admin'
                """
            )
        ).all()

        assert len(admin_rows) == 1
        admin_id = admin_rows[0].id
        assert admin_rows[0].created_at == admin_rows[0].last_activity_at
        assert admin_rows[0].absolute_expires_at is None

        assert len(product_rows) == product_count_before
        assert [row.id for row in product_rows] == product_ids_before
        assert all(row.dataset_id == admin_id for row in product_rows)
        assert db.session.execute(
            text(
                "SELECT COUNT(*) FROM products WHERE dataset_id IS NULL"
            )
        ).scalar_one() == 0
        assert db.session.execute(
            text("SELECT COUNT(*) FROM daily_sales")
        ).scalar_one() == sales_count_before
        assert db.session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM products AS p
                LEFT JOIN datasets AS d ON p.dataset_id = d.id
                WHERE d.id IS NULL
                """
            )
        ).scalar_one() == 0

        inspector = inspect(db.engine)
        _assert_product_dataset_schema(inspector, nullable=False)
        _assert_dataset_constraints(inspector)

        db.session.commit()
        db.session.execute(text("PRAGMA foreign_keys = ON"))
        assert db.session.execute(
            text("PRAGMA foreign_keys")
        ).scalar_one() == 1

        products = sa.table(
            "products",
            sa.column("id", sa.Integer()),
            sa.column("dataset_id", sa.Uuid(as_uuid=True)),
            sa.column("year", sa.Integer()),
            sa.column("month", sa.Integer()),
            sa.column("name", sa.String(length=100)),
            sa.column("price", sa.Integer()),
            sa.column("is_active", sa.Boolean()),
        )
        with pytest.raises(IntegrityError) as error:
            db.session.execute(
                products.insert().values(
                    id=103,
                    dataset_id=uuid.uuid4(),
                    year=2026,
                    month=8,
                    name="不正Dataset商品",
                    price=400,
                    is_active=True,
                )
            )
            db.session.commit()

        db.session.rollback()
        assert "FOREIGN KEY constraint failed" in str(error.value)

        with pytest.raises(IntegrityError) as error:
            db.session.execute(
                products.insert().values(
                    id=104,
                    dataset_id=None,
                    year=2026,
                    month=8,
                    name="Datasetなし商品",
                    price=500,
                    is_active=True,
                )
            )
            db.session.commit()

        db.session.rollback()
        assert "NOT NULL constraint failed" in str(error.value)


def test_not_null_migration_backfills_residual_null_product(tmp_path):
    migration_app = _migration_app(
        tmp_path,
        "residual_null_product_migration",
    )

    with migration_app.app_context():
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=DATASET_REVISION,
        )
        _assert_product_dataset_schema(inspect(db.engine), nullable=True)

        db.session.execute(
            text(
                """
                INSERT INTO products
                    (id, dataset_id, year, month, name, price, is_active)
                VALUES
                    (301, NULL, 2026, 8, '残存NULL商品', 600, 1)
                """
            )
        )
        db.session.commit()
        assert db.session.execute(
            text(
                "SELECT COUNT(*) FROM products WHERE dataset_id IS NULL"
            )
        ).scalar_one() == 1
        db.session.remove()

        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=NOT_NULL_REVISION,
        )

        assert db.session.execute(
            text(
                "SELECT COUNT(*) FROM products WHERE dataset_id IS NULL"
            )
        ).scalar_one() == 0
        assert db.session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM products AS p
                JOIN datasets AS d ON p.dataset_id = d.id
                WHERE p.id = 301
                  AND d.kind = 'admin'
                  AND d.system_key = 'admin'
                """
            )
        ).scalar_one() == 1
        _assert_product_dataset_schema(inspect(db.engine), nullable=False)


def test_not_null_downgrade_only_restores_nullable_dataset_id(tmp_path):
    migration_app = _migration_app(tmp_path, "not_null_downgrade")

    with migration_app.app_context():
        upgrade(
            directory=str(MIGRATIONS_DIR),
            revision=NOT_NULL_REVISION,
        )
        downgrade(
            directory=str(MIGRATIONS_DIR),
            revision=DATASET_REVISION,
        )

        inspector = inspect(db.engine)
        assert "datasets" in inspector.get_table_names()
        _assert_product_dataset_schema(inspector, nullable=True)
        _assert_dataset_constraints(inspector)
        assert db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == DATASET_REVISION
