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


def test_fetch_screener_candidates_keeps_candidates_with_no_known_price(monkeypatch):
    """most-actives candidates carry no price field -- must never be filtered out for lacking
    price data (that would be a silent, unintended universe restriction), only ones movers
    explicitly prices below min_price."""

    def fake_get(url, headers=None, timeout=None):
        if "most-actives" in url:
            return FakeResponse({"most_actives": [{"symbol": "MGN", "volume": 500000}]})
        return FakeResponse({"gainers": [], "losers": []})

    monkeypatch.setattr(sources.requests, "get", fake_get)

    candidates = sources.fetch_screener_candidates(top_n=20, min_price=1.0)

    assert {c["symbol"] for c in candidates} == {"MGN"}


def test_fetch_screener_candidates_defaults_to_no_price_filtering():
    """Backward-compat: callers that don't pass min_price (default 0.0) get every symbol
    regardless of price."""
    assert sources.fetch_screener_candidates.__defaults__ == (0.0,)
