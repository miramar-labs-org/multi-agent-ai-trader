from alpaca.trading.enums import AssetClass

from src.common import eod


class FakePosition:
    def __init__(
        self,
        symbol,
        qty,
        market_value,
        unrealized_plpc,
        asset_class,
        avg_entry_price="0",
        unrealized_pl="0",
        current_price="0",
    ):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value
        self.unrealized_plpc = unrealized_plpc
        self.asset_class = asset_class
        self.avg_entry_price = avg_entry_price
        self.unrealized_pl = unrealized_pl
        self.current_price = current_price


class FakeTradingClient:
    def __init__(self, activities):
        self._activities = activities

    def get(self, path, data=None):
        assert path == "/account/activities"
        assert data["activity_types"] == "FILL"
        return self._activities


def _activity(symbol, side="buy", qty="1", price="100.00", transaction_time="2026-08-03T14:32:01.123456Z"):
    return {"symbol": symbol, "side": side, "qty": qty, "price": price, "transaction_time": transaction_time}


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

    assert result == [
        {
            "symbol": "MGN",
            "side": "buy",
            "qty": 2.5,
            "price": 10.125,
            "time": "2026-08-03T14:32:01.123456Z",
        }
    ]


def test_fetch_fills_carries_transaction_time_through_unchanged(monkeypatch):
    """notify_eod_report/notify_crypto_eod_report format this into a per-fill timestamp -- must
    be passed through as Alpaca returns it, not dropped or reformatted here."""
    monkeypatch.setattr(
        eod, "trading_client", FakeTradingClient([_activity("MGN", transaction_time="2026-08-03T09:31:05.5Z")])
    )

    result = eod.fetch_fills("2026-08-03")

    assert result[0]["time"] == "2026-08-03T09:31:05.5Z"


class FakeAccountClient:
    """A trading client exposing just what the fetch_all_* aggregators touch."""

    def __init__(self, activities=None, positions=None, account=None):
        self._activities = activities or []
        self._positions = positions or []
        self._account = account

    def get(self, path, data=None):
        return self._activities

    def get_all_positions(self):
        return list(self._positions)

    def get_account(self):
        return self._account


class _Acct:
    def __init__(self, equity, last_equity, cash, buying_power):
        self.equity = equity
        self.last_equity = last_equity
        self.cash = cash
        self.buying_power = buying_power


def test_fetch_all_fills_concatenates_every_distinct_account(monkeypatch):
    a = FakeAccountClient(activities=[_activity("MGN")])
    b = FakeAccountClient(activities=[_activity("AAPL250117C00150000", side="buy")])
    monkeypatch.setattr(eod, "distinct_trading_clients", lambda: [a, b])

    result = eod.fetch_all_fills("2026-08-03")

    assert [f["symbol"] for f in result] == ["MGN", "AAPL250117C00150000"]


def test_fetch_all_fills_with_single_shared_account_matches_fetch_fills(monkeypatch):
    only = FakeAccountClient(activities=[_activity("MGN"), _activity("BTC/USD")])
    monkeypatch.setattr(eod, "distinct_trading_clients", lambda: [only])

    assert eod.fetch_all_fills("2026-08-03", only_crypto=False) == [
        f for f in eod.fetch_fills("2026-08-03", client=only) if "/" not in f["symbol"]
    ]


def test_fetch_all_positions_concatenates_every_distinct_account(monkeypatch):
    a = FakeAccountClient(positions=["stock-pos"])
    b = FakeAccountClient(positions=["option-pos-1", "option-pos-2"])
    monkeypatch.setattr(eod, "distinct_trading_clients", lambda: [a, b])

    assert eod.fetch_all_positions() == ["stock-pos", "option-pos-1", "option-pos-2"]


def test_fetch_combined_account_summary_sums_each_field_across_accounts(monkeypatch):
    a = FakeAccountClient(account=_Acct("1050.00", "1000.00", "500.00", "2000.00"))
    b = FakeAccountClient(account=_Acct("220.00", "200.00", "80.00", "300.00"))
    monkeypatch.setattr(eod, "distinct_trading_clients", lambda: [a, b])

    assert eod.fetch_combined_account_summary() == {
        "equity": 1270.0,
        "last_equity": 1200.0,
        "cash": 580.0,
        "buying_power": 2300.0,
    }


def _positions():
    # Alpaca's live Position.symbol for crypto has no slash (e.g. "BTCUSD"), unlike fill/activity
    # records -- summarize_positions must filter on asset_class, not symbol shape.
    return [
        FakePosition(
            "MGN", "3", "150.00", "0.05", AssetClass.US_EQUITY,
            avg_entry_price="45.00", unrealized_pl="7.50", current_price="50.00",
        ),
        FakePosition(
            "BTCUSD", "0.01", "600.00", "-0.02", AssetClass.CRYPTO,
            avg_entry_price="61000.00", unrealized_pl="-10.00", current_price="60000.00",
        ),
    ]


def test_summarize_positions_with_no_filter_returns_everything():
    result = eod.summarize_positions(_positions())

    assert [p["symbol"] for p in result] == ["MGN", "BTCUSD"]


def test_summarize_positions_only_crypto_true_keeps_only_crypto_asset_class():
    result = eod.summarize_positions(_positions(), only_crypto=True)

    assert result == [
        {
            "symbol": "BTCUSD",
            "qty": 0.01,
            "market_value": 600.0,
            "unrealized_plpc": -0.02,
            "unrealized_pl": -10.0,
            "avg_entry_price": 61000.0,
            "current_price": 60000.0,
        }
    ]


def test_summarize_positions_only_crypto_false_excludes_crypto_asset_class():
    result = eod.summarize_positions(_positions(), only_crypto=False)

    assert result == [
        {
            "symbol": "MGN",
            "qty": 3.0,
            "market_value": 150.0,
            "unrealized_plpc": 0.05,
            "unrealized_pl": 7.5,
            "avg_entry_price": 45.0,
            "current_price": 50.0,
        }
    ]


def test_summarize_positions_handles_none_unrealized_pl_and_current_price():
    """unrealized_pl and current_price are Optional[str] on Alpaca's own Position model --
    summarize_positions must pass None through rather than crashing on float(None)."""
    position = FakePosition(
        "MGN", "3", "150.00", "0.05", AssetClass.US_EQUITY,
        avg_entry_price="45.00", unrealized_pl=None, current_price=None,
    )

    result = eod.summarize_positions([position])

    assert result[0]["unrealized_pl"] is None
    assert result[0]["current_price"] is None
    assert result[0]["avg_entry_price"] == 45.0
