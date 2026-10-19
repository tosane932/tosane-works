"""インストール設定は公開の静的資産。業務画面の認証・非キャッシュを保つ。"""

import io
import json
import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import g
from PIL import Image
from sqlalchemy import event

from models import Dataset, db


MANIFEST = "/static/manifest.webmanifest"
ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_icons_are_public_without_database_access(client):
    def unexpected_query(*_args):
        pytest.fail("Public PWA assets must not query business data")

    event.listen(db.engine, "before_cursor_execute", unexpected_query)
    try:
        response = client.get(MANIFEST)
        assert response.status_code == 200
        assert response.mimetype == "application/manifest+json"
        data = json.loads(response.data)
        assert data["name"] == data["short_name"] == "Bakery Hub"
        assert data["id"] == "/bakery-hub"
        assert data["start_url"] == "/home"
        assert data["scope"] == "/"
        assert data["display"] == "standalone"
        assert data["lang"] == "ja"
        assert data["background_color"] == "#fff8ec"
        assert data["theme_color"] == "#72583b"
        assert not data.get("prefer_related_applications")
        assert "orientation" not in data
        assert {(icon["sizes"], icon["purpose"]) for icon in data["icons"]} >= {
            ("192x192", "any"), ("512x512", "any"), ("512x512", "maskable"),
        }
        for icon in data["icons"] + [{"src": "/static/pwa/apple-touch-icon.png", "sizes": "180x180"}]:
            assert icon["src"].startswith("/static/pwa/")
            result = client.get(icon["src"])
            assert result.status_code == 200 and result.mimetype == "image/png"
            with Image.open(io.BytesIO(result.data)) as image:
                size = tuple(map(int, icon["sizes"].split("x")))
                assert image.size == size
                assert image.convert("RGBA").getextrema()[3] == (255, 255)
                if icon.get("purpose") == "maskable":
                    rgb = image.convert("RGB")
                    background = rgb.getpixel((0, 0))
                    for y in range(image.height):
                        for x in range(image.width):
                            if rgb.getpixel((x, y)) != background:
                                assert (x - 256) ** 2 + (y - 256) ** 2 <= (512 * .4) ** 2
    finally:
        event.remove(db.engine, "before_cursor_execute", unexpected_query)


@pytest.mark.parametrize("path", [
    "/home", "/", "/products", "/dashboard", "/input", "/prep",
    "/staff-shifts", "/schedule", "/material-orders", "/shop-tools/memo",
    "/shop-tools/tasks", "/system-overview", "/register/demo",
])
def test_same_pwa_settings_on_existing_pages(flask_app, authenticated_client, admin_dataset, path):
    flask_app.config["REGISTER_V3_PUBLIC_DEMO_ENABLED"] = True
    g.pop("_login_user", None)
    response = authenticated_client.get(path)
    assert response.status_code == 200
    page = BeautifulSoup(response.data, "html.parser")
    assert [link["href"] for link in page.select('head link[rel="manifest"]')] == [MANIFEST]
    assert page.select_one('head meta[name="theme-color"]')["content"] == "#72583b"
    assert page.select_one('head link[rel="apple-touch-icon"]')
    assert response.headers["Cache-Control"] == "no-store"


def test_login_and_launch_use_existing_authentication(client, authenticated_client, admin_dataset, csrf_token):
    # Manifest itself supplies no session or authority.
    assert "Set-Cookie" not in client.get(MANIFEST).headers
    g.pop("_login_user", None)
    response = client.get("/home")
    assert response.status_code == 302
    assert urlsplit(response.location).path == "/login"
    login = BeautifulSoup(client.get(response.location).data, "html.parser")
    assert login.select_one('link[rel="manifest"]')["href"] == MANIFEST
    g.pop("_login_user", None)
    assert authenticated_client.get("/home").status_code == 200
    token = csrf_token(authenticated_client, "/home")
    result = authenticated_client.post("/logout", data={"csrf_token": token})
    assert urlsplit(result.location).path == "/login"
    g.pop("_login_user", None)
    assert authenticated_client.get("/home").status_code == 302


def test_all_full_html_templates_share_metadata_including_offline_shell():
    # Cover secondary forms, inherited screens and shells, not just the home.
    for template in (ROOT / "templates").rglob("*.html"):
        source = template.read_text()
        if "<head>" in source:
            assert '{% include "_pwa_head.html" %}' in source, template.name


def test_guest_manifest_does_not_grant_admin_or_extend_expiry(flask_app, client):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = Dataset(kind="guest", created_at=now - datetime.timedelta(hours=2), last_activity_at=now,
                    absolute_expires_at=now + datetime.timedelta(hours=1))
    db.session.add(guest)
    db.session.commit()
    with client.session_transaction() as cookie:
        cookie["_user_id"] = f"guest:{guest.id}"
        cookie["_fresh"] = True
    flask_app.config["REGISTER_V3_PUBLIC_DEMO_ENABLED"] = True
    g.pop("_login_user", None)
    page = BeautifulSoup(client.get("/home").data, "html.parser")
    assert page.select_one('head link[rel="manifest"]')["href"] == MANIFEST
    g.pop("_login_user", None)
    assert client.get("/register/demo").status_code == 403
    guest.absolute_expires_at = now - datetime.timedelta(minutes=1)
    db.session.commit()
    g.pop("_login_user", None)
    assert client.get(MANIFEST).status_code == 200
    g.pop("_login_user", None)
    assert client.get("/home").status_code == 302


def test_overview_distinguishes_manifest_worker_and_offline_data(client):
    page = BeautifulSoup(client.get("/system-overview").data, "html.parser")
    explanation = page.select_one("#feature-pwa").get_text(" ", strip=True)
    for phrase in ("PWA", "Web App Manifest", "Service Worker", "Cache Storage", "HTTPS",
                   "ネイティブアプリ", "/register/", "未同期", "新しいService Workerは登録しません"):
        assert phrase in explanation
