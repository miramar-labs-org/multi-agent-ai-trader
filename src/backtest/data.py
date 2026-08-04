from datetime import datetime

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from src.common.alpaca_client import crypto_data_client, stock_data_client
from src.common.logging import get_logger

log = get_logger("BACKTEST")

BAR_TIMEFRAME = TimeFrame.Hour  # matches cfg.indicators' `interval: 1h`, so period counts mean
# the same lookback window here as they do in a live TAAPI request


def fetch_historical_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetches hourly OHLCV bars for `symbol` between `start` and `end`. Returns a DataFrame
    indexed by timestamp with columns open/high/low/close/volume, or an empty DataFrame if no
    bars were returned (caller is responsible for skipping the symbol, not crashing)."""
    if "/" in symbol:
        request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=BAR_TIMEFRAME, start=start, end=end)
        bars = crypto_data_client.get_crypto_bars(request)
    else:
        # Free-tier accounts aren't entitled to the default SIP feed; IEX is the free-tier feed,
        # same limitation the live system already has for its own market data.
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=BAR_TIMEFRAME, start=start, end=end, feed=DataFeed.IEX
        )
        bars = stock_data_client.get_stock_bars(request)

    df = bars.df
    if df.empty:
        log(f"⚠️  no historical bars returned for {symbol} between {start} and {end}")
        return df

    if isinstance(df.index, pd.MultiIndex):
        df = df.loc[symbol]

    return df[["open", "high", "low", "close", "volume"]]
