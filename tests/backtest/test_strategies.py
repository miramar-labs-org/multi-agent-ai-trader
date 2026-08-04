from src.backtest import strategies


def test_buy_and_hold_buys_only_on_the_first_bar():
    assert strategies.buy_and_hold({}, position_open=False, is_first_bar=True) == "BUY"
    assert strategies.buy_and_hold({}, position_open=False, is_first_bar=False) == "HOLD"
    assert strategies.buy_and_hold({}, position_open=True, is_first_bar=True) == "HOLD"


def test_simple_rsi_rule_buys_oversold_and_sells_overbought():
    assert strategies.simple_rsi_rule({"rsi": 29.9}, position_open=False, is_first_bar=False) == "BUY"
    assert strategies.simple_rsi_rule({"rsi": 70.1}, position_open=True, is_first_bar=False) == "SELL"


def test_simple_rsi_rule_boundary_values_are_a_hold():
    assert strategies.simple_rsi_rule({"rsi": 30.0}, position_open=False, is_first_bar=False) == "HOLD"
    assert strategies.simple_rsi_rule({"rsi": 70.0}, position_open=True, is_first_bar=False) == "HOLD"


def test_simple_rsi_rule_never_buys_while_already_holding():
    assert strategies.simple_rsi_rule({"rsi": 10.0}, position_open=True, is_first_bar=False) == "HOLD"


def test_simple_macd_rule_buys_on_bullish_and_sells_on_bearish():
    bullish = {"macd": 1.0, "macd_signal": 0.5}
    bearish = {"macd": 0.5, "macd_signal": 1.0}
    assert strategies.simple_macd_rule(bullish, position_open=False, is_first_bar=False) == "BUY"
    assert strategies.simple_macd_rule(bearish, position_open=True, is_first_bar=False) == "SELL"
    assert strategies.simple_macd_rule(bullish, position_open=True, is_first_bar=False) == "HOLD"


def test_multi_indicator_rule_requires_both_signals_to_buy():
    both = {"rsi": 20.0, "macd": 1.0, "macd_signal": 0.5}
    only_rsi = {"rsi": 20.0, "macd": 0.5, "macd_signal": 1.0}
    assert strategies.multi_indicator_rule(both, position_open=False, is_first_bar=False) == "BUY"
    assert strategies.multi_indicator_rule(only_rsi, position_open=False, is_first_bar=False) == "HOLD"


def test_multi_indicator_rule_sells_when_either_signal_turns():
    row = {"rsi": 50.0, "macd": 0.5, "macd_signal": 1.0}
    assert strategies.multi_indicator_rule(row, position_open=True, is_first_bar=False) == "SELL"


def test_no_trade_is_always_hold():
    assert strategies.no_trade({"rsi": 1.0}, position_open=False, is_first_bar=True) == "HOLD"
    assert strategies.no_trade({"rsi": 99.0}, position_open=True, is_first_bar=False) == "HOLD"


def test_random_action_only_offers_legal_moves():
    strat = strategies.get_strategy("random", seed=42)

    for _ in range(20):
        assert strat({}, position_open=False, is_first_bar=False) in ("BUY", "HOLD")
    for _ in range(20):
        assert strat({}, position_open=True, is_first_bar=False) in ("SELL", "HOLD")


def test_random_action_is_reproducible_given_the_same_seed():
    strat_a = strategies.get_strategy("random", seed=42)
    strat_b = strategies.get_strategy("random", seed=42)
    seq_a = [strat_a({}, position_open=False, is_first_bar=False) for _ in range(10)]
    seq_b = [strat_b({}, position_open=False, is_first_bar=False) for _ in range(10)]
    assert seq_a == seq_b


def test_get_strategy_returns_the_named_stateless_strategy():
    assert strategies.get_strategy("rsi") is strategies.simple_rsi_rule
