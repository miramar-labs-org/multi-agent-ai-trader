---
name: configure-strategy
description: >
  Interactive wizard that asks about daily profit/risk goals, account size, position
  sizing, and stop-loss/take-profit preferences, then writes a `strategy:` block into
  config.yaml. Trigger on /configure-strategy, "run the strategy wizard", "reconfigure
  the trading strategy", or "let's set up a new trading goal".
---

# Configure trading strategy

Runs a short adaptive Q&A (using AskUserQuestion) to translate a stated trading goal
into concrete `config.yaml` values, then writes them under a `strategy:` block.

This wizard **only produces configuration values** — it never edits `src/`. Most of
what it asks about is already enforced by `src/floor_broker/execution.py` (daily
profit/loss halt, crypto synthetic stop-loss/take-profit, stock `trading.slP`/`tpP`,
`halt_behavior: block_new_buys`), but a couple of fields are still recorded intent only
(`position_sizing: risk_based`, `halt_behavior: flatten_positions`) — see each step
below for which is which, and say so explicitly in the Step 8 summary. Before writing
the file, `grep -rn "cfg.strategy" src/` to confirm this list hasn't drifted. If a
future answer needs a field nothing reads yet, wiring it up is a separate, larger
change and requires its own explicit go-ahead per this org's "No Implement Without
Approval" rule — never do it as a side effect of running this wizard.

## Step 1 — Frame the goal

If the user hasn't already stated one in this conversation, ask (single free-form
question, not multiple choice — goals vary too much to enumerate):
- What's the trading goal? (e.g. "modest daily profit, minimize risk, short-term" vs.
  "maximize long-term growth, higher risk tolerance")

## Step 2 — Reference numbers (AskUserQuestion, one round)

Ask together, since they're interdependent:
1. **Account equity** to design against (Alpaca paper default is $100,000 unless
   changed — check `docs/backtesting.md` or ask if unknown).
2. **Daily profit target**, in dollars.
3. **Daily loss limit** (the circuit-breaker floor) — offer symmetric to the profit
   target, a tighter fraction (e.g. half), and "something else."

## Step 3 — Halt behavior (AskUserQuestion)

When a daily limit (profit or loss) is hit, what happens to positions already open:
- Block new BUYs only (matches existing `kill_switch.py` semantics — SELL is never
  blocked) — default/recommended. **Enforced**: `execution.py::buy()` checks this on
  every BUY.
- Also flatten open positions. **Recorded intent only** — not enforced; choosing this
  still only blocks new BUYs in practice today.

## Step 4 — Position sizing style (AskUserQuestion)

- **Flat budget** (current behavior, **enforced** — it's just `analyst.default_budget`,
  nothing more to wire up) — every pick gets ~`analyst.default_budget` dollars
  regardless of stop distance; dollar risk floats with wherever the LLM sizes the
  position.
- **Risk-based** — pick a fixed dollar risk per trade (e.g. $100-200), and size each
  position so that a stop-loss hit loses approximately that amount:
  `budget = risk_per_trade_usd / (1 - slP)`. Ask for the risk-per-trade dollar amount
  if this is chosen. **Recorded intent only** — Analyst still sizes off
  `default_budget`; say so if the user picks this.

## Step 5 — Stop-loss / take-profit (AskUserQuestion)

- Current stock defaults are `slP: 0.98` / `tpP: 1.05` (2% stop / 5% target). Ask
  whether to keep, tighten (faster-hit, more trades/day), or widen. **Write the answer
  directly into the existing top-level `trading.slP`/`trading.tpP` keys** — not into the
  `strategy:` block — since that's what `src/dealer/graph.py` already sends Floor
  Broker on every BUY; there is no separate stock-SL/TP field under `strategy:`.
- **Crypto stop-loss/take-profit is enforced too**, but as a hand-rolled synthetic
  check (`execution.py::check_crypto_stops()`, polled from
  `src/floor_broker/main.py::poll_bracket_fills`), not a real Alpaca bracket order —
  Alpaca's bracket orders are equity-only. Ask for `crypto_slP`/`crypto_tpP` as
  fractions of fill price (defaults `0.98`/`1.03`) and write them under `strategy:`.

## Step 6 — Exposure caps (AskUserQuestion)

- Max concurrent positions / universe size (current default `max_universe_size: 10`).
- Whether to include stocks, crypto, or both (`trading.enable_stocks` /
  `trading.enable_crypto`).

## Step 7 — Synthesize and confirm before writing

If `config.default.yaml` doesn't exist yet at the repo root, create it now as
`cp config.yaml config.default.yaml` — an untouched snapshot of the config *before*
this run's changes are applied, so `/revert-strategy` always has a known-good baseline
to restore. Never overwrite an existing `config.default.yaml` — only the very first
`/configure-strategy` run ever creates it.

`account_equity_usd` (Step 2) is only a reference number for this session's own
risk-based-sizing math (Step 4) — it isn't written anywhere, since Floor Broker already
reads live equity straight from Alpaca (`trading_client.get_account()`) for the daily
halt check.

Stock SL/TP (Step 5) is written directly into the existing top-level `trading:` block,
not `strategy:`. Build the proposed `strategy:` YAML block from the rest of the
answers, e.g.:

```yaml
trading:
  slP: 0.98
  tpP: 1.05
  # ...(everything else in trading: unchanged)

strategy:
  daily_profit_target_usd: 1000   # enforced
  daily_loss_limit_usd: 500       # enforced
  halt_behavior: block_new_buys   # enforced; flatten_positions is recorded intent only
  crypto_slP: 0.98                # enforced (synthetic check, not a real bracket order)
  crypto_tpP: 1.03                # enforced (synthetic check, not a real bracket order)
  position_sizing: risk_based     # recorded intent only; flat_budget is what's enforced
  risk_per_trade_usd: 150         # recorded intent only -- see position_sizing note
  max_concurrent_positions: 10    # recorded intent only
```

Show the exact diff against the current `config.yaml` and get explicit confirmation
before writing — this file is loaded by live k8s services (even though it's a paper
account, per this org's "Ask Before Acting" rule, don't write config changes silently).
Preserve every existing top-level key in `config.yaml`; only touch `trading.slP`/
`trading.tpP` and the `strategy:` block itself.

## Step 8 — Log the outcome

Append a dated entry to `docs/strategy.md` (create it if missing) summarizing: the
goal as stated, the answers given, the resulting `trading.slP`/`tpP` and `strategy:`
values, and — critically — which fields are actually enforced
(`daily_profit_target_usd`, `daily_loss_limit_usd`, `halt_behavior: block_new_buys`,
`crypto_slP`/`crypto_tpP`, `trading.slP`/`tpP`) vs. still just recorded intent pending a
future implementation pass (`position_sizing: risk_based`,
`halt_behavior: flatten_positions`, `max_concurrent_positions`).
