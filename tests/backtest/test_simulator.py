import pandas as pd
import pytest
from omegaconf import OmegaConf

from src.backtest import strategies
from src.backtest.simulator import simulate


def _cfg(slippage_bps=0, slP=0.98, tpP=1.05, default_budget=1000):
    return OmegaConf.create(
        {
            "backtest": {"slippage_bps": slippage_bps},
            "trading": {"slP": slP, "tpP": tpP},
            "analyst": {"default_budget": default_budget},
        }
    )


def _bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_buy_and_hold_enters_at_the_next_bars_open_not_the_signal_bars_close():
    data = _bars(
        [
            {"open": 90, "high": 91, "low": 89, "close": 100},  # signal bar (is_first_bar)
            {"open": 110, "high": 111, "low": 109, "close": 110},  # fill bar
            {"open": 110, "high": 111, "low": 109, "close": 110},  # final bar, no bracket hit
        ]
    )

    result = simulate("MGN", data, strategies.buy_and_hold, _cfg())

    assert result.trades[0].entry_price == pytest.approx(110)


def test_take_profit_triggers_and_exits_at_the_take_profit_price():
    data = _bars(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100},  # signal bar
            {"open": 100, "high": 100, "low": 100, "close": 100},  # fill bar, entry_price=100
            {"open": 100, "high": 106, "low": 99, "close": 105},  # high 106 >= tp (105)
        ]
    )

    result = simulate("MGN", data, strategies.buy_and_hold, _cfg())

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].exit_price == pytest.approx(105.0)


def test_stop_loss_triggers_and_exits_at_the_stop_loss_price():
    data = _bars(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100},  # signal bar
            {"open": 100, "high": 100, "low": 100, "close": 100},  # fill bar, entry_price=100
            {"open": 100, "high": 101, "low": 97, "close": 98},  # low 97 <= sl (98)
        ]
    )

    result = simulate("MGN", data, strategies.buy_and_hold, _cfg())

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == pytest.approx(98.0)


def test_stop_loss_wins_when_both_thresholds_are_hit_in_the_same_bar():
    data = _bars(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100},  # signal bar
            {"open": 100, "high": 100, "low": 100, "close": 100},  # fill bar, entry_price=100
            {"open": 100, "high": 110, "low": 90, "close": 100},  # both tp (105) and sl (98) hit
        ]
    )

    result = simulate("MGN", data, strategies.buy_and_hold, _cfg())

    assert result.trades[0].exit_reason == "stop_loss"


def test_slippage_is_applied_against_the_trader_on_entry_and_exit():
    data = _bars(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 100, "low": 100, "close": 100},
            {"open": 100, "high": 100, "low": 100, "close": 100},  # end_of_data close
        ]
    )

    result = simulate("MGN", data, strategies.buy_and_hold, _cfg(slippage_bps=100))  # 1%

    assert result.trades[0].entry_price == pytest.approx(100 * 1.01)
    assert result.trades[0].exit_price == pytest.approx(100 * 0.99)


def test_no_trade_strategy_produces_no_trades_and_a_flat_equity_curve():
    data = _bars([{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(4)])

    result = simulate("MGN", data, strategies.no_trade, _cfg())

    assert result.trades == []
    assert (result.equity_curve == result.starting_cash).all()


def test_position_still_open_at_the_end_is_closed_at_the_final_close_price():
    data = _bars(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 100, "low": 100, "close": 100},  # entry, no bracket hit
            {"open": 100, "high": 101, "low": 99, "close": 103},  # no bracket hit, series ends
        ]
    )

    result = simulate("MGN", data, strategies.buy_and_hold, _cfg())

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end_of_data"
    assert result.trades[0].exit_price == pytest.approx(103.0)


def test_stock_symbol_sizes_to_whole_shares_crypto_sizes_fractionally():
    data = _bars(
        [
            {"open": 333, "high": 334, "low": 332, "close": 333},
            {"open": 333, "high": 334, "low": 332, "close": 333},
            {"open": 333, "high": 334, "low": 332, "close": 333},
        ]
    )
    cfg = _cfg(default_budget=1000)

    stock_result = simulate("MGN", data, strategies.buy_and_hold, cfg)
    crypto_result = simulate("BTC/USD", data, strategies.buy_and_hold, cfg)

    assert stock_result.trades[0].qty == 3.0
    assert crypto_result.trades[0].qty == pytest.approx(1000 / 333)


def test_explicit_sell_signal_closes_the_position_independent_of_the_bracket():
    data = _bars(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100},  # rsi below 30 -> BUY signal
            {"open": 100, "high": 100, "low": 100, "close": 100},  # fill bar
            {"open": 100, "high": 100, "low": 100, "close": 100},  # rsi above 70 -> SELL signal
            {"open": 102, "high": 102, "low": 102, "close": 102},  # SELL fills here
        ]
    )
    data["rsi"] = [20.0, 20.0, 80.0, 80.0]

    result = simulate("MGN", data, strategies.simple_rsi_rule, _cfg())

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "signal"
    assert result.trades[0].exit_price == pytest.approx(102.0)
