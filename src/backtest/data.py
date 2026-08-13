from datetime import datetime

import pandas as pd
from alpaca.data.timeframe import TimeFrame

from src.common.bars import fetch_bars
from src.common.logging import get_logger

log = get_logger("BACKTEST")

BAR_TIMEFRAME = TimeFrame.Hour  # matches cfg.indicators' `interval: 1h`, so period counts mean
# the same lookback window here as they do in a live TAAPI request


def fetch_historical_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetches hourly OHLCV bars for `symbol` between `start` and `end`. Returns a DataFrame
    indexed by timestamp with columns open/high/low/close/volume, or an empty DataFrame if no
    bars were returned (caller is responsible for skipping the symbol, not crashing)."""
    df = fetch_bars(symbol, "1h", start, end)
    if df.empty:
        log(f"⚠️  no historical bars returned for {symbol} between {start} and {end}")
        return df
    return df
