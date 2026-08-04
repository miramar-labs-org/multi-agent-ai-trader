import math

from src.backtest.simulator import SimResult

BARS_PER_YEAR = 6.5 * 252  # ~6.5 trading hours/day, 252 trading days/year -- matches the
# simulator's hourly bar timeframe (src/backtest/data.py's BAR_TIMEFRAME)


def total_return(result: SimResult) -> float:
    final_equity = result.equity_curve.iloc[-1] if len(result.equity_curve) else result.starting_cash
    return (final_equity - result.starting_cash) / result.starting_cash


def benchmark_relative_return(result: SimResult, benchmark: SimResult) -> float:
    return total_return(result) - total_return(benchmark)


def max_drawdown(result: SimResult) -> float:
    if result.equity_curve.empty:
        return 0.0
    running_max = result.equity_curve.cummax()
    drawdown = (result.equity_curve - running_max) / running_max
    return drawdown.min()


def sharpe_ratio(result: SimResult) -> float:
    returns = result.equity_curve.pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return 0.0
    return (returns.mean() / returns.std()) * math.sqrt(BARS_PER_YEAR)


def win_rate(result: SimResult) -> float:
    if not result.trades:
        return 0.0
    wins = sum(1 for t in result.trades if t.pnl > 0)
    return wins / len(result.trades)


def average_win_loss(result: SimResult) -> tuple:
    wins = [t.pnl for t in result.trades if t.pnl > 0]
    losses = [t.pnl for t in result.trades if t.pnl <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return avg_win, avg_loss


def expectancy(result: SimResult) -> float:
    if not result.trades:
        return 0.0
    return sum(t.pnl for t in result.trades) / len(result.trades)


def exposure(result: SimResult) -> float:
    """Fraction of bars during which a position was open."""
    if result.equity_curve.empty:
        return 0.0
    index = result.equity_curve.index
    covered = 0
    for t in result.trades:
        start = index.get_loc(t.entry_time)
        end = index.get_loc(t.exit_time)
        covered += (end - start) + 1
    return min(covered, len(index)) / len(index)


def trade_count(result: SimResult) -> int:
    return len(result.trades)


def summarize(result: SimResult, benchmark: SimResult | None = None) -> dict:
    avg_win, avg_loss = average_win_loss(result)
    summary = {
        "total_return": total_return(result),
        "max_drawdown": max_drawdown(result),
        "sharpe": sharpe_ratio(result),
        "win_rate": win_rate(result),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy(result),
        "exposure": exposure(result),
        "trade_count": trade_count(result),
    }
    if benchmark is not None:
        summary["benchmark_relative_return"] = benchmark_relative_return(result, benchmark)
    return summary
