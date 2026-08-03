from fastapi.testclient import TestClient

from src.floor_broker import app as app_module


def _client():
    return TestClient(app_module.app)


def _payload(action="BUY"):
    return {"symbol": "MGN", "exchange": "stocks", "action": action, "budget": 5000.0, "slP": 0.98, "tpP": 1.05}


def test_execute_response_includes_fill_price_sl_tp_and_reason_for_a_buy(monkeypatch):
    monkeypatch.setattr(
        app_module.execution,
        "buy",
        lambda symbol, exchange, budget, slP, tpP: {
            "status": "executed",
            "reason": "opening_position",
            "detail": "buy order submitted: order-123",
            "order_id": "order-123",
            "fill_price": 10.05,
            "sl_price": 9.8,
            "tp_price": 10.5,
        },
    )

    response = _client().post("/execute", json=_payload("BUY"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["reason"] == "opening_position"
    assert body["order_id"] == "order-123"
    assert body["fill_price"] == 10.05
    assert body["sl_price"] == 9.8
    assert body["tp_price"] == 10.5


def test_execute_response_includes_dealer_signal_reason_and_fill_price_for_a_sell(monkeypatch):
    monkeypatch.setattr(
        app_module.execution,
        "sell",
        lambda symbol: {
            "status": "executed",
            "reason": "dealer_signal",
            "detail": "sell order submitted: order-456",
            "order_id": "order-456",
            "fill_price": 12.0,
        },
    )

    response = _client().post("/execute", json=_payload("SELL"))

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "dealer_signal"
    assert body["fill_price"] == 12.0
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
