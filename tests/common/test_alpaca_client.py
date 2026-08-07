from src.common import alpaca_client


class FakeQuote:
    def __init__(self, ask_price=101.0, bid_price=99.0):
        self.ask_price = ask_price
        self.bid_price = bid_price


class FakeCryptoDataClient:
    def __init__(self):
        self.requests = []

    def get_crypto_latest_quote(self, request):
        self.requests.append(request)
        return {"BTC/USD": FakeQuote()}


class FakeStockDataClient:
    def __init__(self):
        self.requests = []

    def get_stock_latest_quote(self, request):
        self.requests.append(request)
        return {"PUSD": FakeQuote()}


def test_slashless_known_crypto_symbol_routes_to_crypto_quote_client(monkeypatch):
    crypto = FakeCryptoDataClient()
    stock = FakeStockDataClient()
    monkeypatch.setattr(alpaca_client, "crypto_data_client", crypto)
    monkeypatch.setattr(alpaca_client, "stock_data_client", stock)

    assert alpaca_client.get_current_ask_price("BTCUSD") == 101.0

    assert len(crypto.requests) == 1
    assert stock.requests == []


def test_stock_ticker_ending_in_usd_still_routes_to_stock_quote_client(monkeypatch):
    crypto = FakeCryptoDataClient()
    stock = FakeStockDataClient()
    monkeypatch.setattr(alpaca_client, "crypto_data_client", crypto)
    monkeypatch.setattr(alpaca_client, "stock_data_client", stock)

    assert alpaca_client.get_current_bid_price("PUSD") == 99.0

    assert crypto.requests == []
    assert len(stock.requests) == 1
