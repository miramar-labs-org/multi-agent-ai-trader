import json
from datetime import datetime, timezone
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from src.analyst import sources
from src.analyst.schema import PortfolioSelection
from src.common.config import load_config
from src.common.logging import get_logger
from src.common.portfolio_state import write_portfolio as _write_portfolio

log = get_logger("ANALYST")

DEFAULT_INDICATORS = ["rsi", "macd", "vwap", "bbands", "sma", "ema"]


class AnalystState(TypedDict):
    raw_candidates: list
    research_text: str
    selection: dict | None


def discover_candidates(state: AnalystState, cfg) -> AnalystState:
    candidates = sources.fetch_screener_candidates(cfg.analyst.screener_top_n)
    return {**state, "raw_candidates": candidates}


def fetch_research(state: AnalystState, cfg) -> AnalystState:
    news = sources.fetch_news(cfg.analyst.news_days)
    headlines = sources.fetch_yahoo_rss_headlines(cfg.analyst.yahoo_rss_url)
    research_text = f"{news}\n{headlines}"
    return {**state, "research_text": research_text}


def llm_select(state: AnalystState, cfg) -> AnalystState:
    llm = ChatOpenAI(
        base_url=cfg.llm.base_url,
        api_key="not-needed",
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
    ).with_structured_output(PortfolioSelection)

    system_prompt = (
        "You are the Analyst agent on an automated stock trading floor. "
        f"Pick at most {cfg.analyst.max_universe_size} stock symbols worth trading today, "
        "drawn from the candidate list and informed by the research text. "
        f"Each pick needs exchange=\"stocks\", a budget in USD (default to {cfg.analyst.default_budget} "
        "unless you have a specific reason to size a position differently), an indicators list "
        f"(default {DEFAULT_INDICATORS} unless a symbol warrants different indicators), "
        "and a one-line rationale."
    )
    user_prompt = (
        f"Candidate symbols (from screener):\n{json.dumps(state['raw_candidates'])}\n\n"
        f"Market research:\n{state['research_text']}"
    )

    log("🧠 asking LLM to select the tradeable universe")
    selection: PortfolioSelection = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    )

    return {**state, "selection": selection.model_dump()}


def write_portfolio(state: AnalystState, cfg) -> AnalystState:
    selection = state["selection"]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": selection["symbols"],
    }
    _write_portfolio(payload)
    log(f"✅ wrote portfolio with {len(payload['symbols'])} symbols")
    return state


def build_graph():
    cfg = load_config()

    graph = StateGraph(AnalystState)
    graph.add_node("discover_candidates", lambda state: discover_candidates(state, cfg))
    graph.add_node("fetch_research", lambda state: fetch_research(state, cfg))
    graph.add_node("llm_select", lambda state: llm_select(state, cfg))
    graph.add_node("write_portfolio", lambda state: write_portfolio(state, cfg))

    graph.set_entry_point("discover_candidates")
    graph.add_edge("discover_candidates", "fetch_research")
    graph.add_edge("fetch_research", "llm_select")
    graph.add_edge("llm_select", "write_portfolio")
    graph.add_edge("write_portfolio", END)

    return graph.compile()
