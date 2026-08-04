import pytest
from fastapi.testclient import TestClient

from src.floor_broker import app as app_module


def _client():
    return TestClient(app_module.app)


def _payload(action="BUY", **overrides):
    payload = {"symbol": "MGN", "exchange": "stocks", "action": action, "budget": 5000.0, "slP": 0.98, "tpP": 1.05}
    payload.update(overrides)
    return payload


def test_execute_response_includes_sl_tp_and_reason_for_a_buy(monkeypatch):
    monkeypatch.setattr(
        app_module.execution,
        "buy",
        lambda symbol, exchange, budget, slP, tpP: {
            "status": "submitted",
            "reason": "opening_position",
            "detail": "buy order submitted: order-123",
            "order_id": "order-123",
            "sl_price": 9.8,
            "tp_price": 10.5,
        },
    )

    response = _client().post("/execute", json=_payload("BUY"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "submitted"
    assert body["reason"] == "opening_position"
    assert body["order_id"] == "order-123"
    assert body["fill_price"] is None
    assert body["sl_price"] == 9.8
    assert body["tp_price"] == 10.5


def test_execute_response_includes_dealer_signal_reason_for_a_sell(monkeypatch):
    monkeypatch.setattr(
        app_module.execution,
        "sell",
        lambda symbol: {
            "status": "submitted",
            "reason": "dealer_signal",
            "detail": "sell order submitted: order-456",
            "order_id": "order-456",
        },
    )

    response = _client().post("/execute", json=_payload("SELL"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "submitted"
    assert body["reason"] == "dealer_signal"
    assert body["fill_price"] is None
    assert body["sl_price"] is None
    assert body["tp_price"] is None


def test_execute_response_omits_optional_fields_when_skipped(monkeypatch):
    monkeypatch.setattr(app_module.execution, "sell", lambda symbol: {"status": "skipped", "detail": "no open position"})

    response = _client().post("/execute", json=_payload("SELL"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] is None
    assert body["fill_price"] is None
    assert body["sl_price"] is None
    assert body["tp_price"] is None


def test_execute_returns_200_not_500_for_a_buy_while_state_unreconciled(monkeypatch):
    """Regression test: execution.buy()'s state_not_reconciled short-circuit must return a
    status value ExecuteResponse's Literal actually permits (see app.py) -- exercises the real,
    unmocked execution.buy() through the live /execute endpoint so a response-model mismatch
    here (which previously surfaced as a 500, not a validation failure at the execution layer)
    is caught the same way a real caller would hit it."""
    monkeypatch.setattr(app_module.execution, "_state_reconciled", False)

    response = _client().post("/execute", json=_payload("BUY"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "state_not_reconciled"


def test_execute_normalizes_symbol_and_exchange_case(monkeypatch):
    """ROADMAP P0.3: symbol/exchange are normalized (stripped, cased) before reaching
    execution.buy() -- a lowercase symbol or upper-case "STOCKS" exchange from a caller must
    still compare correctly against execution.py's `if exchange == "stocks"` check."""
    received = {}

    def _fake_buy(symbol, exchange, budget, slP, tpP):
        received["symbol"] = symbol
        received["exchange"] = exchange
        return {"status": "executed", "reason": "opening_position", "detail": "ok", "order_id": "order-123"}

    monkeypatch.setattr(app_module.execution, "buy", _fake_buy)

    response = _client().post("/execute", json=_payload("BUY", symbol=" mgn ", exchange="STOCKS"))

    assert response.status_code == 200
    assert received["symbol"] == "MGN"
    assert received["exchange"] == "stocks"


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": ""},
        {"symbol": "TOO/MANY/SLASHES"},
        {"symbol": "TOO.MANY.DOTS"},
        {"symbol": "HAS SPACE"},
        {"exchange": ""},
        {"exchange": "has space"},
        {"budget": 0},
        {"budget": -100.0},
        {"budget": app_module.MAX_BUDGET + 0.01},
        {"slP": 0},
        {"slP": 1},
        {"slP": 1.01},
        {"slP": -0.1},
        {"tpP": 1},
        {"tpP": 2},
        {"tpP": 0.99},
        {"unexpected_field": "nope"},
    ],
    ids=lambda v: str(v),
)
def test_execute_rejects_invalid_request_fields(overrides):
    """ROADMAP P0.3: invalid requests must fail FastAPI's own request validation (422) before
    execution.buy()/sell() is ever called -- no execution.* monkeypatch needed here since a
    valid call should never happen."""
    response = _client().post("/execute", json=_payload("BUY", **overrides))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"budget": app_module.MAX_BUDGET},
        {"slP": 0.01},
        {"slP": 0.99},
        {"tpP": 1.01},
        {"tpP": 1.99},
        {"symbol": "DSX.WS"},
        {"symbol": "BRK.B"},
    ],
    ids=lambda v: str(v),
)
def test_execute_accepts_boundary_valid_values(monkeypatch, overrides):
    monkeypatch.setattr(
        app_module.execution,
        "buy",
        lambda symbol, exchange, budget, slP, tpP: {
            "status": "executed",
            "reason": "opening_position",
            "detail": "ok",
            "order_id": "order-123",
        },
    )

    response = _client().post("/execute", json=_payload("BUY", **overrides))

    assert response.status_code == 200
