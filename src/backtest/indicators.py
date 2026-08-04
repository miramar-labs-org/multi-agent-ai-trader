import pandas as pd


def _properties(cfg, name: str) -> dict:
    """Pulls an indicator's query-parameter properties from cfg.indicators -- the same catalog
    Dealer/Analyst read for live TAAPI requests -- so periods mean the same thing in the backtest
    as they do live, with no second, hand-copied set of period values to drift out of sync."""
    for entry in cfg.indicators:
        if entry.name == name:
            return entry.properties
    raise KeyError(f"no '{name}' entry in cfg.indicators")


def rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0)


def macd(closes: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    fast_ema = closes.ewm(span=fast, adjust=False).mean()
    slow_ema = closes.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": macd_line, "macd_signal": signal_line})


def sma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period).mean()


def ema(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def bbands(closes: pd.Series, period: int, stddev: float) -> pd.DataFrame:
    mid = closes.rolling(period).mean()
    band = closes.rolling(period).std() * stddev
    return pd.DataFrame({"bb_upper": mid + band, "bb_mid": mid, "bb_lower": mid - band})


def compute_indicators(bars: pd.DataFrame, cfg) -> pd.DataFrame:
    """Computes every indicator the strategies in strategies.py need, once over the full bar
    series (no per-bar API calls, unlike live TAAPI), aligned to `bars`' index."""
    closes = bars["close"]

    rsi_props = _properties(cfg, "rsi")
    macd_props = _properties(cfg, "macd")
    sma_props = _properties(cfg, "sma")
    ema_props = _properties(cfg, "ema")
    bbands_props = _properties(cfg, "bbands")

    result = pd.DataFrame(index=bars.index)
    result["rsi"] = rsi(closes, rsi_props.period)
    macd_df = macd(closes, macd_props.optInFastPeriod, macd_props.optInSlowPeriod, macd_props.optInSignalPeriod)
    result["macd"] = macd_df["macd"]
    result["macd_signal"] = macd_df["macd_signal"]
    result["sma"] = sma(closes, sma_props.period)
    result["ema"] = ema(closes, ema_props.period)
    bbands_df = bbands(closes, bbands_props.period, bbands_props.stddev)
    result["bb_upper"] = bbands_df["bb_upper"]
    result["bb_mid"] = bbands_df["bb_mid"]
    result["bb_lower"] = bbands_df["bb_lower"]

    return result
