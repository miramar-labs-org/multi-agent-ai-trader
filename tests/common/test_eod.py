from src.common import eod


class FakePosition:
    def __init__(self, symbol, qty, market_value, unrealized_plpc):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value
        self.unrealized_plpc = unrealized_plpc


class FakeTradingClient:
    def __init__(self, activities):
        self._activities = activities

    def get(self, path, data=None):
        assert path == "/account/activities"
        assert data["activity_types"] == "FILL"
        return self._activities


def _activity(symbol, side="buy", qty="1", price="100.00"):
    return {"symbol": symbol, "side": side, "qty": qty, "price": price}


def test_fetch_fills_with_no_filter_returns_everything(monkeypatch):
    monkeypatch.setattr(eod, "trading_client", FakeTradingClient([_activity("MGN"), _activity("BTC/USD")]))

    result = eod.fetch_fills("2026-08-03")

    assert [f["symbol"] for f in result] == ["MGN", "BTC/USD"]


def test_fetch_fills_only_crypto_true_keeps_only_slash_symbols(monkeypatch):
    monkeypatch.setattr(eod, "trading_client", FakeTradingClient([_activity("MGN"), _activity("BTC/USD")]))

    result = eod.fetch_fills("2026-08-03", only_crypto=True)

    assert [f["symbol"] for f in result] == ["BTC/USD"]


def test_fetch_fills_only_crypto_false_excludes_slash_symbols(monkeypatch):
    monkeypatch.setattr(eod, "trading_client", FakeTradingClient([_activity("MGN"), _activity("BTC/USD")]))

    result = eod.fetch_fills("2026-08-03", only_crypto=False)

    assert [f["symbol"] for f in result] == ["MGN"]


def test_fetch_fills_shapes_qty_and_price_as_floats(monkeypatch):
    monkeypatch.setattr(eod, "trading_client", FakeTradingClient([_activity("MGN", qty="2.5", price="10.125")]))

    result = eod.fetch_fills("2026-08-03")

    assert result == [{"symbol": "MGN", "side": "buy", "qty": 2.5, "price": 10.125}]


def test_summarize_positions_with_no_filter_returns_everything():
    positions = [FakePosition("MGN", "3", "150.00", "0.05"), FakePosition("BTC/USD", "0.01", "600.00", "-0.02")]

    result = eod.summarize_positions(positions)

    assert [p["symbol"] for p in result] == ["MGN", "BTC/USD"]


def test_summarize_positions_only_crypto_true_keeps_only_slash_symbols():
    positions = [FakePosition("MGN", "3", "150.00", "0.05"), FakePosition("BTC/USD", "0.01", "600.00", "-0.02")]

    result = eod.summarize_positions(positions, only_crypto=True)

    assert result == [{"symbol": "BTC/USD", "qty": 0.01, "market_value": 600.0, "unrealized_plpc": -0.02}]


def test_summarize_positions_only_crypto_false_excludes_slash_symbols():
    positions = [FakePosition("MGN", "3", "150.00", "0.05"), FakePosition("BTC/USD", "0.01", "600.00", "-0.02")]

    result = eod.summarize_positions(positions, only_crypto=False)

    assert result == [{"symbol": "MGN", "qty": 3.0, "market_value": 150.0, "unrealized_plpc": 0.05}]
