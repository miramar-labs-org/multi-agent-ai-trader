# Options trading + Alpaca MCP server — hackathon design

Date: 2026-08-25
Context: [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon) (28 Aug – 4 Sep 2026, $6,000 prize pool). Core requirements: autonomous agents on Alpaca's Trading API (already true — this is a live multi-agent system); must utilize Alpaca's MCP server or CLI (new); all strategies must incorporate options trading (new); competition account starting balance $100,000 (a dedicated paper account has already been created, keys `ALPACA_PAPER_API_KEY2`/`ALPACA_PAPER_API_SECRET2`); a one-page write-up covering AI logic, risk gates, and Alpaca infrastructure.

This spec has two ordered parts:

- **Part 1 — Feature-gate naming rename.** Mechanical, no behavior change. Done first so Part 2's new `options_trading.enabled` flag lands in an already-consistent config, and nothing has to be renamed twice.
- **Part 2 — Options trading feature.** The actual hackathon build: MCP-driven contract selection in Dealer, options execution + exit management in Floor Broker, against the dedicated $100k account, plus the hackathon deployment config and the one-page write-up.

---

## Part 1 — Feature-gate naming rename

### Problem

Feature gates in this repo use two inconsistent shapes: newer ones are `<block>.enabled` (e.g. `ohlcv_enrichment.enabled`, `eod_flatten.enabled`, `power_schedule.enabled`, `earnings_blackout.enabled`, `macro_blackout.enabled`, `analyst.candidate_mix.enabled`), while older ones are a flat `enable_<name>` boolean directly under a shared block (e.g. `trading.enable_stocks`). The new `options_trading.enabled` flag this project adds will follow the `.enabled` convention — this part makes every existing gate match it first.

### Rename map

Only the on/off flag itself moves into a one-key sub-block; sibling settings keep their existing names and location.

| Old key | New key | File(s) |
|---|---|---|
| `trading.enable_stocks` | `trading.stocks.enabled` | config.yaml, config.default.yaml |
| `trading.enable_crypto` | `trading.crypto.enabled` | config.yaml, config.default.yaml |
| `strategy.enable_win_rate_throttle` | `strategy.win_rate_throttle.enabled` | config.yaml |
| `strategy.enable_symbol_stop_cooldown` | `strategy.symbol_stop_cooldown.enabled` | config.yaml, config.default.yaml |
| `strategy.enable_dealer_memory` | `strategy.dealer_memory.enabled` | config.yaml, config.default.yaml |
| `analyst.enable_midday_run` | `analyst.midday_run.enabled` | config.yaml |
| `analyst.enable_news` | `analyst.news.enabled` | config.yaml |
| `analyst.enable_indicators` | `analyst.indicators.enabled` | config.yaml |
| `analyst.enable_track_record` | `analyst.track_record.enabled` | config.yaml |
| `analyst.enable_position_pnl` | `analyst.position_pnl.enabled` | config.yaml |

### Code touch points (from repo grep, 2026-08-25)

- `src/analyst/main.py:20` — `cfg.analyst.enable_midday_run` → `cfg.analyst.midday_run.enabled`
- `src/dealer/main.py:40,42` — `cfg.trading.enable_crypto`/`enable_stocks` → `cfg.trading.crypto.enabled`/`cfg.trading.stocks.enabled`
- `src/common/portfolio_state.py:49,51` (+ docstring at 35-36) — same two keys
- `src/dealer/graph.py:178,205,231` — `cfg.strategy.get("enable_dealer_memory", ...)` etc. → `cfg.strategy.get("dealer_memory", {}).get("enabled", ...)` (or `OmegaConf`-safe equivalent — match existing `.get()` fail-open style)
- `src/analyst/graph.py:98,151,156,169,179,194,234,284,430,443` — `cfg.trading.enable_crypto`/`enable_stocks`, `cfg.analyst.enable_news`/`enable_indicators`/`enable_track_record`/`enable_position_pnl`

### Test touch points

- `tests/common/test_portfolio_state.py` — `_cfg()` helper builds `{"trading": {"enable_stocks": ..., "enable_crypto": ...}}`
- `tests/analyst/test_analyst_main.py` — `_cfg(enable_midday_run=...)`
- `tests/analyst/test_graph.py` — multiple `_cfg`/`_mix_cfg`/`_indicator_cfg`/`_track_record_cfg`/`_pnl_cfg` helpers building dicts with the old flat keys
- `tests/dealer/test_call_floor_broker.py` — `cfg.strategy.enable_win_rate_throttle` etc. (attribute assignment and dict construction)
- `tests/dealer/test_dealer_graph.py` — `"strategy": {"enable_dealer_memory": ...}`
- `tests/dealer/test_main.py` — `_cfg(enable_stocks=..., enable_crypto=...)`

Every existing test's *behavior* is unchanged — only the config shape each test constructs needs updating to match the new nested keys.

### Docs/prose touch points

`docs/analysis.md`, `docs/strategy.md`, `docs/future-ideas.md`, `docs/summary-technical.md`, `docs/ROADMAP.md`, `docs/architecture.md`, `skills/configure-strategy/SKILL.md` all reference the old flag names in prose. Update mentions of the renamed keys to the new dotted form; leave everything else in those docs untouched (no unrelated cleanup).

### Acceptance criteria

- `grep -rn "enable_stocks\|enable_crypto\|enable_midday_run\|enable_news\|enable_indicators\|enable_track_record\|enable_position_pnl\|enable_win_rate_throttle\|enable_symbol_stop_cooldown\|enable_dealer_memory"` across the repo (excluding `.venv`, `.git`) returns nothing.
- `.venv/bin/python -m pytest` passes.
- `ruff check .` passes.
- No behavior change: with equivalent config values (old flat key `true`/`false` → new nested key `true`/`false`), every gated code path behaves identically to before the rename.

---

## Part 2 — Options trading feature

### Strategy shape

For each Analyst-picked **stock** symbol (crypto has no listed options — same `exchange == "stocks"` gate `ohlcv_enrichment` already uses), when the Dealer's existing LLM signal is BUY or SELL with confidence at/above `strategy.min_confidence`:

- BUY → look to open a long **call**
- SELL → look to open a long **put**
- HOLD → no options action

Single-leg long options only — no spreads, no naked selling. Max loss is capped by construction (the premium paid), so this needs no bracket/OCO order support (which doesn't exist for options on Alpaca) beyond a software-managed exit, and stays well within the account's existing Level 3 options approval. The account (`ALPACA_PAPER_API_KEY2`/`SECRET2`) is confirmed already approved.

### Architecture

```
Dealer (existing LLM signal: BUY/SELL/HOLD + confidence)
   │  when options_trading.enabled and action != HOLD and confidence >= strategy.min_confidence
   ▼
select_option_contract (new graph node)
   │  agentic LLM step, tool-calling via Alpaca MCP server (options-data toolset:
   │  chain search, quotes, Greeks) — connected over stdio using ALPACA_PAPER_API_KEY2/SECRET2,
   │  scoped to this node only
   ▼
structured OptionContractPick (contract_symbol, strike, expiration, right, delta, premium, reasoning)
   │
   ▼
deterministic risk gates (new, in call_floor_broker, before any HTTP call — same style as
existing macro_blackout / symbol_stop_cooldown / win_rate_throttle / min_confidence gates):
   - DTE within [options_trading.dte_min, options_trading.dte_max]
   - delta within [options_trading.target_delta_min, options_trading.target_delta_max]
   - open interest >= options_trading.min_open_interest, volume >= options_trading.min_volume
   - qty = floor(strategy.risk_per_trade_usd / (premium * 100)); reject if qty < 1
   │  (any failure -> status="skipped", reason="option_contract_rejected", same
   │  Slack-notify + db.record_floor_broker_event pattern as today's skips)
   ▼
Floor Broker: POST /execute-option {contract_symbol, side, qty}
   - execution.buy_option() — market order via alpaca-py TradingClient built with account-2 keys
   - exit management: new _option_stops dict (mirrors _crypto_stops / check_crypto_stops()),
     polled alongside the existing fill-watcher loop:
       - close at options_trading.options_tpP / options_slP (fraction of entry premium)
       - hard force-close once DTE <= options_trading.dte_force_close, regardless of P&L
   ▼
Alpaca paper account #2 ($100k)
```

Dealer already talks to Alpaca directly for market data (`src/common/bars.py`) without going through Floor Broker — the MCP-based options-data lookup in `select_option_contract` follows that same existing split (Dealer reads market data, Floor Broker is the only component that submits orders). No new k8s Deployments/Services — the MCP server runs as a subprocess inside the existing Dealer container.

### Config (new `options_trading:` block)

Added to `config.yaml` and `config.default.yaml` (disabled in both, so `/revert-strategy` also reverts this):

```yaml
options_trading:
  enabled: false            # top-level feature gate
  dte_min: 14
  dte_max: 45
  dte_force_close: 3        # force-close regardless of P&L once DTE drops to/below this
  target_delta_min: 0.30
  target_delta_max: 0.60
  min_open_interest: 100
  min_volume: 10
  options_slP: 0.50         # synthetic stop-loss, fraction of entry premium
  options_tpP: 1.75         # synthetic take-profit, fraction of entry premium
```

Exact numeric defaults (DTE window, delta window, OI/volume floors, sl/tp fractions) are starting points for implementation — reasonable, but not load-bearing to this spec; tune during testing against real chain liquidity.

### Credentials

- `ALPACA_PAPER_API_KEY2` / `ALPACA_PAPER_API_SECRET2` — already created by the user (dedicated $100k paper account, Level 3 options approval confirmed).
- New `src/common/alpaca_client.py`-style second client (or a parameterized constructor) for account 2, used only by: the MCP subprocess launch (Dealer) and the options execution path (Floor Broker). The existing account-1 client is untouched.
- `k8s/secrets.example.yaml` and `update-secrets.sh`: add `ALPACA_PAPER_API_KEY2`/`ALPACA_PAPER_API_SECRET2` as new known keys (mirrors how `ALPACA_PAPER_API_KEY`/`SECRET` are already documented and mirrored to GitHub Actions secrets).

### Database

New `options_trades` table (separate from `dealer_decisions`/`floor_broker_events` — the shape is genuinely different, same reasoning as `position_opens` already being its own table):

```sql
CREATE TABLE IF NOT EXISTS options_trades (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,              -- underlying, e.g. "AAPL"
    contract_symbol TEXT NOT NULL,     -- OCC symbol
    right TEXT NOT NULL,               -- "call" | "put"
    strike NUMERIC NOT NULL,
    expiration DATE NOT NULL,
    delta NUMERIC,
    entry_premium NUMERIC NOT NULL,
    qty INTEGER NOT NULL,
    reasoning TEXT,
    cycle_id TEXT,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    exit_reason TEXT,                  -- "take_profit" | "stop_loss" | "dte_force_close" | "dealer_signal"
    exit_premium NUMERIC
);
```

### Hackathon deployment config

For the competition window, `config.yaml` (the live config every production pod fetches from `main`) is set to run options-only:

```yaml
trading:
  stocks:
    enabled: false
  crypto:
    enabled: false
options_trading:
  enabled: true
```

This intentionally pauses the existing live account-1 equity/crypto loop for the hackathon window (28 Aug – 4 Sep) — confirmed with the user. Reverting after the hackathon is a config-only change (flip the three flags back), no redeploy needed, same instant-rollback story as every other feature gate in this repo.

### One-page write-up

New file: `docs/hackathon-writeup.md`. Drafted once the feature has run and produced a few real paper trades to point to (not written blind before the code exists). Covers, in roughly one page:

- **AI logic** — Dealer's existing indicator-driven BUY/SELL/HOLD + confidence signal, then the agentic MCP tool-calling step that selects a specific contract (chain search, quotes, Greeks) within stated constraints.
- **Risk gates** — the full deterministic chain: existing macro-blackout / symbol-stop-cooldown / win-rate-throttle / min-confidence gates (already gate the underlying signal), plus the new options-specific gates (DTE bounds, delta bounds, liquidity floor, premium-capped position sizing) and the exit rules (premium-based stop/take-profit, DTE force-close).
- **Alpaca infrastructure** — Trading API (account 1 for the base system, account 2 dedicated $100k for the competition), the official `alpaca-mcp-server` for options chain/quote/Greeks lookup, paper-only throughout.

### Testing

- Unit tests for the new deterministic risk gates (`select_option_contract` validation, qty sizing) — same style as existing `tests/dealer/test_call_floor_broker.py` gate tests.
- Unit tests for `execution.buy_option()`/exit management — mirrors `tests/floor_broker` coverage of `_crypto_stops`/`check_crypto_stops()`.
- MCP subprocess interaction is the one piece that's hard to unit test in isolation; plan for a thin adapter function that wraps the MCP client calls so it can be mocked, matching how `alpaca_client.py`'s functions are already mocked in existing tests.

### Rollout

1. Land Part 1 (rename) on `main` first — pure refactor, verified by full test suite, no behavior change, safe to ship immediately regardless of hackathon timing.
2. Build Part 2 with `options_trading.enabled: false` in both config files — merges to `main` without affecting live production behavior.
3. Once tested (including at least one live paper cycle against account 2), flip the hackathon deployment config (§ "Hackathon deployment config" above) for the competition window.
4. Draft `docs/hackathon-writeup.md` once there's real trade history to describe.
