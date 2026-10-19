"""本番には接続しない。CIのloopbackから別々の空DBを新規作成する。"""
from pathlib import Path
import re
import uuid

from flask import Flask
from flask_migrate import Migrate, upgrade
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from data_vault import VaultError, export_bundle, import_bundle
from models import Dataset, RegisterProduct, RegisterPriceHistory, PrepRecipe, PrepIngredient, db
from postgresql_test_utils import get_isolated_postgresql_test_url
from tests.test_data_vault import sample, business_snapshot, STATIC

TEST_URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not TEST_URL, reason='隔離PostgreSQLの設定なし')


@pytest.fixture()
def pg_pair():
    control = create_engine(TEST_URL, isolation_level='AUTOCOMMIT')
    suffix = uuid.uuid4().hex[:12]
    names = [f'sales_data_app_test_vault_{suffix}_{role}' for role in ('source', 'target')]
    created, engines, owners = [], [], []
    try:
        for name in names:
            assert re.fullmatch(r'sales_data_app_test_vault_[0-9a-f]{12}_(source|target)', name)
            with control.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
            created.append(name)
            url = control.url.set(database=name)
            app = Flask('isolated_settings_restore')
            app.config.update(SQLALCHEMY_DATABASE_URI=url.render_as_string(hide_password=False),
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
            db.init_app(app)
            migrations = str(Path(__file__).resolve().parents[1]/'migrations')
            Migrate(app, db, directory=migrations)
            with app.app_context():
                upgrade(directory=migrations, revision='head')
                owners.append(db.session.query(Dataset.id).filter_by(system_key='admin').scalar())
                db.session.remove()
                db.engine.dispose()
            engines.append(create_engine(url))
        yield engines[0], owners[0], engines[1], owners[1]
    finally:
        for engine in engines:
            engine.dispose()
        for name in reversed(created):
            with control.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        control.dispose()


def test_postgresql_real_schema_export_restore_matches_every_business_value(pg_pair, tmp_path):
    source, src, target, dst = pg_pair
    sample(source, src)
    folder = tmp_path/'snapshot'
    statements = []
    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(source, 'before_cursor_execute', observe)
    export_bundle(source, src, folder, static_folder=STATIC)
    event.remove(source, 'before_cursor_execute', observe)
    assert statements[0] == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'
    assert not any(q.startswith(('INSERT','UPDATE','DELETE')) for q in statements)
    assert import_bundle(target, dst, folder, static_folder=STATIC)['dry_run']
    with Session(target) as s:
        assert not s.scalars(select(RegisterProduct)).all()
        assert s.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == 'b6d2e9f4a107'
    import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    assert business_snapshot(source, src) == business_snapshot(target, dst)
    with Session(target) as s:
        assert set(s.scalars(select(RegisterProduct.id))).isdisjoint({101,102,103})
        assert not s.scalars(select(RegisterPriceHistory)).all()
    with pytest.raises(VaultError):
        import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))


def test_postgresql_restore_failure_rolls_back_and_can_retry(pg_pair, tmp_path):
    source, src, target, dst = pg_pair
    sample(source, src)
    folder = tmp_path/'rollback'
    export_bundle(source, src, folder, static_folder=STATIC)
    def fail(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO prep_ingredients'):
            raise RuntimeError('synthetic import interruption')
    event.listen(target, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic'):
            import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    finally:
        event.remove(target, 'before_cursor_execute', fail)
    with Session(target) as s:
        for model in (RegisterProduct, PrepRecipe, PrepIngredient):
            assert not s.scalars(select(model)).all()
    import_bundle(target, dst, folder, static_folder=STATIC, apply=True, confirm_dataset=str(dst))
    assert business_snapshot(source, src) == business_snapshot(target, dst)
