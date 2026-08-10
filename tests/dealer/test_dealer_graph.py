from src.dealer import graph


def _state(indicators_text: str) -> dict:
    return {
        "symbol": "CRV/USD",
        "exchange": "binance",
        "budget": 100.0,
        "indicator_names": ["ALL"],
        "indicators_text": indicators_text,
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
    monkeypatch.setattr(graph.db, "record_dealer_decision", lambda *a, **k: recorded.setdefault("db", a))

    result = graph.skip_missing_indicators(_state(""), cfg=None)

    assert result["signal"]["action"] == "HOLD"
    assert result["execution_result"] == {"status": "skipped", "detail": "missing_indicators"}
    assert recorded["slack"][1] == "HOLD"
    assert recorded["db"][1] == "HOLD"
