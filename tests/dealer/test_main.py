from omegaconf import OmegaConf

from src.dealer.main import should_process_entry


def _cfg(enable_stocks: bool, enable_crypto: bool):
    return OmegaConf.create({"trading": {"enable_stocks": enable_stocks, "enable_crypto": enable_crypto}})


def test_stock_entry_processed_only_when_stocks_enabled():
    entry = {"symbol": "MGN", "exchange": "stocks"}

    assert should_process_entry(entry, _cfg(enable_stocks=True, enable_crypto=True)) is True
    assert should_process_entry(entry, _cfg(enable_stocks=False, enable_crypto=True)) is False


def test_crypto_entry_processed_only_when_crypto_enabled():
    entry = {"symbol": "BTC/USD", "exchange": "binance"}

    assert should_process_entry(entry, _cfg(enable_stocks=True, enable_crypto=True)) is True
    assert should_process_entry(entry, _cfg(enable_stocks=True, enable_crypto=False)) is False
