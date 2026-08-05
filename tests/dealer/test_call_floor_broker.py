from datetime import datetime

import pytz
from omegaconf import OmegaConf

from src.dealer import graph


def _cfg():
    return OmegaConf.create(
        {
            "trading": {"slP": 0.98, "tpP": 1.05},
            "floor_broker": {"base_url": "http://floor-broker.test:8000"},
            "macro_blackout": {"enabled": False, "dates": []},
        }
    )


def _state(action: str, budget: float, size_hint: float = 1.0) -> dict:
    return {
        "symbol": "MGN",
        "exchange": "stocks",
        "budget": budget,
        "signal": {"action": action, "reasoning": "test", "size_hint": size_hint},
        "execution_result": None,
    }


def _silence_slack(monkeypatch):
    # These tests target call_floor_broker's own HTTP dispatch, not Slack notifications --
    # silence both so the environment's real SLACK_WEBHOOK_URL2 (if configured) can't cause an
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


def test_buy_scales_forwarded_budget_by_size_hint(monkeypatch):
    """The Dealer LLM's size_hint (fraction of budget to deploy) was previously captured in the
    schema but never actually applied -- ROADMAP P1.7. A 0.5 hint on a $5000 budget must reach
    Floor Broker as $2500, not the full $5000."""
    _silence_slack(monkeypatch)
    posted = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "executed", "detail": "buy order submitted: order-123"}

    def _fake_post(url, json, timeout):
        posted["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)

    result = graph.call_floor_broker(_state("BUY", budget=5000.0, size_hint=0.5), _cfg())

    assert posted["json"]["budget"] == 2500.0
    assert result["execution_result"]["status"] == "executed"


def test_buy_with_zero_size_hint_is_skipped_without_calling_floor_broker(monkeypatch):
    """A size_hint of exactly 0.0 scales the authorized budget to $0 -- ExecuteRequest requires
    budget > 0 (src/floor_broker/app.py), so this must be refused locally rather than forwarded
    to a request that would fail Pydantic validation."""
    _silence_slack(monkeypatch)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called for a size_hint-zeroed BUY")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    result = graph.call_floor_broker(_state("BUY", budget=5000.0, size_hint=0.0), _cfg())

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "size_hint_zero"


def test_sell_forwards_budget_unscaled_by_size_hint(monkeypatch):
    """size_hint is documented as a BUY-only sizing hint (src/dealer/schema.py) -- Floor Broker's
    sell() ignores budget entirely, but confirm call_floor_broker doesn't scale it for SELL
    either, in case that ever changes."""
    _silence_slack(monkeypatch)
    posted = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "executed", "detail": "sell order submitted: order-456"}

    def _fake_post(url, json, timeout):
        posted["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)

    graph.call_floor_broker(_state("SELL", budget=5000.0, size_hint=0.1), _cfg())

    assert posted["json"]["budget"] == 5000.0


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


def test_buy_is_skipped_during_macro_blackout(monkeypatch):
    """A BUY on a day matching macro_blackout.dates must be refused locally, never forwarded to
    Floor Broker. `_is_quad_witching_day` is forced False so this test isolates the hand-
    maintained date-list path from the auto-computed quad-witching path (tested separately)."""
    _silence_slack(monkeypatch)
    monkeypatch.setattr(graph, "_is_quad_witching_day", lambda d: False)
    today = datetime.now(pytz.timezone("US/Eastern")).date().isoformat()
    cfg = _cfg()
    cfg.macro_blackout.enabled = True
    cfg.macro_blackout.dates = [{"date": today, "label": "CPI release"}]

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called during a macro blackout")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    result = graph.call_floor_broker(_state("BUY", budget=5000.0), cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "macro_blackout"


def test_buy_is_skipped_on_quad_witching_day(monkeypatch):
    """Quad witching is auto-detected (src/dealer/graph.py:_is_quad_witching_day), not a
    config.yaml entry -- forced True here to test that path independent of the current date."""
    _silence_slack(monkeypatch)
    monkeypatch.setattr(graph, "_is_quad_witching_day", lambda d: True)
    cfg = _cfg()
    cfg.macro_blackout.enabled = True

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called on a quad witching day")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    result = graph.call_floor_broker(_state("BUY", budget=5000.0), cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "macro_blackout"


def test_buy_is_not_skipped_when_macro_blackout_date_is_not_today(monkeypatch):
    _silence_slack(monkeypatch)
    monkeypatch.setattr(graph, "_is_quad_witching_day", lambda d: False)
    posted = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "executed", "detail": "buy order submitted: order-123"}

    def _fake_post(url, json, timeout):
        posted["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)

    cfg = _cfg()
    cfg.macro_blackout.enabled = True
    cfg.macro_blackout.dates = [{"date": "1999-01-01", "label": "not today"}]

    result = graph.call_floor_broker(_state("BUY", budget=5000.0), cfg)

    assert result["execution_result"]["status"] == "executed"


def test_sell_forwards_even_during_macro_blackout(monkeypatch):
    """The macro blackout gate only wraps the BUY branch -- SELL must reach Floor Broker
    normally regardless, since risk management (exiting positions) shouldn't itself be paused."""
    _silence_slack(monkeypatch)
    monkeypatch.setattr(graph, "_is_quad_witching_day", lambda d: False)
    posted = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "executed", "detail": "sell order submitted: order-456"}

    def _fake_post(url, json, timeout):
        posted["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)

    today = datetime.now(pytz.timezone("US/Eastern")).date().isoformat()
    cfg = _cfg()
    cfg.macro_blackout.enabled = True
    cfg.macro_blackout.dates = [{"date": today, "label": "CPI release"}]

    graph.call_floor_broker(_state("SELL", budget=5000.0), cfg)

    assert posted["json"]["budget"] == 5000.0


def test_is_quad_witching_day_matches_third_friday_of_quarter_end_months():
    from datetime import date

    assert graph._is_quad_witching_day(date(2026, 3, 20))  # third Friday of March 2026
    assert graph._is_quad_witching_day(date(2026, 6, 19))  # third Friday of June 2026
    assert graph._is_quad_witching_day(date(2026, 9, 18))  # third Friday of September 2026
    assert graph._is_quad_witching_day(date(2026, 12, 18))  # third Friday of December 2026


def test_is_quad_witching_day_false_for_other_fridays_and_months():
    from datetime import date

    assert not graph._is_quad_witching_day(date(2026, 3, 13))  # second Friday of March
    assert not graph._is_quad_witching_day(date(2026, 3, 27))  # fourth Friday of March
    assert not graph._is_quad_witching_day(date(2026, 4, 17))  # third Friday of April (not a quarter-end month)
    assert not graph._is_quad_witching_day(date(2026, 9, 17))  # a Thursday, not a Friday
