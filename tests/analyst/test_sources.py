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
    assert sources.fetch_screener_candidates.__defaults__ == (0.0,)
