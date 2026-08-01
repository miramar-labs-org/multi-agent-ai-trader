# Platform services usage

This project scaffolds from the standard Miramar project template, which is
why the top-level README lists the full platform endpoint table (JupyterLab,
K8s dashboard, KFP UI/API, MLflow, NeMo/NIM, Ollama, Qdrant, Nsight UI, Open
WebUI). Most of those are generic scaffolding this project doesn't actually
call. This doc records which are real dependencies and which aren't, so a
future reader doesn't have to grep the whole codebase to find out.

## Actually used

| Service    | How                                                                                                     |
| ---------- | --------------------------------------------------------------------------------------------------------- |
| vLLM       | `cfg.llm.base_url` (currently a `TBD` placeholder in `config.yaml`) is passed to `langchain_openai.ChatOpenAI` and called by both the Analyst and Dealer LangGraph agents. Not yet a deployed endpoint — this project is waiting on a shared vLLM serving deployment. Not in the README table because vLLM isn't one of the platform's generic always-on services; it's per-project `serving-vllm`. |
| JupyterLab | Indirectly — the README's "Open in JupyterLab" badge is how you open `notebook.ipynb`, but the notebook here is a scratch/dev notebook, not a KFP pipeline definition (see [architecture.md](architecture.md)). |

## Listed in the README table but unused by this project's code

Ollama, NeMo/NIM, Qdrant, MLflow, KFP (UI + API), Kubernetes dashboard,
Nsight UI, Open WebUI — none of these are referenced anywhere in
`src/`. They're part of the generic template scaffolding shared by every
Miramar project and aren't wired into the Analyst/Dealer/Floor Broker code
paths. In particular:

- **MLflow / KFP** — this isn't a training or pipeline project; there's
  nothing to track experiments for or orchestrate as a DAG.
- **Qdrant** — no retrieval/embedding step in any agent.
- **NeMo/NIM, Ollama** — superseded by the `cfg.llm.base_url` vLLM
  placeholder above; neither is called directly.

## Real external dependencies not in the README table

| Service    | Role                                                                                      |
| ---------- | ------------------------------------------------------------------------------------------ |
| Alpaca     | Trading API (paper) — Floor Broker's only external call, order placement/cancellation. See the [paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) link in the top-level README. |
| TAAPI.io   | Technical indicators consumed by the Analyst agent — unrelated to any Miramar platform service. |
| LangSmith  | Tracing/observability for both LangGraph agents — this is the tracing layer actually in use, not MLflow. |

See [architecture.md](architecture.md) for where each of these fits into the
agent data flow.
