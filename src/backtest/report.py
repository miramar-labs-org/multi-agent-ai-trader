import json
from dataclasses import asdict
from pathlib import Path

from src.backtest import metrics
from src.backtest.simulator import SimResult
from src.common.logging import get_logger

log = get_logger("BACKTEST")

OUTPUT_DIR = Path("backtests")

METRIC_COLUMNS = [
    "total_return",
    "benchmark_relative_return",
    "max_drawdown",
    "sharpe",
    "win_rate",
    "expectancy",
    "exposure",
    "trade_count",
]


def _row(symbol: str, strategy: str, summary: dict) -> str:
    cells = [f"{symbol:<10}", f"{strategy:<10}"]
    for key in METRIC_COLUMNS:
        value = summary.get(key)
        if value is None:
            cells.append(f"{'-':>10}")
        elif key in ("trade_count",):
            cells.append(f"{value:>10}")
        elif key in ("total_return", "benchmark_relative_return", "max_drawdown", "win_rate", "exposure"):
            cells.append(f"{value:>9.1%} ")
        else:
            cells.append(f"{value:>10.2f}")
    return "  ".join(cells)


def print_comparison_table(results: dict) -> None:
    """`results` is {symbol: {strategy_name: SimResult}}. Prints a symbol x strategy table of
    every metric in METRIC_COLUMNS, using each symbol's own buy_hold run as the benchmark."""
    header = ["Symbol".ljust(10), "Strategy".ljust(10)] + [f"{c:>10}" for c in METRIC_COLUMNS]
    print("  ".join(header))
    print("-" * (len(header) * 12))

    for symbol, by_strategy in results.items():
        benchmark = by_strategy.get("buy_hold")
        for strategy, result in by_strategy.items():
            summary = metrics.summarize(result, benchmark=benchmark)
            print(_row(symbol, strategy, summary))


def _sim_result_to_dict(result: SimResult) -> dict:
    return {
        "starting_cash": result.starting_cash,
        "trades": [asdict(t) for t in result.trades],
        "equity_curve": [
            {"timestamp": str(ts), "equity": equity} for ts, equity in result.equity_curve.items()
        ],
    }


def write_json_artifact(results: dict, timestamp: str) -> Path:
    """Writes the full result set (metrics + trade logs) to backtests/<timestamp>.json."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{timestamp}.json"

    payload = {}
    for symbol, by_strategy in results.items():
        benchmark = by_strategy.get("buy_hold")
        payload[symbol] = {
            strategy: {
                "metrics": metrics.summarize(result, benchmark=benchmark),
                "result": _sim_result_to_dict(result),
            }
            for strategy, result in by_strategy.items()
        }

    path.write_text(json.dumps(payload, indent=2, default=str))
    log(f"📄 wrote backtest report to {path}")
    return path
