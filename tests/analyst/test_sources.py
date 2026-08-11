from src.analyst import sources


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_fetch_screener_candidates_drops_movers_candidates_below_min_price(monkeypatch):
    """Regression for a live ANSCW BUY crash: a sub-$1 mover (e.g. a sub-penny stock) can
    violate Floor Broker's SL/TP bracket invariants (see execution.py's $0.02 floor/ceiling
    clamp). Filtering these out at the source, using the price movers already report, keeps
    them from ever reaching the LLM/Dealer."""

    def fake_get(url, headers=None, timeout=None):
        if "most-actives" in url:
            return FakeResponse({"most_actives": []})
        return FakeResponse(
            {
                "gainers": [
                    {"symbol": "ANSCW", "price": 0.0097, "percent_change": 500.0},
                    {"symbol": "NVDA", "price": 120.0, "percent_change": 3.2},
                ],
                "losers": [],
            }
        )

    monkeypatch.setattr(sources.requests, "get", fake_get)

    candidates = sources.fetch_screener_candidates(top_n=20, min_price=1.0)

    symbols = {c["symbol"] for c in candidates}
    assert "ANSCW" not in symbols
    assert "NVDA" in symbols


def test_fetch_screener_candidates_looks_up_price_for_most_actives_only_candidates(monkeypatch):
    """Regression for a live DSX.WS BUY: most-actives candidates carry no price field, so they
    used to pass the min_price filter unchecked -- a sub-$1 warrant sourced only from
    most-actives reached the LLM and got bought. A missing price must now be resolved via a
    live quote lookup and judged against min_price like any other candidate."""

    def fake_get(url, headers=None, timeout=None):
        if "most-actives" in url:
            return FakeResponse(
                {"most_actives": [{"symbol": "DSX.WS", "volume": 500000}, {"symbol": "MGN", "volume": 100000}]}
            )
        return FakeResponse({"gainers": [], "losers": []})

    prices = {"DSX.WS": 0.365, "MGN": 12.0}
    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources, "get_current_ask_price", lambda symbol: prices[symbol])

    candidates = sources.fetch_screener_candidates(top_n=20, min_price=1.0)

    symbols = {c["symbol"] for c in candidates}
    assert "DSX.WS" not in symbols
    assert "MGN" in symbols


def test_fetch_screener_candidates_drops_candidate_when_price_lookup_fails(monkeypatch):
    """A quote lookup failure must fail closed (drop the candidate), not silently pass it
    through -- the whole point of the lookup is to never let an unpriced candidate reach the
    LLM unchecked."""

    def fake_get(url, headers=None, timeout=None):
        if "most-actives" in url:
            return FakeResponse({"most_actives": [{"symbol": "MGN", "volume": 500000}]})
        return FakeResponse({"gainers": [], "losers": []})

    def fake_get_price(symbol):
        raise RuntimeError("no quote available")

    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources, "get_current_ask_price", fake_get_price)

    candidates = sources.fetch_screener_candidates(top_n=20, min_price=1.0)

    assert candidates == []


def test_fetch_screener_candidates_defaults_to_no_price_filtering():
    """Backward-compat: callers that don't pass min_price (default 0.0) get every symbol
    regardless of price."""
    assert sources.fetch_screener_candidates.__defaults__ == (0.0, None, None, ())


def test_fetch_screener_candidates_drops_extreme_movers_low_dollar_volume_and_suffixes(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "most-actives" in url:
            return FakeResponse(
                {
                    "most_actives": [
                        {"symbol": "GOOD", "volume": 200000},
                        {"symbol": "THIN", "volume": 1000},
                        {"symbol": "UNITU", "volume": 200000},
                    ]
                }
            )
        return FakeResponse(
            {
                "gainers": [
                    {"symbol": "WILD", "price": 10.0, "percent_change": 55.0},
                    {"symbol": "GOOD", "price": 10.0, "percent_change": 5.0},
                ],
                "losers": [],
            }
        )

    prices = {"GOOD": 10.0, "THIN": 10.0, "UNITU": 10.0}
    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setattr(sources, "get_current_ask_price", lambda symbol: prices[symbol])

    candidates = sources.fetch_screener_candidates(
        top_n=20,
        min_price=1.0,
        max_abs_change_pct=40.0,
        min_dollar_volume=1_000_000,
        excluded_suffixes=["U"],
    )

    assert [c["symbol"] for c in candidates] == ["GOOD"]


def test_fetch_crypto_candidates_keeps_only_usd_quoted_pairs(monkeypatch):
    """Alpaca paper accounts are USD-funded. A live SHIB/USDT pick repeatedly failed at BUY time
    because Alpaca tried to spend USDT, so crypto screener movers must not introduce non-USD
    quote pairs into the tradeable universe."""

    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(
            {
                "gainers": [
                    {"symbol": "SHIB/USDT", "percent_change": 12.3},
                    {"symbol": "DOGE/USD", "percent_change": 5.0},
                ],
                "losers": [{"symbol": "PEPE/USDT", "percent_change": -4.2}],
            }
        )

    monkeypatch.setattr(sources.requests, "get", fake_get)

    symbols = {c["symbol"] for c in sources.fetch_crypto_candidates(top_n=20)}

    assert "SHIB/USDT" not in symbols
    assert "PEPE/USDT" not in symbols
    assert "DOGE/USD" in symbols
    assert {"BTC/USD", "ETH/USD", "SOL/USD"}.issubset(symbols)


def test_fetch_earnings_calendar_returns_symbols_in_blackout_window(monkeypatch):
    """Report date is computed relative to `datetime.now()`, matching what the production code
    itself uses to build its from/to window, so this test never goes stale."""
    monkeypatch.setattr(sources.os, "getenv", lambda key, default="": "fake-token" if key == "FINNHUB_API_KEY" else default)
    tomorrow = (sources.datetime.now() + sources.timedelta(days=1)).date().isoformat()

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(
            {
                "earningsCalendar": [
                    {"symbol": "NVDA", "date": tomorrow},
                    {"symbol": "OTHER", "date": tomorrow},
                ]
            }
        )

    monkeypatch.setattr(sources.requests, "get", fake_get)

    blackout = sources.fetch_earnings_calendar(["NVDA", "MGN"], days_before=2, days_after=1)

    assert blackout == {"NVDA"}


def test_fetch_earnings_calendar_returns_empty_set_without_api_key(monkeypatch):
    monkeypatch.setattr(sources.os, "getenv", lambda key, default="": default)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Finnhub must not be called without an API key")

    monkeypatch.setattr(sources.requests, "get", _fail_if_called)

    assert sources.fetch_earnings_calendar(["NVDA"], days_before=2, days_after=1) == set()


def test_fetch_earnings_calendar_returns_empty_set_for_no_symbols(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Finnhub must not be called with an empty symbol list")

    monkeypatch.setattr(sources.requests, "get", _fail_if_called)

    assert sources.fetch_earnings_calendar([], days_before=2, days_after=1) == set()


def test_fetch_earnings_calendar_fails_soft_on_request_exception(monkeypatch):
    monkeypatch.setattr(sources.os, "getenv", lambda key, default="": "fake-token" if key == "FINNHUB_API_KEY" else default)

    def fake_get(url, params=None, timeout=None):
        raise sources.requests.RequestException("boom")

    monkeypatch.setattr(sources.requests, "get", fake_get)

    assert sources.fetch_earnings_calendar(["NVDA"], days_before=2, days_after=1) == set()


def test_fetch_earnings_calendar_fails_soft_on_non_200(monkeypatch):
    monkeypatch.setattr(sources.os, "getenv", lambda key, default="": "fake-token" if key == "FINNHUB_API_KEY" else default)
    monkeypatch.setattr(sources.requests, "get", lambda url, params=None, timeout=None: FakeResponse({}, status_code=500))

    assert sources.fetch_earnings_calendar(["NVDA"], days_before=2, days_after=1) == set()


def test_fetch_earnings_calendar_fails_soft_on_rate_limit(monkeypatch):
    monkeypatch.setattr(sources.os, "getenv", lambda key, default="": "fake-token" if key == "FINNHUB_API_KEY" else default)
    monkeypatch.setattr(sources.requests, "get", lambda url, params=None, timeout=None: FakeResponse({}, status_code=429))

    assert sources.fetch_earnings_calendar(["NVDA"], days_before=2, days_after=1) == set()
