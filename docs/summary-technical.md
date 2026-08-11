# How this codebase works (technical summary, for programmers)

Architecture-lite: how the system is put together and how the important parts actually work in
code, without [architecture.md](architecture.md)'s full edge-case-by-edge-case detail. Start
here; go to `architecture.md` when you need the exact retry counts, error codes, or config
field reference. For a non-technical framing, see [summary.md](summary.md).

## The shape of it

Four independent Python services, each its own Docker image and k8s workload on the DGX Spark
k3s cluster, coordinated with no queue and no database in the handoff path (a shared Postgres
instance exists for durable decision/event logging — see below — but isn't part of how the
agents talk to each other):

```
08:55 ET CronJob           continuous Deployment           Deployment+Service
┌───────────┐  writes   ┌───────────────┐  ConfigMap  ┌────────────────┐
│  Analyst  │ ────────► │ "portfolio"   │ ◄────read── │     Dealer     │
│ screener  │           │  ConfigMap    │   every     │ indicators→LLM │
│ +news→LLM │           └───────────────┘   600s      └───────┬────────┘
└───────────┘                                                 │ HTTP POST /execute
                                                                ▼
                                                     ┌────────────────────┐
                                                     │    Floor Broker     │
                                                     │ Alpaca order place  │
                                                     └──────────┬─────────┘
                                                                ▼
                                                        Alpaca paper account

13:30-16:30 ET :30 checks (independent of the above)
┌─────────────┐
│  EOD Report  │  reads Alpaca directly, posts once at close+30min
└─────────────┘
```

Analyst never talks to Floor Broker. The only durable cross-service state is the `portfolio`
ConfigMap; the only synchronous network hop is Dealer → Floor Broker.

## Repo layout

```
src/
├── analyst/        {graph.py, main.py, schema.py, sources.py}   CronJob
├── dealer/          {graph.py, main.py, schema.py}                Deployment
├── floor_broker/     {app.py, execution.py, main.py}                Deployment + Service
├── eod_report/         main.py                                        CronJob
├── backtest/            offline CLI, not a k8s workload — see backtesting.md
└── common/                config.py, alpaca_client.py, portfolio_state.py,
                           kill_switch.py, logging.py, slack.py, langsmith.py, eod.py,
                           market_calendar.py, indicators.py
k8s/            manifests deploy.yaml applies (2 CronJobs, 2 Deployments, 1 Service, RBAC)
config.yaml      single OmegaConf source of truth, loaded via common/config.py::load_config()
```

Every `main.py` is its container's entrypoint (`python -m src.<service>.main`). Floor Broker's
`app.py` is the only FastAPI/HTTP server in the repo — everything else is a script or CronJob.

## Analyst — picks the watchlist

Entrypoint: `python -m src.analyst.main`, `batch/v1 CronJob` `55 8 * * *` (`timeZone:
America/New_York`), `concurrencyPolicy: Forbid` (no racing runs against the shared ConfigMap).

`src/analyst/graph.py` is a LangGraph state machine (`discover_candidates → fetch_research →
fetch_indicators → llm_select → validate_selection → write_portfolio → crypto_eod_report`):

- `discover_candidates` — Alpaca screener REST calls (most-actives/movers) merged into a
  symbol→{volume, change_pct} dict, tagged with a `market` field. Gated on
  `state["stock_market_open"]` (computed once in `main.py` via
  `src/common/market_calendar.py::is_stock_market_open`) — on a closed stock-market day the
  stock branch is skipped, but crypto discovery is unconditional (crypto trades 24/7), so
  Analyst still runs and picks crypto symbols. `write_portfolio` passes the same flag to
  `slack.notify_morning_report(..., stock_market_open=..., crypto_enabled=...)`, which prepends
  a "stock market is closed" banner (mentioning that crypto trading continues, if enabled).
- `fetch_research` — Alpaca News API + Yahoo RSS, plain text, no vector store or RAG.
- `fetch_indicators` — top-N by `abs(change_pct)` get a real TAAPI `/bulk` fetch
  (`src.common.indicators.fetch_indicators_bulk`, shared with Dealer).
- `llm_select` — the decision call:
  ```python
  llm = ChatOpenAI(base_url=cfg.llm.base_url, api_key="not-needed",
                    model=cfg.llm.model, temperature=cfg.llm.temperature,
                   ).with_structured_output(PortfolioSelection)
  ```
  `PortfolioSelection` is a pydantic model (`src/analyst/schema.py`) — structured output, no
  manual JSON parsing.
- `validate_selection` — never trusts the LLM's own `exchange` field; re-derives it from the
  `market` tag `discover_candidates` assigned, and drops any picked symbol not actually present
  in the candidate set (a hallucination guard).
- `write_portfolio` — patches the `portfolio` ConfigMap via the `kubernetes` Python client, then
  posts a Slack "Morning Market Report."

**Output** — the entire Analyst→Dealer interface:
```json
{"generated_at": "...", "symbols": [
  {"symbol": "NVDA", "exchange": "stocks", "budget": 5000,
   "indicators": ["rsi","macd","vwap","bbands","sma","ema"], "rationale": "..."}
]}
```
`src/common/portfolio_state.py`'s `write_portfolio()`/`read_portfolio()` are the only functions
that touch it — a plain k8s ConfigMap (`CONFIGMAP_NAME = "portfolio"`,
`DATA_KEY = "portfolio.json"`), no schema versioning, last write wins.

## Dealer — the polling loop

Entrypoint: `python -m src.dealer.main`, `apps/v1 Deployment` `replicas: 1`,
`strategy: {type: Recreate}` (prevents two pods racing on the same portfolio read).

```python
while True:
    if market_is_open(cfg, log):             # Alpaca clock + 15-min post-open buffer
        portfolio = merge_held_positions(read_portfolio(), cfg)  # fresh ConfigMap read every cycle
        for entry in portfolio.get("symbols", []):
            try:
                graph.invoke(DealerState(...))
            except Exception:
                log_and_continue()           # one bad symbol never kills the pod
    sleep(cfg.trading.pollsecs)              # 600s
```

`src/dealer/graph.py` is a 3-node graph: `fetch_indicators` (TAAPI `/bulk`, one request per
symbol per cycle) → `llm_call` (same `ChatOpenAI(...).with_structured_output()` pattern as
Analyst, output schema `Signal = {symbol, action: BUY|HOLD|SELL, reasoning, size_hint}` in
`src/dealer/schema.py`) → `call_floor_broker`, which is a plain synchronous HTTP POST:

```python
requests.post(f"{cfg.floor_broker.base_url}/execute", json={
    "symbol": ..., "exchange": ..., "action": ...,
    "budget": ..., "slP": cfg.trading.slP, "tpP": cfg.trading.tpP,
}, timeout=30)
```

Every signal — including HOLD — posts to Slack via `notify_dealer_signal`; only the Floor Broker
HTTP call is withheld on a HOLD. `cfg.floor_broker.base_url` is in-cluster Service DNS
(`http://floor-broker.multi-agent-ai-trader.svc.cluster.local:8000`) — a k8s primitive, not a
Miramar platform endpoint.

BUY signals are now gated locally in this order before the HTTP call:

1. `macro_blackout` — whole-day scheduled macro/quad-witching pause.
2. `strategy.enable_symbol_stop_cooldown` — same-symbol stop-loss cooldown, using
   `db.fetch_symbol_floor_broker_events_since()` and `_classify_exit_event()`.
3. `strategy.enable_win_rate_throttle` — trailing TP/SL win-rate throttle. The scope is
   `strategy.win_rate_throttle_scope`: `symbol` reads only same-symbol events; `global` restores
   the older portfolio-wide behavior.
4. `strategy.min_confidence` — low-confidence BUY skip.
5. Authorized-budget checks — held-only entries (`budget=0`) and `size_hint=0`.

`llm_call()` also includes a compact "Recent same-symbol trading history" block when
`strategy.enable_dealer_memory` is true. It is advisory prompt context only: DB read failures
log a warning and fail open, while the deterministic cooldown above is the hard re-entry guard.

## Floor Broker — the only order-placement path

Entrypoint: `uvicorn.run("src.floor_broker.app:app", ...)`, `apps/v1 Deployment` + `ClusterIP
Service` on port 8000. Purely mechanical — never calls an LLM.

`src/floor_broker/app.py`:
```python
class ExecuteResponse(BaseModel):
    status: Literal["executed", "submitted", "skipped", "error"]
    detail: str
    reason: str | None = None
    order_id: str | None = None
    fill_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None

@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest):
    ...
    result = execution.buy(...) or execution.sell(...)
    return ExecuteResponse(**result)          # every dict any code path returns must satisfy this Literal
```
That last line is a real trap: any new "declined" outcome in `execution.py` that returns a
`status` value outside the `Literal` raises a Pydantic `ValidationError` inside the generic
`except Exception` handler, which re-raises as an HTTP 500 instead of the intended clean
response. The fix pattern established in this repo is to reuse `"skipped"` with a distinguishing
`reason` string rather than growing the `Literal` per new decline condition — see the git
history around `state_not_reconciled` for a worked example, and always add an end-to-end test
through the actual `/execute` endpoint (not just a mocked `execution.*` unit test) for any new
decline path, since only the real endpoint catches a response-model mismatch.

`src/floor_broker/execution.py` — request validation happens in `app.py` before this file is
ever reached (symbol/exchange regex, budget/slP/tpP bounds); `cfg = load_config()` is loaded at
module level for the `strategy:` fields below. Order logic:
- `buy(symbol, exchange, budget, slP, tpP)` — after the kill switch and daily halt checks
  (below), refuses if a position or open order already exists for the symbol (no pyramiding).
  Stocks get a bracket order (`OrderClass.BRACKET`, SL = `ask_price * slP`, TP =
  `ask_price * tpP`); crypto (`exchange != "stocks"`) gets a plain notional market order
  instead, skipped rather than clamped if the rounded notional falls below Alpaca's $10 minimum.
  Stock BUYs also enforce `strategy.max_bid_ask_spread_pct` when set: the Floor Broker fetches
  one live ask/bid pair, skips wide or invalid spreads with a normal `status="skipped"` reason,
  and reuses the accepted ask as the bracket-order reference price.
- `sell(symbol, reason="dealer_signal")` — sells the full open quantity at market; on Alpaca's
  "conflicting orders" rejection, cancels the blockers and resubmits against the re-fetched
  quantity.
- Both submit and return immediately (`status="submitted"`) — they don't block for a fill.
  `order_id` goes into a module-level in-memory dict, `_pending_fills`; a stock bracket's parent
  order id goes into `_tracked_brackets`. Two daemon threads in `main.py`
  (`poll_pending_fills()`, `poll_bracket_fills()`, 30s each) later resolve these to actual fills
  or terminal non-fills and post the outcome to Slack — the eventual fill is never visible
  through the original `/execute` request/response.

**Daily profit/loss halt (`strategy.daily_profit_target_usd`/`daily_loss_limit_usd`).** Checked
in `buy()` right after the kill switch: `daily_pnl = account.equity - account.last_equity`
(Alpaca's `TradeAccount` already tracks this, no custom bookkeeping). Once `daily_pnl` reaches
the profit target or breaches the loss limit, the BUY is skipped
(`reason="daily_profit_target_reached"`/`"daily_loss_limit_reached"`); `sell()` is unaffected.
No dedicated poller — the normal per-BUY Slack notice already reports the skip reason.

**Crypto synthetic stop-loss/take-profit (`strategy.crypto_slP`/`crypto_tpP`).** Alpaca's
bracket orders are equity-only, so crypto has no server-side SL/TP. `check_pending_fills()`
computes `sl_price`/`tp_price` off the real fill price on a crypto BUY fill and stores them in a
third in-memory dict, `_crypto_stops`. `check_crypto_stops()` runs inside the *existing*
`poll_bracket_fills()` thread (no new thread), fetching the current bid per tracked symbol and
calling `sell(symbol, reason="stop_loss"|"take_profit")` once it's crossed; `sell()` pops the
symbol from `_crypto_stops` so the poller never double-sells. Same in-memory,
restart-drops-tracking tradeoff as `_pending_fills`/`_tracked_brackets`. See `docs/strategy.md`
for the full design log.

**Restart recovery.** `_pending_fills`/`_tracked_brackets` are in-memory, so a pod restart would
otherwise silently lose track of every still-open order. `reconstruct_tracked_state()` runs
before the poll threads start, calling `trading_client.get_orders(GetOrdersRequest(status=
"open", nested=True))` to rebuild both dicts from Alpaca's own state (the source of truth).
It retries `reconcile_tracked_state_once()` up to 5 times with exponential backoff (5s/10s/
20s/40s); `is_state_reconciled()` stays `False` until one succeeds, and `buy()` refuses new
BUYs (`status="skipped", reason="state_not_reconciled"`) while unreconciled — SELL is
unaffected, same asymmetry as the kill switch below. If all startup attempts fail,
`main.poll_reconciliation()` keeps retrying in the background every 60s so a transient Alpaca
outage at boot never permanently strands the pod. One known gap remains: a fill that happens
*during* the restart/reconciliation window itself is correctly executed on Alpaca but its Slack
notice is missed — closing that needs durable fill-history persistence (ROADMAP P1.1), out of
scope today.

**Kill switch.** `src/common/kill_switch.py::buy_kill_switch_active()` reads the
`buy-kill-switch` ConfigMap fresh (no caching) near the top of `buy()` — after the
state-reconciliation check, but before any position/order lookup:
```sh
kubectl patch configmap buy-kill-switch -n multi-agent-ai-trader --type merge -p '{"data":{"active":"true"}}'
```
`sell()` is completely untouched by it. A missing ConfigMap fails open (treated as inactive).

## EOD Report — read-only recap

Entrypoint: `python -m src.eod_report.main`, `batch/v1 CronJob` `30 13-16 * * *` with
`timeZone: America/New_York`, runs daily (including weekends). Each run asks Alpaca for the
official market close and sends exactly once when close+30min has passed, so normal close days
report at 16:30 ET and 13:00 early closes report at 13:30 ET. Closed days post a "market was
closed" notice rather than silently doing nothing. No dependency on the `portfolio` ConfigMap or
on Dealer/Floor Broker — it reads Alpaca's account/positions/fills directly and formats a Slack
summary via `src/common/eod.py`'s `fetch_fills()`/`summarize_positions()`.

## Shared code (`src/common/`)

- `config.py` — `load_config(path="config.yaml")` returns `OmegaConf.load(path)`, an
  `OmegaConf` `DictConfig`, not a custom class. One schema, imported by every service.
- `alpaca_client.py` — one shared `TradingClient(..., paper=True)` (hardcoded, no code path to
  live trading), credentials from `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_API_SECRET`.
- `portfolio_state.py` — the `portfolio` ConfigMap I/O (above), plus `merge_held_positions()`,
  which folds any pre-existing Alpaca position not already in the watchlist into it on every
  Dealer poll, tagged `budget=0.0, is_held_only=True` so a large held position can't silently
  re-authorize an equally large new BUY.
- `kill_switch.py` — the `buy-kill-switch` ConfigMap read (above).
- `logging.py` — `get_logger(prefix)` returns an emoji-prefixed `print(..., file=sys.stdout,
  flush=True)` closure. Stdout-only by k8s convention (`kubectl logs`) — no file logging, no
  MLflow; operational detail only (retries, warnings), not decision/execution history — see
  `db.py` below for that.
- `db.py` — Postgres persistence for Analyst picks, Dealer decisions, and Floor Broker
  execution events, added in v0.6.0 (a schema bug meant no rows actually landed until the
  v0.6.1 fix). Fire-and-forget writes (catch and log, never raise) via `psycopg[binary,pool]`,
  no ORM, no migrations — `CREATE TABLE IF NOT EXISTS` run lazily on first use. Backs the
  `/analyst-explain` skill. See [architecture.md](architecture.md#persistence) for the schema.
- `langsmith.py` — `configure(cfg)` wires LangGraph/LangChain tracing, gated by
  `cfg.langsmith.enabled` and capped by `cfg.langsmith.sampling_rate` to stay under LangSmith's
  free-tier 5k-traces/month quota.
- `slack.py` — every service posts through this one module; it's the human-readable real-time
  feed, while `db.py` is the durable structured record of the same events.

## Tests, build, deploy

- `./.venv/bin/pytest -q` — `tests/<service>/` mirrors `src/<service>/`. Convention: monkeypatch
  a `Fake*` stand-in for the Alpaca/k8s/Slack client, never a real network call.
  `tests/floor_broker/test_execution.py` is the largest and most instructive for the pattern.
  `tests/floor_broker/test_app.py` is where end-to-end `/execute` response-contract tests live
  (see the `ExecuteResponse` trap above).
- `ruff check .` for linting (`ruff format --check .` has pre-existing drift, not enforced).
- Every behavioral fix ships with a regression test proving it — write the test against the old
  code first, confirm it fails, then confirm the fix makes it pass.
- `.github/workflows/build-push.yaml` (`workflow_dispatch` or `push: tags: v*`) builds all 4
  service images on `[self-hosted, dgx]`. `.github/workflows/deploy.yaml`
  (`workflow_dispatch` only, `image_tag` input) applies `k8s/`, waits for `dealer`/
  `floor-broker` rollout, smoke-tests `/healthz`. Release flow: tag → push tag → `gh release
  create` → `gh workflow run deploy.yaml -f image_tag=<tag>`. No direct `docker build`/`kubectl
  apply` in normal operation — GHA is the only path.

## What's not built yet

See `docs/ROADMAP.md` for the full list. Still open at P0: dependency pinning/split (P0.11),
fuller CI validation (P0.12), baseline container security (P0.13), and a formalized structured
outcome/HTTP-status contract for `/execute` (P0.10 — today every declined-BUY outcome collapses
to `status="skipped"` + a `reason` string, HTTP 200, rather than a distinct status/code per
condition, e.g. `state_not_reconciled` arguably warrants 503/`retryable=true` rather than 200).
P1 (durable event store, exact prompt/model capture, shadow mode, forward evaluation, and a true
historical replay of the live LLM's actual past decisions — as opposed to the deterministic
baselines `src/backtest/` already covers) is the larger tier gated on P0 completing.
