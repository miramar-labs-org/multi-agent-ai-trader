# Architecture

`multi-agent-ai-trader` is a three-agent trading floor — **Analyst**, **Dealer**, **Floor
Broker** — that trades US equities (paper account) on Alpaca, deployed as independent
Kubernetes workloads on the DGX Spark k3s cluster. It is a re-platforming of an earlier
single-process script (`gpt-trader.py`) onto three independently-scaled k8s workloads that
communicate via a shared ConfigMap and plain HTTP, rather than a monolithic loop.

```
              08:55 America/New_York daily (k8s CronJob)
     + optional 12:30pm America/New_York run (feature-gated, off by default)
                         ┌─────────────────────────┐
                         │         Analyst          │
                         │  screener → news → LLM   │
                         └────────────┬─────────────┘
                                      │ writes
                                      ▼
                         ┌─────────────────────────┐
                         │  "portfolio" ConfigMap    │  ◄── no MLflow/DB, just a
                         │  {symbol, budget,        │      k8s API object
                         │   indicators, rationale} │
                         └────────────┬─────────────┘
                                      │ reads every poll
                                      ▼
                         ┌─────────────────────────┐
        every 600s  ───► │          Dealer          │  (long-running Deployment)
        while market     │ indicators → LLM signal  │
        is open          └────────────┬─────────────┘
                                      │ HTTP POST /execute
                                      │ (if action != HOLD)
                                      ▼
                         ┌─────────────────────────┐
                         │       Floor Broker        │  (Deployment + Service)
                         │  Alpaca order placement   │
                         └────────────┬─────────────┘
                                      │
                                      ▼
                              Alpaca paper account
```

Analyst and Floor Broker never talk to each other directly. There is no message queue and
no shared filesystem — the only *coordination* state between agents is the `portfolio`
ConfigMap, and the only network hop is Dealer → Floor Broker. Separately, all three agents
also write fire-and-forget history rows to Postgres (see [Persistence](#persistence) below)
— that's an append-only audit trail read back by `/analyst-explain` and, since
`fetch_track_record`, by the Analyst itself, not a coordination channel any agent depends on
to function.

Independently of that cycle, a fourth CronJob queries Alpaca directly once a day after market
close and posts a summary to Slack — it has no dependency on the ConfigMap or on Dealer/Floor
Broker's HTTP hop:

```
               13:30-16:30 America/New_York :30 checks (k8s CronJob)
                         ┌─────────────────────────┐
                         │       EOD Report        │
                         │ close+30 → Slack recap  │
                         └─────────────────────────┘
```

## Repo layout

```
multi-agent-ai-trader/
├── config.yaml                  # single source of config for all agents + EOD report -- fetched
│                                  # live from GitHub at runtime (main branch), not baked into images
├── notebook.ipynb                # bare JupyterLab entry point (no pipeline logic — see below)
├── Dockerfile.analyst/.dealer/.floor-broker/.eod-report
├── k8s/                          # 3 CronJobs, 2 Deployments, 1 Service, RBAC, namespace, secrets doc
└── src/
    ├── common/                   # shared: Alpaca clients, config loader, logger, portfolio I/O, Slack
    ├── analyst/                  # CronJob — picks the tradeable universe, posts the morning report
    ├── dealer/                   # Deployment — decides BUY/HOLD/SELL per symbol
    ├── floor_broker/             # Deployment+Service — executes orders on Alpaca
    └── eod_report/                # CronJob — posts a daily account/trade summary to Slack
```

Each agent is its own Docker image and k8s workload — they scale, restart, and fail
independently. This is deliberate: Analyst runs once a day and can fail loudly without
affecting a mid-day trading loop; Dealer needs exactly one replica (`strategy: Recreate`)
to avoid two pods racing on the same portfolio; Floor Broker is a stateless request/response
service that can be replaced/restarted without losing any state (it holds none).

## Agent 1 — Analyst (`src/analyst/`)

**Workload:** `batch/v1 CronJob`, schedule `55 8 * * *` with `timeZone: America/New_York`
(08:55 ET, 35min before the 9:30 ET open; the run itself takes ~5min, dominated by the TAAPI
indicator-fetch throttle, so the Morning Report typically posts ~09:00 ET — ~30min before the
open), `concurrencyPolicy: Forbid` (no overlapping runs), `backoffLimit: 1` — a failed research
run is meant to surface immediately, not retry-storm. Entrypoint: `python -m src.analyst.main`.

A second, optional CronJob (`k8s/analyst-midday-cronjob-k3s.yaml`, `analyst-midday`) fires at
`30 12 * * *` `America/New_York` (12:30pm ET) using the same image and entrypoint, distinguished
only by an `ANALYST_RUN_LABEL: "midday"` env var. It exists to catch intraday movers/news the
08:55 run missed — Dealer already reacts to price moves on known symbols every poll, but can't
discover a symbol that wasn't in the morning's picks. It's feature-gated via
`analyst.enable_midday_run` (`config.yaml`, default `false`): `main()` reads the env var, and if
it's a midday invocation with the flag off, logs and returns before `build_graph()` is even
called — no screener/news/TAAPI/LLM calls, no portfolio write, no Slack post. Toggling the flag
is a config-only `git push`, live within the same ~60s TTL as any other config change, no
redeploy needed (see `config.py` below). When enabled, `AnalystState["is_midday_run"]` is
threaded through the whole graph run: `write_portfolio` posts a "🕐 Midday Update" instead of
a duplicate "🌅 Morning Market Report" (same `slack.notify_morning_report`, overridden
`title`/`emoji`), and `crypto_eod_report` is skipped outright (it already ran this morning and
only ever covers the *prior* calendar day, so a second run has nothing new to report). One
side effect that's accepted, not fixed: `fetch_track_record`'s query has no upper date bound, so
a midday run's track-record context includes the same day's own earlier morning-run picks —
useful signal for the midday LLM pass, not a bug.

Runs every scheduled day regardless of whether the stock market is open — `main.py` checks
`src/common/market_calendar.py::is_stock_market_open()` (same Alpaca calendar API EOD Report
uses) once and threads the result into `AnalystState["stock_market_open"]`. On a closed day,
`discover_candidates` skips only the stock screener branch; crypto discovery is unconditional
(crypto trades 24/7), so Analyst still produces crypto picks. `write_portfolio` passes the flag
to `slack.notify_morning_report(...)`, which prepends a banner noting the stock market is
closed (and that crypto trading continues, when crypto is enabled) — unlike EOD Report, Analyst
never skips its entire run on a closed day.

**Purpose:** once a day, decide *which symbols are worth trading today* and hand that list
off to the Dealer.

**Implementation:** a 9-node LangGraph state machine (`src/analyst/graph.py`) over an
`AnalystState`:

| Node | What it does |
|---|---|
| `discover_candidates` | when `cfg.trading.enable_stocks` **and** `state["stock_market_open"]` (set once in `main.py`, see above), `sources.fetch_screener_candidates(screener_top_n=20)` — raw REST calls (not wrapped by `alpaca-py`) to Alpaca's `/v1beta1/screener/stocks/most-actives` and `/movers` endpoints, merged into a symbol→{volume, change_pct} dict, each tagged `market: "stocks"`. When `cfg.earnings_blackout.enabled` (default false), the stock candidate list is further filtered through `sources.fetch_earnings_calendar()` — a single market-wide Finnhub free-tier call (Alpaca has no earnings-calendar data, only Corporate Actions for splits/dividends/mergers) — dropping any symbol reporting earnings within `earnings_blackout.days_before`/`days_after` calendar days of today; fails soft to an unfiltered list on any Finnhub error. When `cfg.trading.enable_crypto`, also `sources.fetch_crypto_candidates(...)` — a fixed watchlist (`BTC/USD`, `ETH/USD`, `SOL/USD`; Alpaca's crypto screener has no most-actives equivalent) merged with `/v1beta1/screener/crypto/movers`, tagged `market: cfg.trading.crypto_taapi_exchange`. Crypto discovery is **not** gated on the stock-market-open flag or the earnings blackout (no earnings dates apply to crypto) — it always runs when enabled |
| `fetch_research` | gated on `cfg.analyst.enable_news` (short-circuits before any network call when false) — `sources.fetch_news(news_days=2)` (Alpaca News API, HTML stripped via BeautifulSoup) + `sources.fetch_yahoo_rss_headlines(...)` (Yahoo Finance RSS), concatenated into plain text |
| `fetch_indicators` | gated on `cfg.analyst.enable_indicators` (short-circuits before any TAAPI call when false) — ranks `raw_candidates` by `abs(change_pct)` (missing values sort last) and calls `src.common.indicators.fetch_indicators_bulk` (shared with the Dealer) for the top `cfg.analyst.indicator_fetch_limit` (default 15) — one TAAPI `/bulk` POST per symbol covering `rsi, macd, vwap, bbands, sma, ema`, sleeping `cfg.taapi.min_request_interval_secs` between calls to respect TAAPI's free-tier 1-req/15s cap. At the default limit this adds ~3.5 minutes to the once-daily run — accepted as a fixed cost of a pre-market CronJob, unlike the Dealer's 10-minute poll cycle where the same rate limit is a tighter constraint. Not every candidate gets indicator data; only the top movers by size do |
| `fetch_track_record` | gated on `cfg.analyst.enable_track_record` (short-circuits before any DB call when false) — reads the Analyst's own pick history plus matching Dealer decisions and Floor Broker events from Postgres via `db.fetch_analyst_picks_since()`/`fetch_dealer_decisions_since()`/`fetch_floor_broker_events_since()` for the last `cfg.analyst.track_record_days` (default 5) calendar days, formatted as plain text (qualitative sequence only — no computed P&L; see [Persistence](#persistence)). Runs before `write_portfolio` records this run's own picks, so a symbol picked *this* run never appears in its own track record |
| `fetch_position_pnl` | gated on `cfg.analyst.enable_position_pnl` (short-circuits before any Alpaca call when false) — `trading_client.get_all_positions()` + `summarize_positions()` (`src/common/eod.py`, same shape `crypto_eod_report` already uses, no `only_crypto` filter so both stocks and crypto are included) formatted as plain text: symbol, qty, avg entry price, current price, unrealized $ and %. A live point-in-time snapshot only — not persisted, not compared across days, and not per-pick attribution (a symbol can be bought/sold more than once); complements `fetch_track_record`'s qualitative history rather than replacing it. Fails open (empty text) on any Alpaca API error, matching `fetch_research`/`fetch_indicators` |
| `llm_select` | the actual LLM call — see below |
| `validate_selection` | overrides each pick's `exchange` field with the `market` tag `discover_candidates` actually assigned that symbol (never trusts the LLM's own copy of `exchange`), drops any pick whose symbol isn't in `raw_candidates` at all (a hallucination), then walks the remaining picks in the LLM's own returned order and greedily drops (logs, doesn't error) any pick that would push the running total of `budget` over `cfg.analyst.max_total_budget_usd` — a last-line-of-defense cap since no per-pick `budget` upper bound exists on its own (`src/analyst/schema.py`) and the LLM's suggested `default_budget` is only a prompt hint it can ignore |
| `write_portfolio` | patches the `portfolio` ConfigMap via the `kubernetes` Python client |
| `crypto_eod_report` | gated on `cfg.trading.enable_crypto` — posts a crypto-only "Crypto EOD Report" to Slack covering the prior full ET day's crypto fills/positions; see below |

```mermaid
flowchart TD
    A[discover_candidates] --> B[fetch_research]
    B --> C[fetch_indicators]
    C --> C2[fetch_track_record]
    C2 --> C3[fetch_position_pnl]
    C3 --> D[llm_select]
    D --> E[validate_selection]
    E --> F[write_portfolio]
    F --> G[crypto_eod_report]
    G --> H([END])
```

**LLM call:**
```python
llm = ChatOpenAI(
    base_url=cfg.llm.base_url,   # OpenAI-compatible endpoint — see platform-services.md
    api_key="not-needed",
    model=cfg.llm.model,
    temperature=cfg.llm.temperature,
).with_structured_output(PortfolioSelection)
```
The system prompt instructs the model to pick at most `max_universe_size` (10) symbols from
the candidates/research, each with a `budget` (default `default_budget`=5000), an
`indicators` list (default `[rsi, macd, vwap, bbands, sma, ema]`), and a rationale. Output is
enforced as structured JSON via `PortfolioSelection` (pydantic, `src/analyst/schema.py`) —
there is no manual JSON-parsing/regex step; LangChain's `.with_structured_output()` handles
that entirely. Each candidate the LLM sees carries a `market` field ("stocks", or a crypto
exchange name); the prompt tells it to copy that value into the pick's `exchange` field, but
`validate_selection` re-derives `exchange` from the known `market` tag regardless — the LLM's
own copy is never trusted as-is.

**Output — the `portfolio` ConfigMap** (namespace `multi-agent-ai-trader`):
```json
{
  "generated_at": "2026-08-01T12:55:03Z",
  "symbols": [
    {"symbol": "NVDA", "exchange": "stocks", "budget": 5000, "indicators": ["rsi","macd","vwap","bbands","sma","ema"], "rationale": "..."}
  ]
}
```
This is the **only** interface between Analyst and the rest of the system. No RAG, no
vector search, no MLflow logging of the selection — the "research" step is plain text
concatenation fed straight into the prompt.

Immediately after writing the ConfigMap — still inside the same CronJob pod, before market
open — `write_portfolio` also fetches the account balance (`trading_client.get_account()`) and
posts a "Morning Market Report" to Slack (`slack.notify_morning_report`): the day's picks with
budgets/rationale, plus current equity/cash/buying power. This is a by-product of the same run,
not a separate schedule — there is no dedicated Slack CronJob for it, unlike the EOD Report below.

Right after that, a `crypto_eod_report` graph node (gated on `cfg.trading.enable_crypto`) posts a
second, crypto-only "Crypto EOD Report" to Slack covering the **prior full ET calendar day's**
crypto fills/positions (`slack.notify_crypto_eod_report`). Crypto trades 24/7, so it has no market
close to hang a report off of the way stocks do — rather than a separate always-on schedule, it
piggybacks on the Analyst's existing daily 08:55 America/New_York run, right after the new
day's picks go out (`write_portfolio` runs before `crypto_eod_report` in the graph). Uses
the same `src.common.eod.fetch_fills`/`summarize_positions` helpers as the stock EOD Report below,
filtered to crypto (`"/" in symbol`, the same convention `alpaca_client.get_current_ask_price`
already uses to distinguish crypto tickers).

## Agent 2 — Dealer (`src/dealer/`)

**Workload:** `apps/v1 Deployment`, `replicas: 1`, `strategy: {type: Recreate}` — explicitly
to prevent two Dealer pods racing to read/act on the same portfolio during a rolling update.
No ports exposed (it's a loop, not a server). Entrypoint: `python -m src.dealer.main`.

**Purpose:** continuously, while the market is open, decide BUY/HOLD/SELL for every symbol
in the current portfolio and hand non-HOLD decisions to Floor Broker.

**Main loop** (`src/dealer/main.py`):
```
while True:
    if market_is_open(cfg):              # Alpaca clock + 15-min post-open buffer
        portfolio = read_portfolio()      # fresh read of the ConfigMap every cycle, no caching
        for symbol in portfolio.symbols:
            try:
                graph.invoke(DealerState(...), config={"tags": ["dealer"]})
            except Exception:
                log_and_continue()        # one bad symbol never kills the loop/pod
    sleep(cfg.trading.pollsecs)           # 600s (10 min)
```
`market_is_open` checks `trading_client.get_clock().is_open` via Alpaca, plus a
`cfg.trading.buffer` (15 min) wait after the 9:30 ET open bell "to avoid volatility"; a
`cfg.trading.market_override` config flag can force-treat the market as open for testing.

On the open→closed transition, it also posts `slack.notify_stock_market_closed(next_open)` —
edge-detected via a module-level `_last_market_open` flag so it fires once per transition, not on
every 600s poll while the market stays closed for hours. This is distinct from EOD Report's
`notify_market_closed` (a once-daily "today wasn't a trading day" notice) — this one is Dealer's
own live status signal, so a genuinely closed market and a stuck/crashed Dealer pod are
distinguishable from Slack alone.

**Graph** (`src/dealer/graph.py`), a 3-node state machine over `DealerState`:

| Node | What it does |
|---|---|
| `fetch_indicators` | fetches every indicator configured for the symbol (or all of `cfg.indicators` if the entry says `["ALL"]`) from **TAAPI.io** — a third-party technical-analysis API, unrelated to any Miramar platform service — in a single `/bulk` POST request (`indicators.py`), and builds a natural-language indicator text block |
| `llm_call` | the decision LLM call — see below |
| `call_floor_broker` | HTTP POST to Floor Broker if action != HOLD; a BUY is additionally refused locally (never forwarded) while `cfg.macro_blackout.enabled` and today matches either a `macro_blackout.dates` entry or an auto-computed quad witching day — see [Risk controls](#risk-controls-and-failure-handling) |

```mermaid
flowchart TD
    A[fetch_indicators] --> B[llm_call]
    B --> C[call_floor_broker]
    C --> D([END])
```

**LLM call:** same pattern as Analyst — `ChatOpenAI(base_url=cfg.llm.base_url, ...).with_structured_output(Signal)`.
System prompt: *"You are an expert technical trader in stocks. Based on the values of ALL
of the indicators below, decide if you should BUY, SELL, or HOLD. size_hint must be a decimal
fraction between 0.0 and 1.0 representing the portion of the symbol's budget to deploy on a BUY
(e.g. 0.5 = half the budget, 1.0 = the full budget) — never a dollar amount or share count."*
The `Signal` model
(`src/dealer/schema.py`) is `{symbol, action: BUY|HOLD|SELL, reasoning, size_hint}` —
`size_hint` (a 0–1 fraction, default 1.0) scales the symbol's configured `budget` on a BUY
(`budget * size_hint`); it has no effect on SELL, which ignores `budget` entirely
(`execution.py::sell()` closes the full open position, not a partial amount). Two cases are
refused locally rather than forwarded to Floor Broker: a BUY signal on a held-only entry
(`budget == 0`, see `merge_held_positions()` above — `status="skipped",
reason="no_authorized_budget"`), and a BUY whose `size_hint` scales the budget to exactly $0
(`status="skipped", reason="size_hint_zero"` — `ExecuteRequest.budget` requires `> 0`, so this
would otherwise fail request validation rather than get a graceful business-logic skip). A
budget scaled to a small but nonzero amount is still forwarded as-is — Floor Broker's own
minimum-notional/insufficient-qty checks (`execution.py`) already handle that gracefully with
their own reason codes, so Dealer doesn't need a second floor.

**Dispatch to Floor Broker** — plain in-cluster HTTP, no message queue:
```python
requests.post(f"{cfg.floor_broker.base_url}/execute", json={
    "symbol": ..., "exchange": ..., "action": ...,
    "budget": ..., "slP": cfg.trading.slP, "tpP": cfg.trading.tpP,
}, timeout=30)
```
`cfg.floor_broker.base_url` = `http://floor-broker.multi-agent-ai-trader.svc.cluster.local:8000`
— standard k8s Service DNS, not a Miramar platform endpoint. HOLD signals never leave the
Dealer pod (`execution_result = {"status": "skipped", "detail": "HOLD"}` is set locally).

On a successful response, `call_floor_broker` forwards Floor Broker's `reason`, `fill_price`,
`sl_price`, `tp_price` fields (see Floor Broker's `ExecuteResponse` below) straight through as
keyword arguments to `slack.notify_floor_broker_result`, so the Slack notice for a BUY/SELL shows
the actual fill price and TP/SL levels, not just the request Dealer sent. The error-response and
request-exception branches are unchanged — they call `notify_floor_broker_result` with only the
original 4 positional arguments, so those optional fields simply render as absent.

## Agent 3 — Floor Broker (`src/floor_broker/`)

**Workload:** `apps/v1 Deployment` + `ClusterIP Service` on port 8000. Uses the shared
`multi-agent-ai-trader-configmap-reader` ServiceAccount (ROADMAP P0.5) to read the
`buy-kill-switch` ConfigMap — it still never reads/writes the portfolio ConfigMap Analyst/Dealer
use. Entrypoint: `uvicorn.run("src.floor_broker.app:app", host="0.0.0.0", port=8000)`.

**Purpose:** the only component that actually talks to Alpaca's *trading* API (Analyst and
Dealer only use Alpaca's *data*/*screener*/*news* endpoints). Purely mechanical — it never
calls an LLM.

**API** (`src/floor_broker/app.py`):
| Route | Purpose |
|---|---|
| `GET /healthz` | `{"status": "ok"}` — backs the Deployment's readiness/liveness probes |
| `POST /execute` | body: `{symbol, exchange, action: BUY\|SELL, budget, slP, tpP}` → dispatches to `execution.buy()`/`execution.sell()` |

Alpaca `APIError`s are caught and returned as `{"status": "error", "detail": ...}` (HTTP 200)
rather than raised; unexpected exceptions become a 500.

`ExecuteResponse` also carries optional `reason`, `order_id`, `fill_price`, `sl_price`, `tp_price`
fields (all `None` unless `execution.buy()`/`sell()` populate them — see below), so Dealer can
forward a stable reason code (and, for stock BUYs, the pre-computed bracket prices) to Slack
instead of only the request it sent. `reason` is one of `opening_position` (a BUY), `dealer_signal`
(an explicit SELL from the Dealer's LLM decision), `take_profit`/`stop_loss` (an asynchronous
bracket-leg fill for stocks, or a synthetic crypto stop/target trigger — see below — both detected
by the pollers below), or a skipped-BUY reason (`buy_kill_switch_active`, `state_not_reconciled`,
`budget_below_minimum`, `daily_profit_target_reached`, `daily_loss_limit_reached`,
`open_orders_exist`, `market_value_unavailable`, `budget_exhausted` — see the daily halt section
and `buy()` below). As of ROADMAP P0.14, `/execute` itself never
returns `fill_price` — BUY/SELL orders are submitted and the response comes back immediately with
`status="submitted"`; the actual fill (`opening_position`/`dealer_signal`) and bracket-leg fills
(`take_profit`/`stop_loss`) are both reported later, asynchronously, only via the Slack posts the
pollers below send directly.

**Order logic** (`src/floor_broker/execution.py`):
- **`buy()`** — safety gate first: `budget` is treated as the dollar amount **authorized** for
  the symbol, not a one-shot "open a fresh position" ticket. If an open order already exists for
  the symbol (an in-flight BUY not yet filled, or a pending SELL), the buy is still
  unconditionally skipped (`reason="open_orders_exist"`) — layering a new BUY on an in-flight
  order is racy regardless of budget math. If an open **position** already exists instead, `buy()`
  tops it up rather than skipping: it reads the position's `market_value` and, if
  `budget - market_value > 0`, submits a smaller BUY for just the remainder — repeated BUY
  decisions across Dealer poll cycles converge the position toward `budget` over time, with no
  extra state needed (each call recomputes headroom off the live position). If `market_value` is
  unavailable, the BUY is skipped (`reason="market_value_unavailable"`) rather than guessed — this
  is a trading-money gate, so it fails **closed** on missing data, unlike the Analyst's
  informational P&L snapshot which fails open. If the existing position's value already meets or
  exceeds `budget`, the BUY is skipped (`reason="budget_exhausted"`). A reduced "remaining budget"
  flows unchanged into the same sizing logic below, so a remainder too small for one share
  (stocks) or below Alpaca's crypto minimum both fall through to the existing
  `insufficient_qty`/`budget_below_minimum` skips automatically. For stocks, submits a
  **bracket order** (`OrderClass.BRACKET`) with computed stop-loss (`ask_price * slP`) and
  take-profit (`ask_price * tpP`) legs, `TimeInForce.DAY` — priced off the ask since that's
  where a BUY actually fills, not the bid/ask mid. TP/SL prices are rounded to 4 decimals for
  stocks under $1.00 and 2 decimals otherwise (`_round_to_tick`) — sub-$1 stocks are quoted in
  $0.0001 increments (SEC Rule 612), so 2dp rounding can land TP/SL on the same cent as
  `base_price` and get rejected. For crypto (`exchange != "stocks"`), submits a plain
  notional market buy instead (`TimeInForce.GTC`) — bracket orders aren't used for crypto.
  The notional amount is rounded to 2 decimals before submitting — Alpaca rejects a crypto
  notional with finer precision than that (`code 42210000`). If the rounded notional is below
  `MIN_CRYPTO_NOTIONAL` ($10) — Alpaca also rejects a crypto notional under its minimum order
  value (`code 40310000`, "cost basis must be >= minimal amount of order 10") — the BUY is
  **skipped** (`status="skipped", reason="budget_below_minimum"`), not clamped up: silently
  raising the notional to the minimum could submit an order larger than the caller's intended
  budget.
- **`sell()`** — sells the full open quantity at market. Has an explicit **retry-after-cleanup**
  path: if Alpaca rejects with error code `40310000` (conflicting orders blocking the sell),
  it cancels the blocking orders (ignoring 404s), *re-fetches* the now-current open quantity
  (it can change once blockers clear), and resubmits.

**Asynchronous order submission (ROADMAP P0.14).** Both `buy()` and `sell()` submit the order to
Alpaca and return immediately with `status="submitted"` — they no longer block waiting to learn
whether the order filled. `buy()` registers `order_id` in a module-level, in-memory dict,
`_pending_fills: dict[order_id, context]` (symbol, action, reason, and — for stock BUYs — the
pre-computed `sl_price`/`tp_price`); `sell()` does the same, minus the SL/TP prices. The
`poll_pending_fills()` daemon thread (below) later resolves each entry to a fill (or drops it on
a terminal non-fill status) and posts the actual fill price to Slack once known.

**Async TP/SL fill detection.** A stock BUY's stop-loss/take-profit legs are a bracket
(`OrderClass.BRACKET`, OCO) — whichever leg fills first, Alpaca auto-cancels the other, and
neither fill is visible through the original `/execute` request/response cycle since it can
happen minutes or hours later. To surface these as Slack notices, `buy()` registers the parent
bracket order id in a second module-level, in-memory dict, `_tracked_brackets: dict[symbol,
order_id]` (stocks only — crypto has no bracket legs), and `sell()` removes the symbol from it
immediately before submitting an explicit sell (so a manual/Dealer-driven SELL doesn't also get
reported as a TP/SL fill once the bracket's legs are cancelled as a side effect).

`src/floor_broker/main.py` starts four daemon background threads alongside uvicorn's HTTP server
in the same process — `poll_reconciliation()` (restart recovery, see below) and
`poll_kill_switch()` (see the kill switch section below) are the other two; the two fill-watchers
are:
- **`poll_pending_fills()`** — every `PENDING_FILL_POLL_INTERVAL_S` (30s) calls
  `execution.check_pending_fills()`, which re-fetches each tracked order by id and, once
  `filled_avg_price` is populated, returns a `kind="fill"` event (dropping the entry once it
  does). If the order instead reaches a terminal non-fill status (`CANCELED`/`REJECTED`/
  `EXPIRED`), it returns a `kind="terminal"` event instead, also dropping the entry — this is the
  only place that outcome is ever observed, so it must be reported, not silently dropped.
  `main.py` posts a fill event to Slack via `slack.notify_floor_broker_result(..., status=
  "executed", reason=event["reason"], fill_price=event["fill_price"])`, and a terminal event via
  `status="no_fill"`.
- **`poll_bracket_fills()`** — every `BRACKET_FILL_POLL_INTERVAL_S` (30s) calls
  `execution.check_bracket_fills()`, which re-fetches each tracked bracket order
  (`get_order_by_id(..., filter=GetOrderByIdRequest(nested=True))` for the `legs`), classifies a
  terminal leg by `OrderType` (`LIMIT` → `take_profit`, `STOP` → `stop_loss`) and `OrderStatus`
  (`FILLED` vs. `CANCELED`/`EXPIRED`/`REJECTED`), untracks the symbol once either leg reaches a
  terminal state, and returns a `kind="fill"` event per resolved fill or a `kind="terminal"` event
  if both legs closed with no fill. `main.py` posts each event to Slack the same way (`"executed"`
  vs. `"no_fill"`).

**Transient-vs-terminal error handling.** A poll's `get_order_by_id()` call can itself fail —
rate limit, timeout, an Alpaca-side 5xx. `execution._is_order_not_found()` only treats this as
"nothing left to watch" for a *confirmed* 404 (Alpaca's code `40410000`, exposed as
`ORDER_NOT_FOUND_CODE`); any other error, including one whose body doesn't even parse as JSON, is
treated as transient — the entry stays tracked, and a `poll_failures` counter on it (visible on
`_pending_fills[order_id]["poll_failures"]` or `_tracked_brackets[symbol]["poll_failures"]` once
non-zero) is incremented and logged, then cleared again once the order is reachable. This means a
brief Alpaca outage no longer silently stops watching a still-live order.

Both loops also catch and log any exception per iteration at the top level, so one bad poll (e.g.
an exception outside `check_pending_fills()`/`check_bracket_fills()`'s own error handling) never
kills either thread.

**Restart recovery.** `_tracked_brackets` and `_pending_fills` are both in-memory and
single-process, so a Floor Broker restart would otherwise lose track of every order/bracket that
was still open at the moment it went down. `execution.reconstruct_tracked_state()` runs once at
the top of `main()`, before either poll thread starts, and calls
`trading_client.get_orders(GetOrdersRequest(status="open", nested=True))` — this queries at the
order-*family* level (Alpaca's own semantics: a bracket whose entry already filled but whose
TP/SL legs are still live still counts as "open"), so it correctly re-populates both dicts:
orders with no legs go into `_pending_fills`, brackets with at least one still-open leg go into
`_tracked_brackets`. Only orders still open on Alpaca are restorable this way — by definition
nothing has filled yet, so no notification could have been missed by the restart itself.

`reconstruct_tracked_state()` retries the underlying `execution.reconcile_tracked_state_once()`
up to 5 times with exponential backoff (5s, 10s, 20s, 40s) rather than giving up on the first
`APIError` — a transient Alpaca outage at exactly boot time shouldn't permanently strand the pod
with empty tracking dicts. `execution.is_state_reconciled()` stays `False` until an attempt
succeeds, and `execution.buy()` refuses new BUYs (`status="skipped"`,
`reason="state_not_reconciled"` -- `ExecuteResponse`'s status `Literal` doesn't have a distinct
"rejected" value, so this reuses "skipped" like every other declined-BUY outcome) while it's
`False`, since submitting a fresh order before
Alpaca's live state has been reconciled risks losing track of it exactly like the gap this
mechanism exists to close (SELL is unaffected — same asymmetry as the kill switch below). If all
5 startup attempts fail, `main.poll_reconciliation()` keeps retrying `reconcile_tracked_state_once()`
in the background every 60s until it succeeds, un-blocking BUY execution without needing a pod
restart.

**Limitation:** the one remaining gap is a fill that happens *during* the restart/reconciliation
window itself — between the old pod dying and reconciliation succeeding on the new one. The
underlying order/TP/SL still executes correctly on Alpaca's side (Alpaca owns the order, not
Floor Broker) regardless, but the Slack notice for that specific fill is missed. Closing this
fully would require persisting fill history outside process memory (e.g. the durable event store
in ROADMAP P1.1) to reconcile against Alpaca's *closed* orders too, not just its open ones — out
of scope here. This is a narrow window (pod restart/reconciliation time), not the full
"no persistence at all" gap this section used to describe.

**Runtime BUY kill switch (ROADMAP P0.5).** `src/common/kill_switch.py::buy_kill_switch_active()`
reads the `buy-kill-switch` ConfigMap fresh (no caching) at the very top of `execution.buy()`,
before any position/order lookup. If active, the BUY is skipped
(`status="skipped", reason="buy_kill_switch_active"`) without ever calling Alpaca; `sell()` is
completely untouched, so SELL always remains available even with the switch on. A missing
ConfigMap (e.g. before the deploy workflow's seed step has ever run) fails open — treated as
inactive rather than blocking every BUY on a setup gap — but any other k8s API error propagates.
`src/floor_broker/main.py::poll_kill_switch()` is one of the four daemon threads described above,
checking the switch every `KILL_SWITCH_POLL_INTERVAL_S` (30s) purely to post a
Slack notice (`slack.notify_buy_kill_switch`) on a state *transition* — `/execute` itself already
re-checks the switch fresh on every request, so this thread never gates trading, only reports on
it. The first poll after a pod start only discovers the switch's current state and never counts
as a transition.

Runbook — activate/deactivate without a redeploy:
```sh
# Block new BUY orders (SELL remains permitted):
kubectl patch configmap buy-kill-switch -n multi-agent-ai-trader --type merge -p '{"data":{"active":"true"}}'

# Resume BUY orders:
kubectl patch configmap buy-kill-switch -n multi-agent-ai-trader --type merge -p '{"data":{"active":"false"}}'

# Check current state:
kubectl get configmap buy-kill-switch -n multi-agent-ai-trader -o jsonpath='{.data.active}'
```

**Daily profit/loss halt (`strategy.daily_profit_target_usd`/`daily_loss_limit_usd`, see
`docs/strategy.md`).** A second, config-driven BUY gate in `execution.buy()`, checked right after
the kill switch: it fetches `trading_client.get_account()` and computes
`daily_pnl = equity - last_equity` — Alpaca's own `TradeAccount` model already tracks this, so no
custom day-boundary bookkeeping is needed. If `daily_pnl` has reached the configured profit
target or breached the configured loss limit, the BUY is skipped
(`status="skipped", reason="daily_profit_target_reached"` or `"daily_loss_limit_reached"`) without
submitting an order. Only `halt_behavior: block_new_buys` is implemented — `sell()` is completely
untouched, same SELL-always-available asymmetry as the kill switch above. There's no dedicated
poller or Slack transition notice for this one (unlike the kill switch): every skipped BUY already
gets reported through the normal `slack.notify_floor_broker_result` call in
`src/dealer/graph.py::call_floor_broker`, which is enough to observe the halt taking effect.

**Crypto synthetic stop-loss/take-profit (`strategy.crypto_slP`/`crypto_tpP`, see
`docs/strategy.md`).** Alpaca's bracket orders are equity-only
(`alpaca.trading.enums.OrderClass`'s docstring: "Crypto trading: simple (or \"\")"), so a crypto
BUY (see above) has no server-side SL/TP at all. `check_pending_fills()` closes that gap: once a
crypto BUY's fill price is observed, it computes `sl_price = fill_price * crypto_slP` and
`tp_price = fill_price * crypto_tpP` and stores them in a third module-level, in-memory dict,
`_crypto_stops: dict[symbol, (sl_price, tp_price)]` — same restart-drops-tracking tradeoff as
`_tracked_brackets`/`_pending_fills` above, deliberately not given ConfigMap persistence.
`execution.check_crypto_stops()` is polled from the *existing* `poll_bracket_fills()` daemon
thread (no new thread) on the same `BRACKET_FILL_POLL_INTERVAL_S` (30s) cadence: for each tracked
symbol it fetches the current bid via `alpaca_client.get_current_bid_price()` (the price an
immediate market SELL would realize) and, once it crosses either level, calls
`execution.sell(symbol, reason="stop_loss"|"take_profit")` and drops the entry. A transient
bid-fetch failure just skips that symbol for the round — it stays tracked and is checked again on
the next poll, same transient-vs-terminal philosophy as the order-status polling above. `sell()`
also pops any tracked symbol from `_crypto_stops` on an explicit/Dealer-driven SELL, so the poller
never double-sells.

## EOD Report (`src/eod_report/`)

**Workload:** `batch/v1 CronJob`, schedule `30 13-16 * * *` with
`timeZone: America/New_York` (daily :30 checks from 13:30 through 16:30 ET), `concurrencyPolicy:
Forbid`, `backoffLimit: 1`. No ServiceAccount —
unlike Floor Broker (which has one, scoped to reading the kill-switch ConfigMap — see below), EOD
Report never touches the k8s API at all. Entrypoint: `python -m src.eod_report.main`. The
schedule checks several possible close+30min slots and `main()` sends only once after Alpaca's
official close has passed by 30 minutes: normal 16:00 closes report at 16:30 ET, while 13:00
early closes report at 13:30 ET. The schedule runs every day rather than `1-5` (Mon-Fri)
specifically so `main()`'s own Alpaca-calendar check (below) gets a chance to fire and post a
Slack notice on weekends/holidays — previously the cron schedule itself silently excluded
weekends, and a weekday holiday made `main()` log locally and return with no Slack post at all,
so a closed market was indistinguishable from the CronJob never running.

**Purpose:** once a day after market close, post a plain-language summary of the day — account
equity/cash/P&L and every fill across all three trading agents — to `#miramar-trading-floor`. It
makes no trading decisions (no LLM, no LangGraph); it only reads state that already exists in
Alpaca.

**Logic** (`src/eod_report/main.py`):
1. Checks Postgres' best-effort `eod_report_runs` marker for today's date. If already sent,
   exits before touching Alpaca; if Postgres is unavailable, it fails open so Slack is not
   blocked.
2. Checks `trading_client.get_calendar()` for today's date (Eastern) — if today wasn't a trading
   day (weekend or market holiday), it posts `slack.notify_market_closed("EOD", ...)`, records
   the best-effort run marker, and exits without the rest of the report, so a closed market is
   always visibly reported, not silent.
3. If today is a trading day but the official close+30min has not passed yet, exits silently so
   an earlier check slot does not post prematurely.
4. `trading_client.get_account()` — equity, cash, buying power, and `last_equity` (prior close)
   to compute the day's P&L.
5. `trading_client.get_all_positions()` — current open positions and their unrealized P&L.
6. `src.common.eod.fetch_fills(today)` — a raw REST call under the hood (no dedicated `alpaca-py`
   method exists for `/account/activities`) for every fill executed that day, across all assets.
7. `slack.notify_eod_report(...)` formats and posts all of the above as one message, then records
   the best-effort run marker to prevent later check slots from duplicating it.

Errors call `slack.notify_error("EOD", ...)` before re-raising, same convention as the other
three components.

## Backtesting harness (`src/backtest/`)

**Not a k8s workload** — a local CLI tool: `python -m src.backtest.main`. Runs deterministic
baseline strategies (buy-and-hold, simple RSI/MACD rules, multi-indicator, random, no-trade)
against historical Alpaca bars, reports total return, drawdown, Sharpe, win rate, expectancy,
and exposure per symbol, and writes a JSON artifact to the gitignored `backtests/` dir. It
computes indicators locally rather than replaying the live LLM's actual historical decisions —
see [`docs/backtesting.md`](backtesting.md) for the full design and documented assumptions.

## P/L badges (`src/pl_badges/`)

**Not a k8s workload** — run by a GHA workflow, `.github/workflows/pl-badges.yaml`
(`45 21 * * *` UTC, plus `workflow_dispatch`), on the same `[self-hosted, dgx]` runner as
`test-lint.yaml`. EOD Report also dispatches this workflow immediately after a successful Slack
EOD post when its pod has `GITHUB_WORKFLOW_TOKEN` set in the `mlabs-api-keys` secret; missing or
failing dispatch is logged as a warning and never blocks the EOD report itself. Entrypoint:
`python -m src.pl_badges.main`. Computes Today's and YTD aggregate P&L from the paper account
(`src.common.pl_badges.fetch_pl_summary()` — today's P&L is
`account.equity - account.last_equity`, the same math `execution.py`'s daily loss limit check
uses; YTD P&L is `equity - base_value` from a `get_portfolio_history()` request starting Jan 1 of
the current year — confirmed against a live account that `PortfolioHistory.profit_loss` is a
day-over-day delta series, not cumulative from `base_value`, so `base_value` is the only reliable
YTD anchor) and writes two shields.io endpoint-badge JSON files, `badges/today-pl.json` and
`badges/ytd-pl.json`. The workflow commits and pushes them back to `main` only if the content
changed. README.md's two P/L badges point at those files via
`img.shields.io/endpoint?url=.../raw.githubusercontent.com/...` — shields.io fetches the JSON
directly from GitHub's raw-content CDN at render time, so no publicly-reachable service needs to
run for the badges to work, unlike the always-on Floor Broker/Postgres this data is ultimately
sourced from. Skips the write (and therefore the commit) entirely on weekends/holidays via the
same `is_stock_market_open()` calendar check `eod_report.main()` uses.

## Shared code (`src/common/`)

- **`alpaca_client.py`** — one shared `TradingClient(..., paper=True)` (hardcoded — this is
  paper-trading only, no code path to live trading without a source change), plus
  `StockHistoricalDataClient`/`CryptoHistoricalDataClient` for market data, and
  `get_current_ask_price`/`get_current_bid_price` helpers used by Floor Broker for order sizing.
  Credentials come from `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_API_SECRET` env vars.
- **`config.py`** — `load_config()` fetches `config.yaml` fresh from
  `raw.githubusercontent.com/miramar-labs-org/multi-agent-ai-trader/main/config.yaml`
  (unauthenticated — the repo is public) instead of reading a local file, so every service
  reflects a `config.yaml` push within a short in-process cache TTL (`_REFRESH_SECS`, 60s) with no
  rebuild/redeploy. On a failed refetch, falls back to the last-known-good cached value with a
  warning (a transient GitHub blip must not crash a running trading system or block a Slack
  notification) — but fails closed (raises) if no cached value exists yet, since a fresh pod with
  no bundled fallback config genuinely cannot run without one. Long-running processes (Dealer's
  poll loop, Floor Broker's HTTP handlers, Slack's `_post()`) call `load_config()` fresh at each
  point of use rather than caching the returned object themselves — see `src/dealer/main.py`'s
  per-iteration reload, `src/dealer/graph.py`'s per-node-invocation reload, and
  `src/floor_broker/execution.py::buy()`'s per-call reload. One-shot processes (Analyst, EOD
  Report, Backtest) call it once per invocation, which is already as fresh as this can make them.
- **`portfolio_state.py`** — `read_portfolio()`/`write_portfolio()` against the k8s
  `portfolio` ConfigMap via the `kubernetes` Python client. This is the entire
  Analyst↔Dealer interface. `merge_held_positions()` additionally folds any Alpaca position
  not already in the watchlist (e.g. one opened before this app existed) into it on every
  Dealer poll — stock positions are only merged in as `exchange: "stocks"` when
  `cfg.trading.enable_stocks` is set; crypto positions are only merged in when
  `cfg.trading.enable_crypto` is set, tagged with `cfg.trading.crypto_taapi_exchange` as their
  TAAPI venue. Dealer's own poll loop applies the same two flags symmetrically via
  `should_process_entry()` (`src/dealer/main.py`), so a disabled market is skipped whether or
  not a stray position for it is already sitting in the ConfigMap.

  A merged entry's current market value is **observed exposure, not authorized new-BUY
  capital** — it's carried as `held_value`, while `budget` is set to `0.0` and `is_held_only:
  true` is set. This distinction exists because the position's value flowing straight through
  as `budget` would let a large held position silently re-authorize an equally large new BUY
  (or a shrunk one fall below Alpaca's crypto minimum notional — the original bug behind the
  crypto skip-vs-clamp fix above). `call_floor_broker` (`src/dealer/graph.py`) still lets the
  LLM decide BUY/HOLD/SELL for a held-only entry (using `held_value` as context), but refuses
  to forward a BUY when `budget <= 0` (`status="skipped", reason="no_authorized_budget"`)
  rather than sizing an order off it. Symbols Analyst actually picked keep their own real
  `budget` untouched by the merge step.
- **`logging.py`** — an emoji-prefixed stdout logger (ported from `gpt-trader.py`), still the
  only trail of *operational* detail (retries, warnings, non-decision errors); `kubectl logs`
  plus whatever LangSmith captured of the LLM call chain. Decision/execution history itself is
  durable now — see **`db.py`** below and [Persistence](#persistence).
- **`db.py`** — Postgres persistence for Analyst picks, Dealer decisions, and Floor Broker
  execution events, added in v0.6.0. See [Persistence](#persistence) for the schema and write
  contract.
- **`eod.py`** — `fetch_fills(date, only_crypto=None)` / `summarize_positions(positions,
  only_crypto=None)` shape Alpaca's raw position/activity objects into the plain dicts
  `slack.notify_eod_report`/`notify_crypto_eod_report` expect. Shared by the stock EOD Report (no
  filter — every asset) and the Analyst's crypto EOD node (`only_crypto=True`) so the fetch/shape
  logic isn't duplicated. The two functions filter differently because Alpaca's own API is
  internally inconsistent: `fetch_fills` filters on `"/" in symbol` since `/account/activities`
  fill records are always slash-formatted (e.g. `"BTC/USD"`), but `summarize_positions` filters on
  `p.asset_class == AssetClass.CRYPTO` since live `Position.symbol` for crypto has **no** slash
  (e.g. `"BTCUSD"`) — a `"/" in symbol` check on positions silently matches nothing. Confirmed
  against a live paper account after the crypto EOD report came back empty with zero positions.
  Each fill dict also carries `"time"` (Alpaca's raw `transaction_time`, an ISO 8601 UTC string),
  passed through unformatted — `slack._format_fill_time()` converts it to Eastern-clock-time for
  display only at the point each EOD report line is rendered. `summarize_positions()` also
  returns `unrealized_pl`, `avg_entry_price`, and `current_price` — the EOD Slack reports still
  only read the original four fields, but the Analyst's `fetch_position_pnl` node consumes all
  three of these for its live P&L snapshot (see [Agent 1 — Analyst](#agent-1--analyst)).
  `unrealized_pl` and `current_price` are `Optional[str]` on Alpaca's own `Position` model and
  pass through as `None` when absent rather than raising on `float(None)`.

## Persistence

Added in v0.6.0, closing the gap `docs/ROADMAP.md` P1.1 flagged: before this, the Dealer's
LLM reasoning for every BUY/HOLD/SELL was sent to Slack (`slack.notify_dealer_signal`) and
nowhere else — unrecoverable the moment the message scrolled off the channel. Postgres is a
**shared platform service**, not an app-local k8s resource — provisioned in the separate
`miramar-platform-gcp` repo at `dgx/k3s/postgres/` via the `deploy-postgres.yaml` /
`undeploy-postgres.yaml` GHA workflows, at `postgres.postgres-system.svc.cluster.local:5432`.
This app is one tenant of that shared instance, with its own database/role provisioned by the
same deploy workflow; the connection string is `DATABASE_URL` in the `mlabs-api-keys` secret
(see [Secrets](#secrets)).

`src/common/db.py` uses `psycopg[binary,pool]` directly — no ORM, no migration framework.
Schema is four tables (`analyst_picks`, `dealer_decisions`, `floor_broker_events`,
`position_opens`), created idempotently (`CREATE TABLE IF NOT EXISTS`) by `db.py` itself on
first use — there is no separate migrations step or Job. `dealer_decisions` records the
Dealer's decision only, with no execution-outcome columns; `/analyst-explain` correlates it to
`floor_broker_events` at query time by symbol + same-day timestamp proximity, not a shared
foreign key — deliberately, since a decision and its downstream execution event are written by
two different processes (Dealer, Floor Broker) that don't share a request context.
`position_opens` is different in kind from the other three — not an append-only event log, but
a single current-state row per open symbol (`symbol` primary key, `opened_at`), upserted on a
BUY fill and deleted on a SELL fill; it exists purely to answer "how long has this position
been open" for `check_eod_flatten()`'s conditional mode (below), not as a historical record.

**Write functions are fire-and-forget**, mirroring `slack.py::_post()`'s contract exactly:
they catch and log any exception, never raise. A Postgres outage must never block a trading
decision — this is why `db.py`'s connection pool is built lazily from `DATABASE_URL` on first
use rather than eagerly at import, unlike `alpaca_client.py`'s `trading_client` (a missing or
unreachable DB can't be allowed to crash import of every module that touches a decision, the
way a missing Alpaca credential legitimately should).

Write sites:
- `src/analyst/graph.py::write_portfolio()` — one `record_analyst_pick()` call per symbol in
  the day's selection, right after the `portfolio` ConfigMap write.
- `src/dealer/graph.py::call_floor_broker()` — one `record_dealer_decision()` call per Dealer
  decision, alongside `slack.notify_dealer_signal()`; then a `record_floor_broker_event()` call
  at each of that function's `slack.notify_floor_broker_result()` sites (BUY skipped for no
  budget, `/execute` error, `/execute` success).
- `src/floor_broker/main.py`'s two background poll loops (`poll_bracket_fills`,
  `poll_pending_fills`) — a `record_floor_broker_event()` call alongside each of their
  `slack.notify_floor_broker_result()` sites (fill, no-fill, synthetic crypto stop).
- `src/floor_broker/main.py::poll_pending_fills()` — additionally calls
  `record_position_opened()`/`record_position_closed()` on every BUY/SELL fill respectively,
  keeping `position_opens` in sync. `src/floor_broker/execution.py::reconcile_tracked_state_once()`
  also backfills it from `trading_client.get_all_positions()` on every process start (best-effort,
  `ON CONFLICT DO NOTHING` so it never overwrites an already-tracked symbol's `opened_at`) —
  this is how a position that predates the feature, or was opened in a restart gap, still ends up
  tracked.

There is no historical backfill — the tables start empty at deploy time; decisions made before
v0.6.1 are unrecoverable, same limitation as the Slack-only trail it replaces. (v0.6.0 shipped
a schema bug — `CREATE INDEX` on a `timestamptz::date` expression isn't `IMMUTABLE`, which
rolled back table creation entirely and silently wrote zero rows until the v0.6.1 fix; the
current schema indexes the raw `(symbol, timestamp)` columns instead.) Read access is
via two families of functions in `db.py`: `fetch_*_for_date()`, used by the read-only
`/analyst-explain` skill (`skills/analyst-explain/SKILL.md`) to explain a trading day's P&L
using the actual logged Dealer reasoning rather than a generic summary; and `fetch_*_since()`,
used by `fetch_track_record` (see [Agent 1 — Analyst](#agent-1--analyst-srcanalyst)) to feed
the Analyst's own recent pick history back into its LLM prompt — the first read path that
isn't purely for human/skill consumption.

## Data flow — one full cycle

1. **08:55 America/New_York** — Analyst CronJob pod starts (35min before the 9:30 ET open; the
   ~5min run typically finishes and posts the Morning Report ~09:00 ET, ~30min before the open).
   `main.py` checks the Alpaca calendar once; if the stock market is closed today, the run
   continues anyway (crypto still trades 24/7) rather than exiting early.
2. Discover ≤20 screener candidates (Alpaca `most-actives`/`movers`, skipped if the stock
   market is closed today) → fetch 2 days of news + Yahoo RSS headlines → LLM picks ≤10 symbols
   with budgets/indicators/rationale → written to the `portfolio` ConfigMap → a "Morning Market
   Report" (picks + account balance, prefixed with a closed-market banner if applicable) is
   posted to Slack, before market open → if `enable_crypto`, a crypto-only "Crypto EOD Report"
   covering the prior full ET day's crypto fills/positions is posted right after.
3. **Every 600s while the market is open** — Dealer reads the ConfigMap fresh, and for each
   symbol: fetches its configured indicators from TAAPI.io in one `/bulk` request (throttled
   `taapi.min_request_interval_secs` between symbols to respect TAAPI's per-15s rate limit),
   asks the LLM for BUY/HOLD/SELL, and (if not HOLD) POSTs to Floor Broker.
4. Floor Broker fetches a live quote, runs the position/order safety check, and submits a
   bracket order (stocks) or notional market order (crypto) to Alpaca's paper account.
5. Floor Broker's `{"status": "submitted"|"skipped"|"error"}` response is logged by Dealer and
   not persisted further — the eventual fill (`"executed"`, with `fill_price`) is reported later,
   asynchronously, via its own Slack post from `poll_pending_fills()` (ROADMAP P0.14).
6. If `analyst.enable_midday_run` is true, repeat step 2 once more at **12:30pm America/New_York**
   — the `analyst-midday` CronJob fires on the same schedule regardless, but `main()` exits
   immediately without this step when the flag is off (the default). This run posts a "🕐 Midday
   Update" instead of a second Morning Report, and skips the crypto EOD report entirely (already
   posted this morning).
7. Repeat step 3 until market close; the cycle restarts fresh at 08:55 America/New_York the next day using
   whatever portfolio the Analyst produces (or the prior day's, if the Analyst hasn't run yet
   or failed — the Dealer has no fallback logic here, it just reads whatever ConfigMap exists).
8. **13:30-16:30 America/New_York daily, at :30** — independently of the above cycle, the EOD
   Report CronJob checks Alpaca's official close and posts once when close+30min has passed. It
   queries Alpaca directly for the day's account state and fills, and posts a summary to Slack —
   or, on a weekend/holiday, posts a market-closed notice instead and skips the rest of the report.

## `config.yaml` reference

| Section | Field | Meaning |
|---|---|---|
| `llm` | `base_url`, `model`, `temperature` | shared OpenAI-compatible endpoint for **both** Analyst and Dealer LLM calls — see [platform-services.md](platform-services.md) for current wiring status |
| `langsmith` | `enabled`, `project` | toggles LangGraph/LangChain tracing to LangSmith (requires `LANGCHAIN_API_KEY`) |
| `langsmith` | `sampling_rate` | fraction of traces actually sent to LangSmith (0.5) — keeps Dealer's poll-driven trace volume under the free Developer plan's 5k traces/month limit |
| `slack` | `enabled` | toggles posting interesting events (Morning Report, Crypto EOD Report, Dealer signals, Floor Broker executions, EOD Report, EOD's non-trading-day notice, Dealer's live market-closed notice, errors) to `#miramar-trading-floor` (requires `SLACK_WEBHOOK_URL2`) — Dealer signals, Floor Broker executions, EOD's non-trading-day notice, and errors each carry a message-level Eastern-time timestamp; Morning Report, EOD Report, Crypto EOD Report, and Dealer's live market-closed notice do not. Dealer signal notices keep the LLM's full rationale on their own line, and a Floor Broker execution notice includes fill price/reason/SL/TP whenever `execution.py` supplies them; EOD Report/Crypto EOD Report fill lines each carry the fill's own Eastern-time timestamp even though the message as a whole doesn't |
| `floor_broker` | `base_url` | in-cluster Service DNS Dealer uses to reach Floor Broker |
| `taapi` | `min_request_interval_secs` | seconds Dealer and Analyst (`fetch_indicators`) each wait between symbols' TAAPI `/bulk` calls (15) — sized to the TAAPI Free plan's 1 request/15s cap; lower it if the account is on a paid plan |
| `trading` | `slP` / `tpP` | stop-loss/take-profit price multipliers on bracket orders (0.98/1.05 ≈ 2% stop, 5% target) |
| `trading` | `pollsecs` | Dealer loop cadence (600s) |
| `trading` | `buffer` | minutes to wait after market open before trading (15) |
| `trading` | `market_override` | force-treat-market-as-open, for testing outside market hours |
| `trading` | `enable_stocks` | when true, Analyst screens/picks equities, and Dealer processes/merges stock symbols; set false to pause equities handling entirely |
| `trading` | `enable_crypto` | when true, Analyst also screens/picks from a fixed crypto watchlist (`BTC/USD`, `ETH/USD`, `SOL/USD`) via `fetch_crypto_candidates()`, Dealer also polls merged-in crypto positions, and `merge_held_positions()` folds pre-existing crypto positions into the watchlist |
| `trading` | `crypto_taapi_exchange` | TAAPI venue name (e.g. `"binance"`) used as the `exchange` for crypto positions merged in by `merge_held_positions()` — TAAPI's `/bulk` API requires an actual venue, not the literal word "crypto" |
| `eod_flatten` | `enabled` | feature gate for `poll_eod_flatten()` (`src/floor_broker/main.py`) — when false (default), `check_eod_flatten()` short-circuits before touching the clock or Alpaca positions; opt-in "day trading mode" that closes every open stock position near market close instead of holding overnight |
| `eod_flatten` | `minutes_before_close` | how close (by Alpaca's live clock) to market close before `check_eod_flatten()` starts selling open stock positions (default 10) — crypto is 24/7 and untouched |
| `eod_flatten` | `conditional` | when true, `check_eod_flatten()` only flattens everything if the aggregate unrealized P&L across open stock positions is >= 0; when negative, positions are held overnight instead except any past `max_days_held_loss`. When false (default), always flattens everything, same as pre-`conditional` behavior |
| `eod_flatten` | `max_days_held_loss` | only consulted when `conditional: true` — a position held this many days or more is force-flattened regardless of the aggregate P&L sign, so a single loser can't ride indefinitely (default 5) |
| `earnings_blackout` | `enabled` | feature gate for the earnings-date filter in `discover_candidates` (`src/analyst/graph.py`) — `true` as of 2026-08-05; when false, the stock screener list is unfiltered by earnings dates and Finnhub is never called; requires `FINNHUB_API_KEY` (verified working 2026-08-05) |
| `earnings_blackout` | `days_before` | drop a screener candidate if it's reporting earnings within this many calendar days from today (default 2) — anticipation/IV-crush risk pre-report |
| `earnings_blackout` | `days_after` | drop a screener candidate if it reported earnings within this many calendar days before today (default 1) — post-report gap risk, covers both BMO and AMC reporters without a week-long exclusion |
| `macro_blackout` | `enabled` | feature gate for the macro-calendar check in `call_floor_broker` (`src/dealer/graph.py`) — `true` as of 2026-08-05; when false, new BUY entries proceed as today; SELL/HOLD/`eod_flatten` are never affected regardless of this flag |
| `macro_blackout` | `dates` | hand-maintained list of `{date, label}` scheduled macro releases (FOMC, CPI, NFP, PCE) that can move the whole market — published months ahead by the Fed/BLS/Commerce Dept, so no API is needed; a listed date pauses new stock BUYs for the entire day. Currently 18 real dates covering the rest of 2026 (sourced 2026-08-05); does not self-extend past its last entry, refreshed quarterly via a persistent memory reminder (next due ~2026-11-15) rather than a live lookup. Quarterly quad witching days (3rd Friday of Mar/Jun/Sep/Dec) are auto-computed in code (`_is_quad_witching_day`) and always included when `enabled`, with no config entry needed |
| `eod_report` | `schedule` | informational copy of the CronJob's own `spec.schedule` — daily check slots only; `src/eod_report/main.py` sends once at Alpaca official close+30min. Not templated, must be kept in sync manually |
| `analyst` | `schedule` | informational copy of the CronJob's own `spec.schedule` — not templated, must be kept in sync manually |
| `analyst` | `enable_midday_run` | feature gate for the optional `analyst-midday` CronJob (12:30pm ET) — when false (default), `main()` exits immediately on a midday-labeled run before `build_graph()` is called |
| `analyst` | `midday_schedule` | informational copy of `k8s/analyst-midday-cronjob-k3s.yaml`'s own `spec.schedule` — not templated, must be kept in sync manually |
| `analyst` | `max_universe_size`, `default_budget`, `screener_top_n`, `news_days`, `yahoo_rss_url` | Analyst's selection parameters |
| `analyst` | `max_total_budget_usd` | last-line-of-defense cap (default 50000 = `max_universe_size` × `default_budget`) on the sum of every pick's `budget` in one selection — `validate_selection` drops trailing picks (in the LLM's own returned order) once the running total would exceed it |
| `analyst` | `indicator_fetch_limit` | candidates (top-N by `abs(change_pct)`) that get a real TAAPI `/bulk` indicator fetch in `fetch_indicators` (default 15) — capped by the TAAPI free-tier 15s/request limit |
| `analyst` | `enable_news` | feature gate for `fetch_research` — when false, short-circuits before any Alpaca News/Yahoo RSS call and feeds the LLM an empty research text |
| `analyst` | `enable_indicators` | feature gate for `fetch_indicators` — when false, short-circuits before any TAAPI call and feeds the LLM an empty indicator text |
| `analyst` | `enable_track_record` | feature gate for `fetch_track_record` — when false, short-circuits before any Postgres read and feeds the LLM an empty track-record text |
| `analyst` | `track_record_days` | lookback window in calendar days for `fetch_track_record` (default 5); excludes today by construction, since the node runs before `write_portfolio`'s DB write in the same run |
| `analyst` | `enable_position_pnl` | feature gate for `fetch_position_pnl` — when false, short-circuits before any Alpaca positions call and feeds the LLM an empty P&L snapshot |
| `indicators` | list of `{name, properties}` | TAAPI.io query-parameter catalog per indicator, shared by Dealer |

## Risk controls and failure handling

- **Paper trading only** — hardcoded in `alpaca_client.py`, not a config toggle.
- **No duplicate positions** — Floor Broker's `buy()` refuses to open a new position if one
  (or an open order) already exists for that symbol.
- **Built-in per-trade loss cap** — every stock BUY is a bracket order with a stop-loss and
  take-profit leg; there is no unprotected stock position by construction.
- **Open-bell buffer** — Dealer waits 15 minutes after market open before trading, to avoid
  open-bell volatility.
- **No overlapping Analyst runs** (`concurrencyPolicy: Forbid`) and **no racing Dealer pods**
  (`strategy: Recreate`) — both exist specifically to protect the single shared `portfolio`
  ConfigMap from concurrent writes/reads.
- **Fail loud, don't retry-storm** — Analyst's CronJob has `backoffLimit: 1`; a failed
  research run surfaces immediately rather than being silently retried.
- **Per-symbol isolation in the Dealer loop** — one symbol's exception is caught and logged,
  never crashes the pod or skips the remaining symbols in that cycle.
- **Sell-side retry logic** — Floor Broker explicitly handles Alpaca's "conflicting orders"
  error by cancelling blockers and re-fetching the current quantity before resubmitting,
  rather than failing the sell outright.
- **Optional end-of-day flatten ("day trading mode")** — `eod_flatten.enabled` (default false);
  when on, `poll_eod_flatten()` sells every open stock position once Alpaca's live clock reports
  the market is within `eod_flatten.minutes_before_close` minutes of closing, so the bot never
  carries overnight stock risk. Crypto is 24/7 and excluded. Off by default, config-only toggle
  (no redeploy).
  - **Conditional variant** — `eod_flatten.conditional` (default false); when on, the flatten
    decision is gated on the aggregate unrealized P&L across all open stock positions: `>= 0`
    flattens everything (unchanged from the base behavior), negative holds everything overnight
    instead. `max_days_held_loss` caps how many days an individual losing position can be held
    this way before it's force-flattened regardless of the aggregate sign — backed by the new
    `position_opens` table (see [Persistence](#persistence)).
- **Per-symbol earnings blackout** — `earnings_blackout.enabled: true` as of 2026-08-05; when on,
  `discover_candidates` drops any stock screener candidate reporting earnings within
  `earnings_blackout.days_before`/`days_after` calendar days of today (source: a single
  market-wide Finnhub free-tier call, since Alpaca has no earnings-calendar data — verified live
  with a real key on 2026-08-05, 1500 market-wide entries returned for a one-week window). Fails
  soft to an unfiltered candidate list on any Finnhub error (missing key, network error, non-200,
  429, bad JSON) — a Finnhub outage never blocks or crashes the Analyst run. Crypto candidates
  are never filtered (no earnings dates apply). Config-only toggle (no redeploy).
- **Market-wide macro blackout** — `macro_blackout.enabled: true` as of 2026-08-05; when on,
  `call_floor_broker` refuses any BUY signal locally (never forwarded to Floor Broker) on a day
  matching either a hand-maintained `macro_blackout.dates` entry (FOMC, CPI, NFP, PCE — 18 real
  dates covering the rest of 2026, sourced from federalreserve.gov/bls.gov/bea.gov) or an
  auto-computed quarterly quad witching day (3rd Friday of March/June/September/December — the
  simultaneous expiration of stock options, index options, and index futures). SELL/HOLD and
  `eod_flatten` are never affected — only new BUY entries pause. Config-only toggle (no
  redeploy). The date list is static and does not self-extend past 2026 — a persistent memory
  note (next refresh due ~2026-11-15) re-runs the sourcing process each quarter (see
  `docs/ROADMAP.md`'s P1.13 entry) rather than the live app scraping government calendar pages
  at runtime.
- **TAAPI stays inside its rate limit** — Dealer fetches all of a symbol's indicators in one
  `/bulk` POST instead of one GET per indicator (up to 9 individual calls per symbol would blow
  through TAAPI's per-15s rate limit — even on the Pro plan — the moment two symbols overlapped),
  and throttles `taapi.min_request_interval_secs` between symbols so the whole Dealer loop stays
  inside whatever plan is configured, still comfortably inside the 600s poll cadence.
- **LangSmith trace volume is sampled** — `langsmith.sampling_rate` (0.5) keeps Dealer's
  poll-driven trace count under the free Developer plan's 5k traces/month allowance; set via
  `LANGSMITH_TRACING_SAMPLING_RATE`, wired centrally in `src/common/langsmith.py`.
- **Deploys always force a rollout** — `deploy.yaml` always resolves images to the same
  `:latest` tag string, so `kubectl apply` alone would see no pod-template diff and silently
  leave Dealer/Floor Broker pods on stale code. The `dealer`/`floor-broker` k3s manifests carry
  a `deploy.miramar/commit-sha` pod annotation that `deploy.yaml` stamps with `github.sha` on
  every run, guaranteeing a real diff and a rollout each deploy. CronJobs (Analyst, EOD Report)
  don't need this — every scheduled run already creates a brand-new pod.

## Secrets

All credentials live in one k8s Secret, `mlabs-api-keys` (documented, not created, by
`k8s/secrets.example.yaml` — deploy fails fast if it's missing): `TAAPI_API_KEY`,
`ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`, `LANGCHAIN_API_KEY`, `SLACK_WEBHOOK_URL2`,
`DATABASE_URL` (see [Persistence](#persistence) — provisioned by `miramar-platform-gcp`'s
`deploy-postgres.yaml`, not created by this repo), `FINNHUB_API_KEY` (earnings-calendar lookups
for `earnings_blackout`, only called when that feature is enabled).
Analyst and Dealer get it via `envFrom.secretRef` for both k8s-API access (via their shared
ServiceAccount) and these external API keys. Floor Broker also has a ServiceAccount
(`multi-agent-ai-trader-configmap-reader`, scoped to reading the `buy-kill-switch` ConfigMap)
plus the secret for its Alpaca keys and `SLACK_WEBHOOK_URL2`. EOD Report gets the same secret but
has no ServiceAccount/k8s API access at all.

`pl-badges.yaml` (see [P/L badges](#p-l-badges-src-pl_badges)) runs outside the cluster on a GHA
self-hosted runner, not as a k8s workload, so it can't read `mlabs-api-keys` — it needs its own
copy of `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_API_SECRET` as GitHub Actions repo secrets
(`secrets.ALPACA_PAPER_API_KEY`/`secrets.ALPACA_PAPER_API_SECRET`), same pattern as
`MIRAMAR_ORG_GHCR_PAT` for `build-push.yaml`/`deploy.yaml`.

## `notebook.ipynb`

Unlike other Miramar KFP projects, this notebook is **not** a pipeline definition — it is a
single markdown cell, gitignored and untracked (`af6b54d`), used only for local ad-hoc
interactive exploration. The README's "Open in JupyterLab" badge that used to link to it was
removed since nothing in this project's actual workflow depends on notebooks. There is no KFP
involvement anywhere in this project (see [platform-services.md](platform-services.md)).

## Provenance

Several code comments (`src/common/logging.py`, `config.yaml`'s indicator-catalog comment,
`src/dealer/schema.py`) indicate this project is a re-platforming of an earlier monolithic
script, `gpt-trader.py`, onto three independently-scaled k8s workloads. The indicator
catalog shape and the `Signal` schema (replacing a hand-rolled `xtractjson()` parsing hack)
are both direct carryovers from that script.
