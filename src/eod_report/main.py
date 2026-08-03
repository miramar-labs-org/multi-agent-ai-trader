from datetime import datetime

import pytz
from alpaca.trading.requests import GetCalendarRequest

from src.common import slack
from src.common.alpaca_client import trading_client
from src.common.eod import fetch_fills, summarize_positions
from src.common.logging import get_logger

log = get_logger("EOD")


def main():
    eastern = pytz.timezone("US/Eastern")
    today = datetime.now(eastern).date()

    calendar = trading_client.get_calendar(GetCalendarRequest(start=today, end=today))
    if not calendar:
        log(f"📅 {today} was not a trading day — skipping EOD report.")
        # The CronJob runs daily (not just Mon-Fri) so a closed market still gets a Slack
        # notification -- a silent return here previously left weekends/holidays with zero
        # visibility that the report was intentionally skipped rather than never run at all.
        slack.notify_market_closed("EOD", today.isoformat())
        return

    try:
        account = trading_client.get_account()
        positions = trading_client.get_all_positions()
        fills = fetch_fills(today.isoformat())
    except Exception as exc:
        log(f"💥 EOD report failed: {exc}")
        slack.notify_error("EOD", str(exc))
        raise

    account_summary = {
        "equity": float(account.equity),
        "last_equity": float(account.last_equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
    }
    position_summaries = summarize_positions(positions)

    slack.notify_eod_report(today.isoformat(), account_summary, fills, position_summaries)
    log(f"✅ EOD report sent — {len(fills)} fill(s), {len(position_summaries)} open position(s)")


if __name__ == "__main__":
    main()
