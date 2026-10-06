import datetime
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import event
from werkzeug.exceptions import BadRequest

import app as app_module
from conftest import post_ai
from models import DailySales, Dataset, Product, db
from prompts import build_sales_prompt
from test_guest_ai_limit import (
    _create_guest_dataset,
    _create_guest_sales,
    _guest_client,
    _mock_gemini,
    _usage_count,
)


AI_PATHS = ["/api/ai-advice", "/api/greeting"]


def _snapshot():
    db.session.expire_all()
    return [
        list(db.session.execute(model.__table__.select().order_by(model.id)))
        for model in (Dataset, Product, DailySales)
    ]


@pytest.mark.parametrize("path", AI_PATHS)
def test_ai_get_is_405_without_gemini_or_database_changes(
    flask_app, admin_dataset, monkeypatch, path,
):
    guest = _create_guest_dataset()
    _create_guest_sales(guest)
    client = _guest_client(flask_app, guest)
    generate = _mock_gemini(monkeypatch)
    before = _snapshot()

    response = client.get(path)

    assert response.status_code == 405
    generate.assert_not_called()
    assert _snapshot() == before


@pytest.mark.parametrize("path", AI_PATHS)
@pytest.mark.parametrize("token_mode", ["missing", "tampered"])
def test_ai_csrf_rejection_precedes_auth_activity_and_usage_updates(
    flask_app, admin_dataset, monkeypatch, csrf_token, path, token_mode,
):
    guest = _create_guest_dataset()
    _create_guest_sales(guest)
    _create_guest_dataset()
    client = _guest_client(flask_app, guest)
    generate = _mock_gemini(monkeypatch)
    token = csrf_token(client, "/login")
    headers = {}
    if token_mode == "tampered":
        headers["X-CSRFToken"] = ("x" if token[0] != "x" else "y") + token[1:]
    before = _snapshot()

    response = client.post(path, headers=headers)

    assert response.status_code == 400
    generate.assert_not_called()
    assert _snapshot() == before


@pytest.mark.parametrize(
    ("page", "path"),
    [("/dashboard", "/api/ai-advice"), ("/input", "/api/greeting")],
)
def test_ai_page_supplies_csrf_and_uses_post_even_without_products(
    authenticated_client, admin_dataset, monkeypatch, page, path,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = authenticated_client.get(page)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    document = BeautifulSoup(html, "html.parser")
    token = document.select_one('meta[name="csrf-token"]')
    assert token is not None and token["content"]
    script = "\n".join(tag.get_text() for tag in document.find_all("script"))
    assert path in script
    assert "method: 'POST'" in script
    assert (
        "'X-CSRFToken': document.querySelector('meta[name=\"csrf-token\"]').content"
        in script
    )
    response = authenticated_client.post(
        path, headers={"X-CSRFToken": token["content"]},
    )
    assert response.status_code == 200


def _add_sales(dataset, names, *, quantity=1):
    for name in names:
        product = Product(dataset=dataset, year=2026, month=8, name=name, price=100)
        db.session.add(product)
        db.session.flush()
        db.session.add(DailySales(
            product_id=product.id,
            date=datetime.date(2026, 8, 1),
            quantity=quantity,
        ))
    db.session.commit()


@pytest.mark.parametrize("transport", ["query", "form", "json"])
def test_guest_prompt_ignores_external_dataset_and_other_dataset_totals(
    flask_app, admin_dataset, monkeypatch, transport,
):
    guest_a = _create_guest_dataset()
    guest_b = _create_guest_dataset()
    _add_sales(guest_a, ["共通商品"], quantity=10)
    _add_sales(guest_b, ["共通商品", "Guest B限定"], quantity=90)
    _add_sales(admin_dataset, ["共通商品", "Admin限定"], quantity=99)
    client = _guest_client(flask_app, guest_a)
    with client.session_transaction() as session_data:
        session_data["dataset_id"] = str(admin_dataset.id)
    generate = _mock_gemini(monkeypatch)
    path = "/api/ai-advice?year=2026&month=8"
    options = {}
    if transport == "query":
        path += f"&dataset_id={guest_b.id}"
    else:
        options["data" if transport == "form" else "json"] = {
            "dataset_id": str(guest_b.id),
        }

    response = post_ai(client, path, **options)

    assert response.status_code == 200
    assert generate.call_args.kwargs["contents"] == build_sales_prompt("共通商品: 10個")
    assert _usage_count(guest_a.id) == 1
    assert _usage_count(guest_b.id) == _usage_count(admin_dataset.id) == 0


def test_ai_sql_limits_scoped_aggregated_results_before_materialization(flask_app):
    guest = _create_guest_dataset()
    other = _create_guest_dataset()
    # Route制限前のlegacy行を想定。上位30件を保ち、同数量では商品名順。
    names = [f"商品{i:02d}" for i in range(31)]
    _add_sales(guest, names, quantity=10)
    _add_sales(other, ["対象外"], quantity=999)
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record_sql)
    try:
        result = app_module._get_sales_from_db(guest, row_limit=30)
    finally:
        event.remove(db.engine, "before_cursor_execute", record_sql)

    assert list(result.items()) == [(name, 10) for name in names[:30]]
    aggregate_sql = next(sql for sql in statements if "GROUP BY" in sql)
    assert "products.dataset_id =" in aggregate_sql
    assert "ORDER BY" in aggregate_sql and "LIMIT" in aggregate_sql


@pytest.mark.parametrize("count", [29, 30, 31])
def test_guest_advice_keeps_top_thirty_complete_names_and_bounded_prompt(
    flask_app, monkeypatch, count,
):
    guest = _create_guest_dataset()
    names = [f"{i:02d}" + "あ" * 98 for i in range(count)]
    _add_sales(guest, names)
    # 最大数量の商品を最後に作り、単に先頭30件を取る実装を検出する。
    last_sale = DailySales.query.order_by(DailySales.id.desc()).first()
    last_sale.quantity = 1_000
    db.session.commit()
    client = _guest_client(flask_app, guest)
    generate = _mock_gemini(monkeypatch)

    response = post_ai(client, "/api/ai-advice")

    assert response.status_code == 200
    expected = [(names[-1], 1_000)] + [(name, 1) for name in names[:-1]][:29]
    prompt = generate.call_args.kwargs["contents"]
    assert prompt == build_sales_prompt(
        ", ".join(f"{name}: {qty}個" for name, qty in expected)
    )
    assert len(prompt) <= len(build_sales_prompt("")) + 30 * (100 + 2 + 7 + 1) + 29 * 2
    assert _usage_count(guest.id) == 1


@pytest.mark.parametrize("ranked", [
    [("商品", 1)] * 31,
    [("あ" * 101, 1)],
    [("商品", 9_300_001)],
])
def test_oversized_guest_summary_rejected_before_usage_or_gemini(
    flask_app, monkeypatch, ranked,
):
    guest = _create_guest_dataset()
    generate = _mock_gemini(monkeypatch)
    reserve = Mock(return_value=True)
    monkeypatch.setattr(app_module, "_reserve_guest_ai_usage", reserve)

    with pytest.raises(BadRequest):
        app_module._generate_ai_advice(ranked, guest)

    generate.assert_not_called()
    reserve.assert_not_called()
    assert _usage_count(guest.id) == 0


def test_guest_summary_maximum_valid_quantity_is_not_truncated(flask_app, monkeypatch):
    guest = _create_guest_dataset()
    generate = _mock_gemini(monkeypatch)
    monkeypatch.setattr(app_module, "_reserve_guest_ai_usage", Mock(return_value=True))
    ranked = [("あ" * 100, 930_000)]

    app_module._generate_ai_advice(ranked, guest)

    assert generate.call_args.kwargs["contents"] == build_sales_prompt(
        f'{"あ" * 100}: 930000個'
    )


def test_admin_advice_is_not_limited_to_thirty_products(
    authenticated_client, admin_dataset, monkeypatch,
):
    names = [f"管理者商品{i:02d}" for i in range(31)]
    _add_sales(admin_dataset, names)
    generate = _mock_gemini(monkeypatch)

    response = post_ai(authenticated_client, "/api/ai-advice")

    assert response.status_code == 200
    prompt = generate.call_args.kwargs["contents"]
    for name in names:
        assert f"{name}: 1個" in prompt
    assert _usage_count(admin_dataset.id) == 0


def test_advice_missing_key_does_not_expose_configuration_or_consume_usage(
    flask_app, monkeypatch,
):
    guest = _create_guest_dataset()
    _create_guest_sales(guest)
    client = _guest_client(flask_app, guest)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    factory = Mock()
    monkeypatch.setattr(app_module.genai, "Client", factory)

    response = post_ai(client, "/api/ai-advice")

    assert response.status_code == 200
    assert "GEMINI_API_KEY" not in response.get_data(as_text=True)
    factory.assert_not_called()
    assert _usage_count(guest.id) == 0


@pytest.mark.parametrize("error_kind", ["429", "503", "timeout"])
def test_advice_api_exception_details_are_not_logged_or_returned(
    monkeypatch, caplog, error_kind,
):
    marker = "synthetic-private-error-payload"
    _mock_gemini(monkeypatch, side_effect=RuntimeError(f"{error_kind} {marker}"))

    result = app_module._generate_ai_advice([("商品", 1)])

    assert marker not in result
    assert marker not in caplog.text


@pytest.mark.parametrize("path", AI_PATHS)
def test_api_exception_is_private_and_reservation_committed_before_call(
    flask_app, monkeypatch, caplog, path,
):
    guest = _create_guest_dataset()
    _create_guest_sales(guest)
    client = _guest_client(flask_app, guest)
    marker = "synthetic-private-api-payload"
    transaction_states = []

    def fail_after_reservation(**kwargs):
        transaction_states.append(db.session().in_transaction())
        raise RuntimeError(marker)

    generate = _mock_gemini(monkeypatch, side_effect=fail_after_reservation)
    response = post_ai(client, path)

    assert response.status_code == 200
    generate.assert_called_once()
    assert transaction_states == [False]
    assert _usage_count(guest.id) == 1
    assert marker not in response.get_data(as_text=True)
    assert marker not in caplog.text
