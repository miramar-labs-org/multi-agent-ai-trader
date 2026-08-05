from typing import TypedDict

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from src.common import db, slack
from src.common.config import load_config
from src.common.logging import get_logger
from src.common.indicators import fetch_indicators_bulk
from src.dealer.schema import Signal

log = get_logger("DEALER")


class DealerState(TypedDict):
    symbol: str
    exchange: str
    budget: float
    indicator_names: list[str]
    indicators_text: str
    signal: dict | None
    execution_result: dict | None


def fetch_indicators(state: DealerState, cfg) -> DealerState:
    names = state["indicator_names"]
    if names == ["ALL"]:
        names = [ind["name"] for ind in cfg.indicators]

    indicators_text = fetch_indicators_bulk(cfg.indicators, state["symbol"], state["exchange"], names, log)
    return {**state, "indicators_text": indicators_text}


def llm_call(state: DealerState, cfg) -> DealerState:
    llm = ChatOpenAI(
        base_url=cfg.llm.base_url,
        api_key="not-needed",
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
    ).with_structured_output(Signal)

    system_prompt = (
        "You are an expert technical trader in stocks. "
        "Based on the values of ALL of the indicators below, decide if you should BUY, SELL, or HOLD. "
        "size_hint must be a decimal fraction between 0.0 and 1.0 representing the portion of the "
        "symbol's budget to deploy on a BUY (e.g. 0.5 = half the budget, 1.0 = the full budget) -- "
        "never a dollar amount or share count."
    )
    user_prompt = f"Indicators for {state['symbol']}:\n{state['indicators_text']}"

    log(f"🧠 thinking about {state['symbol']}...")
    signal: Signal = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])

    log(f"🧠 AI {cfg.llm.model} says: {signal.action} {state['symbol']}")
    return {**state, "signal": signal.model_dump()}


def call_floor_broker(state: DealerState, cfg) -> DealerState:
    signal = state["signal"]
    slack.notify_dealer_signal(state["symbol"], signal["action"], signal["reasoning"])
    db.record_dealer_decision(state["symbol"], signal["action"], signal["reasoning"], signal.get("size_hint"))

    if signal["action"] == "HOLD":
        return {**state, "execution_result": {"status": "skipped", "detail": "HOLD"}}

    if signal["action"] == "BUY" and state["budget"] <= 0:
        # A held-only position (merge_held_positions()) carries budget=0 -- its market value is
        # observed exposure, not authorized new-BUY capital. Never forward a BUY for it.
        log(f"⚠️  no authorized BUY budget for {state['symbol']} (held position) -- skipping")
        result = {
            "status": "skipped",
            "reason": "no_authorized_budget",
            "detail": "held position has no authorized new-BUY budget",
        }
        slack.notify_floor_broker_result(state["symbol"], signal["action"], result["status"], result["detail"])
        db.record_floor_broker_event(state["symbol"], "skip", result["detail"])
        return {**state, "execution_result": result}

    payload = {
        "symbol": state["symbol"],
        "exchange": state["exchange"],
        "action": signal["action"],
        "budget": state["budget"],
        "slP": cfg.trading.slP,
        "tpP": cfg.trading.tpP,
    }

    try:
        response = requests.post(f"{cfg.floor_broker.base_url}/execute", json=payload, timeout=30)
        if response.status_code != 200:
            log(f"💥 floor broker error: {response.status_code} {response.text}")
            slack.notify_floor_broker_result(state["symbol"], signal["action"], "error", response.text)
            db.record_floor_broker_event(state["symbol"], "error", response.text)
            return {**state, "execution_result": {"status": "error", "detail": response.text}}
        result = response.json()
        slack.notify_floor_broker_result(
            state["symbol"],
            signal["action"],
            result["status"],
            result["detail"],
            reason=result.get("reason"),
            fill_price=result.get("fill_price"),
            sl_price=result.get("sl_price"),
            tp_price=result.get("tp_price"),
        )
        db.record_floor_broker_event(
            state["symbol"],
            f"{signal['action'].lower()}_{result['status']}",
            result["detail"],
            price=result.get("fill_price"),
        )
        return {**state, "execution_result": result}
    except requests.RequestException as exc:
        log(f"💥 floor broker request failed: {exc}")
        slack.notify_floor_broker_result(state["symbol"], signal["action"], "error", str(exc))
        db.record_floor_broker_event(state["symbol"], "error", str(exc))
        return {**state, "execution_result": {"status": "error", "detail": str(exc)}}


def build_graph():
    # Each lambda calls load_config() fresh at invocation time (once per node per graph run,
    # i.e. once per Dealer poll cycle per symbol) rather than baking one cfg into the closure at
    # build time -- build_graph() itself only runs once per process, but a live config change
    # must be visible within load_config()'s refresh window without a Dealer restart.
    graph = StateGraph(DealerState)
    graph.add_node("fetch_indicators", lambda state: fetch_indicators(state, load_config()))
    graph.add_node("llm_call", lambda state: llm_call(state, load_config()))
    graph.add_node("call_floor_broker", lambda state: call_floor_broker(state, load_config()))

    graph.set_entry_point("fetch_indicators")
    graph.add_edge("fetch_indicators", "llm_call")
    graph.add_edge("llm_call", "call_floor_broker")
    graph.add_edge("call_floor_broker", END)

    return graph.compile()
