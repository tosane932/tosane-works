"""Synthetic-only Gunicorn entrypoint. Never import from a production command."""
import os
import re
import time

from sqlalchemy.engine import make_url
from postgresql_test_utils import get_isolated_postgresql_test_url

url = get_isolated_postgresql_test_url()
assert url and url == os.environ["DATABASE_URL"]
assert re.fullmatch(r"-csearch_path=admin_session_test_[0-9a-f]{32}",
                    make_url(url).query.get("options", ""))

import app as module
from models import db
from startup_guard import check_schema

app = module.app
app.config.update(SESSION_COOKIE_SECURE=False, REGISTER_V3_OFFLINE_MODE_ENABLED=True)
module.get_news = lambda limit=15: []
with app.app_context():
    check_schema(db.engine)


@app.before_request
def exercise_independent_workers():
    # Concurrent probes must give both sync workers an opportunity to accept.
    time.sleep(0.025)


@app.after_request
def identify_synthetic_worker(response):
    response.headers["X-Phase5-Pid"] = str(os.getpid())
    return response


@app.get("/__phase5/health")
def health():
    return "synthetic-ready"
