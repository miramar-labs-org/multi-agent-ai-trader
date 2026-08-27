# Options MCP loop — crash fix + make the feature work

Date: 2026-08-27
Follows: [`2026-08-25-options-trading-mcp-design.md`](2026-08-25-options-trading-mcp-design.md) (the original feature build)

## Problem

The Dealer's option-contract-selection step (`_select_option_contract_async`,
`src/dealer/graph.py:418`) has hard-hung the DGX Spark **twice on 2026-08-27**
(10:20–10:41 and again ~12:59, both ending in a manual reboot). It also does not
work: every invocation returns `completion_tokens: 1` and fails
`OptionContractPick` parsing, so no option contract has ever been selected.

### Root cause (verified from journald + pod logs, 2026-08-27)

The agentic tool-calling loop accumulates and re-sends unbounded context:

```python
for _ in range(_MAX_TOOL_CALL_ROUNDS):          # = 6
    response = agent_llm.invoke(messages)
    messages.append(response)
    if not response.tool_calls:
        break
    for call in response.tool_calls:
        result = await tool.ainvoke(call["args"])
        messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
structured_llm = llm.with_structured_output(OptionContractPick)
return structured_llm.invoke(messages)
```

- `str(result)` dumps the **entire** raw Alpaca `get_option_chain` snapshot
  payload (every contract, every Greek) into the message history, and the whole
  history is re-sent every round. The MCP tool description itself warns "The
  response can be very large."
- Observed prompts to Ollama from the Dealer pod: **258,075** then **376,452**
  tokens (`WARN truncating input prompt limit=131074 prompt=376452`). Dealer-pod
  logs show per-call prompt sizes of 71,593 / 76,980 / 113,899 tokens.
- `qwen3.6:35b-a3b` is a hybrid/SWA model: Ollama logs
  `forcing full prompt re-processing due to lack of cache data` — every one of
  these 100K+ token prompts is fully recomputed (~60–150 s), back-to-back, every
  10-minute Dealer cycle (`trading.pollsecs: 600`), for hours.
- Ollama's context default is unpinned (`OLLAMA_CONTEXT_LENGTH:0` →
  `vram-based default context ... default_num_ctx=262144`), so nothing rejects
  these prompts — the runner sizes 256K-context compute buffers and the prompt
  cache thrashes at its 8 GB cap.
- Sustained GPU saturation + unified-memory/cache thrash on the GB10 →
  kernel/GPU stall → silent hard hang (no OOM-killer, no panic, no Xid, no
  thermal trip, no kdump — same signature both times).

The empty-response failure is the same cause: a ~114K-token prompt overwhelms
the model, which emits ~1 token and stops.

### Why it surfaced now

`1d3c9f4` (PR #4, 09:32) fixed the `api_key` error that until then killed the
option loop *before its first tool call*. That turned the loop on for real.
`0d0a773` (PR #6) removed the earlier two-model Ollama OOM, which had been
masking this by crashing the box first.

## Goals

1. The Dealer option-selection loop can never again send an oversized prompt to
   Ollama — bounded by construction, not by luck.
2. Option-contract selection actually produces a valid `OptionContractPick` for a
   BUY/SELL signal on a stock, verified by one live Dealer cycle.
3. Defense in depth at the Ollama layer: no single request from any workload can
   demand a 256K-token context on the shared DGX box.

## Non-goals

- No change to the Alpaca MCP integration itself — `alpaca-mcp-server` stdio
  subprocess, `get_options_tools()`, `agent_llm.bind_tools(tools)`, the
  tool-calling loop, and the final `with_structured_output(OptionContractPick)`
  call all stay. MCP-driven selection is a hackathon requirement.
- No change to Floor Broker execution, the `options_trades` table, risk gates in
  `call_floor_broker_option`, or exit management.
- No model change. `qwen3.6:35b-a3b` stays.
- No host watchdog / autoscaler in this spec (possible follow-up).

---

## Part A — Dealer option-selection loop (this repo)

All changes are within `src/dealer/graph.py` and a new small helper module.
`_MAX_TOOL_CALL_ROUNDS` stays 6; the token-budget guard below is the real limiter.

### A1. Compact every tool result before it enters the message history

New module `src/dealer/option_chain.py` with a pure function:

```python
def compact_tool_result(tool_name: str, raw: str, *, max_contracts: int = 40,
                        max_chars: int = 6000) -> str
```

- For `get_option_chain` / `get_option_snapshot` payloads: parse JSON, walk the
  contracts (Alpaca snapshot shape is `{"snapshots": {<OCC symbol>: {...}}}` or a
  bare `{<OCC symbol>: {...}}` map), and emit a compact line per contract with
  only the fields the selector needs:
  `symbol, strike, expiration, right, delta, gamma, theta, iv, bid, ask,
  open_interest, day_volume`. Strike/expiration/right are parsed from the OCC
  symbol when not present as fields. Cap to `max_contracts` (keep those nearest
  the target-delta midpoint when over the cap) and append a
  `"… N more contracts omitted; narrow your filters"` marker.
- For any other tool, or on any JSON parse failure: return `raw` truncated to
  `max_chars` with a `"[truncated]"` marker.
- Deterministic, no I/O — unit-tested directly.

The loop calls `compact_tool_result(call["name"], str(result))` instead of
`str(result)` when building each `ToolMessage`.

### A2. Steer the LLM to filtered chain calls

Extend the system prompt in `_select_option_contract_async` to instruct:

- Always call `get_option_chain` with `type` (`call`/`put`), `expiration_date_gte`
  / `expiration_date_lte` computed from the DTE window
  (`today + dte_min` … `today + dte_max`), and a `limit`.
- Never request the full chain.
- Include the concrete date bounds in the human message so the model has them
  verbatim.

### A3. Cumulative token-budget guard

In the loop, after appending each round's messages, estimate the total prompt
size (`langchain_core.messages.utils.count_tokens_approximately`, or a
`sum(len(str(m.content))) // 4` heuristic — plan decides). Constants near
`_MAX_TOOL_CALL_ROUNDS`:

```python
_OPTION_PROMPT_TOKEN_BUDGET = 12_000   # stop looping once the history reaches this
_OPTION_PROMPT_TOKEN_HARD_CAP = 24_000 # never invoke the LLM above this — trim oldest ToolMessages first
```

- When the estimate exceeds `_OPTION_PROMPT_TOKEN_BUDGET`: stop the tool-calling
  loop, log `⚠️ option selection: token budget reached, forcing final pick`, and
  proceed to the structured-output call with the history so far.
- Before **every** `agent_llm.invoke` / `structured_llm.invoke`: if the estimate
  exceeds `_OPTION_PROMPT_TOKEN_HARD_CAP`, drop the oldest `ToolMessage`s (keep
  the system + human + most recent tool results) until under the cap. This is the
  hard guarantee that the Dealer cannot send Ollama a giant prompt even if A1/A2
  regress.

### A4. Client-side request guard (defense in depth)

Both `ChatOpenAI` clients in `graph.py` (`llm_call` at `:67` and
`_select_option_contract_async` at `:426`) gain:

- `timeout=<cfg.llm.request_timeout_s, default 120>` — a hung/2-minute Ollama
  call fails fast instead of stacking.
- `max_retries=0` — no silent retry amplification.

New optional `llm.request_timeout_s` config key (default 120), documented in
`config.yaml` / `config.default.yaml`.

### A5. Deterministic fallback pick

If the final `structured_llm.invoke(messages)` raises or returns an unparseable
result, fall back to a deterministic choice rather than returning `None`:

- From the compacted candidates seen during the loop (tracked in a list as the
  loop runs), keep those satisfying every hard constraint — DTE window, delta in
  `[target_delta_min, target_delta_max]`, `open_interest >= min_open_interest`,
  `day_volume >= min_volume`.
- Pick the one whose delta is closest to the midpoint of the target-delta window;
  tie-break on tightest bid/ask spread.
- Build the `OptionContractPick` from that contract's fields
  (`premium` = mid of bid/ask), `reasoning` = `"deterministic fallback:
  LLM pick unavailable"`.
- If no candidate passes, return `None` (existing behaviour → Floor Broker skip).

This keeps options trading working — a hackathon requirement — even when the
agentic LLM step flakes, while the MCP tool-calling path remains primary.
`call_floor_broker_option`'s existing risk gates still run on the fallback pick.

### A6. Reuse the MCP tool set across the cycle (minor)

`get_options_tools()` currently spawns a fresh `alpaca-mcp-server` subprocess on
every symbol (two FastMCP startup banners per cycle in the logs). Cache the
resolved tool list for the lifetime of one Dealer poll cycle (module-level
`functools.lru_cache` keyed on the account env-var names, or a cleared-per-cycle
cache). Not a crash cause — bundled here because the code is being touched and it
removes real per-symbol overhead.

---

## Part B — Ollama context pin (companion PR in `miramar-platform-gcp`)

`ollama.service` on the DGX is a plain systemd unit with no drop-in management
today. `dgx/ollama/deploy_ollama.sh` runs on the host via the **Ollama Deploy**
GHA workflow (`workflow_dispatch`, `runner: dgx`, `model: qwen3.6:35b-a3b`).

### B1. Write a systemd drop-in before loading the model

In `deploy_ollama.sh`, before the "Load into GPU memory" step, add a step that
writes `/etc/systemd/system/ollama.service.d/10-context.conf`:

```ini
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=32768"
```

- Value driven by a script variable `OLLAMA_CONTEXT_LENGTH="${3:-32768}"` (third
  positional arg), so the workflow can override it later without another repo
  change.
- Only `daemon-reload` + `systemctl restart ollama` **when the file content
  changed** (compare against existing); otherwise leave the running service
  alone. After a restart, re-wait for `/api/tags` before proceeding to the pull.
- `undeploy_ollama.sh` leaves the drop-in in place (it's a service-level setting,
  not per-model).

### B2. Workflow input

Add an optional `context_length` input to `.github/workflows/deploy-ollama.yaml`
(default `32768`), passed as the third arg to the deploy script alongside
`model` and `VRAM_USEABLE`.

### B3. Value rationale

- `qwen3.6:35b-a3b` KV cache is small (5 GB even at 262K), so this is not about
  KV memory — it's about (a) Ollama **truncating** oversized prompts to 32K
  instead of fully reprocessing 130K+, and (b) sizing compute buffers for 32K.
- 32K comfortably covers every legitimate prompt in this project: Dealer stock
  signal (~few K), compacted option selection (~≤12K after Part A), Analyst
  selection (candidate pool of 20 + research + track record — measure during
  verification; bump the drop-in to 65536 in a one-line follow-up if Analyst
  prompts approach the cap).
- A client can still request a larger `num_ctx` per-request if ever needed; this
  only moves the **default**.

---

## Data flow (option selection, after this change)

```
Dealer llm_call → BUY/SELL on a stock, confidence ≥ min_confidence
   │
   ▼
select_option_contract → _select_option_contract_async
   │  system+human prompt now carries explicit type + expiration_date bounds
   ▼
loop (≤6 rounds, stops early at 12K-token budget):
   agent_llm.invoke(messages)            ← history hard-capped at 24K tokens
   → get_option_chain(type=…, expiration_date_gte/lte=…, limit=…)
   → compact_tool_result() → ≤40 contracts, ≤6K chars per ToolMessage
   (candidates accumulated in a list for the fallback path)
   ▼
structured_llm.invoke(messages) → OptionContractPick
   │  on parse failure → A5 deterministic fallback from accumulated candidates
   ▼
call_floor_broker_option → existing risk gates → Floor Broker /execute-option
```

## Error handling

- Loop token budget reached → log + proceed to final pick (not an error).
- History hard-cap hit → trim oldest `ToolMessage`s, log once.
- LLM call timeout (A4) → exception propagates to `select_option_contract`'s
  existing `except Exception` → logs `💥 option contract selection failed` →
  `option_pick = None` → Floor Broker skip (unchanged existing behaviour).
- Structured parse failure → A5 fallback; only `None` if no candidate passes the
  hard constraints.
- MCP subprocess launch failure → unchanged (existing `except` in
  `select_option_contract`).

## Testing

Offline (`./.venv/bin/pytest -q`, must stay green; `ruff check .` clean):

- `tests/dealer/test_option_chain.py` (new) — `compact_tool_result`:
  - real Alpaca snapshot fixture → compact lines, correct field extraction, OCC
    symbol parsing, `max_contracts` cap keeps nearest-target-delta, marker text.
  - non-JSON / unknown-tool input → char truncation + marker.
- `tests/dealer/test_dealer_graph.py` (extend) — with a fake `agent_llm` that
  emits scripted tool calls returning large fixtures:
  - message history never exceeds the hard cap (assert on estimated tokens).
  - loop stops when the token budget is reached even if rounds remain.
  - `_MAX_TOOL_CALL_ROUNDS` still caps a tool-call-happy model.
  - structured pick success → `OptionContractPick` returned as today.
  - structured pick raises → A5 fallback returns the closest-to-target-delta
    candidate; all-fail → `None`.
  - existing `test_select_option_contract_async_passes_api_key_and_needs_no_openai_env`
    still passes (client now also has `timeout` / `max_retries=0`).
- `miramar-platform-gcp`: `bash -n` + `shellcheck` on `deploy_ollama.sh`;
  assert the drop-in is only rewritten/restarted on content change (script-level
  guard, reviewed by reading).

Live (one cycle, on the DGX, after both PRs merge and Ollama Deploy re-runs):

- Trigger / wait for one Dealer poll cycle with a stock BUY at confidence ≥ 0.6.
- Confirm in dealer-pod logs: `get_option_chain` called **with** filter args;
  per-call prompt sizes < ~15K tokens (from Ollama's own request logging);
  a real `OptionContractPick` (or an explicit A5 fallback line) — not
  `completion_tokens: 1`.
- Confirm Slack: `📜` Dealer signal line + Floor Broker option result line.
- Confirm `kubectl -n multi-agent-ai-trader` dealer pod does not OOM/restart; DGX
  `journalctl` shows no reprocessing of >32K-token prompts and stays responsive
  through the cycle.
- Check one Analyst run's prompt size against the 32K pin; note the headroom.

## Rollout sequence

1. **Part A** PR in this repo → merge → auto-deploy chain (Test & Lint → Build &
   Push → Deploy) green. This alone stops the Dealer from emitting giant prompts.
2. **Part B** PR in `miramar-platform-gcp` → merge → run **Ollama Deploy**
   (`runner: dgx`, `model: qwen3.6:35b-a3b`) to apply the drop-in + reload.
3. Live-cycle verification above.
4. Update `docs/models.md` postmortem section and this repo's
   `docs/architecture.md` option-flow prose to match; note the incident in the
   handoff. Write a new memory file for the GB10 silent-hang incident class
   (trigger, signature, mitigation) so a future session recognises it.

## Risks

- **qwen3.6 structured-output + tool-calling reliability.** Even at small context
  the model may not reliably emit a parseable `OptionContractPick` or clean tool
  calls. Mitigated by A5 (deterministic fallback) so the feature still produces
  trades; if the LLM path proves consistently unusable the follow-up is
  `method="json_mode"` or a prompt-parse extraction, tracked separately.
- **Analyst prompt vs 32K pin.** If Analyst prompts exceed ~32K the pin would
  truncate them. Measured during verification; one-line bump to 65536 if needed.
- **Drop-in restart mid-trading.** `systemctl restart ollama` drops the loaded
  model briefly. Ollama Deploy already unloads/reloads the model, so run Part B
  during a power-down window or accept a one-cycle gap — the power scheduler
  reloads with `keep_alive=-1` afterwards.
