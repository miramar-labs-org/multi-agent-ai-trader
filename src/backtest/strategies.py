import random


def buy_and_hold(row, position_open: bool, is_first_bar: bool) -> str:
    if is_first_bar and not position_open:
        return "BUY"
    return "HOLD"


def simple_rsi_rule(row, position_open: bool, is_first_bar: bool) -> str:
    if row["rsi"] < 30 and not position_open:
        return "BUY"
    if row["rsi"] > 70 and position_open:
        return "SELL"
    return "HOLD"


def simple_macd_rule(row, position_open: bool, is_first_bar: bool) -> str:
    bullish = row["macd"] > row["macd_signal"]
    if bullish and not position_open:
        return "BUY"
    if not bullish and position_open:
        return "SELL"
    return "HOLD"


def multi_indicator_rule(row, position_open: bool, is_first_bar: bool) -> str:
    oversold = row["rsi"] < 30
    bullish = row["macd"] > row["macd_signal"]
    if oversold and bullish and not position_open:
        return "BUY"
    if (not oversold or not bullish) and position_open:
        return "SELL"
    return "HOLD"


def no_trade(row, position_open: bool, is_first_bar: bool) -> str:
    return "HOLD"


def make_random_action(seed: int = 0):
    """Returns a strategy fn with the common (row, position_open, is_first_bar) signature,
    closing over its own seeded Random instance so a backtest run is reproducible."""
    rng = random.Random(seed)

    def random_action(row, position_open: bool, is_first_bar: bool) -> str:
        choices = ["SELL", "HOLD"] if position_open else ["BUY", "HOLD"]
        return rng.choice(choices)

    return random_action


STRATEGIES = {
    "buy_hold": buy_and_hold,
    "rsi": simple_rsi_rule,
    "macd": simple_macd_rule,
    "multi": multi_indicator_rule,
    "no_trade": no_trade,
    "random": make_random_action,
}


def get_strategy(name: str, seed: int = 0):
    """Returns a strategy fn with the common (row, position_open, is_first_bar) signature for
    `name`. `random` needs a fresh seeded closure per call (for reproducibility); every other
    strategy is stateless and is returned as-is."""
    if name == "random":
        return make_random_action(seed)
    return STRATEGIES[name]
