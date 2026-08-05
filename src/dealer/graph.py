from datetime import datetime
from typing import TypedDict

import pytz
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


def _is_quad_witching_day(d) -> bool:
    """Third Friday of March/June/September/December -- the quarterly simultaneous expiration of
    stock options, index options, and index futures, historically one of the highest-volume,
    highest-volatility sessions of the year for the broad market. Computed directly rather than
    requiring a config.yaml entry since it's a fixed calendar rule, not an externally-published
    date like FOMC/CPI/NFP -- it can't drift or need quarterly upkeep."""
    if d.month not in (3, 6, 9, 12) or d.weekday() != 4:  # Friday
        return False
    return 15 <= d.day <= 21  # the only possible day-of-month range for a third Friday


def _macro_blackout_active(cfg) -> str | None:
    """Returns the matching event label if today (America/New_York calendar date) is either a
    hand-maintained macro_blackout.dates entry (FOMC, CPI, PPI, PCE, NFP, ISM PMI, GDP, Fed Chair
    testimony, or any other scheduled market-wide macro release) or an auto-computed quad
    witching day, else None. Whole-trading-day granularity, not hours-based -- see
    docs/architecture.md Risk controls for why."""
    if not cfg.macro_blackout.enabled:
        return None
    today = datetime.now(pytz.timezone("US/Eastern")).date()
    if _is_quad_witching_day(today):
        return "Quad witching (quarterly options/futures expiration)"
    today_str = today.isoformat()
    for entry in cfg.macro_blackout.dates:
        if entry["date"] == today_str:
            return entry.get("label", entry["date"])
    return None


def call_floor_broker(state: DealerState, cfg) -> DealerState:
    signal = state["signal"]
    slack.notify_dealer_signal(state["symbol"], signal["action"], signal["reasoning"])
    db.record_dealer_decision(state["symbol"], signal["action"], signal["reasoning"], signal.get("size_hint"))

    if signal["action"] == "HOLD":
        return {**state, "execution_result": {"status": "skipped", "detail": "HOLD"}}

    if signal["action"] == "BUY":
        blackout_label = _macro_blackout_active(cfg)
        if blackout_label:
            log(f"⏭️  BUY for {state['symbol']} skipped -- macro blackout ({blackout_label})")
            result = {
                "status": "skipped",
                "reason": "macro_blackout",
                "detail": f"new BUY entries paused for macro blackout: {blackout_label}",
            }
            slack.notify_floor_broker_result(state["symbol"], signal["action"], result["status"], result["detail"])
            db.record_floor_broker_event(state["symbol"], "skip", result["detail"])
            return {**state, "execution_result": result}

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

    budget = state["budget"]
    if signal["action"] == "BUY":
        size_hint = signal.get("size_hint", 1.0)
        budget = state["budget"] * size_hint
        if budget <= 0:
            # size_hint scaled the authorized budget to exactly $0 -- ExecuteRequest requires
            # budget > 0 (src/floor_broker/app.py), so this must be refused locally rather than
            # forwarded to a request that would 422. A budget scaled to a small-but-positive
            # amount is intentionally still forwarded -- execution.py's own minimum-notional/
            # insufficient-qty checks already handle that gracefully with their own reason codes.
            log(f"⚠️  size_hint={size_hint} scales BUY budget for {state['symbol']} to $0 -- skipping")
            result = {
                "status": "skipped",
                "reason": "size_hint_zero",
                "detail": f"size_hint {size_hint} scales authorized budget to $0",
            }
            slack.notify_floor_broker_result(state["symbol"], signal["action"], result["status"], result["detail"])
            db.record_floor_broker_event(state["symbol"], "skip", result["detail"])
            return {**state, "execution_result": result}

    payload = {
        "symbol": state["symbol"],
        "exchange": state["exchange"],
        "action": signal["action"],
        "budget": budget,
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
