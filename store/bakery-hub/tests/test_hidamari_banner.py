"""Home-only model-store banner and committed PNG asset."""
from pathlib import Path

from bs4 import BeautifulSoup
from flask import g

from test_shop_staff_shifts import guest_client


def test_banner_on_admin_and_guest_home_only(authenticated_client, admin_dataset, flask_app):
    guest, _ = guest_client(flask_app)
    for client in (authenticated_client, guest):
        g.pop("_login_user", None)
        response = client.get("/home")
        assert response.status_code == 200
        page = BeautifulSoup(response.data, "html.parser")
        banner = page.select_one('main.operations-shell .hidamari-banner img[alt="パン工房 ひだまり"]')
        assert banner is not None
        assert banner["src"] == "/static/hidamari-banner.png"
        assert page.select_one(".operations-grid #today-sales") is not None
    g.pop("_login_user", None)
    assert 'hidamari-banner' not in authenticated_client.get("/staff-shifts").get_data(as_text=True)


def test_banner_asset_is_real_png(flask_app):
    path = Path(flask_app.static_folder) / "hidamari-banner.png"
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
