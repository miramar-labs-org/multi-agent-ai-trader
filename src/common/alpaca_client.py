import os

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, CryptoLatestQuoteRequest, OptionLatestQuoteRequest
from omegaconf import OmegaConf

from src.common.config import load_config
from src.common.symbols import canonical_crypto_symbol, is_usd_crypto_symbol


def account_env_names(account: str, default_key_env: str, default_secret_env: str) -> tuple[str, str]:
    """Looks up which env var names hold account's credentials from the live config's
    alpaca.<account>.key_env/secret_env (falls back to the given defaults if the config section is
    absent, or if config itself is unreachable -- credential resolution must never be a new single
    point of failure beyond what src.common.config already tolerates). This is what lets switching
    paper accounts be a config.yaml edit alone: config.yaml is polled fresh every 60s (see
    src.common.config.load_config), so a live pod picks up a new key_env/secret_env pointing at a
    different already-present env var pair with no rebuild/redeploy -- as long as that account's
    actual credentials were already added to the k8s Secret (and the pod restarted once to pick up
    the new env vars themselves; env var *values* are still fixed at pod start, only which pair is
    *active* is live-switchable)."""
    try:
        cfg = load_config()
        key_env = OmegaConf.select(cfg, f"alpaca.{account}.key_env", default=None)
        secret_env = OmegaConf.select(cfg, f"alpaca.{account}.secret_env", default=None)
    except Exception:
        key_env, secret_env = None, None
    return key_env or default_key_env, secret_env or default_secret_env


class _LazyAlpacaClient:
    """Wraps an Alpaca SDK client so it's built lazily -- from whichever env vars
    account_env_names currently resolves to -- on first real use, instead of once at import time.
    Re-resolves (and rebuilds) on every access if the configured env var names have changed since
    the last build, which is what makes a config.yaml account switch take effect without a
    redeploy. Deferring construction out of import time also means importing this module never
    raises just because real Alpaca credentials aren't set (e.g. in a test environment) --
    construction only fails, same as before, the first time a caller actually uses the client."""

    def __init__(self, account: str, default_key_env: str, default_secret_env: str, factory):
        self._account = account
        self._default_key_env = default_key_env
        self._default_secret_env = default_secret_env
        self._factory = factory
        self._resolved_env_names = None
        self._client = None

    def _get_client(self):
        env_names = account_env_names(self._account, self._default_key_env, self._default_secret_env)
        if self._client is None or env_names != self._resolved_env_names:
            key_env, secret_env = env_names
            self._client = self._factory(os.getenv(key_env), os.getenv(secret_env))
            self._resolved_env_names = env_names
        return self._client

    def __getattr__(self, name):
        return getattr(self._get_client(), name)


trading_client = _LazyAlpacaClient(
    "account1", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
    lambda key, secret: TradingClient(key, secret, paper=True),
)
stock_data_client = _LazyAlpacaClient(
    "account1", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET", StockHistoricalDataClient
)
crypto_data_client = _LazyAlpacaClient(
    "account1", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET", CryptoHistoricalDataClient
)

trading_client2 = _LazyAlpacaClient(
    "account2", "ALPACA_PAPER_API_KEY2", "ALPACA_PAPER_API_SECRET2",
    lambda key, secret: TradingClient(key, secret, paper=True),
)
option_data_client2 = _LazyAlpacaClient(
    "account2", "ALPACA_PAPER_API_KEY2", "ALPACA_PAPER_API_SECRET2", OptionHistoricalDataClient
)


def get_current_ask_price(symbol: str) -> float:
    if "/" in symbol or is_usd_crypto_symbol(symbol):
        symbol = canonical_crypto_symbol(symbol)
        quote = crypto_data_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
    else:
        quote = stock_data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
    return quote[symbol].ask_price


def get_current_bid_price(symbol: str) -> float:
    if "/" in symbol or is_usd_crypto_symbol(symbol):
        symbol = canonical_crypto_symbol(symbol)
        quote = crypto_data_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
    else:
        quote = stock_data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
    return quote[symbol].bid_price


def get_current_option_mid_price(contract_symbol: str) -> float:
    quote = option_data_client2.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
    )
    q = quote[contract_symbol]
    return (q.bid_price + q.ask_price) / 2


def get_current_option_ask_price(contract_symbol: str) -> float:
    quote = option_data_client2.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
    )
    return quote[contract_symbol].ask_price
