from datetime import datetime, timedelta

import pytz
from omegaconf import OmegaConf

from src.dealer import graph


def _state(indicators_text: str) -> dict:
    return {
        "symbol": "CRV/USD",
        "exchange": "binance",
        "budget": 100.0,
        "indicator_names": ["ALL"],
        "indicators_text": indicators_text,
        "cycle_id": "cycle-1",
        "raw_bars": {},
        "ohlcv_features_text": "",
        "signal": None,
        "execution_result": None,
    }


def test_route_after_indicators_skips_llm_when_indicators_text_is_empty():
    assert graph._route_after_indicators(_state("")) == "skip_missing_indicators"


def test_route_after_indicators_skips_llm_when_indicators_text_is_whitespace_only():
    assert graph._route_after_indicators(_state("   \n")) == "skip_missing_indicators"


def test_route_after_indicators_calls_llm_when_indicators_text_present():
    assert graph._route_after_indicators(_state("rsi: 71.2")) == "llm_call"


def test_skip_missing_indicators_records_hold_without_calling_llm(monkeypatch):
    """Regression: CRV/USD, WIF/USD, LDO/USD intermittently got empty indicators_text back from
    TAAPI, and the old unconditional fetch_indicators -> llm_call edge let the LLM improvise a
    "please provide indicators" HOLD instead of a real trading decision. This node must record a
    diagnostic HOLD and skip the cycle without ever constructing an LLM client."""

    def _fail_if_called(**kwargs):
        raise AssertionError("LLM must not be called when indicators are missing")

    monkeypatch.setattr(graph, "ChatOpenAI", _fail_if_called)
    recorded = {}
    monkeypatch.setattr(graph.slack, "notify_dealer_signal", lambda *a, **k: recorded.setdefault("slack", a))
    monkeypatch.setattr(graph.db, "record_dealer_decision", lambda *a, **k: recorded.setdefault("db", (a, k)))

    result = graph.skip_missing_indicators(_state(""), cfg=None)

    assert result["signal"]["action"] == "HOLD"
    assert result["execution_result"] == {"status": "skipped", "detail": "missing_indicators"}
    assert recorded["slack"][1] == "HOLD"
    assert recorded["db"][0][1] == "HOLD"
    assert recorded["db"][1] == {"ohlcv_enrichment_active": False, "cycle_id": "cycle-1"}


def test_fetch_market_data_is_noop_when_disabled(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("disabled OHLCV enrichment must not fetch bars")

    monkeypatch.setattr(graph, "fetch_multi_timeframe_bars", _fail_if_called)
    cfg = OmegaConf.create({"ohlcv_enrichment": {"enabled": False}})

    result = graph.fetch_market_data(_state("rsi: 71.2"), cfg)

    assert result["raw_bars"] == {}
    assert result["ohlcv_features_text"] == ""


def test_fetch_market_data_populates_stock_features(monkeypatch):
    bars = {"5m": object()}
    monkeypatch.setattr(graph, "fetch_multi_timeframe_bars", lambda symbol, exchange, cfg: bars)
    monkeypatch.setattr(graph, "compute_derived_features", lambda df, cfg: {"latest_close": 10.0})
    monkeypatch.setattr(graph, "format_features_text", lambda features, symbol: "features text")
    cfg = OmegaConf.create({"ohlcv_enrichment": {"enabled": True}})

    result = graph.fetch_market_data({**_state("rsi: 71.2"), "exchange": "stocks"}, cfg)

    assert result["raw_bars"] == bars
    assert result["ohlcv_features_text"] == "features text"


def test_llm_call_includes_ohlcv_features_when_present(monkeypatch):
    captured = {}

    class FakeSignal:
        action = "HOLD"

        def model_dump(self):
            return {"action": "HOLD", "reasoning": "wait", "size_hint": 0.0, "confidence": 0.0}

    class FakeStructured:
        def invoke(self, messages):
            captured["user_prompt"] = messages[-1].content
            return FakeSignal()

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema):
            return FakeStructured()

    cfg = OmegaConf.create(
        {
            "llm": {"base_url": "http://llm.test/v1", "model": "test-model", "temperature": 0.0},
            "strategy": {"dealer_memory": {"enabled": False}},
        }
    )
    monkeypatch.setattr(graph, "ChatOpenAI", FakeChatOpenAI)

    graph.llm_call({**_state("rsi: 25"), "ohlcv_features_text": "5m return: 2%"}, cfg)

    assert "Additional OHLCV context" in captured["user_prompt"]
    assert "5m return: 2%" in captured["user_prompt"]


def test_llm_call_includes_recent_same_symbol_memory(monkeypatch):
    captured = {}

    class FakeSignal:
        action = "HOLD"

        def model_dump(self):
            return {"action": "HOLD", "reasoning": "wait", "size_hint": 0.0, "confidence": 0.0}

    class FakeStructured:
        def invoke(self, messages):
            captured["user_prompt"] = messages[-1].content
            return FakeSignal()

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema):
            return FakeStructured()

    cfg = OmegaConf.create(
        {
            "llm": {"base_url": "http://llm.test/v1", "model": "test-model", "temperature": 0.0},
            "strategy": {"dealer_memory": {"enabled": True}, "symbol_memory_days": 2, "symbol_memory_limit": 4},
        }
    )
    monkeypatch.setattr(graph, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(
        graph.db,
        "fetch_symbol_dealer_decisions_since",
        lambda symbol, since_date, limit: [
            {"decided_at": "t1", "action": "BUY", "size_hint": 0.5, "reasoning": "prior buy"}
        ],
    )
    monkeypatch.setattr(
        graph.db,
        "fetch_symbol_floor_broker_events_since",
        lambda symbol, since_date, limit: [
            {"occurred_at": "t2", "event_type": "fill", "detail": "stop_loss leg filled: o-1"}
        ],
    )

    result = graph.llm_call(_state("rsi: 25"), cfg)

    assert result["signal"]["action"] == "HOLD"
    assert "Recent same-symbol trading history" in captured["user_prompt"]
    assert "prior buy" in captured["user_prompt"]
    assert "stop_loss leg filled" in captured["user_prompt"]


def test_select_option_contract_is_noop_when_disabled(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("disabled options_trading must not call MCP tools")

    monkeypatch.setattr(graph, "_select_option_contract_async", _fail_if_called)
    cfg = OmegaConf.create({"options_trading": {"enabled": False}})
    state = {**_state("rsi: 71.2"), "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"}}

    result = graph.select_option_contract(state, cfg)

    assert result["option_pick"] is None


def test_select_option_contract_is_noop_on_hold(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("a HOLD signal must not trigger option contract search")

    monkeypatch.setattr(graph, "_select_option_contract_async", _fail_if_called)
    cfg = OmegaConf.create(
        {"options_trading": {"enabled": True}, "strategy": {"min_confidence": 0.6}}
    )
    state = {**_state("rsi: 71.2"), "signal": {"action": "HOLD", "confidence": 0.9, "reasoning": "r"}}

    result = graph.select_option_contract(state, cfg)

    assert result["option_pick"] is None


def test_select_option_contract_returns_pick_dict(monkeypatch):
    async def _fake_select(state, cfg, signal):
        return graph.OptionContractPick(
            contract_symbol="AAPL250117C00200000",
            strike=200.0,
            expiration="2025-01-17",
            right="call",
            delta=0.45,
            premium=3.20,
            reasoning="within delta/DTE window with sufficient OI",
        )

    monkeypatch.setattr(graph, "_select_option_contract_async", _fake_select)
    cfg = OmegaConf.create(
        {"options_trading": {"enabled": True}, "strategy": {"min_confidence": 0.6}}
    )
    state = {**_state("rsi: 71.2"), "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"}}

    result = graph.select_option_contract(state, cfg)

    assert result["option_pick"]["contract_symbol"] == "AAPL250117C00200000"
    assert result["option_pick"]["right"] == "call"


def _option_cfg(**overrides):
    base = {
        "floor_broker": {"base_url": "http://floor-broker.test:8000"},
        "macro_blackout": {"enabled": False, "dates": []},
        "strategy": {
            "risk_per_trade_usd": 100,
            "win_rate_throttle": {"enabled": False},
            "symbol_stop_cooldown": {"enabled": False},
        },
        "options_trading": {
            "dte_min": 14,
            "dte_max": 45,
            "target_delta_min": 0.30,
            "target_delta_max": 0.60,
        },
    }
    base["options_trading"].update(overrides)
    return OmegaConf.create(base)


def _far_expiration(days: int) -> str:
    return (datetime.now(pytz.timezone("US/Eastern")) + timedelta(days=days)).date().isoformat()


def test_call_floor_broker_option_skips_when_dte_out_of_range(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)
    cfg = _option_cfg()
    state = {
        **_state("rsi: 71.2"),
        "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"},
        "option_pick": {
            "contract_symbol": "AAPL250117C00200000",
            "strike": 200.0,
            "expiration": _far_expiration(2),
            "right": "call",
            "delta": 0.45,
            "premium": 3.20,
            "reasoning": "r",
        },
    }

    result = graph.call_floor_broker_option(state, cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "dte_out_of_range"


def test_call_floor_broker_option_skips_when_delta_out_of_range(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)
    cfg = _option_cfg()
    state = {
        **_state("rsi: 71.2"),
        "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"},
        "option_pick": {
            "contract_symbol": "AAPL250117C00200000",
            "strike": 200.0,
            "expiration": _far_expiration(20),
            "right": "call",
            "delta": 0.15,
            "premium": 3.20,
            "reasoning": "r",
        },
    }

    result = graph.call_floor_broker_option(state, cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "delta_out_of_range"


def test_call_floor_broker_option_skips_when_qty_would_be_zero(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)
    cfg = _option_cfg()
    state = {
        **_state("rsi: 71.2"),
        "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"},
        "option_pick": {
            "contract_symbol": "AAPL250117C00200000",
            "strike": 200.0,
            "expiration": _far_expiration(20),
            "right": "call",
            "delta": 0.45,
            "premium": 50.0,
            "reasoning": "r",
        },
    }

    result = graph.call_floor_broker_option(state, cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "qty_zero"


def test_call_floor_broker_option_posts_to_execute_option(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "submitted", "detail": "option buy order submitted: order-1"}

    def _fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)
    cfg = _option_cfg()
    state = {
        **_state("rsi: 71.2"),
        "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"},
        "option_pick": {
            "contract_symbol": "AAPL250117C00200000",
            "strike": 200.0,
            "expiration": _far_expiration(20),
            "right": "call",
            "delta": 0.45,
            "premium": 0.50,
            "reasoning": "r",
        },
    }

    result = graph.call_floor_broker_option(state, cfg)

    assert captured["url"] == "http://floor-broker.test:8000/execute-option"
    assert captured["json"]["contract_symbol"] == "AAPL250117C00200000"
    assert captured["json"]["qty"] == 2  # floor(100 / (0.50 * 100)) == floor(2.0) == 2
    assert result["execution_result"]["status"] == "submitted"


def _base_option_gate_cfg(**strategy_overrides):
    strategy = {
        "risk_per_trade_usd": 100,
        "win_rate_throttle": {"enabled": False},
        "symbol_stop_cooldown": {"enabled": False},
        "min_confidence": 0.6,
    }
    strategy.update(strategy_overrides)
    return OmegaConf.create(
        {
            "floor_broker": {"base_url": "http://floor-broker.test:8000"},
            "macro_blackout": {"enabled": False, "dates": []},
            "strategy": strategy,
            "options_trading": {
                "dte_min": 14,
                "dte_max": 45,
                "target_delta_min": 0.30,
                "target_delta_max": 0.60,
            },
            "analyst": {"track_record_days": 5},
        }
    )


def _option_pick_state(action: str = "BUY"):
    return {
        **_state("rsi: 71.2"),
        "signal": {"action": action, "confidence": 0.9, "reasoning": "r"},
        "option_pick": {
            "contract_symbol": "AAPL250117C00200000",
            "strike": 200.0,
            "expiration": _far_expiration(20),
            "right": "call",
            "delta": 0.45,
            "premium": 0.50,
            "reasoning": "r",
        },
    }


def test_call_floor_broker_option_skips_buy_during_macro_blackout(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called during a macro blackout")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    cfg = _base_option_gate_cfg()
    today = datetime.now(pytz.timezone("US/Eastern")).date().isoformat()
    cfg.macro_blackout.enabled = True
    cfg.macro_blackout.dates = [{"date": today, "label": "CPI release"}]

    result = graph.call_floor_broker_option(_option_pick_state(), cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "macro_blackout"


def test_call_floor_broker_option_skips_buy_when_symbol_recently_stopped_out(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called while symbol cooldown is active")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    cfg = _base_option_gate_cfg(
        symbol_stop_cooldown={"enabled": True}, symbol_stop_cooldown_days=1, max_symbol_stop_losses=1
    )
    monkeypatch.setattr(
        graph.db,
        "fetch_symbol_floor_broker_events_since",
        lambda symbol, since_date, limit=100: [{"event_type": "fill", "detail": "stop_loss leg filled: order-1"}],
    )

    result = graph.call_floor_broker_option(_option_pick_state(), cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "symbol_stop_cooldown"


def test_call_floor_broker_option_skips_buy_when_win_rate_below_minimum(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called while the win-rate throttle is active")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    cfg = _base_option_gate_cfg(
        win_rate_throttle={"enabled": True},
        win_rate_throttle_scope="global",
        min_win_rate=0.3,
        win_rate_min_sample=5,
    )
    events = [{"event_type": "fill", "detail": "stop_loss leg filled: order-1"}] * 4 + [
        {"event_type": "fill", "detail": "take_profit leg filled: order-1"}
    ]
    monkeypatch.setattr(graph.db, "fetch_floor_broker_events_since", lambda since_date: events)

    result = graph.call_floor_broker_option(_option_pick_state(), cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "win_rate_throttle"


def test_call_floor_broker_option_sell_bypasses_buy_only_gates(monkeypatch):
    """A SELL (closing an existing option position) must never be blocked by the BUY-only gates --
    same parity as call_floor_broker's own stock-path gates, which are also `if action == "BUY":`
    guarded."""
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "submitted", "detail": "option sell order submitted: order-1"}

    def _fake_post(url, json, timeout):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(graph.requests, "post", _fake_post)

    cfg = _base_option_gate_cfg()
    today = datetime.now(pytz.timezone("US/Eastern")).date().isoformat()
    cfg.macro_blackout.enabled = True
    cfg.macro_blackout.dates = [{"date": today, "label": "CPI release"}]

    result = graph.call_floor_broker_option(_option_pick_state(action="SELL"), cfg)

    assert result["execution_result"]["status"] == "submitted"
    assert captured["json"]["side"] == "BUY"  # payload's `side` is hardcoded BUY-only today (unrelated, pre-existing)


def test_call_floor_broker_option_skips_when_risk_per_trade_usd_not_configured(monkeypatch):
    monkeypatch.setattr(graph.slack, "notify_floor_broker_result", lambda *a, **k: None)
    monkeypatch.setattr(graph.db, "record_floor_broker_event", lambda *a, **k: None)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Floor Broker must not be called when risk_per_trade_usd is unset")

    monkeypatch.setattr(graph.requests, "post", _fail_if_called)

    cfg = _base_option_gate_cfg(risk_per_trade_usd=None)

    result = graph.call_floor_broker_option(_option_pick_state(), cfg)

    assert result["execution_result"]["status"] == "skipped"
    assert result["execution_result"]["reason"] == "risk_per_trade_usd_not_configured"


def test_route_after_llm_call_selects_option_branch_when_enabled():
    cfg = OmegaConf.create({"options_trading": {"enabled": True}})
    assert graph._route_after_llm_call({}, cfg) == "select_option_contract"


def test_route_after_llm_call_selects_stock_branch_when_disabled():
    cfg = OmegaConf.create({"options_trading": {"enabled": False}})
    assert graph._route_after_llm_call({}, cfg) == "call_floor_broker"
