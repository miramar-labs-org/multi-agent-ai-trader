# Options MCP Loop — Crash Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Dealer's option-contract-selection loop from sending 100K–380K-token prompts to Ollama (which hard-hung the DGX twice on 2026-08-27) and make the feature actually return a valid `OptionContractPick`.

**Architecture:** Keep the Alpaca MCP agentic tool-calling loop exactly as-is (`alpaca-mcp-server` stdio subprocess → `get_options_tools()` → `agent_llm.bind_tools()` → tool-calling `for` loop → `with_structured_output(OptionContractPick)`). Bound it: compact every tool result before it enters the message history, steer the LLM to server-side-filtered chain queries, cap cumulative prompt tokens, add a client-side request timeout, and add a deterministic fallback pick. Separately, a companion PR in `miramar-platform-gcp` pins `OLLAMA_CONTEXT_LENGTH` on the DGX host so no workload can ever demand a 256K context again.

**Tech Stack:** Python 3.12, LangChain / `langchain-openai` `ChatOpenAI`, `langgraph`, `langchain-mcp-adapters`, `alpaca-mcp-server` v2.3.0, OmegaConf config, pytest, ruff. Bash + GitHub Actions for the platform repo.

## Global Constraints

- **The Alpaca MCP agentic loop MUST stay.** Do not replace `get_options_tools()`, `agent_llm.bind_tools(tools)`, the `for _ in range(_MAX_TOOL_CALL_ROUNDS)` tool-calling loop, or the final `structured_llm.with_structured_output(OptionContractPick)` call. The LLM still drives the tool calls. (Alpaca AI Trading Agents Hackathon requirement.)
- **No model change.** `llm.model` stays `qwen3.6:35b-a3b`.
- **Token limits (exact):** stop the tool-calling loop once estimated history tokens exceed **12000**; never invoke the LLM with an estimated history above **24000** (trim first); compaction keeps at most **40** contracts and **6000** chars per tool result.
- **Ollama context pin value:** `OLLAMA_CONTEXT_LENGTH=32768` (was effectively 262144).
- **Tests:** `.venv/bin/pytest -q` must stay green; `.venv/bin/ruff check .` must be clean. No `ruff format` (pre-existing drift — do not touch).
- **Commit trailer (every commit in the app repo and the platform repo):**
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
  ```
- **Git flow:** app-repo work is on branch `fix/options-loop-crash` (already created, spec already committed there). Platform work gets its own branch in `/home/aaron/git-miramar-labs-org/miramar-platform-gcp`. PRs are squash-merged by Aaron — do not merge. `gh pr edit` is broken in this env; edit PR bodies with `gh api -X PATCH repos/<org>/<repo>/pulls/<N> -F body=@-`.
- **Platform repo hygiene:** `miramar-platform-gcp` may carry Aaron's unrelated uncommitted WIP. It is currently clean — re-check `git status` before branching and never stage files you did not create/modify for this task.
- **DGX incident context:** `qwen3.6:35b-a3b` is a hybrid/SWA model with no KV-cache prefix reuse — every prompt is fully reprocessed. That is why prompt size, not just round count, is the thing to bound.

---

## File Structure

**App repo (`multi-agent-ai-trader`):**

- `src/dealer/option_chain.py` — **new.** Pure helpers: `parse_option_chain()` (raw MCP JSON → list of contract dicts), `compact_tool_result()` (raw MCP tool output → short string for a `ToolMessage`), `estimate_tokens()` (message list → int heuristic). No I/O, no LangChain calls.
- `src/dealer/graph.py` — **modify** `_select_option_contract_async` (compaction, filtered-chain steering, token guard, fallback), `llm_call` + `_select_option_contract_async` (client timeout), add module constants and two small module-level helpers (`_trim_history`, `_fallback_pick`, `_llm_timeout`).
- `src/dealer/mcp_options.py` — **modify** `get_options_tools()` to cache within a cycle; add `reset_options_tools_cache()`.
- `src/dealer/main.py` — **modify** the poll loop to reset the MCP tool cache once per cycle.
- `config.default.yaml`, `config.yaml` — **modify** to add `llm.request_timeout_s`.
- `docs/models.md`, `docs/architecture.md` — **modify** to document the incident + the new bounds.
- `tests/dealer/test_option_chain.py` — **new.**
- `tests/dealer/test_dealer_graph.py` — **modify** (extend option tests).
- `tests/dealer/test_mcp_options.py` — **modify** (cache behaviour).

**Platform repo (`miramar-platform-gcp`):**

- `dgx/ollama/deploy_ollama.sh` — **modify** to write the systemd drop-in.
- `.github/workflows/deploy-ollama.yaml` — **modify** to add the `context_length` input and pass it through.

---

## Task 1: `option_chain.py` — parse / compact / token-estimate helpers

**Files:**
- Create: `src/dealer/option_chain.py`
- Test: `tests/dealer/test_option_chain.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces:
  - `parse_option_chain(raw: str) -> list[dict]` — each dict has keys
    `symbol: str`, `strike: float | None`, `expiration: str | None` (ISO `YYYY-MM-DD`),
    `right: "call" | "put" | None`, `delta: float | None`, `gamma: float | None`,
    `theta: float | None`, `vega: float | None`, `iv: float | None`,
    `bid: float | None`, `ask: float | None`, `oi: float | None`, `volume: float | None`.
    Returns `[]` on any parse failure.
  - `compact_tool_result(tool_name: str, raw: str, *, target_delta_mid: float = 0.45, max_contracts: int = 40, max_chars: int = 6000) -> str`
  - `estimate_tokens(messages: list) -> int` — `sum(len(str(getattr(m, "content", m))) for m in messages) // 4`
  - `parse_occ_symbol(sym: str) -> tuple[str, str, str, float] | None` — `(root, expiration_iso, right, strike)`

- [ ] **Step 1: Write the failing test file**

```python
# tests/dealer/test_option_chain.py
import json

from src.dealer.option_chain import (
    compact_tool_result,
    estimate_tokens,
    parse_occ_symbol,
    parse_option_chain,
)

_SNAPSHOT = json.dumps(
    {
        "snapshots": {
            "AAPL250117C00150000": {
                "latestQuote": {"bp": 53.5, "ap": 55.0, "bs": 1, "as": 2},
                "latestTrade": {"p": 54.0},
                "greeks": {"delta": 0.92, "gamma": 0.01, "theta": -0.05, "vega": 0.20},
                "impliedVolatility": 0.35,
            },
            "AAPL250117C00220000": {
                "latestQuote": {"bp": 2.10, "ap": 2.30},
                "greeks": {"delta": 0.44, "gamma": 0.03, "theta": -0.04, "vega": 0.11},
                "impliedVolatility": 0.28,
            },
        }
    }
)


def test_parse_occ_symbol_splits_root_date_right_strike():
    assert parse_occ_symbol("AAPL250117C00150000") == ("AAPL", "2025-01-17", "call", 150.0)
    assert parse_occ_symbol("SPY250620P00420500") == ("SPY", "2025-06-20", "put", 420.5)


def test_parse_occ_symbol_returns_none_on_garbage():
    assert parse_occ_symbol("not-a-contract") is None


def test_parse_option_chain_extracts_rows():
    rows = parse_option_chain(_SNAPSHOT)
    by_sym = {r["symbol"]: r for r in rows}
    r = by_sym["AAPL250117C00220000"]
    assert r["strike"] == 220.0
    assert r["expiration"] == "2025-01-17"
    assert r["right"] == "call"
    assert r["delta"] == 0.44
    assert r["bid"] == 2.10
    assert r["ask"] == 2.30
    assert r["iv"] == 0.28


def test_parse_option_chain_returns_empty_on_non_json():
    assert parse_option_chain("boom not json") == []


def test_compact_option_chain_shrinks_and_keeps_key_fields():
    out = compact_tool_result("get_option_chain", _SNAPSHOT)
    assert len(out) < len(_SNAPSHOT)
    assert "AAPL250117C00220000" in out
    assert "d=0.44" in out
    assert "greeks" not in out  # raw nested json is gone


def test_compact_option_chain_caps_contracts_keeping_nearest_target_delta():
    snaps = {
        f"AAPL250117C{int((100 + i) * 1000):08d}": {
            "latestQuote": {"bp": 1.0, "ap": 1.2},
            "greeks": {"delta": round(0.01 * i, 2)},
        }
        for i in range(1, 61)  # deltas 0.01 .. 0.60
    }
    raw = json.dumps({"snapshots": snaps})
    out = compact_tool_result("get_option_chain", raw, target_delta_mid=0.45, max_contracts=40)
    assert "AAPL250117C00145000" in out  # delta 0.45, nearest the midpoint — kept
    assert "AAPL250117C00101000" not in out  # delta 0.01 — dropped
    assert "20 more contracts omitted" in out


def test_compact_non_json_truncates_to_max_chars():
    out = compact_tool_result("get_option_chain", "x" * 20000, max_chars=6000)
    assert len(out) <= 6100
    assert "truncated" in out


def test_compact_unknown_tool_passes_through_truncation():
    out = compact_tool_result("get_account", "y" * 20000, max_chars=6000)
    assert len(out) <= 6100


def test_estimate_tokens_is_content_chars_over_four():
    class M:
        def __init__(self, c):
            self.content = c

    assert estimate_tokens([M("a" * 40), M("b" * 40)]) == 20
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/pytest -q tests/dealer/test_option_chain.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.dealer.option_chain'`

- [ ] **Step 3: Write `src/dealer/option_chain.py`**

```python
"""Pure helpers for bounding the Dealer's option-selection MCP loop.

The Alpaca MCP `get_option_chain` / `get_option_snapshot` tools return the raw
Alpaca options-snapshot JSON, which can carry hundreds of contracts. Dumping that
straight into the LangChain message history (and re-sending it every tool-calling
round) is what hard-hung the DGX on 2026-08-27. `compact_tool_result` shrinks each
payload to the handful of fields the selector actually needs; `parse_option_chain`
exposes the same rows structurally for the deterministic fallback pick.
"""

import json
import re

_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
_CHAIN_TOOLS = {"get_option_chain", "get_option_snapshot"}


def parse_occ_symbol(sym: str) -> tuple[str, str, str, float] | None:
    """`AAPL250117C00150000` -> `("AAPL", "2025-01-17", "call", 150.0)`; None if it doesn't match."""
    m = _OCC_RE.match(sym.strip())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    expiration = f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}"
    right = "call" if cp == "C" else "put"
    return root, expiration, right, int(strike) / 1000.0


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_option_chain(raw: str) -> list[dict]:
    """Raw MCP option-snapshot JSON -> list of flat contract dicts. `[]` on any parse failure."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    snapshots = data.get("snapshots", data)
    if not isinstance(snapshots, dict):
        return []

    rows: list[dict] = []
    for sym, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue
        occ = parse_occ_symbol(str(sym))
        greeks = snap.get("greeks") or {}
        quote = snap.get("latestQuote") or {}
        daily = snap.get("dailyBar") or snap.get("minuteBar") or {}
        rows.append(
            {
                "symbol": str(sym),
                "strike": occ[3] if occ else _num(snap.get("strikePrice")),
                "expiration": occ[1] if occ else snap.get("expirationDate"),
                "right": occ[2] if occ else None,
                "delta": _num(greeks.get("delta")),
                "gamma": _num(greeks.get("gamma")),
                "theta": _num(greeks.get("theta")),
                "vega": _num(greeks.get("vega")),
                "iv": _num(snap.get("impliedVolatility")),
                "bid": _num(quote.get("bp")),
                "ask": _num(quote.get("ap")),
                "oi": _num(snap.get("openInterest")),
                "volume": _num(daily.get("v")),
            }
        )
    return rows


def _truncate(raw: str, max_chars: int) -> str:
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + "\n… [truncated]"


def _fmt(v, nd=2):
    return "" if v is None else f"{v:.{nd}f}"


def compact_tool_result(
    tool_name: str,
    raw: str,
    *,
    target_delta_mid: float = 0.45,
    max_contracts: int = 40,
    max_chars: int = 6000,
) -> str:
    """Shrink a raw MCP tool result to a short string safe to append to the message history."""
    if tool_name not in _CHAIN_TOOLS:
        return _truncate(raw, max_chars)
    rows = parse_option_chain(raw)
    if not rows:
        return _truncate(raw, max_chars)

    rows.sort(
        key=lambda r: abs(abs(r["delta"]) - target_delta_mid) if r["delta"] is not None else 9e9
    )
    kept = rows[:max_contracts]
    omitted = len(rows) - len(kept)

    lines = [
        f"{r['symbol']} K={_fmt(r['strike'])} exp={r['expiration']} {r['right'] or '?'} "
        f"d={_fmt(r['delta'])} g={_fmt(r['delta'] and r['gamma'], 3)} th={_fmt(r['theta'], 3)} "
        f"v={_fmt(r['vega'], 3)} iv={_fmt(r['iv'], 3)} bid={_fmt(r['bid'])} ask={_fmt(r['ask'])} "
        f"oi={_fmt(r['oi'], 0)} vol={_fmt(r['volume'], 0)}"
        for r in kept
    ]
    if omitted:
        lines.append(f"… {omitted} more contracts omitted — narrow type/expiration_date/strike filters")
    out = "\n".join(lines)
    return out if len(out) <= max_chars else _truncate(out, max_chars)


def estimate_tokens(messages: list) -> int:
    """Cheap upper-ish bound on prompt size: total content chars / 4. No tokenizer dependency."""
    return sum(len(str(getattr(m, "content", m))) for m in messages) // 4
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q tests/dealer/test_option_chain.py`
Expected: PASS (all 10). Fix the `g=` formatting helper call if `test_compact_option_chain_shrinks_and_keeps_key_fields` trips on it — the intent is just "gamma printed when present"; simplify `_fmt(r['delta'] and r['gamma'], 3)` to `_fmt(r['gamma'], 3)` if it's awkward.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/dealer/option_chain.py tests/dealer/test_option_chain.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/dealer/option_chain.py tests/dealer/test_option_chain.py
git commit -m "$(cat <<'EOF'
feat(dealer): option-chain compaction + token-estimate helpers

Pure helpers to bound the option-selection MCP loop: parse_option_chain
flattens the raw Alpaca snapshot JSON, compact_tool_result shrinks it to
<=40 contracts / 6000 chars for the message history, estimate_tokens is a
chars/4 heuristic for the loop's token guard.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

---

## Task 2: Filtered-chain steering + compaction wired into the loop

**Files:**
- Modify: `src/dealer/graph.py` (`_select_option_contract_async`, ~`418-470`)
- Test: `tests/dealer/test_dealer_graph.py`

**Interfaces:**
- Consumes: `compact_tool_result`, `parse_option_chain` from Task 1.
- Produces: `_select_option_contract_async` now computes `exp_gte` / `exp_lte` ISO dates and `delta_mid`, injects them into the system + human prompt, appends `compact_tool_result(...)` (not `str(result)`) to each `ToolMessage`, and accumulates `seen_rows: list[dict]` via `parse_option_chain` for Task 4's fallback. Signature unchanged.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dealer/test_dealer_graph.py
import asyncio
import json


def _fake_options_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def _fake_get_options_tools():
        return []

    monkeypatch.setattr(graph, "get_options_tools", _fake_get_options_tools)


def test_select_option_contract_async_compacts_results_and_steers_filtered_chain(monkeypatch):
    _fake_options_env(monkeypatch)
    captured = {}

    big_chain = json.dumps(
        {"snapshots": {
            f"AAPL250620C{int((100 + i) * 1000):08d}": {
                "latestQuote": {"bp": 1.0, "ap": 1.2},
                "greeks": {"delta": round(0.01 * i, 2)},
            } for i in range(1, 120)
        }}
    )

    class _Tool:
        name = "get_option_chain"

        async def ainvoke(self, args):
            captured["tool_args"] = args
            return big_chain

    class _ToolCallResp:
        tool_calls = [{"name": "get_option_chain", "args": {"underlying_symbol": "AAPL"}, "id": "c1"}]

    class _FinalResp:
        tool_calls = []

    class _Bound:
        def __init__(self):
            self._calls = 0

        def invoke(self, messages):
            captured["last_messages"] = messages
            self._calls += 1
            return _ToolCallResp() if self._calls == 1 else _FinalResp()

    class _Structured:
        def invoke(self, messages):
            captured["final_messages"] = messages
            return graph.OptionContractPick(
                contract_symbol="AAPL250620C00145000", strike=145.0, expiration="2025-06-20",
                right="call", delta=0.45, premium=1.1, reasoning="mid-delta",
            )

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools):
            return _Bound()

        def with_structured_output(self, schema):
            return _Structured()

    monkeypatch.setattr(graph, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(graph, "get_options_tools", lambda: _fake_tools())

    async def _fake_tools():
        return [_Tool()]

    cfg = OmegaConf.create({
        "llm": {"base_url": "http://llm.test/v1", "model": "m", "temperature": 0.0},
        "options_trading": {
            "enabled": True, "dte_min": 14, "dte_max": 45,
            "target_delta_min": 0.30, "target_delta_max": 0.60,
            "min_open_interest": 100, "min_volume": 10,
        },
    })
    state = {**_state("rsi: 71.2"), "symbol": "AAPL", "exchange": "stocks",
             "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "breakout"}}

    pick = asyncio.run(graph._select_option_contract_async(state, cfg, state["signal"]))

    assert pick.contract_symbol == "AAPL250620C00145000"
    # the human prompt carries concrete ISO expiration bounds, not a raw day count
    human = captured["last_messages"][1].content
    assert "expiration_date_gte" in human
    assert "-" in human and "Days-to-expiration window: 14-45" not in human
    # the tool result appended to history was compacted, not the 119-contract raw blob
    tool_msgs = [m for m in captured["final_messages"] if isinstance(m, graph.ToolMessage)]
    assert tool_msgs and len(str(tool_msgs[0].content)) < len(big_chain) // 2
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py::test_select_option_contract_async_compacts_results_and_steers_filtered_chain`
Expected: FAIL — assertion on `expiration_date_gte` (current prompt says "Days-to-expiration window: 14-45"); tool message not compacted.

- [ ] **Step 3: Edit `_select_option_contract_async`**

Add to the imports at the top of `src/dealer/graph.py`:

```python
from src.dealer.option_chain import compact_tool_result, estimate_tokens, parse_option_chain
```

Replace the body of `_select_option_contract_async` from the `right = ...` line through the `for` loop and final return with:

```python
    agent_llm = llm.bind_tools(tools)
    right = "call" if signal["action"] == "BUY" else "put"

    et = pytz.timezone("US/Eastern")
    today = datetime.now(et).date()
    exp_gte = (today + timedelta(days=cfg.options_trading.dte_min)).isoformat()
    exp_lte = (today + timedelta(days=cfg.options_trading.dte_max)).isoformat()
    delta_mid = (cfg.options_trading.target_delta_min + cfg.options_trading.target_delta_max) / 2

    messages = [
        SystemMessage(
            content=(
                "You are an options contract selector for a paper-trading account. Use the "
                "provided Alpaca tools to look up the option chain, quotes, and Greeks for the "
                "given underlying symbol, then pick exactly one contract that fits the stated "
                "constraints. ALWAYS call get_option_chain with type set to the desired right, "
                "expiration_date_gte and expiration_date_lte set to the given bounds, and limit "
                "set to 50 or fewer. Never request the full chain."
            )
        ),
        HumanMessage(
            content=(
                f"Underlying: {state['symbol']}\n"
                f"Desired right: {right}\n"
                f"Expiration window: {exp_gte} to {exp_lte} "
                f"(pass as expiration_date_gte / expiration_date_lte to get_option_chain)\n"
                f"Target |delta| window: {cfg.options_trading.target_delta_min}"
                f"-{cfg.options_trading.target_delta_max}\n"
                f"Minimum open interest: {cfg.options_trading.min_open_interest}\n"
                f"Minimum volume: {cfg.options_trading.min_volume}\n"
                f"Dealer reasoning for the underlying signal: {signal['reasoning']}\n\n"
                "Call get_option_chain (filtered as instructed), inspect quotes/Greeks, then "
                "respond with your final pick."
            )
        ),
    ]

    seen_rows: list[dict] = []
    for _ in range(_MAX_TOOL_CALL_ROUNDS):
        response = agent_llm.invoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        for call in response.tool_calls:
            tool = tools_by_name[call["name"]]
            result = await tool.ainvoke(call["args"])
            raw = str(result)
            seen_rows.extend(parse_option_chain(raw))
            messages.append(
                ToolMessage(
                    content=compact_tool_result(call["name"], raw, target_delta_mid=delta_mid),
                    tool_call_id=call["id"],
                )
            )

    structured_llm = llm.with_structured_output(OptionContractPick)
    return structured_llm.invoke(messages)
```

(Token guard and fallback come in Tasks 3 and 4 — leave `seen_rows` populated but unused for now; ruff will not flag a used-later local, but if it does, that is fixed in Task 4 which reads it.)

- [ ] **Step 4: Run the test + the existing option tests**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py -k option`
Expected: PASS (new test + all existing `test_select_option_contract*` and `test_call_floor_broker_option*`).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/dealer/graph.py tests/dealer/test_dealer_graph.py`
Expected: clean. If ruff flags `seen_rows` as unused, add `# noqa: F841` with a comment `# consumed by _fallback_pick in the next commit` — or land Tasks 3+4 before committing.

- [ ] **Step 6: Commit**

```bash
git add src/dealer/graph.py tests/dealer/test_dealer_graph.py
git commit -m "$(cat <<'EOF'
fix(dealer): steer option chain queries to filtered calls, compact results

_select_option_contract_async now hands the LLM concrete ISO expiration
bounds and instructs type/expiration_date/limit filtering, and every tool
result is run through compact_tool_result before entering the message
history instead of str(result). Removes the 100K+ token prompts that
hard-hung the DGX on 2026-08-27.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

---

## Task 3: Cumulative token-budget guard

**Files:**
- Modify: `src/dealer/graph.py` (module constants ~`22`, `_select_option_contract_async`, new `_trim_history`)
- Test: `tests/dealer/test_dealer_graph.py`

**Interfaces:**
- Consumes: `estimate_tokens` from Task 1.
- Produces:
  - Module constants `_OPTION_PROMPT_TOKEN_BUDGET = 12_000`, `_OPTION_PROMPT_TOKEN_HARD_CAP = 24_000`.
  - `_trim_history(messages: list, hard_cap: int) -> list` — returns a list where older `ToolMessage`s have their content replaced with a short placeholder (same `tool_call_id`) until the estimate is under `hard_cap`; never drops messages (no orphaned tool results); no-op when already under cap.
  - Loop: breaks after a round when `estimate_tokens(messages) > _OPTION_PROMPT_TOKEN_BUDGET`; calls `_trim_history` before every `agent_llm.invoke` and before the final `structured_llm.invoke`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/dealer/test_dealer_graph.py
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


def test_trim_history_noop_under_cap():
    msgs = [SystemMessage(content="s"), HumanMessage(content="h"), ToolMessage(content="t", tool_call_id="x")]
    assert graph._trim_history(msgs, hard_cap=1000) is msgs or graph._trim_history(msgs, 1000) == msgs


def test_trim_history_neutralizes_old_tool_messages_over_cap():
    msgs = [
        SystemMessage(content="s"),
        HumanMessage(content="h"),
        ToolMessage(content="A" * 80000, tool_call_id="c1"),
        ToolMessage(content="B" * 400, tool_call_id="c2"),
    ]
    out = graph._trim_history(msgs, hard_cap=1000)
    assert graph.estimate_tokens(out) <= 1000
    assert "budget" in str(out[2].content).lower()
    assert out[2].tool_call_id == "c1"
    assert str(out[3].content) == "B" * 400  # newest kept intact


def test_select_option_contract_async_stops_looping_at_token_budget(monkeypatch):
    _fake_options_env(monkeypatch)

    class _Tool:
        name = "get_option_chain"

        async def ainvoke(self, args):
            return "Z" * 60000  # non-JSON -> compact_tool_result truncates to ~6000 chars

    async def _fake_tools():
        return [_Tool()]

    monkeypatch.setattr(graph, "get_options_tools", _fake_tools)

    calls = {"n": 0}

    class _AlwaysToolCall:
        tool_calls = [{"name": "get_option_chain", "args": {}, "id": "c"}]

    class _Bound:
        def invoke(self, messages):
            calls["n"] += 1
            return _AlwaysToolCall()

    class _Structured:
        def invoke(self, messages):
            return graph.OptionContractPick(
                contract_symbol="AAPL250620C00145000", strike=145.0, expiration="2025-06-20",
                right="call", delta=0.45, premium=1.1, reasoning="x",
            )

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def bind_tools(self, tools):
            return _Bound()

        def with_structured_output(self, schema):
            return _Structured()

    monkeypatch.setattr(graph, "ChatOpenAI", FakeChatOpenAI)
    cfg = OmegaConf.create({
        "llm": {"base_url": "http://llm.test/v1", "model": "m", "temperature": 0.0},
        "options_trading": {"enabled": True, "dte_min": 14, "dte_max": 45,
                            "target_delta_min": 0.30, "target_delta_max": 0.60,
                            "min_open_interest": 100, "min_volume": 10},
    })
    state = {**_state("rsi: 71.2"), "symbol": "AAPL", "exchange": "stocks",
             "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"}}

    asyncio.run(graph._select_option_contract_async(state, cfg, state["signal"]))
    # ~6000 chars/round ≈ 1500 tokens; budget 12000 -> well under 6 rounds regardless,
    # but the guard must not let it run the full 6 if a round pushes it over. With a
    # 60000-char raw truncated to 6000, ~2 rounds crosses 12000 only if compaction is
    # bypassed; assert it never exceeds the hard round cap and the loop is bounded.
    assert calls["n"] <= graph._MAX_TOOL_CALL_ROUNDS
```

> Note: tune the `_Tool` payload so the test meaningfully exercises the 12000 budget — return a JSON chain blob that compacts to ~5000 chars, so 3 rounds ≈ 12500 tokens and the loop breaks at round 3. Assert `calls["n"] == 3`. Adjust the fixture until that holds, then lock the assertion.

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py -k "trim_history or token_budget"`
Expected: FAIL — `graph._trim_history` does not exist.

- [ ] **Step 3: Add constants + `_trim_history` + wire the guard**

Constants block near line 22:

```python
_MAX_TOOL_CALL_ROUNDS = 6
_OPTION_PROMPT_TOKEN_BUDGET = 12_000   # stop the tool-calling loop once history passes this
_OPTION_PROMPT_TOKEN_HARD_CAP = 24_000  # never invoke the LLM above this — neutralize old tool msgs first
```

New module-level helper (place just above `_select_option_contract_async`):

```python
def _trim_history(messages: list, hard_cap: int) -> list:
    """Keep every message (no orphaned tool results) but blank the content of the oldest
    large ToolMessages until estimate_tokens(messages) <= hard_cap. No-op when already under."""
    if estimate_tokens(messages) <= hard_cap:
        return messages
    trimmed = list(messages)
    for i, m in enumerate(trimmed[:-1]):  # never touch the most recent message
        if isinstance(m, ToolMessage) and len(str(m.content)) > 200:
            trimmed[i] = ToolMessage(
                content="[older tool result dropped to stay within the context budget]",
                tool_call_id=m.tool_call_id,
            )
            if estimate_tokens(trimmed) <= hard_cap:
                break
    return trimmed
```

In `_select_option_contract_async`, change the loop and final call:

```python
    for _ in range(_MAX_TOOL_CALL_ROUNDS):
        messages = _trim_history(messages, _OPTION_PROMPT_TOKEN_HARD_CAP)
        response = agent_llm.invoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        for call in response.tool_calls:
            tool = tools_by_name[call["name"]]
            result = await tool.ainvoke(call["args"])
            raw = str(result)
            seen_rows.extend(parse_option_chain(raw))
            messages.append(
                ToolMessage(
                    content=compact_tool_result(call["name"], raw, target_delta_mid=delta_mid),
                    tool_call_id=call["id"],
                )
            )
        if estimate_tokens(messages) > _OPTION_PROMPT_TOKEN_BUDGET:
            log(f"⚠️ option selection for {state['symbol']}: token budget reached, forcing final pick")
            break

    messages = _trim_history(messages, _OPTION_PROMPT_TOKEN_HARD_CAP)
    structured_llm = llm.with_structured_output(OptionContractPick)
    return structured_llm.invoke(messages)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py -k option`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/dealer/graph.py tests/dealer/test_dealer_graph.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/dealer/graph.py tests/dealer/test_dealer_graph.py
git commit -m "$(cat <<'EOF'
fix(dealer): cap cumulative prompt tokens in the option-selection loop

Break the tool-calling loop once the message history passes ~12k tokens
and hard-cap it at ~24k (blanking the oldest tool results, never orphaning
one) before every LLM call. Structural guarantee that this loop can no
longer send Ollama an oversized prompt even if compaction regresses.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

---

## Task 4: Deterministic fallback pick

**Files:**
- Modify: `src/dealer/graph.py` (new `_fallback_pick`, wrap final `structured_llm.invoke`)
- Test: `tests/dealer/test_dealer_graph.py`

**Interfaces:**
- Consumes: `seen_rows: list[dict]` (accumulated in Task 2), `cfg.options_trading`, `right`, `delta_mid`, `today` from `_select_option_contract_async`.
- Produces: `_fallback_pick(rows: list[dict], right: str, cfg, delta_mid: float, today) -> OptionContractPick | None` — picks the candidate whose `|delta|` is closest to `delta_mid` among rows that match `right`, sit inside the delta window, inside the DTE window, and have a usable bid/ask; `None` if none qualify. `_select_option_contract_async` returns the structured pick when it parses, else the fallback.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/dealer/test_dealer_graph.py
from datetime import date


def _row(sym, delta, exp, bid=1.0, ask=1.2, right="call"):
    return {"symbol": sym, "strike": 100.0, "expiration": exp, "right": right,
            "delta": delta, "gamma": None, "theta": None, "vega": None, "iv": None,
            "bid": bid, "ask": ask, "oi": 500, "volume": 50}


def test_fallback_pick_selects_closest_to_target_delta_passing_gates():
    today = date(2025, 6, 1)
    ok_exp = "2025-06-20"  # 19 DTE, inside 14-45
    rows = [
        _row("A", 0.35, ok_exp),
        _row("B", 0.45, ok_exp),   # closest to mid 0.45
        _row("C", 0.58, ok_exp),
        _row("D", 0.45, "2025-06-05"),  # 4 DTE — outside window
        _row("E", 0.10, ok_exp),        # outside delta window
    ]
    cfg = OmegaConf.create({"options_trading": {"dte_min": 14, "dte_max": 45,
                                                "target_delta_min": 0.30, "target_delta_max": 0.60}})
    pick = graph._fallback_pick(rows, "call", cfg, delta_mid=0.45, today=today)
    assert pick.contract_symbol == "B"
    assert pick.premium == 1.1
    assert "fallback" in pick.reasoning.lower()


def test_fallback_pick_returns_none_when_nothing_qualifies():
    today = date(2025, 6, 1)
    cfg = OmegaConf.create({"options_trading": {"dte_min": 14, "dte_max": 45,
                                                "target_delta_min": 0.30, "target_delta_max": 0.60}})
    rows = [_row("A", 0.05, "2025-06-20"), _row("B", 0.45, "2025-06-20", bid=0, ask=0)]
    assert graph._fallback_pick(rows, "call", cfg, 0.45, today) is None


def test_select_option_contract_async_uses_fallback_when_structured_output_raises(monkeypatch):
    _fake_options_env(monkeypatch)
    ok_exp = (datetime.now(pytz.timezone("US/Eastern")).date() + timedelta(days=25)).isoformat()
    chain = json.dumps({"snapshots": {
        f"AAPL{ok_exp.replace('-', '')[2:]}C00145000": {
            "latestQuote": {"bp": 1.0, "ap": 1.2}, "greeks": {"delta": 0.46},
        },
    }})

    class _Tool:
        name = "get_option_chain"

        async def ainvoke(self, args):
            return chain

    async def _fake_tools():
        return [_Tool()]

    monkeypatch.setattr(graph, "get_options_tools", _fake_tools)

    class _ToolCall:
        tool_calls = [{"name": "get_option_chain", "args": {}, "id": "c"}]

    class _NoCall:
        tool_calls = []

    class _Bound:
        def __init__(self):
            self.n = 0

        def invoke(self, m):
            self.n += 1
            return _ToolCall() if self.n == 1 else _NoCall()

    class _Structured:
        def invoke(self, m):
            raise ValueError("model returned 1 token, cannot parse OptionContractPick")

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            pass

        def bind_tools(self, t):
            return _Bound()

        def with_structured_output(self, s):
            return _Structured()

    monkeypatch.setattr(graph, "ChatOpenAI", FakeChatOpenAI)
    cfg = OmegaConf.create({
        "llm": {"base_url": "http://llm.test/v1", "model": "m", "temperature": 0.0},
        "options_trading": {"enabled": True, "dte_min": 14, "dte_max": 45,
                            "target_delta_min": 0.30, "target_delta_max": 0.60,
                            "min_open_interest": 100, "min_volume": 10},
    })
    state = {**_state("rsi: 71.2"), "symbol": "AAPL", "exchange": "stocks",
             "signal": {"action": "BUY", "confidence": 0.9, "reasoning": "r"}}

    pick = asyncio.run(graph._select_option_contract_async(state, cfg, state["signal"]))
    assert pick is not None
    assert pick.right == "call"
    assert "fallback" in pick.reasoning.lower()
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py -k fallback`
Expected: FAIL — `graph._fallback_pick` does not exist; the async test raises `ValueError` out of `_select_option_contract_async`.

- [ ] **Step 3: Add `_fallback_pick` + wrap the final call**

Add near `_trim_history`:

```python
def _fallback_pick(rows: list[dict], right: str, cfg, delta_mid: float, today) -> OptionContractPick | None:
    """Deterministic pick when the structured LLM call fails: the contract whose |delta| is
    closest to the target-delta midpoint, among those matching right / delta window / DTE window
    with a usable quote. Keeps options trading working when qwen3.6 flakes; the Floor Broker's
    risk gates still run on the result."""
    ot = cfg.options_trading
    best = None
    for r in rows:
        if r.get("right") != right or r.get("delta") is None or not r.get("expiration"):
            continue
        d = abs(r["delta"])
        if not (ot.target_delta_min <= d <= ot.target_delta_max):
            continue
        try:
            dte = (datetime.strptime(r["expiration"], "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if not (ot.dte_min <= dte <= ot.dte_max):
            continue
        bid, ask = r.get("bid"), r.get("ask")
        if not bid or not ask or ask <= 0:
            continue
        mid = round((bid + ask) / 2, 2)
        if mid <= 0:
            continue
        key = (abs(d - delta_mid), ask - bid)
        if best is None or key < best[0]:
            best = (key, r, d, mid)
    if best is None:
        return None
    _, r, d, mid = best
    return OptionContractPick(
        contract_symbol=r["symbol"],
        strike=float(r["strike"]),
        expiration=r["expiration"],
        right=right,
        delta=d,
        premium=mid,
        reasoning="deterministic fallback: structured LLM pick unavailable",
    )
```

Replace the final two lines of `_select_option_contract_async`:

```python
    messages = _trim_history(messages, _OPTION_PROMPT_TOKEN_HARD_CAP)
    structured_llm = llm.with_structured_output(OptionContractPick)
    try:
        pick = structured_llm.invoke(messages)
        if pick:
            return pick
    except Exception as exc:  # noqa: BLE001 - any parse/transport failure falls back deterministically
        log(f"⚠️ option selection for {state['symbol']}: structured pick failed ({exc}); using fallback")
    return _fallback_pick(seen_rows, right, cfg, delta_mid, today)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py -k option`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/dealer/graph.py tests/dealer/test_dealer_graph.py`
Expected: clean (the `# noqa: BLE001` is intentional; if the repo's ruff config doesn't enable `BLE001`, drop the noqa).

- [ ] **Step 6: Commit**

```bash
git add src/dealer/graph.py tests/dealer/test_dealer_graph.py
git commit -m "$(cat <<'EOF'
feat(dealer): deterministic fallback option pick when structured output fails

If with_structured_output can't produce an OptionContractPick (qwen3.6
regularly returns a near-empty completion here), pick the seen contract
whose |delta| is closest to the target-delta midpoint and that passes the
delta / DTE / quote gates. Options trading keeps producing trades; the
Floor Broker's risk gates still apply.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

---

## Task 5: Client-side request timeout + config key

**Files:**
- Modify: `src/dealer/graph.py` (`llm_call` ~`67`, `_select_option_contract_async` ~`426`, new `_llm_timeout`)
- Modify: `config.default.yaml`, `config.yaml` (`llm:` block)
- Test: `tests/dealer/test_dealer_graph.py`

**Interfaces:**
- Consumes: `cfg.llm`.
- Produces: `_llm_timeout(cfg) -> float` returning `float(cfg.llm.get("request_timeout_s", 120))`. Both `ChatOpenAI(...)` constructors in `graph.py` gain `timeout=_llm_timeout(cfg)` and `max_retries=0`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dealer/test_dealer_graph.py
def test_llm_call_sets_request_timeout_and_no_retries(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, schema):
            class _S:
                def invoke(self, m):
                    return graph.Signal(symbol="AAPL", action="HOLD", reasoning="r",
                                        size_hint=0.0, confidence=0.5)
            return _S()

    monkeypatch.setattr(graph, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(graph, "_symbol_memory_text", lambda *a, **k: "")
    cfg = OmegaConf.create({"llm": {"base_url": "http://llm.test/v1", "model": "m",
                                    "temperature": 0.0, "request_timeout_s": 90}})

    graph.llm_call({**_state("rsi: 71"), "symbol": "AAPL"}, cfg)

    assert captured["timeout"] == 90.0
    assert captured["max_retries"] == 0


def test_llm_timeout_defaults_to_120_when_unset():
    cfg = OmegaConf.create({"llm": {"base_url": "x", "model": "m", "temperature": 0.0}})
    assert graph._llm_timeout(cfg) == 120.0
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/pytest -q tests/dealer/test_dealer_graph.py -k "request_timeout or llm_timeout"`
Expected: FAIL — `KeyError: 'timeout'` / `graph._llm_timeout` missing.

- [ ] **Step 3: Add `_llm_timeout` and thread it through**

Add near the other module helpers in `graph.py`:

```python
def _llm_timeout(cfg) -> float:
    """Per-request wall-clock ceiling for every Ollama call. A hung generation fails fast
    instead of stacking behind the Dealer's 10-minute poll cycle."""
    return float(cfg.llm.get("request_timeout_s", 120))
```

In `llm_call`:

```python
    llm = ChatOpenAI(
        base_url=cfg.llm.base_url,
        api_key="not-needed",
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
        timeout=_llm_timeout(cfg),
        max_retries=0,
    ).with_structured_output(Signal)
```

In `_select_option_contract_async`:

```python
    llm = ChatOpenAI(
        base_url=cfg.llm.base_url,
        api_key="not-needed",
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
        timeout=_llm_timeout(cfg),
        max_retries=0,
    )
```

- [ ] **Step 4: Add the config key**

In `config.default.yaml` and `config.yaml`, under `llm:` (after `temperature:`):

```yaml
  request_timeout_s: 120   # per-request wall-clock ceiling for Ollama calls; a hung generation
                           # fails fast instead of stacking behind the 10-min poll cycle
```

- [ ] **Step 5: Run the full dealer suite**

Run: `.venv/bin/pytest -q tests/dealer/`
Expected: PASS. The existing `test_select_option_contract_async_passes_api_key_and_needs_no_openai_env` still passes (its `FakeChatOpenAI.__init__` swallows `**kwargs`).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src/dealer/graph.py tests/dealer/test_dealer_graph.py
git add src/dealer/graph.py tests/dealer/test_dealer_graph.py config.default.yaml config.yaml
git commit -m "$(cat <<'EOF'
fix(dealer): request timeout + no retries on both Ollama clients

New llm.request_timeout_s config (default 120s). Both ChatOpenAI clients
in the dealer graph now pass timeout + max_retries=0 so a hung or slow
generation fails fast instead of stacking behind the poll cycle.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

---

## Task 6: Reuse the MCP tool set within a poll cycle

**Files:**
- Modify: `src/dealer/mcp_options.py`
- Modify: `src/dealer/main.py` (poll loop, ~`98`)
- Test: `tests/dealer/test_mcp_options.py`

**Interfaces:**
- Consumes: `live_account_env_names()` (unchanged).
- Produces:
  - `get_options_tools()` caches its result keyed on `(key_env, secret_env)` for the process; returns the cached list on repeat calls without re-spawning `alpaca-mcp-server`.
  - `reset_options_tools_cache() -> None` — clears the cache. Called once per Dealer poll cycle.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dealer/test_mcp_options.py
import asyncio

from src.dealer import mcp_options


def test_get_options_tools_is_cached_until_reset(monkeypatch):
    monkeypatch.setattr(mcp_options, "live_account_env_names", lambda: ("K", "S"))
    monkeypatch.setenv("K", "key")
    monkeypatch.setenv("S", "secret")
    builds = {"n": 0}

    class FakeClient:
        def __init__(self, cfg):
            builds["n"] += 1

        async def get_tools(self):
            return ["tool-a", "tool-b"]

    monkeypatch.setattr(mcp_options, "MultiServerMCPClient", FakeClient)
    mcp_options.reset_options_tools_cache()

    a = asyncio.run(mcp_options.get_options_tools())
    b = asyncio.run(mcp_options.get_options_tools())
    assert a == b == ["tool-a", "tool-b"]
    assert builds["n"] == 1  # second call served from cache

    mcp_options.reset_options_tools_cache()
    asyncio.run(mcp_options.get_options_tools())
    assert builds["n"] == 2
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/pytest -q tests/dealer/test_mcp_options.py -k cached`
Expected: FAIL — `mcp_options.reset_options_tools_cache` missing; `builds["n"] == 2` after two calls.

- [ ] **Step 3: Add the cache**

Edit `src/dealer/mcp_options.py`:

```python
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

from src.common.alpaca_client import live_account_env_names

_TOOLS_CACHE: dict[tuple[str, str], list] = {}


def reset_options_tools_cache() -> None:
    """Called once per Dealer poll cycle. Within a cycle the Alpaca MCP tool list is stable, so
    we avoid re-spawning alpaca-mcp-server just to re-list tools for every symbol."""
    _TOOLS_CACHE.clear()


async def get_options_tools():
    """... (existing docstring) ..."""
    key_env, secret_env = live_account_env_names()
    cache_key = (key_env, secret_env)
    if cache_key in _TOOLS_CACHE:
        return _TOOLS_CACHE[cache_key]

    client = MultiServerMCPClient(
        {
            "alpaca": {
                "transport": "stdio",
                "command": "alpaca-mcp-server",
                "args": [],
                "env": {
                    "ALPACA_API_KEY": os.environ[key_env],
                    "ALPACA_SECRET_KEY": os.environ[secret_env],
                    "ALPACA_PAPER_TRADE": "True",
                    "ALPACA_TOOLSETS": "assets,options-data,account",
                },
            }
        }
    )
    tools = await client.get_tools()
    _TOOLS_CACHE[cache_key] = tools
    return tools
```

- [ ] **Step 4: Wire the reset into the poll loop**

In `src/dealer/main.py`, add the import and call it right after `cfg = load_config()` inside `while True`:

```python
from src.dealer.mcp_options import reset_options_tools_cache
```

```python
    while True:
        cfg = load_config()  # reloaded every poll cycle so a live config change never needs a restart
        reset_options_tools_cache()  # MCP tool list is stable within a cycle; rebuild once per cycle
        refresh_symbol_bases_if_due()
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest -q tests/dealer/test_mcp_options.py`
Expected: PASS (new test + existing `test_get_options_tools_config_uses_read_only_toolsets`).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src/dealer/mcp_options.py src/dealer/main.py tests/dealer/test_mcp_options.py
git add src/dealer/mcp_options.py src/dealer/main.py tests/dealer/test_mcp_options.py
git commit -m "$(cat <<'EOF'
perf(dealer): cache Alpaca MCP tool list per poll cycle

get_options_tools() spawned a fresh alpaca-mcp-server subprocess for every
symbol just to re-list the same tools. Cache the list keyed on the account
env-var names; reset once per poll cycle in the dealer main loop.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

---

## Task 7: Full suite + docs update, then open the app-repo PR

**Files:**
- Modify: `docs/models.md`, `docs/architecture.md`
- No test changes.

- [ ] **Step 1: Run the whole suite + lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all green, lint clean.

- [ ] **Step 2: Update `docs/models.md`**

Find the section that discusses the `qwen3.6:35b-a3b` choice / Ollama behaviour (search for `qwen3.6` or `context`). Add a short "2026-08-27 incident" note:

```markdown
### 2026-08-27 — option-selection loop hard-hung the DGX (twice)

The Dealer's option-contract-selection MCP loop appended the full raw Alpaca
option-chain snapshot to the LangChain message history every tool-calling round
and re-sent it, producing 100K–380K-token prompts. `qwen3.6:35b-a3b` is a
hybrid/SWA model with no KV-cache prefix reuse, so every one of those prompts was
fully reprocessed — sustained GPU saturation on the GB10 → silent hard hang (no
OOM, panic, Xid, or thermal trip).

Fixes: the loop now compacts each tool result (`src/dealer/option_chain.py`),
steers the LLM to server-side-filtered chain queries, caps cumulative prompt
tokens at ~12k (hard cap ~24k), times out each request at 120s, and falls back to
a deterministic pick if structured output fails. Ollama's default context is
pinned to 32768 on the host via a systemd drop-in (`miramar-platform-gcp`,
`dgx/ollama/deploy_ollama.sh`), so no client can request a 256K context again.
```

- [ ] **Step 3: Update `docs/architecture.md`**

Find the option-flow prose (search for `select_option_contract` or `OptionContractPick`). Add one sentence noting the loop is token-bounded and has a deterministic fallback:

```markdown
The tool-calling loop is bounded by a cumulative prompt-token budget (~12k, hard
cap ~24k) as well as the 6-round cap, and each tool result is compacted before it
enters the message history. If `with_structured_output` fails to produce a valid
`OptionContractPick`, a deterministic fallback selects the seen contract closest
to the target-delta midpoint that passes the delta/DTE/quote gates.
```

- [ ] **Step 4: Commit the docs**

```bash
git add docs/models.md docs/architecture.md
git commit -m "$(cat <<'EOF'
docs: record the 2026-08-27 option-loop DGX hang + the new bounds

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
```

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin fix/options-loop-crash
gh pr create --repo miramar-labs-org/multi-agent-ai-trader --base main --head fix/options-loop-crash \
  --title "fix: bound the option-selection MCP loop (DGX hard-hang root cause)" \
  --body "$(cat <<'EOF'
## Why

The Dealer's option-contract-selection MCP loop hard-hung the DGX Spark twice on
2026-08-27. Each tool-calling round appended the full raw Alpaca option-chain
snapshot to the message history and re-sent it, producing 100K–380K-token prompts
that `qwen3.6:35b-a3b` (hybrid model, no KV prefix reuse) fully reprocessed
back-to-back until the GB10 stalled. The feature also never worked — the final
structured-output call always returned a ~1-token completion.

Design spec: `docs/superpowers/specs/2026-08-27-options-loop-crash-fix-design.md`

## What

- `src/dealer/option_chain.py` (new) — `compact_tool_result` / `parse_option_chain` / `estimate_tokens`
- Steer the LLM to `get_option_chain` calls filtered by `type` + `expiration_date_gte/lte` + `limit`; hand it concrete ISO date bounds
- Compact every tool result before it enters the message history
- Cap cumulative prompt tokens: break the loop at ~12k, hard-cap at ~24k (blank oldest tool results, never orphan one)
- `llm.request_timeout_s` (default 120) + `max_retries=0` on both Ollama clients
- Deterministic fallback `OptionContractPick` when structured output fails, so options trading keeps producing trades
- Cache the Alpaca MCP tool list per poll cycle instead of re-spawning the server per symbol

**The Alpaca MCP agentic tool-calling loop is unchanged** — subprocess, `bind_tools`, the loop, `with_structured_output` all stay (hackathon requirement).

## Companion PR

`miramar-labs-org/miramar-platform-gcp` — pin `OLLAMA_CONTEXT_LENGTH=32768` on the DGX host via `dgx/ollama/deploy_ollama.sh` + the Ollama Deploy workflow. Merge and re-run Ollama Deploy after this.

## Verification

- Offline: `pytest -q` green (`tests/dealer/test_option_chain.py` + extended `test_dealer_graph.py` / `test_mcp_options.py`)
- Live: one DGX dealer cycle with a stock BUY → real `OptionContractPick` (or explicit deterministic fallback) + Floor Broker option order in Slack, per-call prompts < ~15k tokens, box stays responsive

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Task 8: Platform repo — pin `OLLAMA_CONTEXT_LENGTH` (companion PR)

**Files (in `/home/aaron/git-miramar-labs-org/miramar-platform-gcp`):**
- Modify: `dgx/ollama/deploy_ollama.sh`
- Modify: `.github/workflows/deploy-ollama.yaml`

- [ ] **Step 1: Branch (after confirming the repo is clean)**

```bash
cd /home/aaron/git-miramar-labs-org/miramar-platform-gcp
git status --short   # must be empty; if not, STOP and ask Aaron
git checkout main && git pull
git checkout -b fix/pin-ollama-context-length
```

- [ ] **Step 2: Edit `dgx/ollama/deploy_ollama.sh`**

After the `VRAM_BUDGET_GB` line (currently line 8), add:

```bash
OLLAMA_CONTEXT_LENGTH_PIN="${3:-32768}"   # pinned default context window; an unset value lets
                                          # Ollama pick a vram-based default (~262144 on the DGX),
                                          # so oversized client prompts are fully reprocessed
                                          # instead of truncated — this hard-hung the box on
                                          # 2026-08-27. Overridable per-request via num_ctx.
```

Immediately after the conflict-check block (after the `if (( CONFLICT )); then ... fi` that ends near line 106), before `# --- Pull model ---`, add:

```bash
# --- Pin the default context window via a systemd drop-in ---
OVERRIDE_DIR=/etc/systemd/system/ollama.service.d
OVERRIDE_FILE="$OVERRIDE_DIR/10-context.conf"
DESIRED_OVERRIDE=$(printf '[Service]\nEnvironment="OLLAMA_CONTEXT_LENGTH=%s"\n' "$OLLAMA_CONTEXT_LENGTH_PIN")
CURRENT_OVERRIDE="$(cat "$OVERRIDE_FILE" 2>/dev/null || true)"
if [[ "$CURRENT_OVERRIDE" != "$DESIRED_OVERRIDE" ]]; then
  log "Pinning OLLAMA_CONTEXT_LENGTH=${OLLAMA_CONTEXT_LENGTH_PIN} (was: ${CURRENT_OVERRIDE:-unset})"
  sudo mkdir -p "$OVERRIDE_DIR"
  printf '%s' "$DESIRED_OVERRIDE" | sudo tee "$OVERRIDE_FILE" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart ollama
  log "Waiting for Ollama to come back after restart..."
  for _ in $(seq 1 30); do
    curl -sf --connect-timeout 5 --max-time 10 http://localhost:11434/api/tags >/dev/null 2>&1 && break
    sleep 2
  done
  curl -sf --max-time 10 http://localhost:11434/api/tags >/dev/null 2>&1 \
    || { err "Ollama did not come back after the context-length restart."; exit 1; }
else
  log "OLLAMA_CONTEXT_LENGTH already pinned to ${OLLAMA_CONTEXT_LENGTH_PIN} — no restart."
fi
```

- [ ] **Step 3: Syntax + lint the script**

```bash
bash -n dgx/ollama/deploy_ollama.sh
shellcheck dgx/ollama/deploy_ollama.sh   # if installed; expect no new warnings from the added block
```

Expected: no syntax errors.

- [ ] **Step 4: Edit `.github/workflows/deploy-ollama.yaml`**

Add an input after the `model:` input (after line 21):

```yaml
      context_length:
        description: "Pinned OLLAMA_CONTEXT_LENGTH (default 32768; guards against oversized-prompt reprocessing on the GB10)"
        required: false
        default: "32768"
```

In the "Check conflicts and deploy" step, change the `bash -s` line to pass the third arg:

```yaml
            bash -s -- "${{ inputs.model }}" "$VRAM_USEABLE" "${{ inputs.context_length }}" \
            < "$DEPLOY_SCRIPT"
```

- [ ] **Step 5: Validate the workflow YAML**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy-ollama.yaml')); print('ok')"
```

Expected: `ok`.

- [ ] **Step 6: Commit + push + PR**

```bash
git add dgx/ollama/deploy_ollama.sh .github/workflows/deploy-ollama.yaml
git commit -m "$(cat <<'EOF'
fix(dgx): pin OLLAMA_CONTEXT_LENGTH via systemd drop-in on deploy

An unpinned OLLAMA_CONTEXT_LENGTH lets Ollama size a vram-based default
context (~262144 on the DGX), so an oversized client prompt is fully
reprocessed instead of truncated. That behaviour hard-hung the box twice
on 2026-08-27 (the multi-agent-ai-trader Dealer's option-selection loop).
deploy_ollama.sh now writes /etc/systemd/system/ollama.service.d/10-context.conf
with a pinned value (default 32768, overridable via the new context_length
workflow input) and restarts ollama only when the value changed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013T9HeZVGpm794Qa4yUU2Nj
EOF
)"
git push -u origin fix/pin-ollama-context-length
gh pr create --repo miramar-labs-org/miramar-platform-gcp --base main --head fix/pin-ollama-context-length \
  --title "fix(dgx): pin OLLAMA_CONTEXT_LENGTH via systemd drop-in on deploy" \
  --body "$(cat <<'EOF'
## Why

`multi-agent-ai-trader`'s Dealer hard-hung the DGX twice on 2026-08-27 by sending
100K–380K-token prompts to Ollama. The app-side fix bounds those prompts; this is
the host-side defense in depth: with `OLLAMA_CONTEXT_LENGTH` unset, Ollama sizes a
~262144-token default context and fully reprocesses oversized prompts instead of
truncating them.

## What

- `dgx/ollama/deploy_ollama.sh` writes `/etc/systemd/system/ollama.service.d/10-context.conf`
  with `Environment="OLLAMA_CONTEXT_LENGTH=<value>"` (default 32768), restarting
  `ollama` only when the value actually changed, then re-waiting for `/api/tags`.
- New optional `context_length` input on the **Ollama Deploy** workflow (default 32768),
  passed as the third positional arg to the deploy script.

## Rollout

Merge, then run **Ollama Deploy** (`runner: dgx`, `model: qwen3.6:35b-a3b`) to apply.

## Assumption

The DGX deploy user has passwordless `sudo` for `mkdir` / `tee` / `systemctl`
(consistent with the graceful-shutdown script from #57). If not, the deploy step
will prompt and fail — flag for a sudoers rule.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Task 9: Live-cycle verification (after both PRs merge)

**Not code — a manual gate. Do not run until Aaron has squash-merged both PRs and re-run Ollama Deploy.**

- [ ] **Step 1:** Confirm the app deploy chain is green (Test & Lint → Build & Push → Deploy) for the merged commit; `kubectl -n multi-agent-ai-trader get pods` shows the dealer pod on the new image.
- [ ] **Step 2:** Confirm Ollama Deploy ran: `ssh aaron@spark-79b7.local 'cat /etc/systemd/system/ollama.service.d/10-context.conf; systemctl show ollama -p Environment | tr " " "\n" | grep CONTEXT'` → shows `OLLAMA_CONTEXT_LENGTH=32768`.
- [ ] **Step 3:** Wait for / trigger a Dealer poll cycle with a stock BUY at confidence ≥ `strategy.min_confidence`. Watch `kubectl -n multi-agent-ai-trader logs -f deploy/dealer`.
- [ ] **Step 4:** In the dealer logs, confirm: `get_option_chain` is called **with** `type` + `expiration_date_gte`/`_lte` args; no "token budget reached" unless expected; a real `OptionContractPick` is produced (or an explicit `using fallback` line), not silence.
- [ ] **Step 5:** On the DGX, `journalctl -u ollama --since "10 min ago" | grep -iE "prompt=|truncat|context"` — per-call `prompt=` well under 32768; no `forcing full prompt re-processing` on 100K+ prompts; box stays responsive through the cycle (no reboot).
- [ ] **Step 6:** Slack `#miramar-trading-floor`: `📜` Dealer signal line + Floor Broker option result line.
- [ ] **Step 7:** Check one Analyst run's prompt size against the 32768 pin (`journalctl -u ollama | grep prompt=` around the Analyst window). If Analyst prompts approach 32k, open a one-line follow-up bumping the drop-in to 65536.
- [ ] **Step 8:** Update the handoff and write a memory file for the GB10 silent-hang incident class.

---

## Self-Review

**1. Spec coverage:**

| Spec item | Task |
|---|---|
| A1 tool-result compaction | Task 1 (`compact_tool_result`), wired in Task 2 |
| A2 filtered-chain steering (system + human prompt w/ ISO dates) | Task 2 |
| A3 cumulative token-budget guard (12k stop / 24k hard cap) | Task 3 |
| A4 client-side timeout + `llm.request_timeout_s` | Task 5 |
| A5 deterministic fallback pick | Task 4 |
| A6 per-cycle MCP tool cache | Task 6 |
| B1 systemd drop-in in `deploy_ollama.sh`, restart-on-change | Task 8 |
| B2 `context_length` workflow input | Task 8 |
| Offline tests | Tasks 1–6 |
| Live-cycle verification | Task 9 |
| Docs (`models.md`, `architecture.md`) + memory | Tasks 7, 9 |
| Rollout sequence (app PR → platform PR → Ollama Deploy → live cycle) | Tasks 7, 8, 9 |

No gaps. `_MAX_TOOL_CALL_ROUNDS` stays 6 per the spec's "keep 6, the token guard is the real limiter" decision — no task changes it.

**2. Placeholder scan:** Task 3's Step 1 note explicitly says "tune the fixture until the 3-round assertion holds, then lock it" — that is a real instruction, not a placeholder, but the implementer must not commit a test with a loose `<=` assertion. Every other step has concrete code.

**3. Type consistency:** `parse_option_chain` returns dicts with keys `symbol/strike/expiration/right/delta/gamma/theta/vega/iv/bid/ask/oi/volume` — Task 4's `_fallback_pick` reads exactly `right/delta/expiration/bid/ask/strike/symbol`. `compact_tool_result` signature (`tool_name, raw, *, target_delta_mid, max_contracts, max_chars`) matches every call site (Task 2 passes `target_delta_mid=delta_mid`). `estimate_tokens(messages)` and `_trim_history(messages, hard_cap)` signatures match their call sites. `_llm_timeout(cfg)` matches. `reset_options_tools_cache()` matches the `main.py` call.
