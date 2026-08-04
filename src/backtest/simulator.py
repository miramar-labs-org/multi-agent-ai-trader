from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    entry_price: float
    exit_price: float
    exit_reason: str  # "signal", "take_profit", "stop_loss", "end_of_data"
    qty: float
    pnl: float


@dataclass
class SimResult:
    starting_cash: float
    trades: list = field(default_factory=list)
    equity_curve: pd.Series = None


def _qty_for(symbol: str, budget: float, price: float) -> float:
    if "/" in symbol:
        return budget / price
    return float(int(budget // price))


def simulate(symbol: str, data: pd.DataFrame, strategy_fn, cfg) -> SimResult:
    """Bar-by-bar single-symbol, single-strategy portfolio simulation over `data` (OHLCV +
    indicator columns, one row per bar, in chronological order).

    Simplifying assumptions, documented in docs/backtesting.md:
    - A strategy's decision on bar i fills at bar i+1's open (never the signal bar's own close),
      to avoid lookahead bias.
    - Every open position carries a bracket stop-loss/take-profit at cfg.trading.slP/tpP off its
      entry price (mirrors floor_broker/execution.py's bracket_buy_with_SLTP percentages, not its
      sub-cent tick-rounding/base-price-retry edge cases -- not needed for a bar-level backtest).
      A bracket is checked against each bar's high/low every bar it's open, including the entry
      bar itself (the bar's high/low occur over its full duration, at/after the open fill).
      If both stop-loss and take-profit are hit within the same bar, stop-loss is assumed to have
      triggered first (the conservative assumption).
    - A strategy's own SELL signal can also close a position early, independent of the bracket
      (mirrors a live Dealer SELL cancelling the bracket's open legs).
    - Every fill (entry or exit) applies cfg.backtest.slippage_bps against the trader -- price up
      on a BUY fill, down on a SELL/exit fill. Zero commission (Alpaca is commission-free).
    - Position size is a fixed cfg.analyst.default_budget per trade (matching the live Analyst's
      fixed per-symbol budget), not a compounding fraction of account equity. Stocks size to
      whole shares (int(budget // price), matching floor_broker/execution.py's get_qty); crypto
      sizes fractionally off notional.
    - A position still open when the data runs out is closed at the final bar's close price
      (reason="end_of_data") so every simulation's P&L is fully realized.
    """
    slippage = cfg.backtest.slippage_bps / 10_000.0
    budget = cfg.analyst.default_budget
    starting_cash = budget

    cash = starting_cash
    position = None  # {"entry_time", "entry_price", "qty", "stop_loss_px", "take_profit_px"}
    pending_action = None
    trades: list[Trade] = []
    equity_points = []

    for i in range(len(data)):
        row = data.iloc[i]
        ts = data.index[i]

        if pending_action == "BUY" and position is None:
            entry_price = row["open"] * (1 + slippage)
            qty = _qty_for(symbol, budget, entry_price)
            if qty > 0:
                cash -= qty * entry_price
                position = {
                    "entry_time": ts,
                    "entry_price": entry_price,
                    "qty": qty,
                    "stop_loss_px": entry_price * cfg.trading.slP,
                    "take_profit_px": entry_price * cfg.trading.tpP,
                }
        elif pending_action == "SELL" and position is not None:
            exit_price = row["open"] * (1 - slippage)
            cash += position["qty"] * exit_price
            trades.append(
                Trade(
                    entry_time=position["entry_time"],
                    exit_time=ts,
                    entry_price=position["entry_price"],
                    exit_price=exit_price,
                    exit_reason="signal",
                    qty=position["qty"],
                    pnl=(exit_price - position["entry_price"]) * position["qty"],
                )
            )
            position = None
        pending_action = None

        if position is not None:
            hit_sl = row["low"] <= position["stop_loss_px"]
            hit_tp = row["high"] >= position["take_profit_px"]
            if hit_sl or hit_tp:
                reason = "stop_loss" if hit_sl else "take_profit"
                exit_price = position["stop_loss_px"] if hit_sl else position["take_profit_px"]
                exit_price *= 1 - slippage
                cash += position["qty"] * exit_price
                trades.append(
                    Trade(
                        entry_time=position["entry_time"],
                        exit_time=ts,
                        entry_price=position["entry_price"],
                        exit_price=exit_price,
                        exit_reason=reason,
                        qty=position["qty"],
                        pnl=(exit_price - position["entry_price"]) * position["qty"],
                    )
                )
                position = None

        action = strategy_fn(row, position is not None, i == 0)
        if action in ("BUY", "SELL"):
            pending_action = action

        equity = cash + (position["qty"] * row["close"] if position is not None else 0.0)
        equity_points.append((ts, equity))

    if position is not None:
        last = data.iloc[-1]
        exit_price = last["close"] * (1 - slippage)
        cash += position["qty"] * exit_price
        trades.append(
            Trade(
                entry_time=position["entry_time"],
                exit_time=data.index[-1],
                entry_price=position["entry_price"],
                exit_price=exit_price,
                exit_reason="end_of_data",
                qty=position["qty"],
                pnl=(exit_price - position["entry_price"]) * position["qty"],
            )
        )
        equity_points[-1] = (equity_points[-1][0], cash)

    equity_curve = pd.Series(
        [v for _, v in equity_points], index=[t for t, _ in equity_points], name="equity"
    )
    return SimResult(starting_cash=starting_cash, trades=trades, equity_curve=equity_curve)
