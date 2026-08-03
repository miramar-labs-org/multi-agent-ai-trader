from src.analyst.graph import validate_selection


def _state(raw_candidates, picks):
    return {"raw_candidates": raw_candidates, "selection": {"symbols": picks}}


def test_validate_selection_overrides_llm_exchange_with_known_market():
    """The LLM sometimes echoes back the wrong exchange for a pick -- validate_selection must
    trust discover_candidates()'s own `market` tag, not the LLM's copy of the field."""
    state = _state(
        raw_candidates=[{"symbol": "BTC/USD", "market": "binance"}],
        picks=[{"symbol": "BTC/USD", "exchange": "stocks", "budget": 100.0}],
    )

    result = validate_selection(state, cfg=None)

    assert result["selection"]["symbols"] == [{"symbol": "BTC/USD", "exchange": "binance", "budget": 100.0}]


def test_validate_selection_drops_hallucinated_pick():
    """A pick whose symbol was never offered in raw_candidates is a hallucination and must be
    dropped rather than written to the portfolio with a guessed exchange."""
    state = _state(
        raw_candidates=[{"symbol": "MGN", "market": "stocks"}],
        picks=[
            {"symbol": "MGN", "exchange": "stocks", "budget": 100.0},
            {"symbol": "FAKE", "exchange": "stocks", "budget": 100.0},
        ],
    )

    result = validate_selection(state, cfg=None)

    assert [pick["symbol"] for pick in result["selection"]["symbols"]] == ["MGN"]
