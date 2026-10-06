import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup

import app as app_module
from models import DailySales, Dataset, Product, db


def _guest_dataset():
    now = datetime.datetime.now(datetime.timezone.utc)
    return Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )


def _client_for(flask_app, dataset):
    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["_fresh"] = True
        if dataset.kind == "admin":
            session["_user_id"] = "admin"
            session[app_module.ADMIN_AUTH_FINGERPRINT_SESSION_KEY] = (
                app_module._get_admin_auth_fingerprint(
                    flask_app.config["ADMIN_PASSWORD_HASH"]
                )
            )
        else:
            session["_user_id"] = f"guest:{dataset.id}"
    return client


def _get(flask_app, client, path="/products"):
    # Flask-Loginの利用者キャッシュを別clientへ持ち越さない。
    with flask_app.app_context():
        return client.get(path)


def _document(response):
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _snapshot():
    return (
        [
            (p.id, p.dataset_id, p.year, p.month, p.name, p.price, p.is_active)
            for p in Product.query.order_by(Product.id).all()
        ],
        [
            (s.id, s.product_id, s.date, s.quantity)
            for s in DailySales.query.order_by(DailySales.id).all()
        ],
    )


@pytest.fixture()
def catalog_datasets(admin_dataset):
    datasets = {
        "admin": admin_dataset,
        "guest_a": _guest_dataset(),
        "guest_b": _guest_dataset(),
    }
    db.session.add_all(list(datasets.values()))
    for index, (key, dataset) in enumerate(datasets.items(), start=1):
        db.session.add(Product(
            dataset=dataset,
            year=2026,
            month=8,
            name=f"CATALOG_{key}",
            price=index * 100,
        ))
    db.session.commit()
    return datasets


def test_catalog_anonymous_redirects_to_login(client):
    response = client.get("/products")
    assert response.status_code == 302
    location = urlparse(response.headers["Location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["next"] == ["/products"]


@pytest.mark.parametrize("principal", ["admin", "guest_a", "guest_b"])
def test_catalog_displays_only_current_dataset(
    flask_app, catalog_datasets, principal,
):
    client = _client_for(flask_app, catalog_datasets[principal])
    before = _snapshot()
    document = _document(_get(flask_app, client))
    names = [n.get_text(strip=True) for n in document.select(".product-catalog-name")]
    assert names == [f"CATALOG_{principal}"]
    for other in set(catalog_datasets) - {principal}:
        assert f"CATALOG_{other}" not in document.get_text()
    assert _snapshot() == before


@pytest.mark.parametrize("principal", ["admin", "guest_a", "guest_b"])
def test_catalog_ignores_external_dataset_and_period_parameters(
    flask_app, catalog_datasets, principal,
):
    own_dataset = catalog_datasets[principal]
    other = next(d for key, d in catalog_datasets.items() if key != principal)
    client = _client_for(flask_app, own_dataset)
    with client.session_transaction() as session:
        session["dataset_id"] = str(other.id)
    document = _document(_get(
        flask_app, client,
        f"/products?dataset_id={other.id}&year=2025&month=1",
    ))
    names = [n.get_text(strip=True) for n in document.select(".product-catalog-name")]
    assert names == [f"CATALOG_{principal}"]
    for key in set(catalog_datasets) - {principal}:
        assert f"CATALOG_{key}" not in document.get_text()


def test_catalog_displays_name_price_period_and_both_states(
    flask_app, admin_dataset,
):
    db.session.add_all([
        Product(dataset=admin_dataset, year=2026, month=8,
                name="登録中のパン", price=1234, is_active=True),
        Product(dataset=admin_dataset, year=2025, month=12,
                name="登録解除したパン", price=0, is_active=False),
    ])
    db.session.commit()
    document = _document(_get(flask_app, _client_for(flask_app, admin_dataset)))
    rows = document.select(".product-catalog-item")
    assert len(rows) == 2
    assert rows[0].select_one(".product-catalog-price").find_previous("dt").get_text(strip=True) == "最新価格"
    assert rows[0].select_one(".product-catalog-period").find_previous("dt").get_text(strip=True) == "最新登録"
    assert rows[0].select_one(".product-catalog-name").get_text(strip=True) == "登録中のパン"
    assert rows[0].select_one(".product-catalog-price").get_text(strip=True) == "1,234円"
    assert rows[0].select_one(".product-catalog-period").get_text(strip=True) == "2026年8月"
    assert rows[0].select_one(".product-catalog-state").get_text(strip=True) == "登録中"
    assert rows[1].select_one(".product-catalog-name").get_text(strip=True) == "登録解除したパン"
    assert rows[1].select_one(".product-catalog-price").get_text(strip=True) == "0円"
    assert rows[1].select_one(".product-catalog-period").get_text(strip=True) == "2025年12月"
    assert rows[1].select_one(".product-catalog-state").get_text(strip=True) == "登録解除"


def test_catalog_orders_by_year_month_desc_then_id_desc(flask_app, admin_dataset):
    # 登録順・名前順・年だけ/月だけの順では通らない配置。
    records = [
        (2025, 12, "前年12月"),
        (2026, 1, "同月Z先行ID"),
        (2026, 8, "最新月"),
        (2026, 1, "同月A後続ID"),
        (2024, 12, "最古年"),
    ]
    for year, month, name in records:
        db.session.add(Product(dataset=admin_dataset, year=year,
                               month=month, name=name, price=200))
        db.session.flush()
    db.session.commit()
    document = _document(_get(flask_app, _client_for(flask_app, admin_dataset)))
    assert [n.get_text(strip=True) for n in document.select(".product-catalog-name")] == [
        "最新月", "同月A後続ID", "同月Z先行ID", "前年12月", "最古年",
    ]


@pytest.mark.parametrize(
    ("latest_active", "older_price", "expected_state"),
    [(True, 250, "登録中"), (False, 400, "登録解除")],
)
def test_catalog_uses_latest_same_name_product_without_changing_history(
    flask_app, admin_dataset, latest_active, older_price, expected_state,
):
    older = Product(
        dataset=admin_dataset, year=2025, month=12,
        name="クロワッサン", price=older_price, is_active=not latest_active,
    )
    middle = Product(
        dataset=admin_dataset, year=2026, month=3,
        name="クロワッサン", price=280, is_active=not latest_active,
    )
    latest = Product(
        dataset=admin_dataset, year=2026, month=10,
        name="クロワッサン", price=300, is_active=latest_active,
    )
    # 登録順と年月順を逆にして、IDだけで代表を選ぶ誤実装を検出する。
    db.session.add_all([latest, older, middle])
    db.session.flush()
    assert latest.id < older.id < middle.id
    db.session.add(DailySales(
        product_id=older.id,
        date=datetime.date(2025, 12, 10),
        quantity=5,
    ))
    db.session.commit()
    before = _snapshot()

    document = _document(_get(flask_app, _client_for(flask_app, admin_dataset)))
    rows = document.select(".product-catalog-item")
    assert len(rows) == 1
    assert rows[0].select_one(".product-catalog-name").get_text(strip=True) == "クロワッサン"
    assert rows[0].select_one(".product-catalog-price").get_text(strip=True) == "300円"
    assert rows[0].select_one(".product-catalog-period").get_text(strip=True) == "2026年10月"
    assert rows[0].select_one(".product-catalog-state").get_text(strip=True) == expected_state
    assert f"{older_price}円" not in rows[0].get_text()
    assert "280円" not in rows[0].get_text()
    assert _snapshot() == before


def test_catalog_same_name_same_month_uses_highest_id(flask_app, admin_dataset):
    earlier = Product(
        dataset=admin_dataset, year=2026, month=10,
        name="クロワッサン", price=290, is_active=False,
    )
    db.session.add(earlier)
    db.session.flush()
    later = Product(
        dataset=admin_dataset, year=2026, month=10,
        name="クロワッサン", price=300, is_active=True,
    )
    db.session.add(later)
    db.session.commit()
    assert earlier.id < later.id

    document = _document(_get(flask_app, _client_for(flask_app, admin_dataset)))
    rows = document.select(".product-catalog-item")
    assert len(rows) == 1
    assert rows[0].select_one(".product-catalog-name").get_text(strip=True) == "クロワッサン"
    assert rows[0].select_one(".product-catalog-price").get_text(strip=True) == "300円"
    assert rows[0].select_one(".product-catalog-period").get_text(strip=True) == "2026年10月"
    assert rows[0].select_one(".product-catalog-state").get_text(strip=True) == "登録中"


@pytest.mark.parametrize("principal", ["admin", "guest_a", "guest_b"])
def test_catalog_same_name_representative_stays_within_current_dataset(
    flask_app, admin_dataset, principal,
):
    datasets = {
        "admin": admin_dataset,
        "guest_a": _guest_dataset(),
        "guest_b": _guest_dataset(),
    }
    db.session.add_all([datasets["guest_a"], datasets["guest_b"]])
    for key, dataset in datasets.items():
        is_current = key == principal
        db.session.add(Product(
            dataset=dataset,
            year=2025 if is_current else 2026,
            month=12 if is_current else 10,
            name="クロワッサン",
            price=250 if is_current else 900,
            is_active=is_current,
        ))
    db.session.commit()
    before = _snapshot()

    document = _document(_get(flask_app, _client_for(flask_app, datasets[principal])))
    rows = document.select(".product-catalog-item")
    assert len(rows) == 1
    assert rows[0].select_one(".product-catalog-name").get_text(strip=True) == "クロワッサン"
    assert rows[0].select_one(".product-catalog-price").get_text(strip=True) == "250円"
    assert rows[0].select_one(".product-catalog-period").get_text(strip=True) == "2025年12月"
    assert rows[0].select_one(".product-catalog-state").get_text(strip=True) == "登録中"
    assert "900円" not in rows[0].get_text()
    assert _snapshot() == before


def test_catalog_groups_only_exactly_matching_names(flask_app, admin_dataset):
    for name in ("Croissant", "croissant", "食パン"):
        db.session.add(Product(
            dataset=admin_dataset, year=2026, month=10,
            name=name, price=300,
        ))
    db.session.commit()

    document = _document(_get(flask_app, _client_for(flask_app, admin_dataset)))
    names = [name.get_text(strip=True) for name in document.select(".product-catalog-name")]
    assert len(names) == 3
    assert set(names) == {"食パン", "croissant", "Croissant"}


@pytest.mark.parametrize("kind", ["admin", "guest"])
def test_catalog_empty_state_does_not_display_other_dataset(
    flask_app, admin_dataset, kind,
):
    guest = _guest_dataset()
    db.session.add(guest)
    own, other = (admin_dataset, guest) if kind == "admin" else (guest, admin_dataset)
    db.session.add(Product(dataset=other, year=2026, month=8,
                           name="他領域の商品", price=200))
    db.session.commit()
    document = _document(_get(flask_app, _client_for(flask_app, own)))
    assert "商品がまだありません。" in document.get_text()
    assert document.select(".product-catalog-item") == []
    assert "他領域の商品" not in document.get_text()


@pytest.mark.parametrize("expiry", ["absolute", "idle"])
def test_catalog_expired_guest_cannot_read_products(flask_app, expiry):
    now = datetime.datetime.now(datetime.timezone.utc)
    guest = _guest_dataset()
    guest.created_at = now - datetime.timedelta(hours=3)
    if expiry == "absolute":
        guest.absolute_expires_at = now - datetime.timedelta(seconds=1)
    else:
        guest.last_activity_at = now - datetime.timedelta(minutes=31)
    db.session.add(Product(dataset=guest, year=2026, month=8,
                           name="期限切れ秘密商品", price=200))
    db.session.commit()
    response = _get(flask_app, _client_for(flask_app, guest))
    assert response.status_code == 302
    assert urlparse(response.headers["Location"]).path == "/login"
    assert "期限切れ秘密商品" not in response.get_data(as_text=True)
    assert Product.query.count() == 1
    assert Dataset.query.count() == 1


def test_catalog_missing_admin_dataset_fails_closed(authenticated_client):
    response = authenticated_client.get("/products")
    assert response.status_code == 500
    assert "商品がまだありません。" not in response.get_data(as_text=True)


def test_catalog_is_read_only_and_preserves_inactive_product_sales(
    flask_app, admin_dataset, csrf_token,
):
    product = Product(dataset=admin_dataset, year=2025, month=12,
                      name="履歴あり商品", price=200, is_active=False)
    db.session.add(product)
    db.session.flush()
    db.session.add(DailySales(product_id=product.id,
                             date=datetime.date(2025, 12, 10), quantity=5))
    db.session.commit()
    before = _snapshot()
    client = _client_for(flask_app, admin_dataset)
    document = _document(_get(flask_app, client))
    assert "履歴あり商品" in document.get_text()
    assert document.select_one(".product-catalog-state").get_text(strip=True) == "登録解除"
    # CSRF不足の400ではなく、正規tokenでPOST自体が未対応か確認。
    with flask_app.app_context():
        token = csrf_token(client, "/products")
        response = client.post("/products", data={"csrf_token": token})
    assert response.status_code == 405
    assert _snapshot() == before
    assert document.select("main form") == []


def test_catalog_escapes_product_name(flask_app, admin_dataset):
    name = '<script>alert("catalog")</script>'
    db.session.add(Product(dataset=admin_dataset, year=2026, month=8,
                           name=name, price=200))
    db.session.commit()
    document = _document(_get(flask_app, _client_for(flask_app, admin_dataset)))
    heading = document.select_one(".product-catalog-name")
    assert heading.get_text() == name
    assert heading.select("script") == []


@pytest.mark.parametrize("kind", ["admin", "guest"])
def test_catalog_navigation_marks_only_catalog_current(flask_app, admin_dataset, kind):
    dataset = admin_dataset if kind == "admin" else _guest_dataset()
    db.session.add(dataset)
    db.session.commit()
    document = _document(_get(flask_app, _client_for(flask_app, dataset)))
    current = document.select('#app-sidebar a[aria-current="page"]')
    assert len(current) == 1
    assert current[0]["href"] == "/products"
    assert current[0].get_text(strip=True) == "商品一覧"
    assert document.select_one(".app-navigation-open-button") is not None
    assert document.select_one('meta[name="viewport"]') is not None
