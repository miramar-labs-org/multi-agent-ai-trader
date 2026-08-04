import json
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict

import pytz
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from src.analyst import sources
from src.analyst.schema import PortfolioSelection
from src.common import db, slack
from src.common.alpaca_client import trading_client
from src.common.config import load_config
from src.common.eod import fetch_fills, summarize_positions
from src.common.indicators import fetch_indicators_bulk
from src.common.logging import get_logger
from src.common.portfolio_state import write_portfolio as _write_portfolio

log = get_logger("ANALYST")

DEFAULT_INDICATORS = ["rsi", "macd", "vwap", "bbands", "sma", "ema"]


class AnalystState(TypedDict):
    raw_candidates: list
    research_text: str
    indicator_text: str
    selection: dict | None
    stock_market_open: bool


def discover_candidates(state: AnalystState, cfg) -> AnalystState:
    candidates = []

    if cfg.trading.enable_stocks and state["stock_market_open"]:
        stock_candidates = sources.fetch_screener_candidates(cfg.analyst.screener_top_n, cfg.analyst.min_price_usd)
        for c in stock_candidates:
            c["market"] = "stocks"
        candidates.extend(stock_candidates)

    if cfg.trading.enable_crypto:
        crypto_candidates = sources.fetch_crypto_candidates(cfg.analyst.screener_top_n)
        for c in crypto_candidates:
            c["market"] = cfg.trading.crypto_taapi_exchange
        candidates.extend(crypto_candidates)

    return {**state, "raw_candidates": candidates}


def fetch_research(state: AnalystState, cfg) -> AnalystState:
    news = sources.fetch_news(cfg.analyst.news_days)
    headlines = sources.fetch_yahoo_rss_headlines(cfg.analyst.yahoo_rss_url)
    research_text = f"{news}\n{headlines}"
    return {**state, "research_text": research_text}


def fetch_indicators(state: AnalystState, cfg) -> AnalystState:
    """Fetches real TAAPI indicator values for the top candidates by move size, so llm_select
    can reason about actual RSI/MACD/etc rather than news text alone. Bounded to
    `indicator_fetch_limit` candidates -- TAAPI's free-tier rate limit is 1 request/15s
    (cfg.taapi.min_request_interval_secs), and fetching every screened candidate would make the
    daily CronJob run take far longer than warranted for candidates the LLM is unlikely to pick."""
    ranked = sorted(
        state["raw_candidates"],
        key=lambda c: abs(c["change_pct"]) if c.get("change_pct") is not None else -1,
        reverse=True,
    )
    top_candidates = ranked[: cfg.analyst.indicator_fetch_limit]

    lines = []
    for i, candidate in enumerate(top_candidates):
        if i > 0:
            time.sleep(cfg.taapi.min_request_interval_secs)
        text = fetch_indicators_bulk(
            cfg.indicators, candidate["symbol"], candidate["market"], DEFAULT_INDICATORS, log
        )
        if text:
            lines.append(f"{candidate['symbol']}:\n{text}")

    return {**state, "indicator_text": "\n".join(lines)}


def llm_select(state: AnalystState, cfg) -> AnalystState:
    llm = ChatOpenAI(
        base_url=cfg.llm.base_url,
        api_key="not-needed",
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
    ).with_structured_output(PortfolioSelection)

    system_prompt = (
        "You are the Analyst agent on an automated trading floor. "
        f"Pick at most {cfg.analyst.max_universe_size} symbols worth trading today, "
        "drawn from the candidate list and informed by the research text. "
        "Where a candidate has technical indicator values provided, ground your reasoning in "
        "those actual values rather than news sentiment alone. "
        "Each candidate object includes a `market` field (\"stocks\", or a crypto exchange name "
        "for a 24/7-traded crypto pair) -- set your pick's exchange field to exactly that value. "
        f"Give each pick a budget in USD (default to {cfg.analyst.default_budget} "
        "unless you have a specific reason to size a position differently), an indicators list "
        f"(default {DEFAULT_INDICATORS} unless a symbol warrants different indicators), "
        "and a one-line rationale."
    )
    user_prompt = (
        f"Candidate symbols (from screener):\n{json.dumps(state['raw_candidates'])}\n\n"
        f"Technical indicators (top {cfg.analyst.indicator_fetch_limit} candidates by move size "
        f"only -- not every candidate has these):\n{state['indicator_text']}\n\n"
        f"Market research:\n{state['research_text']}"
    )

    log("🧠 asking LLM to select the tradeable universe")
    selection: PortfolioSelection = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    )

    return {**state, "selection": selection.model_dump()}


def validate_selection(state: AnalystState, cfg) -> AnalystState:
    """The LLM's `exchange` field is regenerated text, not guaranteed to match the candidate it
    was actually given -- trust discover_candidates()'s own `market` tag instead, and drop any
    pick whose symbol isn't one we actually offered (a hallucination)."""
    market_by_symbol = {c["symbol"]: c["market"] for c in state["raw_candidates"]}

    validated = []
    for pick in state["selection"]["symbols"]:
        market = market_by_symbol.get(pick["symbol"])
        if market is None:
            log(f"⚠️ dropping hallucinated pick {pick['symbol']} -- not in candidate list")
            continue
        pick["exchange"] = market
        validated.append(pick)

    return {**state, "selection": {**state["selection"], "symbols": validated}}


def write_portfolio(state: AnalystState, cfg) -> AnalystState:
    selection = state["selection"]
    generated_at = datetime.now(timezone.utc)
    payload = {
        "generated_at": generated_at.isoformat(),
        "symbols": selection["symbols"],
    }
    _write_portfolio(payload)
    log(f"✅ wrote portfolio with {len(payload['symbols'])} symbols")

    for pick in payload["symbols"]:
        db.record_analyst_pick(
            pick["symbol"], pick.get("exchange"), pick.get("budget"), pick.get("rationale"), generated_at
        )

    account = trading_client.get_account()
    account_summary = {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
    }
    report_date = datetime.now(pytz.timezone("US/Eastern")).date().isoformat()
    slack.notify_morning_report(
        report_date,
        account_summary,
        payload["symbols"],
        stock_market_open=state["stock_market_open"],
        crypto_enabled=cfg.trading.enable_crypto,
    )
    return state


def crypto_eod_report(state: AnalystState, cfg) -> AnalystState:
    """Crypto trades 24/7, so it has no market close to hang an EOD report off of -- instead this
    rides along with the Analyst's morning run and covers the prior full ET calendar day, right
    after today's new picks go out in write_portfolio()'s notify_morning_report()."""
    if not cfg.trading.enable_crypto:
        return state

    report_date = (datetime.now(pytz.timezone("US/Eastern")) - timedelta(days=1)).date().isoformat()
    positions = summarize_positions(trading_client.get_all_positions(), only_crypto=True)
    fills = fetch_fills(report_date, only_crypto=True)
    slack.notify_crypto_eod_report(report_date, fills, positions)
    return state


def build_graph():
    cfg = load_config()

    graph = StateGraph(AnalystState)
    graph.add_node("discover_candidates", lambda state: discover_candidates(state, cfg))
    graph.add_node("fetch_research", lambda state: fetch_research(state, cfg))
    graph.add_node("fetch_indicators", lambda state: fetch_indicators(state, cfg))
    graph.add_node("llm_select", lambda state: llm_select(state, cfg))
    graph.add_node("validate_selection", lambda state: validate_selection(state, cfg))
    graph.add_node("write_portfolio", lambda state: write_portfolio(state, cfg))
    graph.add_node("crypto_eod_report", lambda state: crypto_eod_report(state, cfg))

    graph.set_entry_point("discover_candidates")
    graph.add_edge("discover_candidates", "fetch_research")
    graph.add_edge("fetch_research", "fetch_indicators")
    graph.add_edge("fetch_indicators", "llm_select")
    graph.add_edge("llm_select", "validate_selection")
    graph.add_edge("validate_selection", "write_portfolio")
    graph.add_edge("write_portfolio", "crypto_eod_report")
    graph.add_edge("crypto_eod_report", END)

    return graph.compile()
