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

## Open question if we act on this

Qwen3.6-35B-A3B already ships a pre-quantized FP8 checkpoint, so adopting it
wouldn't require running `qwen25-7b-fp8-quant-pipeline` at all — that
pipeline's ongoing value would be for models that don't yet have a
community/vendor pre-quantized checkpoint, or when we want our own
calibration dataset.

## Sources

- [Atlas: Open-source inference engine for DGX Spark](https://forums.developer.nvidia.com/t/atlas-open-source-inference-engine-for-dgx-spark-2minute-cold-start-100-tok-s-on-qwen3-6-35b-fp8-13-supported-models/369263)
- [Qwen3.6-35B-A3B: Agentic Coding Power, Now Open to All](https://qwen.ai/blog?id=qwen3.6-35b-a3b)
- [Qwen/Qwen3.6-35B-A3B · Hugging Face](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Qwen3.6-27B: 27B Model Beats 397B on Coding (2026)](https://www.buildfastwithai.com/blogs/qwen3-6-27b-review-2026)
- [NVFP4 Is a Trap on GB10: FP8 Wins by 32% (vLLM + SGLang Tested)](https://ai-muninn.com/en/blog/dgx-spark-nvfp4-trap-gb10-fp8-wins)
- [nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 · Hugging Face](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4)
- [vLLM on the DGX Spark: Architecture, Configuration, and Local Evaluation](https://vllm.ai/blog/2026-06-01-vllm-dgx-spark)
