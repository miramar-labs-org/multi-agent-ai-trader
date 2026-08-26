from omegaconf import OmegaConf

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


def test_account_env_names_falls_back_to_defaults_when_config_section_absent(monkeypatch):
    monkeypatch.setattr(alpaca_client, "load_config", lambda: OmegaConf.create({}))

    assert alpaca_client.account_env_names("account2", "DEFAULT_KEY", "DEFAULT_SECRET") == ("DEFAULT_KEY", "DEFAULT_SECRET")


def test_account_env_names_uses_configured_env_var_names(monkeypatch):
    cfg = OmegaConf.create({"alpaca": {"account2": {"key_env": "ALPACA_ALT_KEY", "secret_env": "ALPACA_ALT_SECRET"}}})
    monkeypatch.setattr(alpaca_client, "load_config", lambda: cfg)

    assert alpaca_client.account_env_names("account2", "DEFAULT_KEY", "DEFAULT_SECRET") == ("ALPACA_ALT_KEY", "ALPACA_ALT_SECRET")


def test_account_env_names_falls_back_to_defaults_when_config_unreachable(monkeypatch):
    def _raise():
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(alpaca_client, "load_config", _raise)

    assert alpaca_client.account_env_names("account1", "DEFAULT_KEY", "DEFAULT_SECRET") == ("DEFAULT_KEY", "DEFAULT_SECRET")


def test_lazy_alpaca_client_builds_from_configured_env_vars_on_first_use(monkeypatch):
    """Regression: the whole point of _LazyAlpacaClient is that switching accounts is a config.yaml
    edit alone, with no rebuild/redeploy -- construction must be deferred to first real use (not
    import time) and must re-resolve which env vars to read from the live config on every access."""
    monkeypatch.setenv("KEY_A", "key-a")
    monkeypatch.setenv("SECRET_A", "secret-a")
    monkeypatch.setenv("KEY_B", "key-b")
    monkeypatch.setenv("SECRET_B", "secret-b")

    built = []
    client = alpaca_client._LazyAlpacaClient(
        "account2", "KEY_A", "SECRET_A", lambda key, secret: built.append((key, secret)) or object()
    )

    monkeypatch.setattr(alpaca_client, "load_config", lambda: OmegaConf.create({}))
    client._get_client()
    assert built == [("key-a", "secret-a")]

    # Same env var names resolved again -- must not rebuild.
    client._get_client()
    assert built == [("key-a", "secret-a")]

    # Config now points account2 at a different env var pair -- must rebuild off the new pair.
    cfg = OmegaConf.create({"alpaca": {"account2": {"key_env": "KEY_B", "secret_env": "SECRET_B"}}})
    monkeypatch.setattr(alpaca_client, "load_config", lambda: cfg)
    client._get_client()
    assert built == [("key-a", "secret-a"), ("key-b", "secret-b")]


def test_lazy_alpaca_client_proxies_attribute_access_to_the_resolved_client(monkeypatch):
    monkeypatch.setenv("KEY_A", "key-a")
    monkeypatch.setenv("SECRET_A", "secret-a")
    monkeypatch.setattr(alpaca_client, "load_config", lambda: OmegaConf.create({}))

    class FakeClient:
        def ping(self):
            return "pong"

    client = alpaca_client._LazyAlpacaClient("account1", "KEY_A", "SECRET_A", lambda key, secret: FakeClient())

    assert client.ping() == "pong"
