# Roadmap

This roadmap prioritizes three outcomes in order:

1. **Safe and reproducible paper-trading execution**
2. **Measurable evidence of strategy quality**
3. **Production-grade platform controls and rollout discipline**

P0 hardens the existing execution path without expanding trading scope. P1 adds durable evidence, replay, and evaluation capabilities. P2 adds deeper observability, security, scaling, and controlled rollout mechanisms.

All trading remains **paper-only**. SELL operations that reduce exposure should remain available whenever possible, even when BUY operations are blocked by policy or emergency controls.

---

## Status

| ID | Item | Priority | Status | Depends on |
|---|---|---:|---|---|
| P0.1 | Skip crypto orders below minimum notional | P0 | Done | — |
| P0.2 | Validate merged-position budget semantics | P0 | Done | P0.1 |
| P0.3 | Constrained Floor Broker request schema | P0 | Partial (see below) | — |
| P0.4 | Centralized Floor Broker risk policy | P0 | Partial (narrower mechanism built; see below) | P0.3 |
| P0.5 | Runtime BUY kill switch | P0 | Done (required behavior only; see below) | P0.4 |
| P0.6 | Serialize risk check and order submission | P0 | Planned (see below) | P0.4 |
| P0.7 | Idempotent execution and Alpaca client order IDs | P0 | Planned | P0.3 |
| P0.8 | Single-quote stock sizing | P0 | Done | — |
| P0.9 | Quantity and bracket-price invariant checks | P0 | Done | P0.8 |
| P0.10 | Structured execution outcomes and HTTP contract | P0 | Planned | P0.3 |
| P0.11 | Dependency split and reproducible pinning | P0 | Planned | — |
| P0.12 | CI, linting, validation, and image-build checks | P0 | Partial (pytest + ruff check only; see below) | P0.11 |
| P0.13 | Baseline container security | P0 | Planned | P0.11 |
| P0.14 | Asynchronous order submission and fill reporting | P0 | Done | — |
| P1.1 | Durable decision and event schema | P1 | Partial (3-table MVP live; see below) | P0 complete |
| P1.2 | Exact model, prompt, and input version capture | P1 | Planned | P1.1 |
| P1.3 | Shadow execution mode | P1 | Planned | P1.1 |
| P1.4 | Forward evaluation and replay | P1 | Planned | P1.1, P1.3 |
| P1.5 | Historical backtesting harness | P1 | Done (deterministic baselines only; see below) | P1.1 |
| P1.6 | Deterministic baseline strategies | P1 | Done | P1.4 or P1.5 |
| P1.7 | Resolve `size_hint` semantics | P1 | Done | P1.1 |
| P1.8 | Daily loss, trade-count, and aggregate exposure controls | P1 | Partial (daily P&L halt only; see below) | P0.4, P1.1 |
| P1.9 | Core operational metrics | P1 | Planned | P1.1 |
| P1.14 | Multi-modal Dealer: OHLCV enrichment + AGX-Orin Visual Analyst (shadow) | P1 | Planned (full plan drafted, approved, not started; see below) | P1.1 |
| P1.15 | Analyst candidate-mix: fixed large-cap/crypto/screener pool ratio | P1 | Done | — |
| P1.16 | Options trading via MCP contract selection | P1 | Done | P1.1 |
| P2.1 | Full Prometheus and Grafana observability | P2 | Planned | P1.9 |
| P2.2 | Automatic circuit breakers | P2 | Planned | P1.8, P1.9 |
| P2.3 | Distributed execution coordination | P2 | Planned | Future scaling need |
| P2.4 | Kubernetes NetworkPolicies and hardened runtime controls | P2 | Planned | P0.13 |
| P2.5 | Image signing, SBOM, and verification | P2 | Planned | P0.12 |
| P2.6 | Canary strategy and model rollout | P2 | Planned | P1.3, P1.4 |
| P2.7 | Durable messaging, only if justified | P2 | Planned | Recovery or scale requirement |

---

# P0 — Safe and reproducible execution

P0 is complete when the current paper-trading system can reject unsafe commands deterministically, avoid duplicate submissions, reproduce its builds, and prove those behaviors in CI.

## P0.1 — Skip crypto orders below minimum notional

### Problem

`src/floor_broker/execution.py::buy()` currently raises a crypto notional below Alpaca's minimum to `MIN_CRYPTO_NOTIONAL`. That can exceed the caller's intended budget.

### Change

When the rounded crypto notional is below the brokerage minimum:

- do not submit an order;
- return `status="skipped"`;
- return `reason="budget_below_minimum"`;
- include the requested and minimum notionals in structured details;
- notify Slack only if the existing notification policy treats the event as operationally relevant.

### Acceptance criteria

- A requested crypto BUY below the minimum produces no Alpaca submission.
- The returned result is deterministic and machine-readable.
- Unit tests cover values below, equal to, and above the minimum.
- `docs/architecture.md` describes skip behavior rather than clamp behavior.

---

## P0.2 — Validate merged-position budget semantics

### Problem

A merged existing crypto position may carry its current `market_value` into the Dealer portfolio entry. That value must not accidentally be interpreted as authorization to purchase the same amount again.

### Change

Trace the full path through:

- held-position discovery;
- portfolio merge;
- Dealer state construction;
- `call_floor_broker`;
- Floor Broker request creation.

Define separate fields when necessary:

- current position market value;
- maximum additional BUY budget;
- SELL-only tracking state.

### Acceptance criteria

- Existing position value is never implicitly reused as a new BUY budget.
- A held position can remain in the watchlist for SELL/HOLD decisions without authorizing an unintended additional BUY.
- Regression tests cover held crypto and stock positions.
- Portfolio schema documentation distinguishes observed exposure from authorized new capital.

---

## P0.3 — Constrained Floor Broker request schema

**Partial.** `ExecuteRequest` (`src/floor_broker/app.py`) now normalizes and constrains `symbol`
(stripped, uppercased, letters/digits with at most one `/`), `exchange` (stripped, lowercased,
`stocks` or an identifier shape — not a fixed enum, since `cfg.trading.crypto_taapi_exchange` is
config-driven), `budget` (`0 < budget <= MAX_BUDGET`, a 100,000 sanity ceiling — 20x
`config.yaml`'s `analyst.default_budget`), `slP` (`0 < slP < 1`), and `tpP` (`1 < tpP < 2`), and
rejects unknown fields (`extra="forbid"`). `action` was already a `Literal["BUY","SELL"]`.
Invalid requests fail FastAPI's own validation (422) before `execution.buy()`/`sell()` is ever
called. Tests: `tests/floor_broker/test_app.py`.

Not yet done — deliberately deferred, not forgotten: a required `decision_id` field, which needs
Dealer's request payload changed too and is really P0.7's (idempotent execution) concern, not
just a schema constraint. Revisit as part of P0.7.

### Problem

`ExecuteRequest` currently validates types but does not enforce meaningful domain constraints.

### Change

Use Pydantic constraints and validators for:

- non-empty normalized symbol;
- allowed asset class or exchange identifier;
- `budget > 0`;
- bounded budget;
- `0 < slP < 1`;
- `tpP > 1`;
- bounded `tpP`;
- supported actions;
- required `decision_id`;
- optional portfolio or strategy version metadata.

Reject unknown fields unless forward compatibility requires otherwise.

### Acceptance criteria

- Invalid requests fail before execution code is called.
- Tests cover negative, zero, out-of-range, malformed, and unsupported values.
- Validation errors use a stable response shape.
- OpenAPI documentation reflects the constraints.

---

## P0.4 — Centralized Floor Broker risk policy

**Partial — a narrower mechanism was built instead of this item's full spec.** Rather
than a dedicated `src/floor_broker/risk.py` module with exposure-notional caps
(`max_order_notional`/`max_symbol_exposure`/`max_total_exposure`/`max_open_positions`/
`min_buying_power_reserve`), `/configure-strategy` (`skills/configure-strategy/SKILL.md`)
and a `strategy:` config block cover only what that wizard asks about: a daily
profit/loss halt and crypto synthetic stop-loss/take-profit, both enforced inline in
`src/floor_broker/execution.py::buy()`/`check_pending_fills()`/`check_crypto_stops()` —
see `docs/strategy.md` for the full design log. This was an explicit scope decision
(narrower ask, narrower build) — the exposure-cap/notional-limit design below remains
valid future work if that fuller scope is ever wanted; it has not been implemented.

### Problem

The Dealer proposes trades, but the Floor Broker must be the final authority on whether a BUY is permissible.

### Change

Add `src/floor_broker/risk.py` and a `risk:` configuration block.

Initial controls:

```yaml
risk:
  max_order_notional: 5000
  max_symbol_exposure: 10000
  max_total_exposure: 25000
  max_open_positions: 10
  min_buying_power_reserve: 5000
```

For a proposed BUY, evaluate:

```text
current position market value
+ open BUY order exposure
+ proposed order exposure
```

Also evaluate:

- total open exposure;
- open positions plus pending BUYs;
- available buying power after the proposed order;
- policy configuration validity.

SELL operations are not blocked by BUY-side exposure limits.

### Failure behavior

BUY risk checks fail closed when required Alpaca account, position, or order state cannot be verified.

### Acceptance criteria

- Every BUY passes through the risk module immediately before submission.
- SELL remains available when BUY risk policy blocks new exposure.
- Open orders are included in exposure calculations.
- Tests cover each limit independently and in combination.
- Tests cover Alpaca state-fetch failure and confirm BUY fails closed.
- Risk-denial results include a stable `reason` and measured values.

---

## P0.5 — Runtime BUY kill switch

**Done, required behavior only** — the optional follow-up (cancel open BUY orders on
activation) is deliberately out of scope. `src/common/kill_switch.py::buy_kill_switch_active()`
reads a new `buy-kill-switch` ConfigMap (`data.active`, `"true"`/`"false"`) fresh on every call
via the `kubernetes` client — no caching, so an operator's `kubectl patch` takes effect on the
very next `/execute` BUY request without a redeploy. A missing ConfigMap (404) fails open
(treated as inactive, since the deploy workflow always seeds it — a 404 means a setup gap, not a
deliberate activation); any other k8s API error propagates. `execution.buy()` checks this first,
before any position/order lookup, and returns `status="skipped", reason="buy_kill_switch_active"`
without ever calling Alpaca; `sell()` is untouched, so SELL stays available. Floor Broker now
uses the existing shared `multi-agent-ai-trader-configmap-reader` ServiceAccount/Role/RoleBinding
(`k8s/rbac.yaml`) rather than a new one, since its verbs already cover the required read.
`src/floor_broker/main.py::poll_kill_switch()` is a second daemon thread (mirroring
`poll_bracket_fills()`) that posts a Slack notice (`slack.notify_buy_kill_switch`) only on a
state transition, not on every 30s poll. The ConfigMap is seeded once via
`k8s/buy-kill-switch-configmap.yaml` and a new `.github/workflows/deploy.yaml` step (same
seed-once pattern as the `portfolio` ConfigMap — never clobbered on redeploy). Runbook commands
are documented in `docs/architecture.md`'s Floor Broker section and in
`k8s/buy-kill-switch-configmap.yaml` itself. Tests: `tests/common/test_kill_switch.py` (active/
inactive/missing-key/404-fail-open/non-404-raises), `tests/floor_broker/test_execution.py`
(`test_buy_skips_with_kill_switch_reason_when_active_and_touches_no_client`,
`test_sell_still_permitted_when_kill_switch_active`), `tests/floor_broker/test_floor_broker_main.py`
(`test_poll_kill_switch_notifies_only_on_transition` and related), and
`tests/common/test_slack.py` (`test_notify_buy_kill_switch_*`).

### Problem

An emergency switch stored only in image-baked `config.yaml` is too slow to operate.

### Change

Implement a runtime-controlled BUY kill switch using a dedicated Kubernetes ConfigMap or equivalent runtime source that Floor Broker reads on every execution request.

Required behavior:

- block new BUY orders;
- continue to permit SELL;
- keep `/healthz` operational;
- return `status="skipped"` and `reason="buy_kill_switch_active"`;
- emit an operational notification when the switch changes state;
- document the exact enable and disable commands.

Optional follow-up behavior:

- cancel open BUY orders when the switch is activated.

### Acceptance criteria

- The switch can be changed without rebuilding an image.
- The changed state takes effect for the next execution request.
- BUY is blocked and SELL is still allowed.
- Operational runbook commands are documented and tested.

---

## P0.6 — Serialize risk check and order submission

**Still planned, not built.** The daily-halt/crypto-stop mechanism built for P0.4/P1.8
(see those sections) has no account-wide lock — like the rest of `execution.py`, it
reads state and acts on it without serializing against concurrent requests. This was a
deliberate scope decision (the user explicitly asked for "nothing more complex" than a
configurable strategy) rather than an oversight; it carries the same
concurrent-request race this item describes and remains applicable if/when it's
prioritized.

### Problem

Concurrent requests can independently read the same account state, pass risk checks, and then submit orders that jointly exceed policy.

### Change

For the current single-replica Floor Broker, serialize the critical section:

```text
load runtime controls
→ fetch account/order/position state
→ evaluate risk
→ submit order
```

Use an account-wide in-process lock. Per-symbol locks may be added later only if account-wide policy remains correct.

Document that horizontal Floor Broker scaling is unsupported until P2 distributed coordination exists.

### Acceptance criteria

- Concurrent BUY tests prove that only requests permitted by final combined exposure are submitted.
- The critical section includes both policy evaluation and submission.
- SELL behavior is documented during concurrent BUY activity.
- Deployment remains one replica unless distributed coordination is implemented.

---

## P0.7 — Idempotent execution and Alpaca client order IDs

### Problem

A request can be submitted successfully to Alpaca while the response is lost. Retrying without an idempotency key can create a duplicate order.

### Change

Dealer generates a stable `decision_id` once per actionable decision. The identifier should include enough context to distinguish legitimate later decisions, for example:

```text
portfolio_version
+ dealer_cycle_timestamp
+ symbol
+ action
+ strategy_version
```

Floor Broker:

- requires `decision_id`;
- derives a deterministic Alpaca-compatible `client_order_id`;
- checks for an existing matching order before submitting;
- treats a repeated request as an idempotent success or stable duplicate outcome;
- never creates a second order for the same `decision_id`.

Durable cross-restart event storage remains P1, but Alpaca order lookup must provide immediate duplicate protection.

### Acceptance criteria

- Repeating an identical request does not create a second Alpaca order.
- A lost-response simulation followed by retry returns the existing order result.
- A later legitimate decision for the same symbol can use a new `decision_id`.
- Tests cover BUY, SELL, timeout/retry, and existing-order cases.

---

## P0.8 — Single-quote stock sizing

**Done.** `execution.get_qty()` now takes `ask` as a parameter instead of fetching its own
quote; `bracket_buy_with_SLTP()` fetches `ask` once and passes that same value into `get_qty()`
and the TP/SL calculation. The `base_price` retry path recomputes qty and prices from the same
`base_price` reference. Tests: `test_bracket_buy_uses_a_single_quote_for_qty_and_prices`
(asserts `get_current_ask_price()` is called exactly once) in
`tests/floor_broker/test_execution.py`.

### Problem

Bracket price calculation and quantity sizing currently fetch independent ask prices.

### Change

For each initial stock BUY attempt:

1. fetch one ask;
2. compute quantity from that ask;
3. compute TP/SL from that same ask;
4. build the order from that same reference.

If Alpaca rejects the order with an authoritative `base_price`, retry once using that value.

On retry:

- recompute bracket prices from `base_price`;
- recompute quantity when needed;
- ensure estimated notional does not exceed the authorized budget.

### Acceptance criteria

- The initial path calls `get_current_ask_price()` exactly once.
- Quantity and bracket prices use the same initial reference.
- Retry uses Alpaca's `base_price`.
- Tests cover large divergence between local ask and Alpaca base price.
- Estimated notional remains within budget.

---

## P0.9 — Quantity and bracket-price invariant checks

**Done.** `execution._validate_bracket_order()` enforces `reference_price > 0`, `budget > 0`,
`qty >= 1`, `0 < stop_loss_px < reference_price < take_profit_px`, and
`estimated_notional <= budget`, called from `bracket_buy_with_SLTP()` just before the order is
built. Two typed exceptions: `InvalidOrderParameters` (bad quote/price relationship — propagates
as an unexpected error, matching this repo's existing "unexpected exceptions become a loud
failure" convention in `app.py`) and its subclass `InsufficientQuantity` (`qty < 1` — an
expected, not exceptional, outcome; `buy()` catches this specifically and returns
`status="skipped", reason="insufficient_qty"`, on both the initial attempt and the `base_price`
retry). Tests cover zero-price quotes, a stop-loss price going non-positive on an
extremely-low-priced symbol, and the insufficient-qty skip path on both the initial and retry
attempts.

### Change

Before constructing or submitting a stock bracket order, enforce:

```text
reference_price > 0
qty >= 1
budget > 0
0 < stop_loss_px < reference_price
take_profit_px > reference_price
stop_loss_px < take_profit_px
estimated_notional <= authorized_budget
```

Use a typed domain exception such as `InvalidOrderParameters`.

For `qty < 1`, return:

- `status="skipped"`;
- `reason="insufficient_qty"`.

### Acceptance criteria

- Zero-price quotes do not produce an order.
- Budgets below one share do not produce an order.
- Stop prices at or below zero do not produce an order.
- Invalid TP/SL relationships do not produce an order.
- Tests cover sub-dollar and extremely low-priced symbols.

---

## P0.10 — Structured execution outcomes and HTTP contract

### Change

Standardize response fields:

```json
{
  "status": "executed | skipped | rejected | error",
  "reason": "stable_machine_readable_code",
  "detail": "human-readable explanation",
  "order_id": "optional",
  "decision_id": "required when supplied",
  "retryable": false,
  "upstream_code": "optional"
}
```

Initial reason codes:

- `executed`;
- `duplicate_decision`;
- `open_orders_or_position`;
- `no_position`;
- `budget_below_minimum`;
- `insufficient_qty`;
- `risk_limit_exceeded`;
- `buy_kill_switch_active`;
- `state_not_reconciled`;
- `invalid_order_parameters`;
- `alpaca_rejected`;
- `alpaca_unavailable`;
- `unexpected_error`.

### HTTP mapping

| Condition | HTTP |
|---|---:|
| Executed | 200 |
| Idempotent duplicate resolved successfully | 200 |
| Valid no-op such as SELL with no position | 200 |
| Existing state conflicts with requested BUY | 409 |
| Request violates domain or risk policy | 422 |
| BUY kill switch active | 423 |
| Tracked state not yet reconciled with Alpaca after restart | 503, `retryable=true` |
| Alpaca timeout, connectivity failure, or temporary unavailability | 503 |
| Malformed or unrecognized upstream response | 502 |
| Unexpected internal failure | 500 |

Known Alpaca business rejections should preserve the upstream error code and use `retryable=false` unless explicitly known to be transient.

`state_not_reconciled` (added by the async fill-watcher's restart-recovery mechanism, see
`docs/architecture.md`) is `status="skipped"`/HTTP 200 for now, matching every other declined-BUY
outcome under the current, pre-P0.10 contract -- not a deliberate design choice, just consistent
with what already exists until this section's full contract lands. It's a valid 503 candidate
above (the request may succeed later unchanged, unlike e.g. an invalid order), not 423 (it's not
an operator-controlled lock like the kill switch) or a domain-specific 409/422.

### Acceptance criteria

- Dealer checks both HTTP status and structured body.
- No Alpaca business rejection is silently represented as an ordinary execution success.
- Tests cover every status/reason mapping.
- API documentation includes the response contract table.

---

## P0.11 — Dependency split and reproducible pinning

### Problem

The current runtime dependency list is unpinned and shared across workloads with different needs.

### Change

Audit imports and create workload-specific dependency groups:

```text
requirements-common.txt
requirements-analyst.txt
requirements-dealer.txt
requirements-floor-broker.txt
requirements-eod-report.txt
requirements-dev.txt
constraints.txt
```

Guidelines:

- keep only dependencies imported by that workload;
- do not put Kubernetes in Floor Broker unless a direct runtime import requires it;
- do not put LangChain/LangGraph in EOD Report;
- remove unused notebook, Anthropic, OpenAI, pandas, NumPy, or other packages unless the code actually imports them;
- pin a supported Python version;
- pin transitive resolution through a generated constraints or lock file;
- configure Dependabot or Renovate for controlled updates.

A later migration to `pyproject.toml` may replace the requirements-file layout, but P0 must first make image builds reproducible.

### Acceptance criteria

- Each Docker image installs only its required dependency group.
- All images build independently from a clean environment.
- Rebuilding the same commit resolves the same versions.
- Unused direct dependencies are removed.
- Dependency update PRs run the full CI suite.

---

## P0.12 — CI, linting, validation, and image-build checks

### Change

**Partial.** `.github/workflows/test-lint.yaml` runs on every push and pull request:

- `pytest` (fully mocked, no DGX hardware or real credentials required);
- `ruff check`.

Deviates from the original spec below in one respect: it runs on the `[self-hosted, dgx]`
runner, not a GitHub-hosted runner, matching `build-push.yaml`/`deploy.yaml`/`undeploy.yaml`'s
existing convention — a deliberate choice (this org has no GitHub-hosted runner registered) made
when this was scoped, trading "doesn't tie up the DGX runner" for "matches every other workflow
here."

Still outstanding from the original P0.12 scope:

- `ruff format --check` — not enforced yet; there's pre-existing formatting drift across the
  repo that predates this workflow and hasn't been cleaned up, so gating on it now would fail on
  unrelated files;
- YAML validation;
- Dockerfile linting;
- Kubernetes manifest validation;
- secret scanning;
- build all four images without pushing (`build-push.yaml` only builds+pushes on
  `workflow_dispatch`/version tags, never as a pushless CI check);
- branch protection actually blocking merge on failure (the workflow runs and reports status,
  but nothing currently enforces it as a required check).

CI also runs on Python 3.11, not the 3.12 every `Dockerfile.*` and local dev actually use — the
`[self-hosted, dgx]` runner container (`ghcr.io/miramar-labs-org/mlabs-runner`) is Debian 12
bookworm, which doesn't have 3.12 in its apt repos. No 3.12-only feature is in use today, so
this hasn't caused a divergence, but it's a real gap between what CI tests and what runs in
production. The correct fix is baking Python 3.12 into the shared `mlabs-runner` image — an
org-wide asset used by every repo, out of scope to change from here.

`test-lint.yaml` used to `sudo apt-get install python3.11-venv` on every single run before
creating `.venv-ci` -- requiring root on the runner, modifying the runner host's package set,
adding an apt-repository dependency, avoidable runtime, and possible contention with concurrent
build/deploy jobs on the same host. `python3-venv` is now baked into the shared `mlabs-runner`
image itself (`miramar-platform-gcp/mlabs-runner/Dockerfile`), so `test-lint.yaml` only needs to
create the venv, not install the tooling for it first. Same org-wide-asset caveat as the
Python 3.11-vs-3.12 gap above: the Dockerfile change ships from `miramar-platform-gcp`, not from
here, and the *running* runner container(s) on DGX/AGX must be rebuilt and redeployed before
`test-lint.yaml`'s simplified step will actually pass in CI.

### Acceptance criteria

- [x] CI runs automatically on pull requests (and on push).
- [ ] All four images build in CI.
- [ ] A failing test, lint error, invalid manifest, or detected secret blocks merge.
- [x] README includes a CI badge.
- [x] Supported Python version is documented (`README.md`'s new Development section: 3.12).

---

## P0.13 — Baseline container security

### Change

For all four images and workloads:

- create and run as a non-root user;
- set `runAsNonRoot: true`;
- set `allowPrivilegeEscalation: false`;
- drop Linux capabilities;
- set an appropriate seccomp profile;
- avoid writable filesystem paths except where explicitly needed;
- preserve liveness/readiness behavior.

Read-only root filesystems may be deferred for workloads that require additional compatibility work.

### Acceptance criteria

- Every workload starts successfully as non-root.
- Kubernetes manifests contain baseline `securityContext`.
- Containers do not require added Linux capabilities.
- CI validates manifests and image startup where practical.

---

## P0.14 — Asynchronous order submission and fill reporting

**Done.** `_wait_for_fill()` used to block the synchronous `/execute` route for up to ~5s
(`FILL_POLL_ATTEMPTS=5` x `FILL_POLL_INTERVAL_S=1.0`) after every BUY/SELL submission, polling
Alpaca for the fill price before responding -- tying up a request-handling worker thread and
mixing order acceptance with fill observation into one request/response cycle. `buy()`/`sell()`
now submit the order and return immediately with `status="submitted"` and `order_id` (stock BUYs
still return `sl_price`/`tp_price` immediately -- those are computed pre-submission and need no
polling). A new `execution.check_pending_fills()`, tracking `_pending_fills: dict[order_id,
context]`, mirrors the existing `check_bracket_fills()`/`_tracked_brackets` pattern already used
for TP/SL leg fills; it's polled by a new `poll_pending_fills()` daemon thread in
`src/floor_broker/main.py` (same shape as `poll_bracket_fills()`/`poll_kill_switch()`), which
posts the eventual fill as its own Slack notification
(`slack.notify_floor_broker_result(status="executed", fill_price=...)`) once observed --
decoupling order acceptance from fill reporting, matching the async model the bracket-fill
watcher already established for TP/SL legs. `ExecuteResponse.status` and
`notify_floor_broker_result`'s emoji mapping both gained a `"submitted"` case. `_wait_for_fill()`
and its two constants are removed -- no caller needs bounded synchronous polling anymore.

### Problem

`_wait_for_fill()` blocks the synchronous `/execute` FastAPI route for up to ~5s after every
BUY/SELL submission, polling Alpaca for the fill price before responding. This ties up a
request-handling worker thread on every trade, increases request latency, increases the chance
of tripping Dealer's 30s HTTP timeout under load, and mixes order acceptance with eventual fill
observation into a single request/response cycle. The existing bracket-fill watcher
(`poll_bracket_fills()`) already reports TP/SL leg fills asynchronously and separately from
`/execute` -- the initial order's own fill is the one part of this flow still handled
synchronously.

### Change

Submit the order and return immediately with `status="submitted"` and the order ID; track the
fill asynchronously via a background poller; report the fill event separately (a new Slack
notification), reusing the same tracking-dict-plus-daemon-thread pattern the bracket-fill watcher
already established.

### Acceptance criteria

- `/execute` returns without blocking on a fill for any BUY or SELL.
- The eventual fill is reported via a separate Slack notification, not the `/execute` response.
- `ExecuteResponse.status` includes `"submitted"`.
- Existing bracket TP/SL fill reporting (`poll_bracket_fills`) is unaffected.

---

# P1 — Evidence and strategy validation

P1 is complete when the system can explain, replay, and evaluate its decisions using durable records rather than ephemeral logs.

## P1.1 — Durable decision and event schema

**Partial — an MVP slice landed in v0.6.0.** `src/common/db.py` persists three tables to a
shared Postgres instance (provisioned by `miramar-platform-gcp`'s `dgx/k3s/postgres/`, via
`deploy-postgres.yaml`): `analyst_picks`, `dealer_decisions`, `floor_broker_events` — see
`docs/architecture.md` § Persistence for the schema and write sites. This covers the specific
gap that motivated it (the Dealer's LLM reasoning was previously sent to Slack and nowhere
else, making it unrecoverable) and backs the new `/analyst-explain` skill. It does **not**
yet implement the full target event schema below — no research-input or indicator-snapshot
capture, no portfolio/strategy/model/prompt version columns, no input-data timestamps. There
is no historical backfill: the tables start empty at deploy time. Note also that v0.6.0
shipped with a schema bug — `CREATE INDEX` on a `timestamptz::date` expression isn't
`IMMUTABLE`, which rolled back table creation entirely, so no rows were actually written
until the fix landed in **v0.6.1**. Decisions made before v0.6.1 remain unrecoverable. The
rest of this section is the still-Planned full scope.

Persist structured events for:

- Analyst candidate discovery;
- research inputs;
- Analyst selections and validation changes;
- Dealer indicator snapshots;
- raw and validated LLM outputs;
- risk evaluations;
- execution requests;
- Alpaca responses;
- fills;
- position snapshots;
- EOD results.

Each event should include:

- event ID;
- decision ID where applicable;
- timestamp and timezone;
- symbol and asset class;
- portfolio version;
- strategy version;
- model and prompt version;
- input-data timestamps;
- outcome and reason code.

Choose a durable store only after defining access and retention requirements. A small relational database is preferable to introducing a message queue without a demonstrated need.

---

## P1.2 — Exact model, prompt, and input version capture

Record enough information to reproduce a decision:

- model identifier and serving backend;
- model parameters;
- prompt template version or hash;
- exact structured input;
- candidate universe;
- research text or content hash;
- technical indicator names, values, source, and timestamps;
- schema version;
- post-validation changes.

LangSmith traces may complement this record but are not the sole system of record.

---

## P1.3 — Shadow execution mode

Add a mode in which Analyst and Dealer run normally, but actionable signals are recorded without reaching live paper execution.

Support:

- global shadow mode;
- strategy-version shadow mode;
- model-version shadow mode;
- side-by-side production and shadow decisions;
- explicit labeling in logs, metrics, Slack, and durable events.

Shadow mode must not share an idempotency key with the active execution path.

---

## P1.4 — Forward evaluation and replay

Use live-captured durable events to evaluate signals after the fact.

Capabilities:

- replay a Dealer decision from its captured indicator snapshot;
- calculate forward returns at fixed horizons;
- evaluate realized and unrealized outcomes;
- compare active and shadow strategies;
- identify prompt/model regressions;
- generate daily and weekly evaluation reports.

This is the most faithful evaluation path because it uses the exact data observed by the deployed system.

---

## P1.5 — Historical backtesting harness

**Done — deterministic baselines only.** Implemented in `src/backtest/`, documented in
[`docs/backtesting.md`](backtesting.md). A local CLI tool (`python -m src.backtest.main`), not a
k8s workload — it needed no durable event store (P1.1), since it computes indicators locally
from bulk historical bars rather than replaying live-captured decisions.

The scope split explicitly follows this section's own framing: this covers only the
"separate historical-data path for deterministic offline testing" half. True historical
reconstruction of the live LLM's actual past decisions — contemporaneous news, screener
membership, exact API data, LLM serving behavior — remains P1.4 (Forward evaluation and
replay), gated on P1.1's durable event store, and is still Planned.

Backtest assumptions are documented in `docs/backtesting.md`: data source, survivorship bias,
slippage, spreads, fees, execution timing, missing-data handling.

---

## P1.6 — Deterministic baseline strategies

**Done**, alongside P1.5. `src/backtest/strategies.py` implements buy-and-hold, simple RSI rule,
simple MACD rule, deterministic multi-indicator rule, random action baseline, and no-trade
baseline, each run per-symbol against the harness's own historical data (not yet compared
against SPY or an equal-weight candidate portfolio — those remain open extensions).

`src/backtest/metrics.py` reports: total return, benchmark-relative return (vs. that symbol's
own buy-and-hold run), maximum drawdown, Sharpe, win rate, average win/loss, expectancy,
exposure, trade count. Turnover is not yet reported.

No claim of trading edge should be made without these comparisons.

---

## P1.7 — Resolve `size_hint` semantics

**Done.** Chose option 3: `size_hint` is used deterministically as a bounded fraction (`0.0`–
`1.0`, schema-enforced in `src/dealer/schema.py`) of the authorized budget.
`call_floor_broker` (`src/dealer/graph.py`) now computes `budget = state["budget"] * size_hint`
for BUY signals only; SELL forwards `budget` unscaled since `execution.sell()` ignores it
entirely and closes the whole position.

`size_hint=0` is a skipped BUY, not a HOLD or a valid zero-allocation signal: it would scale the
authorized budget to exactly `$0`, which `ExecuteRequest.budget`'s `gt=0` constraint
(`src/floor_broker/app.py`) would reject as a validation error, so it's refused locally instead
(`status="skipped", reason="size_hint_zero"`) before ever reaching Floor Broker. A budget scaled
to a small-but-positive amount is still forwarded as-is — Floor Broker already applies all P0
risk limits and has its own graceful skip paths for that case (`reason="budget_below_minimum"`,
`reason="insufficient_qty"`), so Dealer doesn't need a second floor/clamp on top.

Tests: `tests/dealer/test_call_floor_broker.py` (`test_buy_scales_forwarded_budget_by_size_hint`,
`test_buy_with_zero_size_hint_is_skipped_without_calling_floor_broker`,
`test_sell_forwards_budget_unscaled_by_size_hint`).

---

## P1.8 — Daily loss, trade-count, and aggregate exposure controls

**Partial — only the daily-loss half was built, and via a narrower mechanism than
described below.** `strategy.daily_profit_target_usd`/`daily_loss_limit_usd`
(`config.yaml`, enforced in `src/floor_broker/execution.py::buy()`) block new BUYs once
today's Alpaca account `equity - last_equity` crosses either bound — no
`risk.py` module, no durable local event log, just a live Alpaca account-state read on
every BUY. A Dealer-side same-symbol stop-loss cooldown was added on 2026-08-11, but trade-count
limits, failed-submission-rate limits, per-asset-class aggregate exposure limits, and generalized
cooldown-after-rejection controls are not implemented. See
`docs/strategy.md` for the design log and `docs/ROADMAP.md`'s P0.4 note above for the
same scope decision.

Extend `risk.py` with:

- maximum realized daily loss;
- maximum total daily P&L drawdown;
- maximum trades per day;
- maximum failed submissions per interval;
- maximum aggregate exposure by asset class;
- optional cooldown after repeated rejection or portfolio-level loss events.

Use Alpaca account activities plus durable local events. Define timezone and trading-day boundaries explicitly.

SELL remains permitted when these controls block new BUY exposure.

---

## P1.9 — Core operational metrics

Expose at least:

```text
floor_broker_requests_total
orders_executed_total
orders_skipped_total
orders_rejected_total
alpaca_errors_total
risk_denials_total
duplicate_decisions_total
llm_requests_total
llm_parse_failures_total
agent_run_failures_total
```

Add latency histograms for:

- LLM calls;
- TAAPI calls;
- Alpaca calls;
- Floor Broker requests;
- Analyst and Dealer runs.

Do not expose secrets, prompts containing sensitive data, or raw account identifiers through metrics.

---

## P1.10 — Optional end-of-day position flatten ("day trading mode")

**Done.** `eod_flatten.enabled` (`config.yaml`, default `false`) gates a 5th Floor Broker daemon
thread, `poll_eod_flatten()` (`src/floor_broker/main.py`), which calls
`execution.check_eod_flatten()` on a 60s cadence. When enabled and Alpaca's live clock reports
the market is within `eod_flatten.minutes_before_close` minutes (default 10) of closing, it sells
every open stock position via the existing `sell()` path — crypto is 24/7 and explicitly excluded
(`AssetClass.US_EQUITY` filter). This is the first time `strategy.halt_behavior`'s
`flatten_positions` value becomes real, enforced behavior rather than recorded intent only (see
`docs/strategy.md`); it's a separate, independently-toggled feature, not wired to
`halt_behavior`.

Runs entirely in-process so the eventual fill is picked up automatically by the already-running
`poll_pending_fills()` thread, the same way it already handles Dealer-initiated sells — no new
CronJob, image, or deploy manifest needed. Off by default; toggling is a config-only change (no
rebuild/redeploy), same live-reload story as `analyst.midday_run.enabled`.

Tests: `tests/floor_broker/test_execution.py` (`check_eod_flatten()` — disabled/closed-market/
not-yet-in-window no-ops, sells stocks and skips crypto, excludes a `skipped` sell from returned
events) and `tests/floor_broker/test_floor_broker_main.py` (`poll_eod_flatten()` — Slack
notification per event, no-op with no events, survives an exception).

---

## P1.11 — Conditional (aggregate-P&L-gated) EOD flatten

**Done.** New `eod_flatten.conditional` flag (`config.yaml`, default `false`) layers an
aggregate-P&L gate on top of P1.10's unconditional flatten. When `true`, `check_eod_flatten()`
sums `unrealized_pl` across all open stock positions at the same trigger point
(`minutes_before_close`); if the aggregate is `>= 0` ("UP") it flattens everything exactly as
P1.10 does, and if it's negative ("DOWN") it holds every stock position open overnight instead —
except any single position held `>= eod_flatten.max_days_held_loss` days (default `5`), which is
force-flattened regardless of the aggregate sign so a loser can't ride indefinitely. The UP/DOWN
call is whole-account aggregate, not per-symbol, per explicit direction from the strategy owner.

Days-held tracking is new: Alpaca's `Position` object has no entry-date field and the installed
alpaca-py SDK has no account-activities endpoint, so a small `position_opens` table
(`symbol TEXT PRIMARY KEY, opened_at TIMESTAMPTZ`) was added to `src/common/db.py`, populated from
the existing BUY/SELL fill distinction in `poll_pending_fills()` (`src/floor_broker/main.py`) —
`ON CONFLICT (symbol) DO NOTHING` on open so a BUY that adds to an already-open position never
resets the clock, `DELETE` on close. `sell()` always closes the full current position in this
codebase, so no partial-lot history is needed. Pre-existing or gap-opened positions are
backfilled once at process start in `reconcile_tracked_state_once()`
(`src/floor_broker/execution.py`), best-effort and non-blocking to order reconciliation.

Off by default (`conditional: false`); toggling is a config-only change (no rebuild/redeploy),
same live-reload story as `eod_flatten.enabled` itself.

Tests: `tests/common/test_db.py` (`record_position_opened`/`record_position_closed`/
`fetch_position_opened_at`, upsert-not-overwrite semantics, exception-swallowing) and
`tests/floor_broker/test_execution.py` (`check_eod_flatten()` conditional aggregate-UP/DOWN
branches, force-flatten past `max_days_held_loss`, `reconcile_tracked_state_once()` backfill and
its failure isolation) and `tests/floor_broker/test_floor_broker_main.py`
(`poll_pending_fills()` records position open/close on BUY/SELL fills).

---

## P1.12 — README "Today's P/L" / "YTD P/L" badges

**Done.** New `src/pl_badges/` — not a k8s workload, run by a scheduled GHA workflow
(`.github/workflows/pl-badges.yaml`, `45 21 * * *` UTC + `workflow_dispatch`) on the same
`[self-hosted, dgx]` runner as `test-lint.yaml`. `python -m src.pl_badges.main` computes Today's
P&L (`account.equity - account.last_equity`) and YTD P&L (`equity - base_value` from
`get_portfolio_history()` starting Jan 1 of the current year) and writes two shields.io
endpoint-badge JSON files, `badges/today-pl.json` / `badges/ytd-pl.json`; the workflow commits
and pushes them back to `main` only if the content changed. README's two new badges point at
those files via `img.shields.io/endpoint?url=.../raw.githubusercontent.com/...`, so shields.io
fetches the JSON directly from GitHub's raw-content CDN at render time — no publicly-reachable
service needed, unlike the Floor Broker/Postgres this data is ultimately sourced from. Skips the
write (and commit) entirely on weekends/holidays via the same `is_stock_market_open()` calendar
check `eod_report.main()` uses.

Requires `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_API_SECRET` as GitHub Actions repo secrets (the
runner is outside the cluster, so it can't read the `mlabs-api-keys` k8s Secret) — same pattern
as `MIRAMAR_ORG_GHCR_PAT`.

Tests: `tests/common/test_pl_badges.py` (`fetch_pl_summary()` today/YTD math including the
negative case, `build_badge_payload()` color/formatting including the zero-is-up edge case) and
`tests/pl_badges/test_pl_badges_main.py` (`main()` skips on a closed market, writes both badge
files on an open one).

---

## P1.13 — Earnings and macro-event blackout windows

**Done.** Two independent, config-gated risk controls addressing gap-risk windows the Analyst/
Dealer previously had zero structured awareness of (the only prior "research" input was
unstructured headline text, `analyst.news.enabled`).

**Per-symbol earnings blackout** — new `earnings_blackout` block (`config.yaml`, default
`enabled: false`). When on, `discover_candidates` (`src/analyst/graph.py`) drops any stock
screener candidate reporting earnings within `days_before`/`days_after` calendar days of today.
Alpaca has no earnings-calendar data (its Corporate Actions API covers splits/dividends/mergers
only), so the date source is a single market-wide Finnhub free-tier call
(`sources.fetch_earnings_calendar()`, `src/analyst/sources.py`) — one call per Analyst run, not
per symbol, well inside the free tier's 250 calls/day. Fails soft (returns an empty set, logs a
warning) on a missing `FINNHUB_API_KEY`, network error, non-200, 429, or bad JSON, so a Finnhub
outage degrades to "no earnings filter this run" rather than a crashed CronJob or fully blocked
picks. Crypto candidates are never filtered (no earnings dates apply).

**Market-wide macro blackout** — new `macro_blackout` block (`config.yaml`, default
`enabled: false`). When on, `call_floor_broker` (`src/dealer/graph.py`) refuses any BUY signal
locally — never forwarded to Floor Broker — on a day matching either a hand-maintained
`macro_blackout.dates` entry (FOMC, CPI, NFP, PCE, PPI, ISM PMI, GDP, Fed Chair testimony, and
other scheduled market-wide releases published by the Fed/BLS/Commerce Dept months ahead, so no
API is needed) or an auto-computed quarterly quad witching day — the 3rd Friday of March/June/
September/December, the simultaneous expiration of stock options, index options, and index
futures, historically one of the highest-volume, highest-volatility sessions of the year
(`_is_quad_witching_day()`, a fixed calendar rule computed directly in code rather than a
config.yaml entry, since it can't drift or need quarterly upkeep). Whole-trading-day granularity
— a listed date pauses new BUY entries for the entire day. SELL/HOLD and `eod_flatten` are never
affected, only new BUY entries.

Both shipped off by default, same config-only-toggle story as `eod_flatten.enabled` — no rebuild/
redeploy needed to flip either flag. Both were flipped to `true` on 2026-08-05.
`earnings_blackout.enabled` required a real, working `FINNHUB_API_KEY` in the `mlabs-api-keys`
secret — the key first tried during pre-enable verification turned out to be a Financial Modeling
Prep key mistakenly copied instead of a Finnhub one (rejected by every Finnhub auth method:
`?token=`, `?apikey=`, and the `X-Finnhub-Token` header); a real Finnhub key was generated and
verified with a live `/calendar/earnings` call (HTTP 200, 1500 market-wide entries) before
enabling. `macro_blackout.enabled` was flipped to `true` with its date list replaced by 18 real
FOMC (federalreserve.gov) / CPI+NFP (bls.gov) / PCE (bea.gov) dates covering the remainder of
2026 — see `config.yaml`'s `macro_blackout.dates` comment for the sourcing note and quarterly
update reminder. The list does not self-extend past its last entry (`2026-12-23`); nothing
auto-generates new dates, so it needs a manual refresh once 2027's schedules are published (quad
witching days are the exception — those are computed in code, not listed, and need no upkeep).

Tests: `tests/analyst/test_sources.py` (`fetch_earnings_calendar()` — blackout-window matching,
fails soft on missing key/no symbols/request exception/non-200/429) and
`tests/analyst/test_graph.py` (`discover_candidates()` drops earnings-blackout symbols when
enabled, never calls Finnhub when disabled) and `tests/dealer/test_call_floor_broker.py`
(`call_floor_broker()` skips BUY on a matching `macro_blackout.dates` entry and on a forced quad
witching day, forwards BUY normally when no date matches, forwards SELL unconditionally even
during a blackout; `_is_quad_witching_day()` matches the third Friday of each quarter-end month
and rejects other Fridays/months).

---

## P1.14 — Multi-modal Dealer: OHLCV enrichment + AGX-Orin Visual Analyst (shadow)

**Planned — full implementation plan drafted and approved 2026-08-12, not started.**
Adds two independently config-gated enhancements to the Dealer's input, bundled in one v1:

- **OHLCV enrichment**: multi-timeframe candle data and derived technical features
  (return%, realized volatility, ATR, volume-relative, distance-from-high/low, VWAP
  distance, trend slope) feeding the numerical Dealer's existing prompt, in addition to
  today's TAAPI scalar indicator snapshot.
- **Visual Analyst**: an independent, shadow-only second opinion from a vision-capable
  model (target: a 7B-class VLM on AGX Orin's Ollama, e.g. `qwen2.5-vl:7b`, tag to be
  verified before enabling) reasoning over a rendered candlestick chart. Stocks only in
  v1; degrades cleanly to today's numerical-only behavior whenever AGX/its model is
  unreachable. The numerical Dealer is never told the visual opinion exists — no
  consensus/enforcement gate is built in v1 — so the two signals stay decorrelated for a
  future ablation study. Persisted separately as `visual_analyst_decisions`, not merged
  into `dealer_decisions`.

Both flags ship `false` by default; enabling either is a config-only change
(`ohlcv_enrichment.enabled`, `visual_analyst.enabled` in `config.yaml` and
`config.default.yaml`), same live-reload story as `eod_flatten.enabled`. Full design,
rollout sequencing, and manual-verification checklist:
`/home/aaron/.claude/plans/tingly-pondering-codd.md`.

---

## P1.15 — Analyst candidate-mix: fixed large-cap/crypto/screener pool ratio

**Done.** Before this change, the Analyst's entire stock candidate pool came from Alpaca's
screener (`most-actives` by volume, `movers` gainers/losers by %-change), and `fetch_indicators`
(`src/analyst/graph.py`) ranked candidates by `abs(change_pct)` before fetching real TAAPI
indicator data for only the top `indicator_fetch_limit`. Mega-caps (AAPL, NVDA, MSFT, ...) almost
never move enough in a day to compete with a thinly-traded microcap's 20-30%+ swing, so blue chips
essentially never appeared as picks — confirmed against a live run where 16 surviving stock
candidates were all in the penny/microcap range.

New `analyst.candidate_mix` block (`config.yaml`/`config.default.yaml`, default `enabled: true`)
composes the daily stock+crypto candidate pool as a fixed percentage mix instead of letting the
day's movers ranking alone decide what the LLM sees: `pool_size: 20` total, split
`large_cap_pct: 0.40` / `crypto_pct: 0.30` / `screener_pct: 0.30`. The large-cap bucket draws from
a new `analyst.large_cap_symbols` list (15 blue-chip names) via `sources.fetch_large_cap_candidates`
— fails *open* on a quote-lookup failure (unlike the screener's fail-closed behavior), since a
hand-picked large-cap symbol carries none of the illiquid-mover risk the fail-closed path exists to
guard against. The crypto bucket is silently redistributed to the other two buckets when
`trading.crypto.enabled` is false. All existing downstream risk controls (earnings blackout, macro
blackout, spread guard, daily P&L halt, position caps) apply unchanged to every symbol regardless
of which bucket it came from — the mix only changes what reaches the candidate pool, not what
happens after. `enabled: false` reverts to today's screener-only discovery; the whole feature is a
config-only, no-redeploy change.

Commits: `daeba6b` (feat: candidate mix), `a6d2662` (ruff fix). Tests:
`tests/analyst/test_sources.py` (`fetch_large_cap_candidates` — price populated per symbol, fails
open on a lookup error, empty input short-circuits without calling anything) and
`tests/analyst/test_graph.py` (pool composed by ratio, screener_pct excludes symbols already in
`large_cap_symbols`, crypto redistribution when crypto is disabled, `enabled: false` regression
parity with pre-change behavior).

**Live-verified 2026-08-12**: a manual rerun plus the scheduled midday cronjob both produced
correctly-mixed portfolios (large-cap names like NVDA/JPM alongside crypto and screener picks,
each earnings-blackout-filtered and indicator-backed). The pipeline was confirmed end-to-end —
Analyst mix pick → Dealer BUY decision → Floor Broker bracket order → fill — on NVDA (13 shares @
\$223.98, bracket order `880e66c2-f430-41f8-93c4-dbc15dd9f05a`). See `docs/analysis.md`'s
`2026-08-12 13:35 ET` entry for the full narrative.

---

## P1.16 — Options trading via MCP contract selection

**Done.** A new instrument path at the **Dealer** layer (the Analyst is unchanged — it still
screens and picks the same stock/crypto universe). When `options_trading.enabled` (`config.yaml`,
currently `true`), every **stock** Dealer signal is expressed as a long option instead of an
equity bracket order; crypto is untouched.

**Direction.** `right = "call" if signal["action"] == "BUY" else "put"` — a bearish SELL becomes
a long put. There is no short-premium / naked path anywhere; put entries are additionally gated
the same way call entries are.

**Contract selection.** New `select_option_contract` node (`src/dealer/graph.py`) runs
`_select_option_contract_async()`, a LangChain tool-calling loop (`_MAX_TOOL_CALL_ROUNDS = 6`)
over the same `cfg.llm` model with Alpaca's options data bound as MCP tools via
`langchain-mcp-adapters` + `alpaca-mcp-server` (`src/dealer/mcp_options.py`,
`ALPACA_TOOLSETS=assets,options-data,account`, all read-only, resolving the one live account's
credentials). The
prompt carries the `dte_min`–`dte_max`, `target_delta_min`–`target_delta_max`, and
`min_open_interest`/`min_volume` windows; output schema `OptionContractPick`
(`src/dealer/schema.py`). HOLD or sub-`strategy.min_confidence` signals produce no pick.

**Re-validation + sizing + execution.** `call_floor_broker_option` re-runs the macro-blackout /
symbol-stop-cooldown / win-rate-throttle gates, re-checks the picked contract's DTE and
`abs(delta)` are still in-window, requires `strategy.risk_per_trade_usd` to be set, sizes
`qty = int(risk_per_trade_usd // (premium * 100))` (skip reasons `dte_out_of_range`,
`delta_out_of_range`, `risk_per_trade_usd_not_configured`, `qty_zero`), then
`POST /execute-option` → `execution.buy_option()` on `trading_client` (the one live account, so
option BUYs share the daily-P&L halt and position cap with stocks/crypto). `buy_option()` re-quotes
the live ask, rejects with `reason="notional_cap_exceeded"` if
`qty * live_ask * 100 > options_trading.max_notional_usd` (outer ceiling
`MAX_OPTION_NOTIONAL = $100k`), and submits a single-leg `MarketOrderRequest` (`TimeInForce.DAY`),
returning `status="submitted"`.

**Fill tracking + protection.** `_option_positions` and the `options_trades` DB row are written
only on a confirmed fill, observed by a dedicated `poll_pending_option_fills()` thread (Floor
Broker now runs seven daemon threads). Alpaca has no server-side option brackets, so
`check_option_stops()` — run inside `poll_bracket_fills()` and **unconditionally regardless of
`options_trading.enabled`**, so already-open contracts stay protected after the flag is flipped
off — force-closes via `sell_option()` on `dte <= options_trading.dte_force_close` (checked
first, regardless of P&L), `mid <= entry_premium * options_slP`, or
`mid >= entry_premium * options_tpP`. `/flatten-options` → `flatten_all_options()` is wired into
the power scheduler behind `power_schedule.flatten_options_before_powerdown` (default false).

**One live account.** As originally shipped this used a dedicated options account (`account2`);
that split was removed shortly after (see the follow-up note below). `src/common/alpaca_client.py`
now exposes a single set of clients resolving `config.yaml`'s `alpaca.live.key_env`/`secret_env`
(default `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_API_SECRET`), re-resolved on the normal 60 s config
refresh. Every order — stocks, crypto, options — routes there.
`ALPACA_PAPER_API_KEY2`/`ALPACA_PAPER_API_SECRET2` are pre-wired as the switch target for the
competition's $100k Level-3 account.

**Persistence.** New `options_trades` table (`src/common/db.py`), one row per option position:
`record_options_trade_opened()` on the confirmed BUY fill, `record_options_trade_updated()` /
`record_options_trade_closed()` on exit (`closed_at`/`exit_reason`/`exit_premium`). Schema is now
six tables.

New config: `options_trading` block (`enabled`, `dte_min`/`dte_max`, `dte_force_close`,
`target_delta_min`/`target_delta_max`, `min_open_interest`/`min_volume`, `options_slP`/
`options_tpP`, `max_notional_usd`); `strategy.risk_per_trade_usd` doubles as the per-option
budget; `power_schedule.flatten_options_before_powerdown`. New dependencies: `alpaca-mcp-server`,
`langchain-mcp-adapters`.

Commits: `d74ec6f`/`a97775a` (design spec + implementation plan), `9dccab2` (config block),
`83bbeed` (account-2 client), `379531a` (deps), `a3cdf1e` (`options_trades` table), `6c725c3`
(`OptionContractPick` schema), plus the `mcp_options.py` / `graph.py` / `execution.py` /
`buy_option`/`sell_option` work and a long review-driven fix chain (`86e9ef4`, `fb8dc7c`,
`c316fc9`, `3c1ca1a`, `59188b2`, `996c5cb`, `a528def`, `3e521a4`, `7e94220`), merged via PR #1
(`d080e74`) and PR #2 (`df9d10e`). Config-driven credentials: `8009fbe`, `c22c983`. Enabled
live: `44d87e9`. Tests: `tests/dealer/test_mcp_options.py`, `tests/dealer/test_dealer_graph.py`,
`tests/dealer/test_schema.py`, `tests/floor_broker/test_execution.py` /
`test_app.py` / `test_floor_broker_main.py`, `tests/common/test_db.py`,
`tests/power_scheduler/test_power_scheduler_main.py`.

**Known gaps (not blockers).** The backtesting harness has no options support. Options are
buy-only single legs — no spreads, no premium selling.

**Follow-up — unified onto one account (2026-08-26).** The dedicated options account was removed:
stocks, crypto and options now all trade on the one `alpaca.live` paper account. `trading_client2`
/ `option_data_client2` / `account_env_names()` / `distinct_trading_clients()` and the commit
`01e1dd2` multi-account EOD/badge aggregation are gone; `live_account_env_names()` replaces the
per-account resolver. Consequence: option BUYs obey the shared `strategy.daily_loss_limit_usd` /
`daily_profit_target_usd` halt and `strategy.max_concurrent_positions` cap. All reporting reads
the one live account — no aggregation to under-report. Moving the whole floor to a different
account (e.g. the competition's $100k account) is a 2-line `config.yaml` edit, no redeploy.

---

# P2 — Platform maturity

P2 begins after the execution path is safe and the strategy is measurable.

## P2.1 — Full Prometheus and Grafana observability

Add:

- Prometheus scraping;
- Grafana dashboards;
- alert rules;
- SLOs for agent availability and execution reliability;
- account-equity and drawdown panels with restricted access;
- per-symbol and per-asset-class exposure views;
- trace-to-event correlation.

---

## P2.2 — Automatic circuit breakers

Automatically activate BUY blocking when configured conditions occur, such as:

- N consecutive Alpaca rejections;
- repeated upstream timeouts;
- daily loss threshold;
- unexpected exposure growth;
- stale portfolio or stale indicator data;
- excessive LLM schema failures;
- invalid account state.

Circuit breakers must:

- identify the trip reason;
- preserve SELL capability;
- notify operators;
- require an explicit reset or documented recovery policy;
- emit durable events and metrics.

---

## P2.3 — Distributed execution coordination

Only required if Floor Broker scales beyond one replica.

Options may include:

- database-backed idempotency and reservations;
- distributed locks;
- serialized command queue;
- transactional outbox pattern.

Do not scale Floor Broker horizontally until risk checks and submission remain atomic across replicas.

---

## P2.4 — Kubernetes NetworkPolicies and hardened runtime controls

Add:

- namespace default-deny policies;
- Dealer-to-Floor-Broker allow rules;
- required egress rules for Alpaca, TAAPI, Slack, LangSmith, and local LLM access;
- read-only root filesystems where compatible;
- explicit writable volumes;
- service-account token disabling where unused;
- namespace and RBAC review.

---

## P2.5 — Image signing, SBOM, and verification

For every image:

- generate an SBOM;
- scan for vulnerabilities;
- sign the image;
- publish immutable digest references;
- verify signatures before deployment;
- document exception handling for critical CVEs.

Prefer digest-pinned deployments for releases.

---

## P2.6 — Canary strategy and model rollout

Build on shadow mode to support controlled execution rollout:

- small subset of symbols;
- reduced risk budget;
- restricted time window;
- automatic rollback criteria;
- active-versus-canary comparison;
- separate strategy and model version labels.

Canary execution must use independent decision IDs and explicit exposure limits.

---

## P2.7 — Durable messaging, only if justified

Do not introduce Kafka, RabbitMQ, or another queue solely to make the system appear more distributed.

Adopt durable messaging only when requirements demonstrate a need for:

- replay across service outages;
- independent consumer scaling;
- guaranteed delivery;
- durable command sequencing;
- decoupled event processing.

Document the failure mode that the queue solves before adding it.

---

# Release milestones

## v0.2 — Safe execution

Target scope:

- all P0 items complete;
- CI green;
- reproducible image builds;
- idempotent paper orders;
- centralized risk policy;
- runtime BUY kill switch;
- documented operator runbook.

## v0.3 — Measurable strategy

Target scope:

- durable events;
- exact prompt/model/input capture;
- shadow mode;
- forward evaluation;
- deterministic baselines;
- initial operational metrics.

## v0.4 — Production-platform hardening

Target scope:

- full observability;
- automatic circuit breakers;
- hardened Kubernetes networking;
- signed images and SBOMs;
- canary rollout support.

---

# Definition of done

A roadmap item is complete only when:

1. implementation is merged;
2. automated tests cover expected and failure behavior;
3. CI passes;
4. documentation is updated;
5. operational behavior is observable;
6. rollback or disable behavior is documented;
7. no real-trading capability has been introduced.
