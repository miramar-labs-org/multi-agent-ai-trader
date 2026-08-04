import pandas as pd
import pytest

from src.backtest import metrics
from src.backtest.simulator import SimResult, Trade


def _result(equity_values, trades=None, starting_cash=1000):
    index = pd.RangeIndex(len(equity_values))
    equity_curve = pd.Series(equity_values, index=index, name="equity")
    return SimResult(starting_cash=starting_cash, trades=trades or [], equity_curve=equity_curve)


def test_total_return_matches_hand_computed_percentage():
    result = _result([1000, 1100, 1200], starting_cash=1000)
    assert metrics.total_return(result) == pytest.approx(0.2)


def test_total_return_is_zero_for_an_empty_equity_curve():
    result = SimResult(starting_cash=1000, trades=[], equity_curve=pd.Series([], dtype=float))
    assert metrics.total_return(result) == pytest.approx(0.0)


def test_benchmark_relative_return_is_the_difference_in_total_return():
    result = _result([1000, 1200], starting_cash=1000)  # +20%
    benchmark = _result([1000, 1100], starting_cash=1000)  # +10%
    assert metrics.benchmark_relative_return(result, benchmark) == pytest.approx(0.10)


def test_max_drawdown_finds_the_worst_peak_to_trough_decline():
    result = _result([1000, 1200, 900, 1100], starting_cash=1000)
    # peak 1200 -> trough 900 = -25%
    assert metrics.max_drawdown(result) == pytest.approx(-0.25)


def test_max_drawdown_is_zero_for_a_monotonically_increasing_curve():
    result = _result([1000, 1100, 1200], starting_cash=1000)
    assert metrics.max_drawdown(result) == pytest.approx(0.0)


def test_win_rate_counts_positive_pnl_trades():
    trades = [
        Trade("t0", "t1", 100, 110, "signal", 1, pnl=10),
        Trade("t0", "t1", 100, 90, "signal", 1, pnl=-10),
        Trade("t0", "t1", 100, 105, "signal", 1, pnl=5),
    ]
    result = _result([1000, 1000], trades=trades)
    assert metrics.win_rate(result) == pytest.approx(2 / 3)


def test_win_rate_is_zero_with_no_trades():
    result = _result([1000, 1000])
    assert metrics.win_rate(result) == 0.0


def test_average_win_loss_separates_positive_and_nonpositive_pnl():
    trades = [
        Trade("t0", "t1", 100, 110, "signal", 1, pnl=10),
        Trade("t0", "t1", 100, 120, "signal", 1, pnl=20),
        Trade("t0", "t1", 100, 90, "signal", 1, pnl=-10),
    ]
    result = _result([1000, 1000], trades=trades)
    avg_win, avg_loss = metrics.average_win_loss(result)
    assert avg_win == pytest.approx(15.0)
    assert avg_loss == pytest.approx(-10.0)


def test_expectancy_is_average_pnl_across_all_trades():
    trades = [
        Trade("t0", "t1", 100, 110, "signal", 1, pnl=10),
        Trade("t0", "t1", 100, 90, "signal", 1, pnl=-10),
        Trade("t0", "t1", 100, 105, "signal", 1, pnl=5),
    ]
    result = _result([1000, 1000], trades=trades)
    assert metrics.expectancy(result) == pytest.approx(5 / 3)


def test_exposure_is_the_fraction_of_bars_with_an_open_position():
    index = pd.RangeIndex(5)
    trades = [Trade(entry_time=1, exit_time=2, entry_price=100, exit_price=110, exit_reason="signal", qty=1, pnl=10)]
    result = SimResult(starting_cash=1000, trades=trades, equity_curve=pd.Series([1000] * 5, index=index))
    assert metrics.exposure(result) == pytest.approx(2 / 5)


def test_trade_count_matches_number_of_trades():
    trades = [Trade("t0", "t1", 100, 110, "signal", 1, pnl=10)] * 3
    result = _result([1000, 1000], trades=trades)
    assert metrics.trade_count(result) == 3


def test_summarize_includes_benchmark_relative_return_only_when_a_benchmark_is_given():
    result = _result([1000, 1100], starting_cash=1000)
    benchmark = _result([1000, 1050], starting_cash=1000)

    without_benchmark = metrics.summarize(result)
    with_benchmark = metrics.summarize(result, benchmark)

    assert "benchmark_relative_return" not in without_benchmark
    assert with_benchmark["benchmark_relative_return"] == pytest.approx(0.05)
