from datetime import datetime, timezone

import pandas as pd

from src.backtest import data


def test_fetch_historical_bars_delegates_to_shared_hourly_fetch(monkeypatch):
    calls = []
    expected = pd.DataFrame({"open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [100]})

    def _fake_fetch_bars(symbol, timeframe_key, start, end):
        calls.append((symbol, timeframe_key, start, end))
        return expected

    monkeypatch.setattr(data, "fetch_bars", _fake_fetch_bars)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 2, tzinfo=timezone.utc)

    result = data.fetch_historical_bars("BTC/USD", start, end)

    assert result is expected
    assert calls == [("BTC/USD", "1h", start, end)]
