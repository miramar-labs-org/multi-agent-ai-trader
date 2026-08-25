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
