# Strategy discussion: short-term modest daily profit, minimized risk

Working notes from an ongoing design conversation. Not a spec yet — capturing the discussion as
it happens so we don't lose context between sessions.

## Goal

Configure the trader to target a **modest daily profit (up to ~$1000/day)** while
**minimizing risk**, optimizing for **short-term** outcomes rather than long-term/compounding
profit growth.

## Current mechanics (verified against code, 2026-08-04)

- `config.yaml`: `trading.slP: 0.98` / `tpP: 1.05` — every **stock** BUY gets a bracket order:
  2% stop-loss, 5% take-profit off the ask. Flat percentages — same for a $5 stock and a $500
  stock (`src/floor_broker/execution.py::bracket_buy_with_SLTP`).
- `analyst.default_budget: 5000`, `max_universe_size: 10` — Analyst LLM (`src/analyst/graph.py`)
  picks up to 10 symbols/day, ~$5k each unless it decides otherwise on its own discretion → up to
  $50k deployed, not a fixed risk budget.
- `trading.pollsecs: 600` — Dealer (`src/dealer/graph.py`) re-evaluates every 10 minutes,
  decides BUY/HOLD/SELL per symbol from technical indicators.
- Stock brackets are `TimeInForce.DAY` — they auto-cancel at close, so stock positions are
  already forced flat daily. This part already matches "short-term."
- Manual kill switch exists (`src/common/kill_switch.py`, `buy-kill-switch` ConfigMap) — blocks
  new BUYs when an operator flips it. Never SELL. Not tied to P&L automatically.

## Gaps identified vs. the stated goal

1. **Crypto has no stop-loss/take-profit at all.** `execution.py`'s crypto branch submits a
   plain `GTC` market order — no bracket, no forced exit. Risk is unbounded except by the Dealer
   LLM choosing to SELL on a later 10-min poll. Biggest hole for a "minimize risk" goal.
2. **No daily profit/loss circuit breaker.** The kill switch is manual only. Nothing today halts
   new BUYs automatically once the day is up $1000, and nothing halts them once the day is down
   $1000 either — trading continues until market close or a human intervenes.
3. **Position sizing isn't risk-based.** `default_budget: 5000` is a flat dollar amount, not
   "risk $X per trade." Actual dollar risk per trade = `budget × (1 - slP)`, which floats with
   whatever the LLM sizes the position at — no direct dial for "risk $150/trade, want ~7 trades
   to plausibly net $1000."

## Directions on the table

- **A — config-only tuning.** Tighten `slP`/`tpP` toward a smaller, faster-hit target; shrink
  `default_budget`/`max_universe_size` to cap total exposure. No code changes. Leaves crypto's
  open-ended risk and the missing daily auto-halt unfixed.
- **B — daily P&L auto-halt.** Extend the existing `kill_switch.py` mechanism so something
  (EOD-adjacent job or a new poller) flips `buy-kill-switch` on once realized daily P&L crosses
  +$1000 (profit target hit) or a max-loss floor (risk limit hit). Reuses infrastructure that
  already exists rather than inventing new plumbing.
- **C — risk-based position sizing.** Replace flat `default_budget` with "risk $X per trade"
  math (`budget = risk_amount / (1 - slP)`), and add bracket-style SL/TP to crypto too, so every
  position — stock or crypto — has a known, bounded downside.

## Decision: combination of A+B+C

User wants a combination of all three directions rather than picking one. Concrete
per-trade/per-day numbers depend on account equity and risk tolerance, which only the
user can set — deferred to a repeatable configuration tool rather than nailed down
one-off in this conversation.

## Decision: build a reusable configuration wizard

Rather than settle on one set of numbers in this conversation, the user wants a
repeatable wizard — the same kind of adaptive Q&A we've been doing here — that can be
re-run whenever the trading goal changes, producing a config file the live services
load.

Two design choices made (both via AskUserQuestion, both landed on the recommended
option):
- **Mechanism**: a Claude Code skill (`/configure-strategy`), not a standalone CLI
  script — adaptive branching per answer, at the cost of needing a Claude Code session
  to run it (vs. a fixed-flow script usable anywhere).
- **Output location**: a new `strategy:` block inside the existing `config.yaml`
  (loaded via `OmegaConf.load`, `src/common/config.py`), not a separate file — keeps
  everything the trading services read in one place.

**Built**: `skills/configure-strategy/SKILL.md` — git-tracked source of truth in the
repo's source tree, with `.claude/skills/configure-strategy` as a symlink to it (not a
copy — avoids the "No Code Duplication" risk of a copy drifting out of sync with what
Claude Code actually loads).
Walks through: goal framing → account equity + daily profit target + daily loss limit
→ halt behavior (block new BUYs vs. flatten) → position sizing style (flat budget vs.
risk-based, `budget = risk_per_trade_usd / (1 - slP)`) → stock SL/TP tightening →
crypto SL/TP (recorded as *intent only* — not yet enforced anywhere in code) → exposure
caps (`max_concurrent_positions`, stocks/crypto toggle) → synthesize proposed
`strategy:` YAML → show diff, get explicit confirm → write → log outcome here.

**Important scope boundary the skill enforces on itself**: producing the `strategy:`
config block is *not* the same as the trading services acting on it. As of this
writing, `src/analyst`, `src/dealer`, and `src/floor_broker` don't read any
`cfg.strategy.*` field — none of it is live yet. In particular:
- No daily P&L auto-halt exists (direction B) — the manual `buy-kill-switch` ConfigMap
  is still the only kill switch, and nothing ties it to realized P&L automatically.
- No risk-based position sizing exists (direction C) — `default_budget` is still a flat
  number Analyst may deviate from at its own discretion.
- Crypto still has no stop-loss/take-profit mechanism at all, regardless of what a
  `strategy:` block records.

Wiring the trading services to actually consume `strategy:` fields is separate,
larger, live-behavior-changing work and needs its own explicit go-ahead before any
code in `src/` changes — the wizard skill is instructed never to do this as a side
effect of writing config.

## Decision: add a revert path

Before running the wizard for the first time, user asked for a way back to the
default config too.

- Snapshotted the untouched `config.yaml` as `config.default.yaml` (repo root,
  git-tracked) — taken now, before `/configure-strategy` has ever run, so it's a clean
  baseline.
- Built `skills/revert-strategy/SKILL.md` (symlinked into `.claude/skills/` the same
  way as `configure-strategy`) — `/revert-strategy` diffs `config.yaml` against
  `config.default.yaml`, shows the diff, confirms, then restores verbatim
  (`cp config.default.yaml config.yaml`). No selective/partial revert — always an
  exact return to baseline.
- `configure-strategy`'s Step 7 updated to create `config.default.yaml` itself on its
  own first run if the file is ever missing (e.g. in a fresh clone), so the revert
  path is self-sufficient rather than depending on this session having run it manually.

Current repo layout:
```
config.yaml            ← live config, loaded by src/common/config.py
config.default.yaml    ← untouched baseline snapshot, for /revert-strategy
skills/configure-strategy/SKILL.md   ← source of truth
skills/revert-strategy/SKILL.md      ← source of truth
.claude/skills/configure-strategy    → symlink
.claude/skills/revert-strategy       → symlink
```

## Gotcha: config.yaml is baked into images, not mounted at runtime

`Dockerfile.{analyst,dealer,floor-broker,eod-report}` all `COPY config.yaml .` at
build time — unlike the `portfolio`/`buy-kill-switch` ConfigMaps
(`src/common/portfolio_state.py`, `src/common/kill_switch.py`), which are read fresh
from the k8s API on every call, nothing mounts `config.yaml` at runtime. So:

1. `/configure-strategy` and `/revert-strategy` only ever change the local checkout —
   no effect on live pods by themselves.
2. Reaching the live trader requires the normal release flow: `git tag vX.Y.Z && git
   push origin vX.Y.Z` → `build-push.yaml` → `gh release create` → `gh workflow run
   deploy.yaml`.
3. Even after that redeploy, nothing changes in *behavior* yet — no code in
   `src/analyst`/`src/dealer`/`src/floor_broker` reads `cfg.strategy.*` (see the scope
   boundary noted above). Shipping the config via a redeploy and wiring the services
   to act on it are two separate, both-still-pending steps.

## Decision: enforce the `strategy:` config, minimal scope (2026-08-04)

Greenlit the implementation pass. Scoped via Plan Mode; the first plan drafted was
ROADMAP-aligned (a new `risk.py` module, exposure-notional caps, an account-wide lock,
ConfigMap-backed crypto-stop persistence) and was **explicitly rejected** — the ask was
"a configurable strategy (configured by a skill), nothing more complex," with
instructions to adjust `docs/ROADMAP.md` afterward rather than build to its larger
spec. Rebuilt the plan around the minimum needed to make the fields
`/configure-strategy` already asks about actually take effect, and built exactly that:

- **`trading.slP`/`trading.tpP`** (stock stop-loss/take-profit) — already existed,
  unchanged; the wizard was fixed to write here directly instead of a shadow
  `strategy.stock_slP`/`stock_tpP` copy.
- **`strategy.daily_profit_target_usd`/`daily_loss_limit_usd`/`halt_behavior`** — new.
  `src/floor_broker/execution.py::buy()` reads today's Alpaca account
  `equity - last_equity` on every BUY (no custom day-boundary bookkeeping needed —
  Alpaca's `TradeAccount` already tracks this) and returns
  `status="skipped", reason="daily_profit_target_reached"` or
  `"daily_loss_limit_reached"` once either bound is crossed. Only
  `halt_behavior: block_new_buys` is implemented; SELL is never blocked, same as the
  existing manual kill switch. `flatten_positions` is recorded intent only.
- **`strategy.crypto_slP`/`crypto_tpP`** — new. Alpaca's bracket orders are
  equity-only (`alpaca.trading.enums.OrderClass` docstring: "Crypto trading: simple (or
  \"\")"), so this is a hand-rolled synthetic stop, not a config tweak to the existing
  bracket logic: on a crypto BUY fill, `check_pending_fills()` computes
  `sl_price`/`tp_price` off the real fill price and stores them in a new in-memory
  `_crypto_stops` dict (mirrors the existing `_tracked_brackets`/`_pending_fills`
  pattern — same restart-drops-tracking tradeoff, deliberately not given ConfigMap
  persistence). `check_crypto_stops()` is polled from the existing
  `poll_bracket_fills` daemon thread in `src/floor_broker/main.py` (no new thread) and
  calls `execution.sell(symbol, reason="stop_loss"|"take_profit")` when the current bid
  crosses either level.
- **`strategy.position_sizing`/`risk_per_trade_usd`/`max_concurrent_positions`** —
  still recorded intent only, not enforced this pass (risk-based sizing and exposure
  caps are the larger P0.4/P1.8 scope that was explicitly deferred).

`config.default.yaml` (the `/revert-strategy` baseline) was updated to include the new
`strategy:` block with sane defaults, since it's now load-bearing —
`execution.py` reads `cfg.strategy.*` unconditionally, so "revert" now means "return to
sane defaults," not "remove all risk controls."

Tests: `tests/floor_broker/test_execution.py` (daily halt pass/profit-skip/loss-skip/
SELL-unaffected, crypto stop set-on-fill/trigger-sell/clear-on-sell/transient-price-
fetch-error-keeps-tracking) and `tests/floor_broker/test_floor_broker_main.py`
(`poll_bracket_fills` now also reports a triggered crypto stop, and survives an
exception from `check_crypto_stops()`) — full file and suite green.

`docs/ROADMAP.md` P0.4/P0.6/P1.8 updated to "Partial"/note-only rather than "Done" —
each now explains what was actually built vs. what of the original larger spec
(exposure-notional caps, an account-wide lock, trade-count/failed-submission-rate/
per-asset-class exposure limits) remains "Planned" if that fuller scope is ever wanted.

## Open question

Whether/when to run `/configure-strategy` again now that enforcement actually exists,
to replace today's defaults (`$1000` profit target / `$500` loss limit / `0.98`-`1.03`
crypto SL/TP) with deliberately chosen numbers rather than the placeholders currently
in `config.yaml`.

---
*This file is a live scratchpad for the strategy conversation, updated as the discussion
progresses. Reflected in `docs/ROADMAP.md` (P0.4/P0.6/P1.8) and `docs/architecture.md`
(Floor Broker section — daily halt and crypto synthetic stop-loss/take-profit) as of
2026-08-04.*
