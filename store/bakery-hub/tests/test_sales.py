import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app as app_module
from models import DailySales, Dataset, Product, db


def _sales_snapshot():
    return [
        (sale.id, sale.product_id, sale.date, sale.quantity)
        for sale in DailySales.query.order_by(DailySales.id).all()
    ]


def _freeze_app_today(monkeypatch, frozen_today):
    monkeypatch.setattr(
        app_module,
        "business_today",
        lambda: frozen_today,
    )


def _create_products_for_sales(dataset, *, count, sale_date):
    products = [
        Product(
            dataset=dataset,
            year=sale_date.year,
            month=sale_date.month,
            name=f"売上入力商品{index}",
            price=100,
        )
        for index in range(count)
    ]
    db.session.add_all(products)
    db.session.commit()
    return products


def _create_guest_dataset():
    now = datetime.datetime.now(datetime.timezone.utc)
    dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    db.session.add(dataset)
    db.session.commit()
    return dataset


def _guest_client(flask_app, dataset):
    test_client = flask_app.test_client()
    with test_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{dataset.id}"
        session_data["_fresh"] = True
    return test_client


@pytest.fixture()
def sales_records(flask_app, admin_dataset):
    sale_date = app_module.business_today()
    existing_product = Product(
        dataset=admin_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="既存商品",
        price=200,
    )
    new_product = Product(
        dataset=admin_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="新規売上商品",
        price=300,
    )
    db.session.add_all([existing_product, new_product])
    db.session.flush()

    existing_sale = DailySales(
        product_id=existing_product.id,
        date=sale_date,
        quantity=5,
    )
    db.session.add(existing_sale)
    db.session.commit()

    return SimpleNamespace(
        date=sale_date,
        existing_product_id=existing_product.id,
        new_product_id=new_product.id,
    )


@pytest.fixture()
def cross_dataset_sales_records(flask_app, admin_dataset):
    now = datetime.datetime.now(datetime.timezone.utc)
    sale_date = app_module.business_today()
    guest_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    admin_product = Product(
        dataset=admin_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="管理者売上商品",
        price=200,
        is_active=True,
    )
    guest_product = Product(
        dataset=guest_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="ゲスト売上商品",
        price=300,
        is_active=True,
    )
    db.session.add_all([
        guest_dataset,
        admin_product,
        guest_product,
    ])
    db.session.flush()
    db.session.add_all([
        DailySales(
            product_id=admin_product.id,
            date=sale_date,
            quantity=11,
        ),
        DailySales(
            product_id=guest_product.id,
            date=sale_date,
            quantity=987654,
        ),
    ])
    db.session.commit()

    return SimpleNamespace(
        date=sale_date,
        admin_product_id=admin_product.id,
        guest_product_id=guest_product.id,
    )


def test_admin_sales_get_excludes_guest_dataset_product(
    authenticated_client,
    cross_dataset_sales_records,
):
    response = authenticated_client.get("/input")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "管理者売上商品" in response_text
    assert "ゲスト売上商品" not in response_text


def test_sales_quantity_input_keeps_current_value_and_selects_on_focus(
    authenticated_client,
    sales_records,
):
    response = authenticated_client.get("/input")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    product_row = next(
        row
        for row in document.select(".product-row")
        if row.select_one('input[name="product_id"]').get("value")
        == str(sales_records.existing_product_id)
    )
    quantity_input = product_row.select_one('input[name="quantity"]')
    page_title = document.find("h1")
    assert page_title is not None
    assert page_title.get_text(" ", strip=True) == "日次売上入力"
    assert page_title.select_one(
        'svg[data-icon="clipboard-pen-line"]'
    ) is not None
    input_guidance = page_title.find_next_sibling("p")
    script_text = "\n".join(
        script.get_text()
        for script in document.find_all("script")
    )

    assert response.status_code == 200
    assert input_guidance is not None
    assert input_guidance.get_text(strip=True) == (
        "※本日の累計販売数を入力してください。"
        "入力済みの数値は上書きされます。"
    )
    assert quantity_input.get("value") == "5"
    assert quantity_input.get("max") == "1000"
    assert "querySelectorAll('.qty-input')" in script_text
    assert "addEventListener('focus'" in script_text
    assert "requestAnimationFrame" in script_text
    assert "quantityInput.select()" in script_text
    assert "販売数量は0〜1,000個の整数で入力してください。" in script_text


def test_sales_input_month_rollover_shows_only_frozen_current_month_product(
    authenticated_client,
    admin_dataset,
    monkeypatch,
):
    frozen_today = datetime.date(2026, 9, 1)
    _freeze_app_today(monkeypatch, frozen_today)
    previous_month_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=8,
        name="月替わり前月商品",
        price=200,
        is_active=True,
    )
    current_month_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=9,
        name="月替わり現在月商品",
        price=300,
        is_active=True,
    )
    db.session.add_all([previous_month_product, current_month_product])
    db.session.commit()

    response = authenticated_client.get("/input")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    form = document.select_one('form[method="POST"]')

    assert response.status_code == 200
    assert "月替わり現在月商品" in document.get_text()
    assert "月替わり前月商品" not in document.get_text()
    assert form is not None
    csrf_input = form.select_one('input[name="csrf_token"]')
    assert csrf_input is not None
    assert csrf_input.get("value")


def test_sales_input_year_rollover_shows_only_frozen_current_year_month_product(
    authenticated_client,
    admin_dataset,
    monkeypatch,
):
    frozen_today = datetime.date(2027, 1, 1)
    _freeze_app_today(monkeypatch, frozen_today)
    previous_month_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=12,
        name="年替わり前年12月商品",
        price=200,
        is_active=True,
    )
    previous_year_same_month_product = Product(
        dataset=admin_dataset,
        year=2026,
        month=1,
        name="年替わり前年同月商品",
        price=250,
        is_active=True,
    )
    current_month_product = Product(
        dataset=admin_dataset,
        year=2027,
        month=1,
        name="年替わり現在月商品",
        price=300,
        is_active=True,
    )
    db.session.add_all([
        previous_month_product,
        previous_year_same_month_product,
        current_month_product,
    ])
    db.session.commit()

    response = authenticated_client.get("/input")
    document = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    form = document.select_one('form[method="POST"]')

    assert response.status_code == 200
    assert "年替わり現在月商品" in document.get_text()
    assert "年替わり前年12月商品" not in document.get_text()
    assert "年替わり前年同月商品" not in document.get_text()
    assert form is not None
    csrf_input = form.select_one('input[name="csrf_token"]')
    assert csrf_input is not None
    assert csrf_input.get("value")


def test_admin_sales_get_excludes_guest_dataset_today_sales(
    authenticated_client,
    cross_dataset_sales_records,
    monkeypatch,
):
    captured_today_sales = {}
    real_render_template = app_module.render_template

    def capture_input_context(template_name, *args, **kwargs):
        if template_name == "input.html":
            captured_today_sales.update(kwargs["today_sales"])
        return real_render_template(template_name, *args, **kwargs)

    monkeypatch.setattr(
        app_module,
        "render_template",
        capture_input_context,
    )

    response = authenticated_client.get("/input")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert captured_today_sales[
        cross_dataset_sales_records.admin_product_id
    ] == 11
    assert (
        cross_dataset_sales_records.guest_product_id
        not in captured_today_sales
    )
    assert "ゲスト売上商品" not in response_text
    assert "987654" not in response_text
    assert (
        f'value="{cross_dataset_sales_records.guest_product_id}"'
        not in response_text
    )


def test_admin_sales_post_rejects_guest_dataset_product_without_changes(
    authenticated_client,
    cross_dataset_sales_records,
    csrf_post,
):
    sales_before = _sales_snapshot()

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": cross_dataset_sales_records.date.isoformat(),
            "product_id": [
                str(cross_dataset_sales_records.guest_product_id),
            ],
            "quantity": ["99"],
        },
    )

    sales_after = _sales_snapshot()
    assert sales_after == sales_before
    assert response.status_code in {400, 403}


def test_guest_a_sales_post_rejects_guest_b_product_without_changes(
    flask_app,
    csrf_post,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    sale_date = app_module.business_today()

    guest_a_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )
    guest_b_dataset = Dataset(
        kind="guest",
        system_key=None,
        created_at=now,
        last_activity_at=now,
        absolute_expires_at=now + datetime.timedelta(hours=2),
    )

    guest_a_product = Product(
        dataset=guest_a_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="Guest A売上商品",
        price=200,
        is_active=True,
    )
    guest_b_product = Product(
        dataset=guest_b_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="Guest B売上商品",
        price=300,
        is_active=True,
    )

    db.session.add_all([
        guest_a_dataset,
        guest_b_dataset,
        guest_a_product,
        guest_b_product,
    ])
    db.session.flush()

    db.session.add_all([
        DailySales(
            product_id=guest_a_product.id,
            date=sale_date,
            quantity=11,
        ),
        DailySales(
            product_id=guest_b_product.id,
            date=sale_date,
            quantity=22,
        ),
    ])
    db.session.commit()

    sales_before = _sales_snapshot()

    guest_a_client = flask_app.test_client()
    with guest_a_client.session_transaction() as session_data:
        session_data["_user_id"] = f"guest:{guest_a_dataset.id}"
        session_data["_fresh"] = True

    response = csrf_post(
        guest_a_client,
        "/input",
        {
            "date": sale_date.isoformat(),
            "product_id": [
                str(guest_b_product.id),
            ],
            "quantity": ["999"],
        },
    )

    sales_after = _sales_snapshot()

    assert response.status_code in {400, 403}
    assert sales_after == sales_before


def test_admin_sales_post_mixed_datasets_is_atomic(
    authenticated_client,
    cross_dataset_sales_records,
    csrf_post,
):
    sales_before = _sales_snapshot()

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": cross_dataset_sales_records.date.isoformat(),
            "product_id": [
                str(cross_dataset_sales_records.admin_product_id),
                str(cross_dataset_sales_records.guest_product_id),
            ],
            "quantity": ["55", "77"],
        },
    )

    sales_after = _sales_snapshot()
    assert sales_after == sales_before
    assert response.status_code in {400, 403}


def test_valid_sales_post_updates_existing_and_adds_new_sale(
    authenticated_client,
    sales_records,
    csrf_post,
):
    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                str(sales_records.new_product_id),
            ],
            "quantity": ["9", "0"],
        },
    )

    assert response.status_code == 200
    assert DailySales.query.count() == 2
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 9
    assert DailySales.query.filter_by(
        product_id=sales_records.new_product_id,
        date=sales_records.date,
    ).one().quantity == 0


def test_sales_post_does_not_update_same_product_sale_from_other_date(
    authenticated_client,
    admin_dataset,
    csrf_post,
):
    today = app_module.business_today()
    previous_date = today.replace(day=1)
    target_date = today.replace(day=2)
    product = Product(
        dataset=admin_dataset,
        year=target_date.year,
        month=target_date.month,
        name="別日売上テスト商品",
        price=200,
        is_active=True,
    )
    db.session.add(product)
    db.session.flush()
    db.session.add(
        DailySales(
            product_id=product.id,
            date=previous_date,
            quantity=5,
        )
    )
    db.session.commit()

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": target_date.isoformat(),
            "product_id": [str(product.id)],
            "quantity": ["9"],
        },
    )

    product_sales = DailySales.query.filter_by(
        product_id=product.id,
    ).order_by(DailySales.date).all()

    assert response.status_code == 200
    assert [
        (sale.date, sale.quantity)
        for sale in product_sales
    ] == [
        (previous_date, 5),
        (target_date, 9),
    ]


def test_sales_rolls_back_all_changes_when_database_commit_fails(
    authenticated_client,
    sales_records,
    monkeypatch,
    csrf_post,
):
    sales_before = _sales_snapshot()
    rollback_spy = Mock(wraps=db.session.rollback)

    monkeypatch.setattr(
        db.session,
        "commit",
        Mock(side_effect=SQLAlchemyError("forced commit failure")),
    )
    monkeypatch.setattr(db.session, "rollback", rollback_spy)

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                str(sales_records.new_product_id),
            ],
            "quantity": ["9", "7"],
        },
    )

    sales_after = _sales_snapshot()

    assert response.status_code == 500
    rollback_spy.assert_called_once_with()
    assert sales_after == sales_before
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 5
    assert DailySales.query.filter_by(
        product_id=sales_records.new_product_id,
        date=sales_records.date,
    ).first() is None
    assert "本日の売上個数を更新しました" not in response.get_data(
        as_text=True
    )


def test_daily_sales_product_and_date_are_unique_at_database_level(
    flask_app,
    admin_dataset,
):
    sale_date = app_module.business_today()
    product = Product(
        dataset=admin_dataset,
        year=sale_date.year,
        month=sale_date.month,
        name="一意制約テスト商品",
        price=250,
    )
    db.session.add(product)
    db.session.flush()
    db.session.add(
        DailySales(
            product_id=product.id,
            date=sale_date,
            quantity=3,
        )
    )
    db.session.commit()

    db.session.add(
        DailySales(
            product_id=product.id,
            date=sale_date,
            quantity=7,
        )
    )

    with pytest.raises(IntegrityError):
        db.session.commit()

    db.session.rollback()

    sales = DailySales.query.filter_by(
        product_id=product.id,
        date=sale_date,
    ).all()
    assert len(sales) == 1
    assert sales[0].quantity == 3


@pytest.mark.parametrize(
    "invalid_case",
    [
        "invalid_date",
        "empty_quantity",
        "string_quantity",
        "negative_quantity",
        "decimal_quantity",
        "missing_quantity",
        "extra_quantity",
    ],
)
def test_invalid_sales_post_rejects_entire_request(
    authenticated_client,
    sales_records,
    invalid_case,
    csrf_post,
):
    date_value = sales_records.date.isoformat()
    product_ids = [
        str(sales_records.existing_product_id),
        str(sales_records.new_product_id),
    ]
    quantities = ["9", "4"]

    if invalid_case == "invalid_date":
        date_value = "not-a-date"
    elif invalid_case == "empty_quantity":
        quantities[1] = ""
    elif invalid_case == "string_quantity":
        quantities[1] = "four"
    elif invalid_case == "negative_quantity":
        quantities[1] = "-1"
    elif invalid_case == "decimal_quantity":
        quantities[1] = "1.5"
    elif invalid_case == "missing_quantity":
        quantities.pop()
    elif invalid_case == "extra_quantity":
        quantities.append("7")

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": date_value,
            "product_id": product_ids,
            "quantity": quantities,
        },
    )

    assert response.status_code == 400
    assert DailySales.query.count() == 1
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 5
    assert DailySales.query.filter_by(
        product_id=sales_records.new_product_id,
        date=sales_records.date,
    ).first() is None


@pytest.mark.parametrize("invalid_product_id", ["", "abc"])
def test_sales_rejects_invalid_product_ids_without_database_changes(
    authenticated_client,
    sales_records,
    invalid_product_id,
    csrf_post,
):
    sales_before = _sales_snapshot()

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                invalid_product_id,
            ],
            "quantity": ["9", "4"],
        },
    )

    sales_after = _sales_snapshot()

    assert response.status_code == 400
    assert len(sales_after) == len(sales_before)
    assert sales_after == sales_before
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 5


def test_sales_rejects_duplicate_product_ids_without_database_changes(
    authenticated_client,
    sales_records,
    csrf_post,
):
    sales_before = _sales_snapshot()

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                str(sales_records.existing_product_id),
            ],
            "quantity": ["5", "9"],
        },
    )

    sales_after = _sales_snapshot()

    assert response.status_code == 400
    assert len(sales_after) == len(sales_before)
    assert sales_after == sales_before
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 5


def test_sales_rejects_empty_submission_without_database_changes(
    authenticated_client,
    sales_records,
    csrf_post,
):
    sales_before = _sales_snapshot()

    response = csrf_post(
        authenticated_client,
        "/input",
        {"date": sales_records.date.isoformat()},
    )

    sales_after = _sales_snapshot()

    assert response.status_code == 400
    assert len(sales_after) == len(sales_before)
    assert sales_after == sales_before


@pytest.mark.parametrize(
    "invalid_product_case",
    [
        "unknown_product",
        "wrong_month_product",
        "inactive_product",
    ],
)
def test_sales_rejects_unknown_wrong_month_and_inactive_products(
    authenticated_client,
    admin_dataset,
    sales_records,
    invalid_product_case,
    csrf_post,
):
    if invalid_product_case == "unknown_product":
        invalid_product_id = 999999
    elif invalid_product_case == "wrong_month_product":
        if sales_records.date.month == 1:
            product_year = sales_records.date.year - 1
            product_month = 12
        else:
            product_year = sales_records.date.year
            product_month = sales_records.date.month - 1

        invalid_product = Product(
            dataset=admin_dataset,
            year=product_year,
            month=product_month,
            name="別月商品",
            price=400,
        )
        db.session.add(invalid_product)
        db.session.commit()
        invalid_product_id = invalid_product.id
    else:
        invalid_product = Product(
            dataset=admin_dataset,
            year=sales_records.date.year,
            month=sales_records.date.month,
            name="販売終了商品",
            price=500,
            is_active=False,
        )
        db.session.add(invalid_product)
        db.session.flush()
        db.session.add(
            DailySales(
                product_id=invalid_product.id,
                date=sales_records.date,
                quantity=2,
            )
        )
        db.session.commit()
        invalid_product_id = invalid_product.id

    sales_before = [
        (sale.id, sale.product_id, sale.date, sale.quantity)
        for sale in DailySales.query.order_by(DailySales.id).all()
    ]

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                str(invalid_product_id),
            ],
            "quantity": ["9", "4"],
        },
    )

    sales_after = [
        (sale.id, sale.product_id, sale.date, sale.quantity)
        for sale in DailySales.query.order_by(DailySales.id).all()
    ]

    assert response.status_code == 400
    assert len(sales_after) == len(sales_before)
    assert sales_after == sales_before
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 5


def test_admin_sales_post_is_not_limited_to_thirty_products(
    authenticated_client,
    admin_dataset,
    csrf_post,
):
    sale_date = app_module.business_today()
    products = _create_products_for_sales(
        admin_dataset,
        count=31,
        sale_date=sale_date,
    )

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sale_date.isoformat(),
            "product_id": [str(product.id) for product in products],
            "quantity": ["1"] * 31,
        },
    )

    assert response.status_code == 200
    assert DailySales.query.count() == 31


def test_sales_post_rejects_more_than_thirty_products_atomically(
    flask_app,
    csrf_post,
):
    guest_dataset = _create_guest_dataset()
    guest_client = _guest_client(flask_app, guest_dataset)
    sale_date = app_module.business_today()

    products = _create_products_for_sales(
        guest_dataset,
        count=31,
        sale_date=sale_date,
    )
    sales_before = _sales_snapshot()

    response = csrf_post(
        guest_client,
        "/input",
        {
            "date": sale_date.isoformat(),
            "product_id": [str(product.id) for product in products],
            "quantity": ["1"] * 31,
        },
    )

    assert response.status_code == 400
    assert _sales_snapshot() == sales_before


@pytest.mark.parametrize(
    "invalid_quantity",
    [
        pytest.param("1001", id="above-max"),
        pytest.param("9" * 5000, id="very-long"),
    ],
)
def test_sales_post_rejects_quantity_above_limit_without_changes(
    authenticated_client,
    sales_records,
    csrf_post,
    invalid_quantity,
):
    sales_before = _sales_snapshot()

    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                str(sales_records.new_product_id),
            ],
            "quantity": ["9", invalid_quantity],
        },
    )

    assert response.status_code == 400
    assert _sales_snapshot() == sales_before


def test_sales_post_accepts_quantity_limit_boundaries(
    authenticated_client,
    sales_records,
    csrf_post,
):
    response = csrf_post(
        authenticated_client,
        "/input",
        {
            "date": sales_records.date.isoformat(),
            "product_id": [
                str(sales_records.existing_product_id),
                str(sales_records.new_product_id),
            ],
            "quantity": ["0", "1000"],
        },
    )

    assert response.status_code == 200
    assert DailySales.query.filter_by(
        product_id=sales_records.existing_product_id,
        date=sales_records.date,
    ).one().quantity == 0
    assert DailySales.query.filter_by(
        product_id=sales_records.new_product_id,
        date=sales_records.date,
    ).one().quantity == 1000
