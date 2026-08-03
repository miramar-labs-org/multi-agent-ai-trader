import threading
import time

import uvicorn

from src.common import slack
from src.common.logging import get_logger
from src.floor_broker import execution

log = get_logger("FLOOR")

BRACKET_FILL_POLL_INTERVAL_S = 30


def poll_bracket_fills():
    """Runs for the lifetime of the process, watching for TP/SL bracket legs that fill
    asynchronously on Alpaca's side -- outside of any /execute request/response cycle, so this is
    the only place those fills ever get observed and reported."""
    while True:
        try:
            for event in execution.check_bracket_fills():
                log(f"🎯 {event['reason']} filled for {event['symbol']} @ {event['fill_price']}")
                slack.notify_floor_broker_result(
                    event["symbol"],
                    "SELL",
                    "executed",
                    f"{event['reason']} leg filled: {event['order_id']}",
                    reason=event["reason"],
                    fill_price=event["fill_price"],
                )
        except Exception as exc:
            log(f"💥 bracket-fill poll failed: {exc}")
        time.sleep(BRACKET_FILL_POLL_INTERVAL_S)


def main():
    threading.Thread(target=poll_bracket_fills, daemon=True).start()
    uvicorn.run("src.floor_broker.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
