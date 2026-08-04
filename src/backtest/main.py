import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.backtest.data import fetch_historical_bars
from src.backtest.indicators import compute_indicators
from src.backtest.report import print_comparison_table, write_json_artifact
from src.backtest.simulator import simulate
from src.backtest.strategies import STRATEGIES, get_strategy
from src.common.config import load_config
from src.common.logging import get_logger

log = get_logger("BACKTEST")


def parse_args(cfg, argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic-baseline backtesting harness")
    parser.add_argument(
        "--symbols",
        default=",".join(cfg.backtest.default_symbols),
        help="comma-separated list of symbols (stocks or SYM/USD crypto pairs)",
    )
    parser.add_argument("--start", help="ISO date, e.g. 2025-01-01 (default: --lookback-days before --end)")
    parser.add_argument("--end", help="ISO date, e.g. 2025-12-31 (default: now)")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=cfg.backtest.lookback_days,
        help="used when --start is not given",
    )
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGIES.keys()),
        help="comma-separated strategy names",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for the random strategy")
    return parser.parse_args(argv)


def resolve_window(args) -> tuple[datetime, datetime]:
    end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)
    start = datetime.fromisoformat(args.start) if args.start else end - timedelta(days=args.lookback_days)
    return start, end


def run(cfg, symbols: list[str], strategy_names: list[str], start: datetime, end: datetime, seed: int) -> dict:
    """Returns {symbol: {strategy_name: SimResult}}, skipping symbols with no historical data."""
    results = {}
    for symbol in symbols:
        bars = fetch_historical_bars(symbol, start, end)
        if bars.empty:
            continue

        indicators = compute_indicators(bars, cfg)
        data = pd.concat([bars, indicators], axis=1).dropna()
        if data.empty:
            log(f"⚠️  {symbol}: no bars left after indicator warm-up window, skipping")
            continue

        results[symbol] = {}
        for name in strategy_names:
            strategy_fn = get_strategy(name, seed=seed)
            results[symbol][name] = simulate(symbol, data, strategy_fn, cfg)

    return results


def main(argv=None) -> None:
    cfg = load_config()
    args = parse_args(cfg, argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    strategy_names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    start, end = resolve_window(args)

    log(f"running backtest for {symbols} over {strategy_names} from {start.date()} to {end.date()}")
    results = run(cfg, symbols, strategy_names, start, end, args.seed)

    if not results:
        log("⚠️  no symbols produced usable data, nothing to report")
        return

    print_comparison_table(results)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    write_json_artifact(results, timestamp)


if __name__ == "__main__":
    main()
