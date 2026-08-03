# Roadmap

Derived from an external code review (7.5/10) of the repo as an AI-platform/agentic-systems
portfolio project. The review's ~25 recommendations are grouped into three tiers below. **P0 is
scoped and ready to implement but not yet started** — this document itself is the current
deliverable; P0 code changes land in a follow-up pass, each still subject to this repo's standing
rule of an explicit go-ahead per commit/push/deploy. P1 and P2 are captured here so the review's
findings aren't lost, but neither has a concrete design yet.

## Status

| Item                                         | Status     | Notes                                                                 |
| --------------------------------------------- | ---------- | ---------------------------------------------------------------------- |
| P0.1 Crypto minimum-notional: skip, not clamp | 📋 Planned | Reverses this repo's earlier clamp-up fix per the review               |
| P0.2 Floor Broker risk-policy authority        | 📋 Planned | New `risk.py` + `config.yaml` `risk:` block                            |
| P0.3 Single-quote sizing                       | 📋 Planned | One ask fetch threaded through qty + TP/SL                             |
| P0.4 Zero-quantity guard                       | 📋 Planned | Skip a BUY sized to less than one share                                |
| P0.5 Positive-price guard on stops             | 📋 Planned | Reject a computed `stop_loss_px <= 0`                                  |
| P0.6 Meaningful HTTP status codes              | 📋 Planned | `reason` codes on `execution.py` results drive `app.py` status mapping |
| P0.7 Dependency pinning + CI                   | 📋 Planned | Split `requirements.txt`, pin versions, add `ci.yaml` running pytest   |
| P1 Durable decision/event schema               | 📋 Planned | No design yet                                                          |
| P1 Idempotency keys                            | 📋 Planned | No design yet                                                          |
| P1 Backtesting / evaluation harness            | 📋 Planned | No design yet                                                          |
| P1 Wire up or drop `size_hint`                 | 📋 Planned | No design yet                                                          |
| P1 Daily loss / trade-count limits             | 📋 Planned | No design yet; needs Alpaca account-activities API                     |
| P2 Prometheus metrics                          | 📋 Planned | No design yet                                                          |
| P2 Automatic circuit breaker / kill switch      | 📋 Planned | P0.2 ships a manual kill switch only                                   |
| P2 k8s NetworkPolicies + securityContext        | 📋 Planned | No design yet                                                          |
| P2 Image signing / SBOM                        | 📋 Planned | No design yet                                                          |
| P2 Shadow mode / canary evaluation             | 📋 Planned | No design yet                                                          |

## P0 — Harden what exists (scoped, not yet implemented)

1. **Crypto minimum-notional: skip, not clamp.** `src/floor_broker/execution.py::buy()` currently
   clamps a too-small crypto notional up to Alpaca's $10 minimum (`MIN_CRYPTO_NOTIONAL`). The
   review flagged that silently inflating the order size can exceed the caller's intended budget.
   Fix: skip the BUY instead (`status="skipped"`, `reason="budget_below_minimum"`), update
   `tests/floor_broker/test_execution.py` and `docs/architecture.md` accordingly.

2. **Floor Broker becomes the risk-policy authority.** `ExecuteRequest`
   (`src/floor_broker/app.py`) has zero `Field()` constraints today — Floor Broker trusts
   Dealer-supplied `budget`/`slP`/`tpP` almost unconditionally. Add real Pydantic constraints,
   plus a new `src/floor_broker/risk.py` enforcing a `config.yaml` `risk:` block
   (`max_order_notional`, `max_symbol_exposure`, `max_open_positions`, `kill_switch`) before any
   BUY reaches `execution.py`. SELL is never blocked (de-risking should always be allowed).
   Daily-realized-loss and max-trades-per-day limits are deferred to P1 — they need Alpaca's
   account-activities API and are meaningfully more involved than the other checks.

3. **Single-quote sizing.** `bracket_buy_with_SLTP()` and `get_qty()`
   (`src/floor_broker/execution.py`) each independently call `get_current_ask_price()` — two
   quotes that can disagree. Fetch the ask once in `buy()` and thread it through both.

4. **Zero-quantity guard.** A budget too small for one share currently isn't caught before order
   construction. Add a `qty < 1` check right after sizing, returning
   `status="skipped"`/`reason="insufficient_qty"`.

5. **Positive-price guard on stops.** `bracket_buy_with_SLTP()` should reject (not silently
   submit) a computed `stop_loss_px <= 0`.

6. **Meaningful HTTP status codes.** Every `execution.py` result already carries `status` — add a
   stable `reason` code (`open_orders_or_position`, `no_position`, `budget_below_minimum`,
   `insufficient_qty`) so `app.py` can map outcomes to HTTP status instead of returning 200 for
   everything except unexpected exceptions: 200 for executed/no-op, 409 for a BUY blocked by an
   existing position/order, 422 for a request that can't be fulfilled as given (risk violation,
   insufficient qty, below-minimum budget, Alpaca rejection after retry), 500 unchanged for
   unexpected errors. Distinguishing "Alpaca rejected the order" from "Alpaca's API is down" (502)
   is deferred — it needs inspecting `APIError`'s HTTP status, not just its Alpaca error code.

7. **Dependency pinning + CI.** `requirements.txt`'s 17 dependencies are entirely unpinned, and no
   workflow runs pytest despite a full `tests/` tree existing. Split into `requirements-core.txt`
   (what Floor Broker actually needs — fastapi, uvicorn, alpaca-py, requests, pyyaml, omegaconf,
   kubernetes, pytz) and `requirements-llm.txt` (core + langchain/langgraph/langsmith/openai/
   anthropic/pandas/numpy, for Analyst/Dealer/EOD Report), pin every version, and update each
   Dockerfile to install only the tier it needs. Add `.github/workflows/ci.yaml` running pytest on
   a plain `ubuntu-latest` runner (no DGX needed — the test suite is fully mocked). A full
   `pyproject.toml`/lockfile migration is a bigger structural change, noted here rather than
   bundled in.

## P1 — Establish trading validity (future, not yet designed)

- **Durable decision/event schema.** Dealer's per-decision `reasoning` is only ever sent to
  Slack/logs as free text — never persisted structurally. The `portfolio` ConfigMap only carries
  Analyst's picks, not Dealer's decisions. There's no record anywhere of what the system decided
  and why, beyond ephemeral logs.
- **Idempotency keys.** No idempotency key (`client_order_id` or similar) exists anywhere in the
  Dealer → Floor Broker → Alpaca path, and Dealer's poll loop (`src/dealer/main.py`) has no
  dedup/replay guard.
- **Backtesting / evaluation harness.** No historical-price, backtesting, evaluation, or
  baseline-strategy code exists anywhere in the repo — there is currently no evidence the trading
  strategy has any edge over a naive baseline (e.g. buy-and-hold, random entry).
- **`size_hint` is validated but unused.** `Signal.size_hint` (`src/dealer/schema.py`,
  `Field(default=1.0, ge=0.0, le=1.0)`) is produced by the LLM and validated by Pydantic but never
  read in `call_floor_broker` (`src/dealer/graph.py`) — the interface looks more sophisticated
  than its actual behavior. Either wire it into sizing or drop it from the schema.
- **Daily loss / trade-count limits.** Carried over from P0's risk module (item 2 above) — these
  need Alpaca's account-activities API and belong with the rest of the trading-validity work.

## P2 — Production-platform concerns (future, not yet designed)

- **Metrics.** No Prometheus metrics anywhere in the system — no visibility into decision
  latency, order success/rejection rates, or per-symbol exposure over time.
- **Circuit breakers / kill switch at the platform level.** P0 adds a `config.yaml` kill switch,
  but there's no automatic trip condition (e.g. N consecutive rejections, daily loss threshold) —
  it's manual-only.
- **NetworkPolicies.** `k8s/*-k3s.yaml` sets `resources.requests/limits` on all 4 workloads and
  Floor Broker has liveness/readiness probes, but zero `securityContext`
  (`runAsNonRoot`/`readOnlyRootFilesystem`/capability drops) and zero `NetworkPolicy` resources
  exist anywhere in `k8s/`. All 4 Dockerfiles also run as root.
- **Image signing / SBOM.** No supply-chain verification on the GHCR images built by `Build and
  Push`.
- **Shadow mode / canary evaluation.** No mechanism to run a new strategy or model version
  alongside the live one without risking real (paper) trades on it.
