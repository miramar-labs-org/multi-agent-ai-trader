from alpaca.trading.enums import AssetClass
from omegaconf import OmegaConf

from src.common import portfolio_state


class FakePosition:
    def __init__(self, symbol, asset_class, market_value):
        self.symbol = symbol
        self.asset_class = asset_class
        self.market_value = market_value


class FakeTradingClient:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


def _cfg(enable_stocks: bool, enable_crypto: bool):
    return OmegaConf.create(
        {
            "trading": {
                "enable_stocks": enable_stocks,
                "enable_crypto": enable_crypto,
                "crypto_taapi_exchange": "binance",
            }
        }
    )


def test_stock_position_skipped_when_stocks_disabled(monkeypatch):
    """Regression: an unmanaged equity position must not be merged in while enable_stocks=False,
    mirroring Dealer's own should_process_entry() gate."""
    monkeypatch.setattr(
        portfolio_state, "trading_client", FakeTradingClient([FakePosition("MGN", AssetClass.US_EQUITY, 100.0)])
    )

    result = portfolio_state.merge_held_positions({"symbols": []}, _cfg(enable_stocks=False, enable_crypto=True))

    assert result["symbols"] == []


def test_crypto_position_skipped_when_crypto_disabled(monkeypatch):
    monkeypatch.setattr(
        portfolio_state, "trading_client", FakeTradingClient([FakePosition("BTC/USD", AssetClass.CRYPTO, 10.0)])
    )

    result = portfolio_state.merge_held_positions({"symbols": []}, _cfg(enable_stocks=True, enable_crypto=False))

    assert result["symbols"] == []


def test_crypto_position_merged_with_configured_exchange_when_enabled(monkeypatch):
    monkeypatch.setattr(
        portfolio_state, "trading_client", FakeTradingClient([FakePosition("BTC/USD", AssetClass.CRYPTO, 10.0)])
    )

    result = portfolio_state.merge_held_positions({"symbols": []}, _cfg(enable_stocks=True, enable_crypto=True))

    assert result["symbols"] == [
        {"symbol": "BTC/USD", "exchange": "binance", "budget": 10.0, "indicators": ["ALL"]}
    ]
