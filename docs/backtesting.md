# Backtesting harness

`src/backtest/` is a local CLI tool for offline, deterministic-baseline backtesting. It is
**not** a k8s workload — run it from a checkout with Alpaca paper credentials already in your
environment:

```sh
./.venv/bin/python -m src.backtest.main --symbols AAPL,NVDA,BTC/USD --lookback-days 90
```

It fetches historical hourly bars, computes indicators locally, runs each symbol through every
deterministic baseline strategy, prints a comparison table, and writes a JSON artifact to
`backtests/<timestamp>.json` (gitignored).

## What this is not

This validates rule-based baselines against historical price data. It does **not** replay the
live LLM's actual historical decisions — contemporaneous news, screener membership, and exact
model serving behavior from past days can't be reconstructed after the fact. True forward
evaluation of live decisions is ROADMAP P1.4, gated on P1.1's durable event store, and is a
separate, larger piece of work.

## Strategies (ROADMAP P1.6)

`src/backtest/strategies.py`: `buy_hold`, `rsi` (simple RSI rule), `macd` (simple MACD
crossover), `multi` (RSI + MACD combined), `no_trade`, `random` (seeded for reproducibility).

## Metrics (ROADMAP P1.6)

`src/backtest/metrics.py`, reported per symbol × strategy against that symbol's own `buy_hold`
run as the benchmark: total return, benchmark-relative return, max drawdown, annualized Sharpe,
win rate, average win/loss, expectancy, exposure, trade count.

## Assumptions (ROADMAP P1.5)

- **Data source**: Alpaca historical bars via `stock_data_client`/`crypto_data_client`
  (`src/common/alpaca_client.py`) — the free-tier IEX feed, the same limitation the live system
  already has. Hourly bars (`TimeFrame.Hour`), matching `cfg.indicators`' `interval: 1h` so
  indicator periods mean the same lookback window here as they do in a live TAAPI request.
- **Survivorship bias**: not addressed. The symbol universe is whatever `--symbols` names today
  (default `cfg.backtest.default_symbols`); there is no historical index-membership
  reconstruction.
- **Slippage**: a flat `cfg.backtest.slippage_bps` applied against the trader on every fill
  (entry and exit).
- **Spreads**: not modeled — only bar open/high/low/close, no bid/ask.
- **Fees**: zero. Alpaca is commission-free on both paper and live stock/crypto trading.
- **Execution timing**: every fill (entry or exit) happens at the *next* bar's open after a
  signal, never the signal bar's own close, to avoid lookahead bias. Bracket stop-loss/take-
  profit (`cfg.trading.slP`/`tpP`, mirroring `floor_broker/execution.py`'s
  `bracket_buy_with_SLTP` percentages) is checked against each open bar's high/low, including
  the entry bar itself; if both thresholds are hit in the same bar, stop-loss is assumed to have
  triggered first.
- **Missing data**: a symbol with no bars returned for the window is logged and skipped, not
  crashed on. Rows still `NaN` after indicator warm-up (e.g. the first `bbands` `period` bars)
  are dropped before simulation.
- **Position sizing**: fixed `cfg.analyst.default_budget` per trade (matching the live
  Analyst's fixed per-symbol budget), not a compounding fraction of account equity. Stocks size
  to whole shares; crypto sizes fractionally off notional.
- **Instruments**: stocks and crypto only. The harness has **no options support** — it simulates
  the underlying's own bars, so it does not model the `options_trading.enabled` path (MCP
  contract selection, premium/delta/DTE dynamics, the synthetic option SL/TP/DTE-force-close).
  A backtest run reflects the equity-bracket-order behavior regardless of the live options flag.

## Config

```yaml
backtest:
  default_symbols: ["NVDA", "AAPL", "MSFT", "BTC/USD"]
  lookback_days: 365
  slippage_bps: 5
```

## CLI flags

`--symbols`, `--start`/`--end` (ISO dates) or `--lookback-days`, `--strategies`, `--seed` (for
the `random` strategy). All default from `cfg.backtest` / `STRATEGIES` when omitted.
