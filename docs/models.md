# Candidate models for local hosting on DGX Spark

Research snapshot (2026-08) into whether a better local model exists than
Qwen2.5-7B-Instruct for the planned vLLM endpoint (`config.yaml`'s
`llm.base_url`, see [README.md](../README.md#how-it-decides-trades)). Not
acted on yet — kept here for when we're ready to pick a serving model.

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

## Sources

- [Atlas: Open-source inference engine for DGX Spark](https://forums.developer.nvidia.com/t/atlas-open-source-inference-engine-for-dgx-spark-2minute-cold-start-100-tok-s-on-qwen3-6-35b-fp8-13-supported-models/369263)
- [Qwen3.6-35B-A3B: Agentic Coding Power, Now Open to All](https://qwen.ai/blog?id=qwen3.6-35b-a3b)
- [Qwen/Qwen3.6-35B-A3B · Hugging Face](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Qwen3.6-27B: 27B Model Beats 397B on Coding (2026)](https://www.buildfastwithai.com/blogs/qwen3-6-27b-review-2026)
- [NVFP4 Is a Trap on GB10: FP8 Wins by 32% (vLLM + SGLang Tested)](https://ai-muninn.com/en/blog/dgx-spark-nvfp4-trap-gb10-fp8-wins)
- [nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 · Hugging Face](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4)
- [vLLM on the DGX Spark: Architecture, Configuration, and Local Evaluation](https://vllm.ai/blog/2026-06-01-vllm-dgx-spark)
