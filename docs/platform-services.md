# Platform services usage

This project scaffolds from the standard Miramar project template, which
ships a full platform endpoint table (JupyterLab, K8s dashboard, KFP UI/API,
MLflow, NeMo/NIM, Ollama, Qdrant, Nsight UI, Open WebUI). Most of those are
generic scaffolding this project doesn't actually call, which is why the
top-level README's Quick Links section only lists the ones that matter here.
This doc records which are real dependencies and which aren't, so a future
reader doesn't have to grep the whole codebase to find out.

## Actually used

| Service    | How                                                                                                     |
| ---------- | --------------------------------------------------------------------------------------------------------- |
| Ollama     | `cfg.llm.base_url` (`config.yaml`'s `llm.base_url`, an Ollama systemd service on the DGX host, not in k3s) is passed to `langchain_openai.ChatOpenAI` and called by both the Analyst and Dealer LangGraph agents. See [docs/models.md](models.md) for the model choice and Ollama-vs-vLLM decision — this project ended up on Ollama rather than a deployed vLLM `serving-vllm` endpoint. |
| Kubernetes dashboard | Ops only, not called from `src/` — used to inspect the `multi-agent-ai-trader` namespace's pods/jobs when debugging (see the top-level README's Quick Links). |

## Listed in the full Miramar platform endpoint table but unused by this project's code

NeMo/NIM, Qdrant, MLflow, KFP (UI + API), Nsight UI, Open WebUI, JupyterLab —
none of these are referenced anywhere in `src/`. They're part of the generic
template scaffolding shared by every Miramar project and aren't wired into
the Analyst/Dealer/Floor Broker code paths. In particular:

- **MLflow / KFP** — this isn't a training or pipeline project; there's
  nothing to track experiments for or orchestrate as a DAG.
- **Qdrant** — no retrieval/embedding step in any agent.
- **NeMo/NIM** — superseded by the `cfg.llm.base_url` Ollama endpoint above;
  neither is called directly.
- **JupyterLab** — the README's "Open in JupyterLab" badge was removed;
  `notebook.ipynb` is a local, gitignored scratch file (see
  [architecture.md](architecture.md)), not a tracked project asset.

## Real external dependencies not in the README table

| Service    | Role                                                                                      |
| ---------- | ------------------------------------------------------------------------------------------ |
| Alpaca     | Trading API (paper) — Floor Broker's only external call, order placement/cancellation. See the [paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) link in the top-level README. |
| TAAPI.io   | Technical indicators consumed by the Analyst agent — unrelated to any Miramar platform service. |
| LangSmith  | Tracing/observability for both LangGraph agents — this is the tracing layer actually in use, not MLflow. |

See [architecture.md](architecture.md) for where each of these fits into the
agent data flow.
