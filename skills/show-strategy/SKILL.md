---
name: show-strategy
description: >
  Displays the current trading strategy config from config.yaml in plain,
  human-readable language -- read-only, no changes made. Trigger on
  /show-strategy, "show me the current strategy", "what's the trading
  config set to", "what strategy is active right now".
---

# Show current trading strategy config

Reads `config.yaml` and prints the current strategy settings as plain
English, not raw YAML. This is read-only -- no file is ever written, so
there's no confirmation step (unlike `/configure-strategy` or
`/revert-strategy`, which mutate `config.yaml` and follow "Ask Before
Acting").

## Step 1 — Read config.yaml

Read `config.yaml` and pull out:
- `trading.slP`, `trading.tpP` -- stock stop-loss/take-profit
- the whole `strategy:` block

## Step 2 — Translate to plain English

Render each field as a short, human sentence, not a key/value dump. Group
into "Active now" (enforced by `src/floor_broker/execution.py`) and
"Recorded, not yet enforced" (accepted by the wizard, saved to config, but
no code path reads it yet) -- this split matters because a user glancing at
raw YAML would otherwise assume every field is live. The current
enforced/recorded split (keep this in sync with
`skills/configure-strategy/SKILL.md` Step 8 if that ever changes):

**Active now:**
- `trading.slP` / `trading.tpP` -> stock stop-loss / take-profit, e.g.
  "Stock stop-loss: 2% below entry (slP 0.98). Stock take-profit: 5% above
  entry (tpP 1.05)."
- `strategy.daily_profit_target_usd` / `daily_loss_limit_usd` -> "Daily
  profit target: $1000 -- once today's account gain reaches this, no new
  BUYs go out until tomorrow (existing positions can still be sold)." /
  same phrasing for the loss limit. If either is `null`, say so explicitly:
  "No daily profit target set -- this halt is disabled."
- `strategy.halt_behavior` -> only report the `block_new_buys` half as
  active: "When a daily limit is hit, new BUYs are blocked (existing
  positions are left open, not flattened)."
- `strategy.crypto_slP` / `crypto_tpP` -> "Crypto stop-loss: 2% below fill
  price. Crypto take-profit: 3% above fill price." If either is `null`,
  say crypto stop/target tracking is disabled.
- `strategy.position_sizing: flat_budget` -> "Every pick gets the same
  fixed dollar budget (`analyst.default_budget`)."

**Recorded, not yet enforced (wizard accepts these, no code reads them yet):**
- `strategy.halt_behavior: flatten_positions` (only mention if this is the
  configured value -- note it's saved but the system still only blocks new
  buys, it does not close existing positions)
- `strategy.position_sizing: risk_based` / `risk_per_trade_usd` (only
  mention if `risk_based` is configured -- note sizing is still flat-budget
  in practice)
- `strategy.max_concurrent_positions` -- always mention as recorded-only;
  `analyst.max_universe_size` is the real cap on concurrent picks today

## Step 3 — Print the summary

Output the plain-English summary from Step 2 directly to the user. Don't
also dump the raw YAML unless asked -- the point of this skill is the
translation. If `config.yaml` and `config.default.yaml` currently differ
(a quick diff), mention that the strategy has been customized from
baseline; otherwise mention it's still at default.
