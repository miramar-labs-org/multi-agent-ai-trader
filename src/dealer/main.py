import json
import os
import time
from datetime import datetime, time as dtime, timedelta

import pytz
from kubernetes.client.exceptions import ApiException

from src.common import slack
from src.common.alpaca_client import trading_client
from src.common.config import load_config
from src.common.logging import get_logger
from src.common.portfolio_state import read_portfolio
from src.dealer.graph import build_graph

log = get_logger("DEALER")


def is_after_open_buffer(buffer_minutes: int) -> bool:
    eastern = pytz.timezone("US/Eastern")
    now_et = datetime.now(eastern)

    market_open_naive = datetime.combine(now_et.date(), dtime(9, 30))
    market_open_et = eastern.localize(market_open_naive)

    adj_open_et = market_open_et + timedelta(minutes=buffer_minutes)
    return now_et >= adj_open_et


def market_is_open(cfg, log) -> bool:
    if cfg.trading.market_override:
        log("📈 OVERRIDE: stock market is OPEN.")
        return True

    if trading_client.get_clock().is_open:
        if not is_after_open_buffer(cfg.trading.buffer):
            log(f"📈 stock market is OPEN but we are waiting {cfg.trading.buffer} minutes to avoid volatility")
            return False
        log("📈 stock market is OPEN.")
        return True

    log("🔒 stock market is CLOSED.")
    log(f"next open: {trading_client.get_clock().next_open}")
    log(f"next close: {trading_client.get_clock().next_close}")
    return False


def main():
    cfg = load_config()

    if cfg.langsmith.enabled:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_PROJECT"] = cfg.langsmith.project

    graph = build_graph()

    while True:
        if market_is_open(cfg, log):
            try:
                portfolio = read_portfolio()
            except (ApiException, json.JSONDecodeError) as exc:
                log(f"⚠️ BAD portfolio read .. cannot proceed: {exc}")
                slack.notify_error("DEALER", f"portfolio read failed: {exc}")
                time.sleep(60)
                continue

            for entry in portfolio.get("symbols", []):
                if entry["exchange"] != "stocks":
                    continue
                try:
                    state = {
                        "symbol": entry["symbol"],
                        "exchange": entry["exchange"],
                        "budget": entry["budget"],
                        "indicator_names": entry["indicators"],
                        "indicators_text": "",
                        "signal": None,
                        "execution_result": None,
                    }
                    graph.invoke(state, config={"tags": ["dealer"]})
                except Exception as exc:
                    log(f"💥 failed processing {entry['symbol']}: {exc}")
                    slack.notify_error("DEALER", f"{entry['symbol']}: {exc}")
                    continue

        log(f"------------------ PAUSED FOR {cfg.trading.pollsecs}s -----------------------")
        time.sleep(cfg.trading.pollsecs)


if __name__ == "__main__":
    main()
