"""専用PostgreSQLだけで実際の複合FK・一意制約・並行POSTを確認する。"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from models import Dataset, PrepRecipe, PrepPlan, PrepIngredient, db
from postgresql_test_utils import get_isolated_postgresql_test_url

URL = get_isolated_postgresql_test_url()
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL URL is not configured")

WORKER = r'''
import concurrent.futures, json, threading
from werkzeug.security import generate_password_hash
import app as app_module
app = app_module.app
app.config.update(TESTING=True, SECRET_KEY='only-for-isolated-test', ADMIN_USERNAME='prep-admin',
                  ADMIN_PASSWORD_HASH=generate_password_hash('prep-password'),
                  SESSION_COOKIE_SECURE=False, WTF_CSRF_ENABLED=False,
                  ADMIN_LOGIN_RATE_LIMIT_TEST_CLIENT_IP='127.0.0.1')
barrier = threading.Barrier(2)
def post(_):
    client = app.test_client()
    assert client.post('/login', data={'username':'prep-admin','password':'prep-password'}).status_code == 302
    barrier.wait(timeout=15)
    return client.post('/prep/plans',data={'recipe_id':'1','business_date':'2026-10-15','quantity':'3'}).status_code
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    print(json.dumps(list(pool.map(post, range(2)))))
'''


def test_postgresql_dataset_constraints_and_concurrent_plan_posts():
    engine = create_engine(URL, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            db.metadata.create_all(conn)
            admin, guest = uuid.uuid4(), uuid.uuid4()
            conn.execute(Dataset.__table__.insert(), [
                {"id": admin, "kind": "admin", "system_key": "admin"},
                {"id": guest, "kind": "guest", "absolute_expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)},
            ])
            recipe_id = conn.execute(PrepRecipe.__table__.insert().values(
                dataset_id=admin, product_key="template:croissant", display_name="クロワッサン", base_quantity=10
            ).returning(PrepRecipe.id)).scalar_one()
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(PrepIngredient.__table__.insert().values(
                    dataset_id=guest, recipe_id=recipe_id, name="小麦粉", unit="g", amount=100))
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(PrepPlan.__table__.insert().values(
                    dataset_id=guest, recipe_id=recipe_id, business_date=datetime.date(2026, 10, 15), quantity=1))
        worker = subprocess.run([sys.executable, "-c", WORKER], cwd=Path(__file__).resolve().parent.parent,
                                env={**os.environ, "DATABASE_URL": URL}, capture_output=True, text=True, timeout=45)
        assert worker.returncode == 0, worker.stderr[-1500:]
        statuses = json.loads(worker.stdout.strip().splitlines()[-1])
        assert sorted(statuses) == [303, 409]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM prep_plans")).scalar_one() == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
