# Future ideas — Analyst & Dealer algorithm improvements

This is a brainstorm, not a commitment — unlike `docs/ROADMAP.md`, nothing here has been
scoped, prioritized, or agreed to. Grounded in the actual pipelines as of 2026-08-05
(`src/analyst/graph.py`, `src/dealer/graph.py`, `src/dealer/main.py`), not generic ML advice.
Cross-references `docs/ROADMAP.md` and `docs/strategy.md` where an idea overlaps an existing
tracked item.

---

## Quick wins (small change, real payoff)

### ~~Wire up `size_hint` — or remove it~~ — Done (2026-08-05)

Implemented: `call_floor_broker` (`src/dealer/graph.py`) now scales BUY budgets by
`state["budget"] * size_hint`; SELL is unaffected since `execution.sell()` ignores budget
entirely. `size_hint=0` is refused locally (`reason="size_hint_zero"`) rather than forwarded to a
request that would fail `ExecuteRequest`'s `budget > 0` validation. A small-but-positive scaled
budget is still forwarded — Floor Broker's existing minimum-notional/insufficient-qty skip paths
already handle that gracefully. See `docs/ROADMAP.md` P1.7 (now Done) and
`docs/architecture.md`'s Dealer section for full detail.

### ~~Cap Analyst's total deployed capital~~ — Done (2026-08-05)

Implemented: new `analyst.max_total_budget_usd` config field (default 50000). `validate_selection`
(`src/analyst/graph.py`) greedily walks picks in the LLM's own returned order, dropping any pick
that would push the running total over the cap (logged) while still considering — and potentially
keeping — smaller picks that come after a dropped one. See `docs/architecture.md`'s config
reference and Analyst section.

---

## Analyst

### Give the LLM a real edge signal, not just narrative indicators

Indicators currently reach the LLM as text for it to "reason about" — there's no quantitative
pre-scoring. `src/backtest/strategies.py` already implements RSI, MACD, and a deterministic
multi-indicator rule with measured historical performance (`src/backtest/metrics.py`). Running
those same deterministic rules over each candidate and passing the *rule's own vote*
(e.g. "RSI rule says BUY, MACD rule says HOLD") as an extra input feature would give the LLM a
cheap, already-validated baseline to agree or disagree with, rather than free-associating from
raw indicator values every time.

### Indicator budget is spent on volatility, not opportunity

`fetch_indicators` only pulls real TAAPI data for the top `indicator_fetch_limit` (15) candidates
ranked by `abs(change_pct)` — the rest reach the LLM with no indicator values at all. This
structurally biases picks toward whatever already moved most today (chasing), and can starve a
quieter setup that the news/track-record text otherwise supports. Consider ranking by a blend
(e.g. liquidity + relative volume + `abs(change_pct)`) instead of pure price-change magnitude, or
raising the TAAPI budget if the 15s rate limit allows it now that indicator use is feature-gated.

### Quantify the track record instead of narrating it

`fetch_track_record` formats the last `track_record_days` as qualitative text — there's no
computed win rate, average realized return, or per-symbol-repeat-pick outcome. That means the
LLM's only feedback loop from its own past decisions is whatever it infers from prose, not a
number. Computing actual realized/unrealized P&L per past pick (using the same fill data already
persisted to `floor_broker_events`) and feeding it as structured stats — "last 5 days: 60% of
BUY picks were profitable, avg +2.1%, worst -4.3%" — would be a much stronger self-correction
signal than narrative summary. Related to `docs/ROADMAP.md` P1.4 (forward evaluation), but doesn't
need P1.1's full event schema to get a first version working off what's already recorded.

### Add a confidence/conviction field to the selection schema

`PortfolioSelection`'s per-symbol output (`symbol, exchange, budget, indicators, rationale`) has
no confidence score. Without one, every pick is treated identically downstream — there's no way
to size by conviction, filter low-confidence picks, or flag "high conviction" symbols for tighter
monitoring. A 0–1 confidence field (distinct from `budget`, which is dollar sizing) would let
future work condition on it without a schema migration later.

### Crypto candidate discovery is static

Stocks get real discovery (`most-actives` + `movers` screener, top 20 each). Crypto is a fixed
3-symbol watchlist (`BTC/USD, ETH/USD, SOL/USD`) plus crypto movers — there's no equivalent
broad crypto screener sweep, so a coin outside that fixed list can never be picked no matter how
it's moving.

---

## Dealer

### Give the Dealer LLM more than bare indicators

The Dealer's per-symbol prompt is *only* that symbol's indicator text — no news, no current
position/unrealized-P&L, no account-wide state, nothing about *why* Analyst picked it in the
first place (the original rationale isn't carried forward from the portfolio ConfigMap into the
Dealer prompt). A Dealer LLM deciding SELL vs HOLD on a position that's down 8% has no way to
know it's down 8% unless the indicators happen to imply it. At minimum, passing current
unrealized P&L and the original Analyst rationale into the Dealer prompt would let it reason about
"has the original thesis broken" rather than re-deriving a verdict from indicators alone every
10 minutes.

### ~~Dealer has no memory across polls~~ — Partly addressed (2026-08-11)

Implemented: `strategy.dealer_memory.enabled` now adds recent same-symbol Dealer decisions and
Floor Broker outcomes to the Dealer LLM prompt. This gives each poll context about recent BUY
skips, fills, stop-outs, and prior reasoning for the same symbol. It is intentionally
same-symbol memory only; broader account state, current unrealized P&L, and the original Analyst
rationale are still open improvements under "Give the Dealer LLM more than bare indicators."

### Crypto only gets synthetic, polled stop-loss/take-profit

Stocks get native Alpaca bracket orders (SL/TP enforced at the exchange, ROADMAP P0.8/P0.9).
Crypto gets `crypto_slP`/`crypto_tpP` checked by Floor Broker's own poll loop
(`check_crypto_stops()`) — real protection, but with gap risk: a crypto position can move past
its synthetic stop between polls with no exchange-side order actually catching it, unlike a stock
bracket order which is live at the exchange the instant it's placed. Worth deciding whether that
gap is acceptable given crypto's 24/7 volatility, or whether it's worth investigating whether
Alpaca's crypto API supports native bracket/stop orders now (it didn't when this was originally
built, per `docs/strategy.md`).

### Daily loss control is a single blunt equity check

`strategy.daily_loss_limit_usd` blocks new BUYs once `equity - last_equity` crosses the bound —
but doesn't distinguish realized vs. unrealized loss, has no trade-count limit, and has no
per-asset-class exposure cap. As of 2026-08-11, there is a same-symbol stop-loss cooldown
(`strategy.symbol_stop_cooldown.enabled`) that prevents immediate re-entry after recent stop-outs,
but there is still no generalized rejection-streak cooldown or portfolio-level loss-streak
controller. This is `docs/ROADMAP.md` P1.8, already tracked as Partial — flagging here because
it's directly upstream of any algorithmic sizing change: a smarter sizing model is only as safe
as the blunt instrument backing it up.

---

## Cross-cutting / evaluation

### The core open question: does the LLM pipeline actually beat a simple rule?

`src/backtest/` already has deterministic baselines (buy-and-hold, RSI, MACD, multi-indicator,
random, no-trade) with real metrics (Sharpe, drawdown, win rate, expectancy) — but they're
benchmarked against historical data independently, not against what the live Analyst+Dealer LLM
pipeline actually decided on the same days. `docs/ROADMAP.md` P1.4 (forward evaluation/replay) is
exactly this comparison, gated on P1.1's full durable event store. Until that lands, there's no
answer to "is the two-LLM architecture earning its complexity" versus a cheap deterministic rule
— which arguably should be the highest-priority item on this whole list, since it would validate
or invalidate every other suggestion here.

### No model/prompt version tracking

`docs/ROADMAP.md` P1.2 (Planned) — every LLM call uses whatever `llm.base_url`/model/temperature
`config.yaml` currently has, with no per-decision record of which prompt template or model
version produced it. A prompt tweak or model swap today is invisible in the historical record —
if performance shifts, there's no way to attribute it to a specific change after the fact.

### Single one-shot call, no self-consistency check

Both Analyst and Dealer make one `ChatOpenAI` call per decision (`temperature: 0.1`,
`.with_structured_output(...)`) — no sampling multiple times and checking agreement, no
chain-of-thought requirement, no critique/retry pass. For Dealer specifically, given it
re-evaluates every 10 minutes, sampling 2-3 times and requiring majority agreement before acting
on a *change* from the prior HOLD state (not on every decision — that would multiply LLM cost by
2-3x for little benefit) could filter out single-sample noise-driven flips without much added
cost, since most cycles are HOLD anyway.
