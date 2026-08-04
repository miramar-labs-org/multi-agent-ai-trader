import threading
import time

import uvicorn

from src.common import kill_switch, slack
from src.common.logging import get_logger
from src.floor_broker import execution

log = get_logger("FLOOR")

BRACKET_FILL_POLL_INTERVAL_S = 30
KILL_SWITCH_POLL_INTERVAL_S = 30


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


def poll_kill_switch():
    """Watches the buy-kill-switch ConfigMap for a state change (ROADMAP P0.5) and posts a Slack
    notice only on transition, not on every poll -- `/execute` itself already re-checks the
    switch fresh on each BUY request, so this loop exists purely to surface the change as an
    operational event. `last_state` starts at None (not yet observed) so the very first poll,
    which is just discovering whatever state the switch was seeded/left in, never fires a
    transition notice on its own."""
    last_state = None
    while True:
        try:
            active = kill_switch.buy_kill_switch_active()
            if last_state is not None and active != last_state:
                log(f"🛑 BUY kill switch changed: {'ACTIVE' if active else 'inactive'}")
                slack.notify_buy_kill_switch(active)
            last_state = active
        except Exception as exc:
            log(f"💥 kill-switch poll failed: {exc}")
        time.sleep(KILL_SWITCH_POLL_INTERVAL_S)


def main():
    threading.Thread(target=poll_bracket_fills, daemon=True).start()
    threading.Thread(target=poll_kill_switch, daemon=True).start()
    uvicorn.run("src.floor_broker.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
