"""Synthetic migration preservation; PostgreSQL requires explicit loopback opt-in."""
import datetime
from pathlib import Path
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade, downgrade
from alembic import command as alembic_command
import pytest
from sqlalchemy import create_engine, inspect, text

from admin_sessions import issue_admin_session, revoke_admin_session, token_hash, active_admin_session
from models import Dataset, Product, DailySales, ShopMemo, RegisterProduct, RegisterCutover, db
from postgresql_test_utils import get_isolated_postgresql_test_url
from register_offline_service import prepare_offline, revoke_offline
from register_service import utc_now, TOKYO
from startup_guard import check_schema

OLD = "c8d3e7f1a204"
NEW = "d7a9c2e4f601"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_app(request, tmp_path):
    schema = None
    options = {"hide_parameters": True}
    admin_engine = None
    if request.param == "postgresql":
        url = get_isolated_postgresql_test_url()
        if not url:
            pytest.skip("Opt-in loopback PostgreSQL server is not available")
        schema = "admin_session_test_" + uuid.uuid4().hex
        admin_engine = create_engine(url, hide_parameters=True)
        with admin_engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        options["connect_args"] = {"options": "-csearch_path=" + schema}
    else:
        url = f"sqlite:///{tmp_path / 'synthetic.db'}"
    app = Flask("admin_session_migration_test")
    app.config.update(SQLALCHEMY_DATABASE_URI=url, SQLALCHEMY_ENGINE_OPTIONS=options,
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY="synthetic-only",
                      ADMIN_SESSION_MAX_AGE_SECONDS=43200)
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS))
    with app.app_context():
        try:
            upgrade(directory=str(MIGRATIONS), revision=OLD)
            yield app
        finally:
            db.session.remove()
            db.engine.dispose()
            if admin_engine is not None:
                with admin_engine.begin() as conn:
                    conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
                admin_engine.dispose()


def snapshot():
    values = {}
    with db.engine.connect() as conn:
        for table in inspect(conn).get_table_names():
            if table in {"alembic_version", "admin_auth_sessions"}:
                continue
            quoted = conn.dialect.identifier_preparer.quote(table)
            values[table] = sorted([tuple(row) for row in conn.exec_driver_sql(f"SELECT * FROM {quoted}")], key=repr)
    return values


def seed_business_rows():
    now = utc_now()
    owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
    guest = Dataset(kind="guest", created_at=now, last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=2))
    db.session.add(guest)
    db.session.flush()
    for dataset in (owner, guest):
        product = Product(dataset=dataset, year=2026, month=10, name="保存確認", price=123)
        db.session.add(product)
        db.session.flush()
        db.session.add_all([DailySales(product_id=product.id, date=now.date(), quantity=3),
                            ShopMemo(dataset=dataset, title="架空メモ", body="保持確認")])
    db.session.commit()


def test_migration_preserves_business_data_and_startup_order(migration_app):
    seed_business_rows()
    before = snapshot()
    with pytest.raises(RuntimeError, match="revision"):
        check_schema(db.engine)
    upgrade(directory=str(MIGRATIONS), revision=NEW)
    assert snapshot() == before
    assert {x["name"] for x in inspect(db.engine).get_columns("admin_auth_sessions")} == {
        "token_hash", "issued_at", "expires_at", "revoked_at"}
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW
    db.session.rollback()
    for _ in range(2):
        check_schema(db.engine)
    assert snapshot() == before
    downgrade(directory=str(MIGRATIONS), revision=OLD)
    assert "admin_auth_sessions" not in inspect(db.engine).get_table_names()
    assert snapshot() == before
    with pytest.raises(RuntimeError, match="revision"):
        check_schema(db.engine)


def test_populated_session_downgrade_fails_before_discarding_records(migration_app):
    upgrade(directory=str(MIGRATIONS), revision=NEW)
    token = issue_admin_session()
    db.session.commit()
    with pytest.raises(RuntimeError, match="Refusing to discard"):
        migration_config = migration_app.extensions["migrate"].migrate.get_config(str(MIGRATIONS))
        alembic_command.downgrade(migration_config, OLD)
    db.session.remove()
    assert active_admin_session(token)
    assert db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == NEW


def test_postgresql_revocation_is_atomic_and_visible_after_commit(migration_app):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Separate-connection visibility requires PostgreSQL")
    upgrade(directory=str(MIGRATIONS), revision=NEW)
    owner = Dataset.query.filter_by(kind="admin", system_key="admin").one()
    day = utc_now().astimezone(TOKYO).date()
    db.session.add_all([RegisterProduct(dataset_id=owner.id, name="架空パン", price_yen=120),
                        RegisterCutover(dataset_id=owner.id, first_register_date=day)])
    db.session.commit()
    grant, _, _, _ = prepare_offline(owner, str(uuid.uuid4()))
    first, second = issue_admin_session(), issue_admin_session()
    db.session.commit()
    grant_id, digest = grant.id, token_hash(first)

    def observed():
        with db.engine.connect() as conn:
            auth = conn.execute(text("SELECT revoked_at FROM admin_auth_sessions WHERE token_hash=:digest"),
                                {"digest": digest}).scalar_one()
            offline = conn.execute(text("SELECT revoked_at FROM register_offline_permits WHERE id=:id"),
                                   {"id": grant_id}).scalar_one()
            return auth, offline

    revoke_offline(owner, commit=False)
    revoke_admin_session(first)
    assert observed() == (None, None)
    db.session.rollback()
    assert observed() == (None, None)
    revoke_offline(owner, commit=False)
    revoke_admin_session(first)
    db.session.commit()
    auth, offline = observed()
    assert auth is not None and offline is not None
    db.session.remove()
    assert not active_admin_session(first)
    assert active_admin_session(second)
