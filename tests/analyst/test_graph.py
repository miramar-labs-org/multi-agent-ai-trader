from datetime import datetime, timedelta

import pytz
from omegaconf import OmegaConf

from src.analyst import graph
from src.analyst.graph import crypto_eod_report, validate_selection
from src.common import eod


def _state(raw_candidates, picks):
    return {"raw_candidates": raw_candidates, "selection": {"symbols": picks}}


def _cfg(enable_crypto: bool):
    return OmegaConf.create({"trading": {"enable_crypto": enable_crypto}})


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


class FakePosition:
    def __init__(self, symbol, qty, market_value, unrealized_plpc):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value
        self.unrealized_plpc = unrealized_plpc


class FakeTradingClient:
    def __init__(self, positions, activities=None):
        self._positions = positions
        self._activities = activities or []

    def get_all_positions(self):
        return self._positions

    def get(self, path, data=None):
        return self._activities


def test_crypto_eod_report_skipped_when_crypto_disabled(monkeypatch):
    monkeypatch.setattr(graph, "trading_client", FakeTradingClient([]))
    posted = {}
    monkeypatch.setattr(graph.slack, "notify_crypto_eod_report", lambda *a, **k: posted.setdefault("called", True))

    crypto_eod_report({}, _cfg(enable_crypto=False))

    assert "called" not in posted


def test_crypto_eod_report_posts_only_crypto_positions_and_fills_for_the_prior_day(monkeypatch):
    """Crypto trades 24/7 -- this rides along with the morning report and must cover the prior
    full ET calendar day, filtered to crypto-only positions/fills (equities are the stock EOD
    CronJob's job, not this one's)."""
    positions = [
        FakePosition("MGN", "3", "150.00", "0.05"),
        FakePosition("BTC/USD", "0.01", "600.00", "-0.02"),
    ]
    activities = [
        {"symbol": "MGN", "side": "buy", "qty": "1", "price": "100.00"},
        {"symbol": "BTC/USD", "side": "sell", "qty": "0.005", "price": "60000.00"},
    ]
    fake_client = FakeTradingClient(positions, activities)
    monkeypatch.setattr(graph, "trading_client", fake_client)
    monkeypatch.setattr(eod, "trading_client", fake_client)
    posted = {}
    monkeypatch.setattr(
        graph.slack,
        "notify_crypto_eod_report",
        lambda report_date, fills, positions: posted.update(report_date=report_date, fills=fills, positions=positions),
    )

    crypto_eod_report({}, _cfg(enable_crypto=True))

    expected_date = (datetime.now(pytz.timezone("US/Eastern")) - timedelta(days=1)).date().isoformat()
    assert posted["report_date"] == expected_date
    assert [f["symbol"] for f in posted["fills"]] == ["BTC/USD"]
    assert [p["symbol"] for p in posted["positions"]] == ["BTC/USD"]
