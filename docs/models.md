# Candidate models for local hosting on DGX Spark

Research and decision log (2026-08) for the local model that serves both the
Analyst and the Dealer LLM calls (`config.yaml`'s `llm.base_url`, see
[README.md](../README.md#how-it-decides-trades)). It began as a survey of
whether a better local model exists than the original Qwen2.5-7B-Instruct; the
dated **Decision** sections below track what was actually picked and re-picked
since. **Current state: `qwen3.6:35b-a3b` served by Ollama on the DGX** (see
"Decision update (2026-08-27): reverted to qwen3.6:35b-a3b") — not a custom
vLLM endpoint.

## Hardware constraint that shapes every choice below

DGX Spark's GB10 GPU is **SM121** ("consumer/workstation-class" Blackwell).
It lacks the `cvt.e2m1x2` hardware instruction that SM120 (RTX 5090) and
SM100 (B200) have natively for NVFP4 compute. One benchmark
("NVFP4 Is a Trap on GB10") found **FP8 is 32% faster than NVFP4 on this
exact hardware** — so FP8 remains the right quantization format here even
though NVFP4 checkpoints exist for some of the models below. This validates
`qwen25-7b-fp8-quant-pipeline`'s existing `qformat: fp8` choice.

Also relevant: GB10's 128GB is unified memory shared across CPU + GPU + OS +
container runtime + model weights + KV cache — not an independent ~100GB
GPU-only budget. Treat ~100GiB as a practical safety margin within that pool.

## Candidates (best fit first)

### Qwen3.6-35B-A3B — MoE, 35B total / 3B active params
- Apache 2.0, released 2026-04-14.
- Only 3B active params per token → fast generation despite the larger
  on-disk size.
- Ships with **day-1 pre-quantized FP8 and NVFP4 checkpoints** — could be
  served directly without running our own PTQ pipeline.
- ~35GB weights at FP8, comfortable headroom for KV cache in the shared pool.
- Benchmarked on GB10 via the "Atlas" community inference engine at
  100+ tok/s.
- 73.4% SWE-bench Verified; vision-language performance matching/exceeding
  Claude Sonnet 4.5 on several benchmarks (92.0 RefCOCO, 50.8 ODInW13).

### Qwen3.6-27B — dense
- Apache 2.0, released 2026-04-22.
- Text + image + video input, native 262,144-token context (extensible to
  1,010,000).
- 77.2% SWE-bench Verified, 87.8 GPQA Diamond.
- Simpler to serve than an MoE model if that's preferred; ~27GB at FP8.

### nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B — MoE, ~31B total / ~3B active
- Mamba2-Transformer hybrid.
- NVIDIA-published NVFP4 checkpoint, 4.98 effective bits/weight
  (~20.9GB for the Omni-Reasoning variant).
- First-party NVIDIA attention to DGX Spark specifically — this is the
  model family used in vLLM's own "Nemotron-3-Super on DGX Spark" reference
  blog post.

### Not recommended: DeepSeek-V4-Flash-NVFP4
- 284B total / 13B active params, quantized via nvidia-modelopt v0.44.0,
  1M context.
- Likely too large for comfortable single-GPU DGX Spark hosting even at
  NVFP4 — excluded as a candidate for this hardware.

## Decision (2026-08-01)

**Model: Qwen3.6-35B-A3B. Serving: Ollama on DGX, not a custom vLLM project.**

Qwen3.6-35B-A3B already ships a pre-quantized FP8 checkpoint, so adopting it
doesn't require running `qwen25-7b-fp8-quant-pipeline` at all — that
pipeline's ongoing value is for models that don't yet have a community/vendor
pre-quantized checkpoint, or when we want our own calibration dataset. It's
standalone infrastructure work, not a dependency of this project.

Serving it required picking a mechanism. None of the platform's
`create-project.yaml` project types fit "serve a vendor-pre-quantized HF
checkpoint via plain `vllm serve`, no LoRA, no our-own-PTQ output" —
`serving-vllm` is hardcoded to GKE + LoRA adapters, `serving-trt-fp8` expects
output from our own quantization pipeline. Building a new `serving-vllm-hf`
project type was considered, but rejected in favor of the platform's existing
**Ollama** stack (already running as a systemd service on DGX, with a working
`deploy-ollama.yaml` Deploy/Undeploy workflow, dashboard variables, and an
OpenAI-compatible `/v1` API that the Dealer's LangChain client already
expects — zero new platform engineering, zero code changes).

Confirmed Ollama's library has tags for both remaining candidates:
`qwen3.6:35b-a3b` and `qwen3.6:27b` (plus `q4_K_M`/`q8_0`/mlx/nvfp4 variants).
Chosen tag: `qwen3.6:35b-a3b` (library default quantization).

Known trade-off: the FP8-beats-NVFP4-by-32% finding above is vLLM/SGLang-
specific (from tensor-core kernel benchmarks) and hasn't been separately
verified for Ollama's llama.cpp/GGUF-based backend. Accepted because the
Dealer's workload is a low-QPS periodic poll loop, not throughput-sensitive —
vLLM's batching advantage doesn't matter here.

### Verified 2026-08-01: Ollama deploy + structured-output path

Deployed via `deploy-ollama.yaml` (`CURRENT_OLLAMA_MODEL=qwen3.6:35b-a3b`,
32GB VRAM). Confirmed reachable from inside k3s at `http://192.168.1.200:11434`
(`DGX_HOST_IP`) with no `OLLAMA_HOST` binding fix needed.

Important operational finding: this model emits a chain-of-thought
`reasoning` field before its actual answer/tool call, even under forced
`tool_choice`. A `max_tokens: 600` cap truncated before any `tool_call` was
ever emitted (`finish_reason: "length"`, empty `tool_calls`) — the reasoning
alone can run 2000-3500+ tokens for a 3-indicator prompt. **Do not set a
tight `max_tokens`** on the Dealer's `ChatOpenAI` call — confirmed the
existing code (`src/dealer/graph.py`, no `max_tokens` param, i.e. unbounded)
reliably reaches `finish_reason: "tool_calls"` with a schema-valid `Signal`
payload. This costs real latency per decision (thousands of tokens of
thinking) but is a non-issue against the 600s (`trading.pollsecs`) poll
interval.

## Decision update (2026-08-11): switched to nemotron-3-super

Re-checked the actual DGX Ollama budget live rather than trusting the
original ~100GB nominal figure: `qwen3.6:35b-a3b` was only using **32.2GB**
VRAM (`/api/ps` → `size_vram: 34561840256`, `Q4_K_M`, not the FP8 checkpoint
originally discussed above — Ollama's library-default quantization turned
out to be Q4_K_M), leaving substantial unused headroom in a budget that's
already paid for. `ollama list` showed several much larger models already
pulled and idle, so no download was needed to use a bigger one.

Two already-pulled candidates were evaluated against live free-memory
numbers (`free -h`, DGX unified memory): `gpt-oss:120b` (65.4GB, MXFP4,
116.8B total) with a comfortable ~40GB margin, and `nemotron-3-super:latest`
(86.8GB, Q4_K_M, 123.6B total, Nemotron-H hybrid) with a tighter ~18GB
margin. `nemotron-3-super` is the architecture NVIDIA/vLLM specifically
validated for DGX Spark (see the vLLM DGX Spark blog post in Sources below),
so despite the smaller margin it was chosen over `gpt-oss:120b`.

### Verified 2026-08-11: Ollama deploy + structured-output path

Deployed via `deploy-ollama.yaml` (`CURRENT_OLLAMA_MODEL=nemotron-3-super:latest`,
88GB VRAM per `/api/ps`: `size_vram: 94152404736`). Auto-undeploy step
cleanly evicted `qwen3.6:35b-a3b` first, per the workflow's built-in
behavior.

Like `qwen3.6:35b-a3b`, this model emits chain-of-thought before its tool
call, exposed via a separate `reasoning` field rather than inline in
`content`. Sent a manual test request to `/v1/chat/completions` mirroring
the Dealer's `Signal` schema, no `max_tokens` cap, `tool_choice: "required"`:
reliably reached `finish_reason: "tool_calls"` with a schema-valid `Signal`
payload (`symbol`, `action`, `reasoning`, `size_hint`, `confidence` all
present and correctly typed). 517 completion tokens, **39.2s** total
latency — well under the 600s (`trading.pollsecs`) poll interval. Confirms
the existing unbounded-`max_tokens` `ChatOpenAI` call in
`src/dealer/graph.py` needs no change for this model.

## Note (2026-08-14): options contract-selection uses the same model, agentically

The options path (`options_trading.enabled`, ROADMAP P1.16) adds a *third* LLM call site, but no
new model or endpoint: `select_option_contract` (`src/dealer/graph.py`) uses the same `cfg.llm`
model via `ChatOpenAI`, differing only in shape — instead of one structured-output turn it runs a
tool-calling **agent loop** (`_MAX_TOOL_CALL_ROUNDS = 6`) with Alpaca's options-data MCP tools
bound (`src/dealer/mcp_options.py`), so the model issues several `tool_calls` rounds to walk the
option chain before returning a final `OptionContractPick`. Both models discussed above already
reach `finish_reason: "tool_calls"` reliably, which is the only capability this loop depends on;
worst case is ~6× the single-call latency, still far under the 600s poll interval. No `max_tokens`
cap here either.

## Decision update (2026-08-27): reverted to qwen3.6:35b-a3b

Switched `config.yaml`'s `llm.model` back from `nemotron-3-super:latest` to
`qwen3.6:35b-a3b` after the options path went live in prod and exposed two
costs of the 123.6B model that the 2026-08-11 single-call test hadn't:

- **Latency under real load.** The 39.2s / 517-token figure above was an
  isolated `Signal` call. In production, `llm_call` on the full
  indicator + multi-timeframe-OHLCV prompt was taking 10+ minutes per symbol,
  and a single live `_select_option_contract_async` run (the 6-round MCP
  tool-calling loop, each round preceded by a full 2000-3500-token reasoning
  dump) ran 15+ minutes without returning a pick. The "~6x single-call
  latency, still far under 600s" assumption in the 2026-08-14 note did not
  hold once each round carried the real message history.
- **Memory headroom.** `nemotron-3-super` sits at 94GB VRAM / 100% GPU on a
  box reporting ~1GB free / ~10GB available. That margin OOM-killed the
  512Mi dealer pod's in-cluster verification runs and leaves nothing for KV
  cache growth or the other DGX services sharing the unified pool.

`qwen3.6:35b-a3b` (MoE, 35B total / **3B active**, already pulled, 23GB
Q4_K_M) was the 2026-08-01 decision and was only displaced by the
"NVIDIA validated this architecture for DGX Spark" argument -- a
hardware-compatibility point, never an accuracy one. It already reached
`finish_reason: "tool_calls"` with schema-valid `Signal` payloads in the
2026-08-11 verification. The 3B active-param count is the key lever: it
decodes several times faster than a dense 30B and frees ~70GB.

Thinking is still emitted by this model; the same "no tight `max_tokens`
cap" rule from the 2026-08-01 verification applies. If latency is still
higher than wanted, the next levers (not taken here) are suppressing the
reasoning trace on the structured-output calls
(`chat_template_kwargs={"enable_thinking": False}`) and lowering
`_MAX_TOOL_CALL_ROUNDS` from 6.

The Ollama side of the switch (evict `nemotron-3-super`, preload
`qwen3.6:35b-a3b` with `keep_alive: -1`) is handled by `power_scheduler`'s
`manage_ollama_model` path on the next power cycle, and manually at
switch time.

### Postmortem (2026-08-27): the switch stranded `nemotron-3-super` and OOM'd the box

The config change alone did **not** free the old model. `power_scheduler`
only ever stopped/started `cfg.llm.model` (the *new* name), so
`nemotron-3-super` stayed pinned (`keep_alive: -1`, ~94GB) while the next
power-up loaded `qwen3.6:35b-a3b` *on top of it*. GB10 unified memory hit
zero -- `NVRM ... _memdescAllocInternal` out-of-memory fired 6x between 10:20
and 10:41, Ollama hung, Aaron rebooted the DGX manually. Not a crash, not
thermal (no panic / thermal trip / GPU reset in `journalctl -b -1`; 16h
uptime).

**Fix:** `_start_ollama_model` now calls `_evict_other_ollama_models(cfg)`
first -- it `GET`s `/api/ps` and unloads (`keep_alive: 0`) every resident
model whose name != `cfg.llm.model` before preloading the configured one.
This makes every power-up self-healing after a model swap, matching the
platform's "one pinned model at a time" convention
(`dgx/ollama/deploy_ollama.sh`). A stale request is logged + Slack-notified,
never raised.

**Manual model-swap procedure** (between `config.yaml` merge and the next
power cycle): `POST /api/generate {"model": "<old>", "keep_alive": 0}`,
confirm `/api/ps` shows only the new model (or nothing), then
`POST /api/generate {"model": "<new>", "keep_alive": -1}`. `power_scheduler`
now enforces this each power-up regardless.

## Incident (2026-08-27): options MCP loop drove a silent GB10 hard-hang

The live option-contract-selection path (`_select_option_contract_async`,
`src/dealer/graph.py`) runs an agentic Alpaca-MCP tool-calling loop against
`qwen3.6:35b-a3b`. `get_option_chain` with no filters returns the full raw
Alpaca options-snapshot JSON for a symbol; that whole blob was appended to the
message history verbatim every round, so the prompt grew without bound
(observed: 258,075 then 376,452 tokens; Ollama logged
`WARN truncating input prompt limit=131074 prompt=376452`). `qwen3.6:35b-a3b`
is a hybrid SWA/MoE model with no KV-cache prefix reuse, so every call
reprocessed the entire prompt. Under that sustained maxed-GPU prompt-cache
thrash the DGX hard-hung with no kernel panic, OOM-kill, Xid, thermal trip, or
kdump -- journald just stopped. Distinct from the 2026-08-27 model-swap OOM
above (that one left NVRM OOM traces; this one left nothing).

Contributing factor: `OLLAMA_CONTEXT_LENGTH` was unset, so Ollama used its
VRAM-based default of 262,144 (256K) tokens -- a context window large enough to
let the runaway prompt keep growing instead of erroring early.

**Fix (this repo):**
- Tool results are compacted before entering history -- option chains are parsed,
  ranked by proximity to the target delta, and rendered as <=40 one-line rows
  (`compact_tool_result`, `src/dealer/option_chain.py`); non-chain results are
  truncated to 6000 chars.
- The tool-calling loop is token-bounded: it stops requesting tools once history
  passes ~12k estimated tokens, and `_trim_history` neutralizes old `ToolMessage`
  bodies before any LLM call that would exceed a 24k hard cap. Round cap stays 6.
- The system/human prompts now instruct the model to always pass
  `type` + `expiration_date_gte/lte` + a small `limit` to `get_option_chain`,
  never the full chain, with the DTE window pre-computed as ISO dates.
- Both Ollama `ChatOpenAI` clients get `timeout=llm.request_timeout_s` (default
  120s) and `max_retries=0`, so a hung generation fails fast instead of stacking
  behind the 10-minute poll cycle.
- If structured output still fails, `_fallback_pick` deterministically chooses a
  contract from the rows already seen (delta + DTE + live quote gates).

**Fix (platform):** `dgx/ollama/deploy_ollama.sh` now writes a systemd drop-in
pinning `OLLAMA_CONTEXT_LENGTH=32768` (overridable via the `Ollama Deploy`
workflow's `context_length` input), so a single call can never ask Ollama to
hold a 256K-token prompt again.

## Sources

- [Atlas: Open-source inference engine for DGX Spark](https://forums.developer.nvidia.com/t/atlas-open-source-inference-engine-for-dgx-spark-2minute-cold-start-100-tok-s-on-qwen3-6-35b-fp8-13-supported-models/369263)
- [Qwen3.6-35B-A3B: Agentic Coding Power, Now Open to All](https://qwen.ai/blog?id=qwen3.6-35b-a3b)
- [Qwen/Qwen3.6-35B-A3B · Hugging Face](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Qwen3.6-27B: 27B Model Beats 397B on Coding (2026)](https://www.buildfastwithai.com/blogs/qwen3-6-27b-review-2026)
- [NVFP4 Is a Trap on GB10: FP8 Wins by 32% (vLLM + SGLang Tested)](https://ai-muninn.com/en/blog/dgx-spark-nvfp4-trap-gb10-fp8-wins)
- [nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 · Hugging Face](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4)
- [vLLM on the DGX Spark: Architecture, Configuration, and Local Evaluation](https://vllm.ai/blog/2026-06-01-vllm-dgx-spark)
