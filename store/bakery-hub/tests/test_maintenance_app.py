"""The temporary cutover image must never load Bakery Hub or touch its DB."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_maintenance_container_runs_only_standalone_app():
    command = (ROOT / "Dockerfile").read_text().split("CMD ", 1)[1]
    assert "maintenance_app:app" in command
    assert "startup_guard.py" not in command
    assert "app:app" not in command.replace("maintenance_app:app", "")
    assert "flask db upgrade" not in command
    assert "seed_demo.py" not in command


def test_maintenance_refuses_all_business_requests_without_importing_app():
    code = '''
import io
import sys
from wsgiref.util import setup_testing_defaults
from maintenance_app import app
for method, path in (("GET", "/login"), ("POST", "/login"),
                     ("POST", "/guest/start"), ("GET", "/home"),
                     ("POST", "/api/register/offline/sync"),
                     ("GET", "/api/register/transactions/key/status"),
                     ("GET", "/register/offline"),
                     ("GET", "/static/register.js"), ("HEAD", "/")):
    env = {}
    setup_testing_defaults(env)
    env["REQUEST_METHOD"] = method
    env["PATH_INFO"] = path
    env["wsgi.input"] = io.BytesIO(b"any request body")
    recorded = []
    body = b"".join(app(env, lambda status, headers: recorded.append((status, dict(headers)))))
    status, headers = recorded[0]
    assert status.startswith("503 "), (method, path, status)
    assert headers["Cache-Control"] == "no-store"
    assert headers["Retry-After"] == "120"
    assert "Set-Cookie" not in headers
    assert body == (b"" if method == "HEAD" else "ただいまメンテナンス中です。しばらくしてから再度お試しください。".encode())
assert "app" not in sys.modules and "models" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
