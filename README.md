# multi-agent-ai-trader

[![Open in JupyterLab](https://img.shields.io/badge/Open%20in-JupyterLab-F37626?logo=jupyter&logoColor=white)](http://localhost:8888/lab/tree/git-miramar-labs-org/projects/multi-agent-ai-trader/notebook.ipynb)

Multi-agent AI trading system (Analyst, Dealer, Floor Broker) trading on Alpaca, powered by a locally-hosted LLM on the DGX via k3s

Trades are paper-only — see the [Alpaca paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) for live account state, positions, and order history.

## What this is

Three independently-deployed Kubernetes workloads that together run a daily equities
trading loop against Alpaca's **paper** trading account:

- **Analyst** — a `CronJob` that runs once a day before market open, screens for candidate
  symbols, and decides a tradeable watchlist for the day.
- **Dealer** — a long-running `Deployment` that polls every 10 minutes while the market is
  open, pulls technical indicators for each watchlist symbol, and asks an LLM whether to
  BUY, HOLD, or SELL.
- **Floor Broker** — a `Deployment` + `Service` that is the only component that actually
  places orders. It never calls an LLM — it takes a BUY/SELL decision and executes it as a
  bracket order (stocks) or notional market order (crypto) on Alpaca.

Analyst and Dealer never talk to each other directly — Analyst writes its daily watchlist to
a shared `portfolio` ConfigMap, and Dealer reads it fresh on every poll. Dealer talks to
Floor Broker over plain in-cluster HTTP. There is no database, message queue, or shared
filesystem anywhere in the system. See [docs/architecture.md](docs/architecture.md) for the
full breakdown (per-agent internals, data flow, config reference, risk controls) and
[docs/platform-services.md](docs/platform-services.md) for which platform services below are
actually wired up vs. unused scaffolding.

This is a re-platforming of an earlier single-process script (`gpt-trader.py`) onto three
independently-scaled k8s workloads.

## How it decides trades

Both Analyst and Dealer make their decisions the same way: gather data, hand it to an LLM as
context, and parse the response into a strict structured-output schema (via LangChain's
`.with_structured_output()` — no hand-rolled JSON parsing). Both are implemented as small
[LangGraph](https://langchain-ai.github.io/langgraph/) state machines:

- **Analyst** (4 nodes): discover screener candidates → fetch news/RSS research → LLM picks
  up to 10 symbols with a budget and rationale each → write the `portfolio` ConfigMap.
- **Dealer** (3 nodes, per symbol per poll): fetch technical indicators → LLM decides
  BUY/HOLD/SELL → if not HOLD, dispatch to Floor Broker over HTTP.

Both LLM calls go through `langchain_openai.ChatOpenAI` against a single OpenAI-compatible
`base_url` shared by the whole system (`config.yaml`'s `llm.base_url`) — this is intended to
point at a vLLM endpoint serving a locally-hosted model on the DGX, so no external LLM API
key is required for trading decisions themselves.

Floor Broker has no decision logic of its own — by the time it receives a request, the
BUY/SELL call has already been made; it only handles order placement, safety checks
(no duplicate positions), and Alpaca's order-conflict retry cases.

## External APIs used

| API | Used by | Purpose |
|---|---|---|
| [Alpaca Trading API](https://docs.alpaca.markets/) | Floor Broker | The only component that places/cancels orders — bracket orders for stocks, notional market orders for crypto. Paper account only, hardcoded (not a config toggle). |
| [Alpaca Market Data / Screener / News API](https://docs.alpaca.markets/) | Analyst, Dealer | Screener `most-actives`/`movers` endpoints and News API for Analyst's daily candidate research; live bid/ask quotes for Floor Broker's order sizing. |
| [TAAPI.io](https://taapi.io/) | Dealer | Technical indicators (RSI, MACD, VWAP, Bollinger Bands, SMA, EMA) per watchlist symbol — a third-party service, unrelated to any Miramar platform component. |
| Yahoo Finance RSS | Analyst | Supplementary headline research alongside Alpaca's News API. |
| [LangSmith](https://www.langchain.com/langsmith) | Analyst, Dealer | Tracing for both LangGraph agent runs (optional, `config.yaml`'s `langsmith.enabled`). This is the tracing layer actually in use — not MLflow, despite MLflow appearing in the platform endpoint table below. |
| [Slack](https://api.slack.com/messaging/webhooks) | Analyst, Dealer, Floor Broker | Incoming-webhook notifications for interesting events (portfolio picks, BUY/SELL/HOLD signals, executions, errors) to `#miramar-trading-floor` — optional, `config.yaml`'s `slack.enabled`. |
| OpenAI-compatible LLM endpoint (vLLM, planned) | Analyst, Dealer | Both agents' BUY/HOLD/SELL and symbol-selection decisions. Currently a placeholder in `config.yaml` pending a deployed vLLM serving endpoint. |

## Platform endpoints

All services require the SSH tunnel from your laptop:

```sh
ssh -L 8001:localhost:8001 \
    -L 8888:localhost:8888 \
    -L 5000:localhost:5000 \
    -L 8080:localhost:8080 \
    -L 8082:localhost:8082 \
    -L 8890:localhost:8890 \
    -L 11434:localhost:11434 \
    -L 6333:localhost:6333 \
    -L 8889:localhost:8889 \
    -L 8084:localhost:8084 \
    <user>@spark-79b7.local
```

Add to your laptop's `/etc/hosts` (Windows: `C:\Windows\System32\drivers\etc\hosts`):

```
127.0.0.1 nemo.test nim.test data-store.test
```

| Service              | URL                                                                                                                                                                                                                | Notes                              |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- |
| JupyterLab           | [http://localhost:8888](http://localhost:8888)                                                                                                                                                                     | Notebook environment               |
| Kubernetes dashboard | [http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/http:kubernetes-dashboard:/proxy/](http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/http:kubernetes-dashboard:/proxy/) | Cluster state                      |
| KFP UI               | [http://localhost:8080](http://localhost:8080)                                                                                                                                                                     | Kubeflow Pipelines UI              |
| KFP API              | [http://localhost:8890/apis/v2beta1/healthz](http://localhost:8890/apis/v2beta1/healthz)                                                                                                                           | KFP REST API                       |
| MLflow               | [http://localhost:5000](http://localhost:5000)                                                                                                                                                                     | Experiment tracking                |
| NeMo / NIM           | [http://nemo.test:8082](http://nemo.test:8082)                                                                                                                                                                     | NeMo Microservices + NIM inference |
| Ollama               | [http://localhost:11434](http://localhost:11434)                                                                                                                                                                   | Local LLM inference                |
| Qdrant               | [http://localhost:6333/dashboard](http://localhost:6333/dashboard)                                                                                                                                                 | Vector database                    |
| Nsight UI            | [http://localhost:8889](http://localhost:8889)                                                                                                                                                                     | Nsight Operator profiling UI       |
| Open WebUI           | [http://localhost:8084](http://localhost:8084)                                                                                                                                                                     | Chat UI (Ollama / vLLM backend)    |
