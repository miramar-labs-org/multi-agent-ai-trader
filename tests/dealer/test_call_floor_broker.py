from omegaconf import OmegaConf

from src.dealer import graph


def _cfg():
    return OmegaConf.create(
        {
            "trading": {"slP": 0.98, "tpP": 1.05},
            "floor_broker": {"base_url": "http://floor-broker.test:8000"},
        }
    )


def _state(action: str, budget: float) -> dict:
    return {
        "symbol": "MGN",
        "exchange": "stocks",
        "budget": budget,
        "signal": {"action": action, "reasoning": "test", "size_hint": 1.0},
        "execution_result": None,
    }


def _silence_slack(monkeypatch):
    # These tests target call_floor_broker's own HTTP dispatch, not Slack notifications --
    # silence both so the environment's real SLACK_WEBHOOK_URL (if configured) can't cause an
    # actual network call, and so its use of the shared `requests` module doesn't clobber the
    # fake `requests.post` these tests install for the Floor Broker call.
    monkeypatch.setattr(graph.slack, "notify_dealer_signal", lambda *a, **k: None)
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)


def test_buy_on_held_only_position_is_skipped_without_calling_floor_broker(monkeypatch):
    """Regression: merge_held_positions() gives held-only entries budget=0.0 -- a BUY signal on
    one of these must be refused locally, never forwarded to Floor Broker sized off a market
    value that was never authorized new-BUY capital."""
    _silence_slack(monkeypatch)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called for a zero-budget BUY")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    result = graph.call_floor_broker(_state("BUY", budget=0.0), _cfg())

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "no_authorized_budget"


def test_buy_with_authorized_budget_is_forwarded_to_floor_broker(monkeypatch):
    """A normal Analyst-authorized BUY (nonzero budget) must still reach Floor Broker."""
    _silence_slack(monkeypatch)
    posted = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "executed", "detail": "buy order submitted: order-123"}

    def _fake_post(url, json, timeout):
        posted["url"] = url
        posted["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)

    result = graph.call_floor_broker(_state("BUY", budget=5000.0), _cfg())

    assert posted["json"]["budget"] == 5000.0
    assert result["execution_result"]["status"] == "executed"


def test_execution_result_fields_are_forwarded_to_the_slack_notification(monkeypatch):
    """execution.py's reason/fill_price/sl_price/tp_price must reach the Slack notice, not just
    status/detail -- confirms call_floor_broker doesn't drop them on the way through."""
    monkeypatch.setattr(graph.slack, "notify_dealer_signal", lambda *a, **k: None)
    posted = {}
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: posted.update(kwargs=k))

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "executed",
                "detail": "buy order submitted: order-123",
                "reason": "opening_position",
                "order_id": "order-123",
                "fill_price": 10.05,
                "sl_price": 9.8,
                "tp_price": 10.5,
            }

    monkeypatch.setattr(graph.requests, "post", lambda url, json, timeout: FakeResponse())

    graph.call_floor_broker(_state("BUY", budget=5000.0), _cfg())

    assert posted["kwargs"]["reason"] == "opening_position"
    assert posted["kwargs"]["fill_price"] == 10.05
    assert posted["kwargs"]["sl_price"] == 9.8
    assert posted["kwargs"]["tp_price"] == 10.5
