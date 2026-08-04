import math

import pandas as pd
import pytest
from omegaconf import OmegaConf

from src.backtest import indicators


def _cfg():
    return OmegaConf.create(
        {
            "indicators": [
                {"name": "rsi", "properties": {"period": 14}},
                {"name": "macd", "properties": {"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}},
                {"name": "sma", "properties": {"period": 3}},
                {"name": "ema", "properties": {"period": 3}},
                {"name": "bbands", "properties": {"period": 2, "stddev": 2}},
            ]
        }
    )


def test_rsi_is_100_when_every_bar_is_a_gain():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = indicators.rsi(closes, period=3)
    assert (result.iloc[1:] == 100.0).all()


def test_rsi_is_0_when_every_bar_is_a_loss():
    closes = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
    result = indicators.rsi(closes, period=3)
    assert (result.iloc[1:] == 0.0).all()


def test_sma_matches_hand_computed_rolling_mean():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = indicators.sma(closes, period=3)
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(3.0)
    assert result.iloc[4] == pytest.approx(4.0)
    assert math.isnan(result.iloc[1])


def test_ema_matches_hand_computed_recursive_formula():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = indicators.ema(closes, period=3)
    # span=3 -> alpha=0.5; ema[i] = alpha*close[i] + (1-alpha)*ema[i-1]
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    for actual, want in zip(result, expected):
        assert actual == pytest.approx(want)


def test_macd_line_is_fast_ema_minus_slow_ema():
    closes = pd.Series([float(i) for i in range(1, 40)])
    result = indicators.macd(closes, fast=5, slow=10, signal=3)
    expected_line = indicators.ema(closes, 5) - indicators.ema(closes, 10)
    pd.testing.assert_series_equal(result["macd"], expected_line, check_names=False)
    expected_signal = expected_line.ewm(span=3, adjust=False).mean()
    pd.testing.assert_series_equal(result["macd_signal"], expected_signal, check_names=False)


def test_bbands_upper_and_lower_bracket_the_midline():
    closes = pd.Series([1.0, 3.0])
    result = indicators.bbands(closes, period=2, stddev=2)
    assert result["bb_mid"].iloc[1] == pytest.approx(2.0)
    band = math.sqrt(2) * 2  # sample std of [1, 3] (ddof=1) times stddev multiplier
    assert result["bb_upper"].iloc[1] == pytest.approx(2.0 + band)
    assert result["bb_lower"].iloc[1] == pytest.approx(2.0 - band)


def test_compute_indicators_reads_periods_from_cfg_and_returns_expected_columns():
    bars = pd.DataFrame({"close": [float(i) for i in range(1, 40)]})
    result = indicators.compute_indicators(bars, _cfg())

    assert list(result.columns) == ["rsi", "macd", "macd_signal", "sma", "ema", "bb_upper", "bb_mid", "bb_lower"]
    assert len(result) == len(bars)


def test_properties_raises_for_unknown_indicator_name():
    with pytest.raises(KeyError):
        indicators._properties(_cfg(), "vwap")
