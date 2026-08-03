# Architecture

`multi-agent-ai-trader` is a three-agent trading floor — **Analyst**, **Dealer**, **Floor
Broker** — that trades US equities (paper account) on Alpaca, deployed as independent
Kubernetes workloads on the DGX Spark k3s cluster. It is a re-platforming of an earlier
single-process script (`gpt-trader.py`) onto three independently-scaled k8s workloads that
communicate via a shared ConfigMap and plain HTTP, rather than a monolithic loop.

```
                         06:00 UTC daily (k8s CronJob)
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

Analyst and Floor Broker never talk to each other directly. There is no message queue, no
database, and no shared filesystem — the only durable state between agents is the
`portfolio` ConfigMap, and the only network hop is Dealer → Floor Broker.

Independently of that cycle, a fourth CronJob queries Alpaca directly once a day after market
close and posts a summary to Slack — it has no dependency on the ConfigMap or on Dealer/Floor
Broker's HTTP hop:

```
                         21:30 UTC Mon-Fri (k8s CronJob)
                         ┌─────────────────────────┐
                         │       EOD Report        │
                         │ account + fills → Slack │
                         └─────────────────────────┘
```

## Repo layout

```
multi-agent-ai-trader/
├── config.yaml                  # single source of config for all agents + EOD report
├── notebook.ipynb                # bare JupyterLab entry point (no pipeline logic — see below)
├── Dockerfile.analyst/.dealer/.floor-broker/.eod-report
├── k8s/                          # 2 CronJobs, 2 Deployments, 1 Service, RBAC, namespace, secrets doc
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

**Workload:** `batch/v1 CronJob`, schedule `0 6 * * *` (06:00 UTC, before US market open),
`concurrencyPolicy: Forbid` (no overlapping runs), `backoffLimit: 1` — a failed research run
is meant to surface immediately, not retry-storm. Entrypoint: `python -m src.analyst.main`.

**Purpose:** once a day, decide *which symbols are worth trading today* and hand that list
off to the Dealer.

**Implementation:** a 4-node LangGraph state machine (`src/analyst/graph.py`) over an
`AnalystState`:

| Node | What it does |
|---|---|
| `discover_candidates` | `sources.fetch_screener_candidates(screener_top_n=20)` — raw REST calls (not wrapped by `alpaca-py`) to Alpaca's `/v1beta1/screener/stocks/most-actives` and `/movers` endpoints, merged into a symbol→{volume, change_pct} dict |
| `fetch_research` | `sources.fetch_news(news_days=2)` (Alpaca News API, HTML stripped via BeautifulSoup) + `sources.fetch_yahoo_rss_headlines(...)` (Yahoo Finance RSS), concatenated into plain text |
| `llm_select` | the actual LLM call — see below |
| `write_portfolio` | patches the `portfolio` ConfigMap via the `kubernetes` Python client |

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
that entirely.

**Output — the `portfolio` ConfigMap** (namespace `multi-agent-ai-trader`):
```json
{
  "generated_at": "2026-08-01T06:00:03Z",
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

**Graph** (`src/dealer/graph.py`), a 3-node state machine over `DealerState`:

| Node | What it does |
|---|---|
| `fetch_indicators` | for each indicator configured for the symbol (or all of `cfg.indicators` if the entry says `["ALL"]`), calls the matching function in `indicators.py`, which queries **TAAPI.io** — a third-party technical-analysis API, unrelated to any Miramar platform service — and builds a natural-language indicator text block |
| `llm_call` | the decision LLM call — see below |
| `call_floor_broker` | HTTP POST to Floor Broker if action != HOLD |

**LLM call:** same pattern as Analyst — `ChatOpenAI(base_url=cfg.llm.base_url, ...).with_structured_output(Signal)`.
System prompt: *"You are an expert technical trader in stocks. Based on the values of ALL
of the indicators below, decide if you should BUY, SELL, or HOLD."* The `Signal` model
(`src/dealer/schema.py`) is `{symbol, action: BUY|HOLD|SELL, reasoning, size_hint}` —
`size_hint` (a 0–1 fraction) is captured in the schema but **not currently consumed**;
`call_floor_broker` forwards the symbol's configured `budget` unmodified regardless of
`size_hint`.

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

## Agent 3 — Floor Broker (`src/floor_broker/`)

**Workload:** `apps/v1 Deployment` + `ClusterIP Service` on port 8000. No ServiceAccount —
unlike Analyst/Dealer it never touches the k8s API (it doesn't read/write the portfolio
ConfigMap). Entrypoint: `uvicorn.run("src.floor_broker.app:app", host="0.0.0.0", port=8000)`.

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

**Order logic** (`src/floor_broker/execution.py`):
- **`buy()`** — safety gate first: if an open position or open order already exists for the
  symbol, the buy is skipped ("aborting BUY") rather than pyramiding. For stocks, submits a
  **bracket order** (`OrderClass.BRACKET`) with computed stop-loss (`mid_price * slP`) and
  take-profit (`mid_price * tpP`) legs, `TimeInForce.DAY`. For crypto symbols (`/` in the
  ticker), submits a plain notional market buy instead (`TimeInForce.GTC`) — bracket orders
  aren't used for crypto.
- **`sell()`** — sells the full open quantity at market. Has an explicit **retry-after-cleanup**
  path: if Alpaca rejects with error code `40310000` (conflicting orders blocking the sell),
  it cancels the blocking orders (ignoring 404s), *re-fetches* the now-current open quantity
  (it can change once blockers clear), and resubmits.

## EOD Report (`src/eod_report/`)

**Workload:** `batch/v1 CronJob`, schedule `30 21 * * 1-5` (21:30 UTC Mon-Fri — after the 4pm ET
close in both EDT and EST), `concurrencyPolicy: Forbid`, `backoffLimit: 1`. No ServiceAccount —
like Floor Broker, it never touches the k8s API. Entrypoint: `python -m src.eod_report.main`.

**Purpose:** once a day after market close, post a plain-language summary of the day — account
equity/cash/P&L and every fill across all three trading agents — to `#miramar-trading-floor`. It
makes no trading decisions (no LLM, no LangGraph); it only reads state that already exists in
Alpaca.

**Logic** (`src/eod_report/main.py`):
1. Checks `trading_client.get_calendar()` for today's date (Eastern) — if today wasn't a trading
   day (market holiday; weekends are already excluded by the cron schedule itself), it logs and
   exits without posting, so the channel never gets a noisy "nothing happened" message.
2. `trading_client.get_account()` — equity, cash, buying power, and `last_equity` (prior close)
   to compute the day's P&L.
3. `trading_client.get_all_positions()` — current open positions and their unrealized P&L.
4. `trading_client.get("/account/activities", data={"activity_types": "FILL", "date": ...})` — a
   raw REST call (no dedicated `alpaca-py` method exists for this endpoint) for every fill
   executed that day.
5. `slack.notify_eod_report(...)` formats and posts all of the above as one message.

Errors call `slack.notify_error("EOD", ...)` before re-raising, same convention as the other
three components.

## Shared code (`src/common/`)

- **`alpaca_client.py`** — one shared `TradingClient(..., paper=True)` (hardcoded — this is
  paper-trading only, no code path to live trading without a source change), plus
  `StockHistoricalDataClient`/`CryptoHistoricalDataClient` for market data, and
  `get_current_ask_price`/`get_current_bid_price` helpers used by Floor Broker for order sizing.
  Credentials come from `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_API_SECRET` env vars.
- **`config.py`** — loads `config.yaml` once via OmegaConf; every agent imports the same
  loader, so there is exactly one config schema for the whole system.
- **`portfolio_state.py`** — `read_portfolio()`/`write_portfolio()` against the k8s
  `portfolio` ConfigMap via the `kubernetes` Python client. This is the entire
  Analyst↔Dealer interface.
- **`logging.py`** — an emoji-prefixed stdout logger (ported from `gpt-trader.py`) — the only
  durable trail of a trading decision is `kubectl logs` output plus whatever LangSmith
  captured of the LLM call chain; trade outcomes are not written to any database or MLflow.

## Data flow — one full cycle

1. **06:00 UTC** — Analyst CronJob pod starts.
2. Discover ≤20 screener candidates (Alpaca `most-actives`/`movers`) → fetch 2 days of news +
   Yahoo RSS headlines → LLM picks ≤10 symbols with budgets/indicators/rationale → written to
   the `portfolio` ConfigMap → a "Morning Market Report" (picks + account balance) is posted to
   Slack, still before market open.
3. **Every 600s while the market is open** — Dealer reads the ConfigMap fresh, and for each
   symbol: fetches its configured indicators from TAAPI.io, asks the LLM for BUY/HOLD/SELL,
   and (if not HOLD) POSTs to Floor Broker.
4. Floor Broker fetches a live quote, runs the position/order safety check, and submits a
   bracket order (stocks) or notional market order (crypto) to Alpaca's paper account.
5. Floor Broker's `{"status": "executed"|"skipped"|"error"}` response is logged by Dealer and
   not persisted further.
6. Repeat step 3 until market close; the cycle restarts fresh at 06:00 UTC the next day using
   whatever portfolio the Analyst produces (or the prior day's, if the Analyst hasn't run yet
   or failed — the Dealer has no fallback logic here, it just reads whatever ConfigMap exists).
7. **21:30 UTC Mon-Fri** — independently of the above cycle, the EOD Report CronJob queries
   Alpaca directly for the day's account state and fills, and posts a summary to Slack.

## `config.yaml` reference

| Section | Field | Meaning |
|---|---|---|
| `llm` | `base_url`, `model`, `temperature` | shared OpenAI-compatible endpoint for **both** Analyst and Dealer LLM calls — see [platform-services.md](platform-services.md) for current wiring status |
| `langsmith` | `enabled`, `project` | toggles LangGraph/LangChain tracing to LangSmith (requires `LANGCHAIN_API_KEY`) |
| `slack` | `enabled` | toggles posting interesting events (Morning Report, Dealer signals, Floor Broker executions, EOD Report, errors) to `#miramar-trading-floor` (requires `SLACK_WEBHOOK_URL`) |
| `floor_broker` | `base_url` | in-cluster Service DNS Dealer uses to reach Floor Broker |
| `trading` | `slP` / `tpP` | stop-loss/take-profit price multipliers on bracket orders (0.98/1.05 ≈ 2% stop, 5% target) |
| `trading` | `pollsecs` | Dealer loop cadence (600s) |
| `trading` | `buffer` | minutes to wait after market open before trading (15) |
| `trading` | `market_override` | force-treat-market-as-open, for testing outside market hours |
| `eod_report` | `schedule` | informational copy of the CronJob's own `spec.schedule` — not templated, must be kept in sync manually |
| `analyst` | `schedule` | informational copy of the CronJob's own `spec.schedule` — not templated, must be kept in sync manually |
| `analyst` | `max_universe_size`, `default_budget`, `screener_top_n`, `news_days`, `yahoo_rss_url` | Analyst's selection parameters |
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
- **Deploys always force a rollout** — `deploy.yaml` always resolves images to the same
  `:latest` tag string, so `kubectl apply` alone would see no pod-template diff and silently
  leave Dealer/Floor Broker pods on stale code. The `dealer`/`floor-broker` k3s manifests carry
  a `deploy.miramar/commit-sha` pod annotation that `deploy.yaml` stamps with `github.sha` on
  every run, guaranteeing a real diff and a rollout each deploy. CronJobs (Analyst, EOD Report)
  don't need this — every scheduled run already creates a brand-new pod.

## Secrets

All credentials live in one k8s Secret, `mlabs-api-keys` (documented, not created, by
`k8s/secrets.example.yaml` — deploy fails fast if it's missing): `TAAPI_API_KEY`,
`ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`, `LANGCHAIN_API_KEY`, `SLACK_WEBHOOK_URL`.
Analyst and Dealer get it via `envFrom.secretRef` for both k8s-API access (via their shared
ServiceAccount) and these external API keys; Floor Broker and EOD Report get the same secret for
their Alpaca keys and `SLACK_WEBHOOK_URL` — neither has a ServiceAccount/k8s API access at all.

## `notebook.ipynb`

Unlike other Miramar KFP projects, this notebook is **not** a pipeline definition — it is a
single markdown cell serving only as the target of the README's "Open in JupyterLab" badge,
for ad-hoc interactive exploration. There is no KFP involvement anywhere in this project (see
[platform-services.md](platform-services.md)).

## Provenance

Several code comments (`src/common/logging.py`, `config.yaml`'s indicator-catalog comment,
`src/dealer/schema.py`) indicate this project is a re-platforming of an earlier monolithic
script, `gpt-trader.py`, onto three independently-scaled k8s workloads. The indicator
catalog shape and the `Signal` schema (replacing a hand-rolled `xtractjson()` parsing hack)
are both direct carryovers from that script.
