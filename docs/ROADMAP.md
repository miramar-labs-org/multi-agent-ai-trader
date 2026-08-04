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
| P0.3 | Constrained Floor Broker request schema | P0 | Planned | — |
| P0.4 | Centralized Floor Broker risk policy | P0 | Planned | P0.3 |
| P0.5 | Runtime BUY kill switch | P0 | Planned | P0.4 |
| P0.6 | Serialize risk check and order submission | P0 | Planned | P0.4 |
| P0.7 | Idempotent execution and Alpaca client order IDs | P0 | Planned | P0.3 |
| P0.8 | Single-quote stock sizing | P0 | Planned | — |
| P0.9 | Quantity and bracket-price invariant checks | P0 | Planned | P0.8 |
| P0.10 | Structured execution outcomes and HTTP contract | P0 | Planned | P0.3 |
| P0.11 | Dependency split and reproducible pinning | P0 | Planned | — |
| P0.12 | CI, linting, validation, and image-build checks | P0 | Partial (pytest + ruff check only; see below) | P0.11 |
| P0.13 | Baseline container security | P0 | Planned | P0.11 |
| P1.1 | Durable decision and event schema | P1 | Planned | P0 complete |
| P1.2 | Exact model, prompt, and input version capture | P1 | Planned | P1.1 |
| P1.3 | Shadow execution mode | P1 | Planned | P1.1 |
| P1.4 | Forward evaluation and replay | P1 | Planned | P1.1, P1.3 |
| P1.5 | Historical backtesting harness | P1 | Done (deterministic baselines only; see below) | P1.1 |
| P1.6 | Deterministic baseline strategies | P1 | Done | P1.4 or P1.5 |
| P1.7 | Resolve `size_hint` semantics | P1 | Planned | P1.1 |
| P1.8 | Daily loss, trade-count, and aggregate exposure controls | P1 | Planned | P0.4, P1.1 |
| P1.9 | Core operational metrics | P1 | Planned | P1.1 |
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
| Alpaca timeout, connectivity failure, or temporary unavailability | 503 |
| Malformed or unrecognized upstream response | 502 |
| Unexpected internal failure | 500 |

Known Alpaca business rejections should preserve the upstream error code and use `retryable=false` unless explicitly known to be transient.

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

# P1 — Evidence and strategy validation

P1 is complete when the system can explain, replay, and evaluate its decisions using durable records rather than ephemeral logs.

## P1.1 — Durable decision and event schema

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

Choose one:

1. remove `size_hint`;
2. rename it as informational only;
3. use it deterministically as a bounded fraction of an authorized budget.

If used:

```text
proposed_notional = authorized_budget × size_hint
```

The Floor Broker must still apply all P0 risk limits.

Document whether `size_hint=0` means HOLD, a skipped BUY, or a valid zero-allocation signal.

---

## P1.8 — Daily loss, trade-count, and aggregate exposure controls

Extend `risk.py` with:

- maximum realized daily loss;
- maximum total daily P&L drawdown;
- maximum trades per day;
- maximum failed submissions per interval;
- maximum aggregate exposure by asset class;
- optional cooldown after repeated rejection or loss events.

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
