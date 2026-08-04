from datetime import datetime, timedelta

import pytz
from alpaca.trading.enums import AssetClass
from omegaconf import OmegaConf

from src.analyst import graph
from src.analyst.graph import crypto_eod_report, validate_selection
from src.common import eod


def _state(raw_candidates, picks):
    return {"raw_candidates": raw_candidates, "selection": {"symbols": picks}}


def _cfg(enable_crypto: bool):
    return OmegaConf.create({"trading": {"enable_crypto": enable_crypto}})


def test_discover_candidates_skips_stocks_when_market_closed(monkeypatch):
    """Crypto trades 24/7 and must still be discovered on a day the stock market is closed --
    only the stock screener branch is gated on stock_market_open."""
    monkeypatch.setattr(graph.sources, "fetch_screener_candidates", lambda n: [{"symbol": "MGN"}])
    monkeypatch.setattr(graph.sources, "fetch_crypto_candidates", lambda n: [{"symbol": "BTC/USD"}])
    cfg = OmegaConf.create(
        {
            "trading": {"enable_stocks": True, "enable_crypto": True, "crypto_taapi_exchange": "binance"},
            "analyst": {"screener_top_n": 20},
        }
    )
    state = {
        "raw_candidates": [],
        "research_text": "",
        "indicator_text": "",
        "selection": None,
        "stock_market_open": False,
    }

    result = graph.discover_candidates(state, cfg)

    symbols = [c["symbol"] for c in result["raw_candidates"]]
    assert symbols == ["BTC/USD"]


def test_discover_candidates_includes_stocks_when_market_open(monkeypatch):
    monkeypatch.setattr(graph.sources, "fetch_screener_candidates", lambda n: [{"symbol": "MGN"}])
    monkeypatch.setattr(graph.sources, "fetch_crypto_candidates", lambda n: [])
    cfg = OmegaConf.create(
        {
            "trading": {"enable_stocks": True, "enable_crypto": False, "crypto_taapi_exchange": "binance"},
            "analyst": {"screener_top_n": 20},
        }
    )
    state = {
        "raw_candidates": [],
        "research_text": "",
        "indicator_text": "",
        "selection": None,
        "stock_market_open": True,
    }

    result = graph.discover_candidates(state, cfg)

    assert [c["symbol"] for c in result["raw_candidates"]] == ["MGN"]


class FakeAccountForPortfolio:
    def __init__(self):
        self.equity = "1000.00"
        self.cash = "500.00"
        self.buying_power = "2000.00"


class FakeAccountClient:
    def get_account(self):
        return FakeAccountForPortfolio()


def test_write_portfolio_passes_market_status_to_slack(monkeypatch):
    monkeypatch.setattr(graph, "_write_portfolio", lambda payload: None)
    monkeypatch.setattr(graph, "trading_client", FakeAccountClient())
    captured = {}
    monkeypatch.setattr(
        graph.slack, "notify_morning_report", lambda *a, **k: captured.update(args=a, kwargs=k)
    )
    state = {
        "raw_candidates": [],
        "research_text": "",
        "indicator_text": "",
        "selection": {"symbols": []},
        "stock_market_open": False,
    }
    cfg = OmegaConf.create({"trading": {"enable_crypto": True}})

    graph.write_portfolio(state, cfg)

    assert captured["kwargs"]["stock_market_open"] is False
    assert captured["kwargs"]["crypto_enabled"] is True


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
    def __init__(self, symbol, qty, market_value, unrealized_plpc, asset_class):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value
        self.unrealized_plpc = unrealized_plpc
        self.asset_class = asset_class


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
    # Alpaca's live Position.symbol for crypto has no slash (e.g. "BTCUSD"), unlike fill/activity
    # records ("BTC/USD") -- filtering must key off asset_class, not symbol shape.
    positions = [
        FakePosition("MGN", "3", "150.00", "0.05", AssetClass.US_EQUITY),
        FakePosition("BTCUSD", "0.01", "600.00", "-0.02", AssetClass.CRYPTO),
    ]
    activities = [
        {"symbol": "MGN", "side": "buy", "qty": "1", "price": "100.00", "transaction_time": "2026-08-02T14:00:00Z"},
        {
            "symbol": "BTC/USD",
            "side": "sell",
            "qty": "0.005",
            "price": "60000.00",
            "transaction_time": "2026-08-02T15:00:00Z",
        },
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
    assert [p["symbol"] for p in posted["positions"]] == ["BTCUSD"]


def _indicator_cfg(indicator_fetch_limit):
    return OmegaConf.create(
        {
            "analyst": {"indicator_fetch_limit": indicator_fetch_limit},
            "taapi": {"min_request_interval_secs": 15},
            "indicators": [],
        }
    )


def test_fetch_indicators_sorts_by_change_pct_and_respects_the_fetch_limit(monkeypatch):
    """Only the top `indicator_fetch_limit` candidates by |change_pct| get a real TAAPI call --
    fetching every screened candidate would blow past the 15s/request rate limit for candidates
    the LLM is unlikely to pick anyway. A missing change_pct (e.g. the crypto watchlist) must
    sort last, not crash the comparison."""
    candidates = [
        {"symbol": "A", "market": "stocks", "change_pct": 1.0},
        {"symbol": "B", "market": "stocks", "change_pct": -5.0},
        {"symbol": "C", "market": "stocks", "change_pct": 2.0},
        {"symbol": "D", "market": "stocks"},
    ]
    calls = []
    monkeypatch.setattr(
        graph,
        "fetch_indicators_bulk",
        lambda indicators_cfg, symbol, exchange, names, log: calls.append(symbol) or f"{symbol}-text",
    )
    sleeps = []
    monkeypatch.setattr(graph.time, "sleep", lambda s: sleeps.append(s))
    state = {"raw_candidates": candidates, "research_text": "", "indicator_text": "", "selection": None}

    result = graph.fetch_indicators(state, _indicator_cfg(indicator_fetch_limit=2))

    assert calls == ["B", "C"]
    assert sleeps == [15]
    assert "B-text" in result["indicator_text"]
    assert "C-text" in result["indicator_text"]
    assert "A-text" not in result["indicator_text"]
    assert "D-text" not in result["indicator_text"]


def test_fetch_indicators_omits_candidates_with_no_indicator_data(monkeypatch):
    candidates = [{"symbol": "A", "market": "stocks", "change_pct": 1.0}]
    monkeypatch.setattr(graph, "fetch_indicators_bulk", lambda *a, **k: "")
    monkeypatch.setattr(graph.time, "sleep", lambda s: None)
    state = {"raw_candidates": candidates, "research_text": "", "indicator_text": "", "selection": None}

    result = graph.fetch_indicators(state, _indicator_cfg(indicator_fetch_limit=5))

    assert result["indicator_text"] == ""


class FakeSelection:
    def __init__(self, symbols):
        self._symbols = symbols

    def model_dump(self):
        return {"symbols": self._symbols}


class FakeLLM:
    def __init__(self, captured):
        self._captured = captured

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self._captured["messages"] = messages
        return FakeSelection([])


def test_llm_select_prompt_includes_indicator_text_and_research_text(monkeypatch):
    captured = {}
    monkeypatch.setattr(graph, "ChatOpenAI", lambda **kwargs: FakeLLM(captured))
    cfg = OmegaConf.create(
        {
            "analyst": {"max_universe_size": 10, "default_budget": 5000, "indicator_fetch_limit": 15},
            "llm": {"base_url": "http://x", "model": "m", "temperature": 0.1},
        }
    )
    state = {
        "raw_candidates": [{"symbol": "MGN", "market": "stocks"}],
        "research_text": "MGN announces earnings beat",
        "indicator_text": "MGN:\nThe current RSI for MGN is 71.2",
        "selection": None,
    }

    graph.llm_select(state, cfg)

    user_content = captured["messages"][1].content
    assert "The current RSI for MGN is 71.2" in user_content
    assert "MGN announces earnings beat" in user_content
