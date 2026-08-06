from datetime import datetime, timedelta

import pytz

from src.common import db, slack
from src.common.alpaca_client import trading_client
from src.common.eod import fetch_fills, summarize_positions
from src.common.logging import get_logger
from src.common.market_calendar import get_stock_market_close

log = get_logger("EOD")


def _now_eastern() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern"))


def main():
    now = _now_eastern()
    today = now.date()

    if db.eod_report_already_sent(today):
        log(f"⏭️ EOD report for {today} already sent")
        return

    market_close = get_stock_market_close(today)
    if market_close is None:
        log(f"📅 {today} was not a trading day — skipping EOD report.")
        # The CronJob runs daily (not just Mon-Fri) so a closed market still gets a Slack
        # notification -- a silent return here previously left weekends/holidays with zero
        # visibility that the report was intentionally skipped rather than never run at all.
        slack.notify_market_closed("EOD", today.isoformat())
        db.record_eod_report_sent(today)
        return

    report_due_at = market_close + timedelta(minutes=30)
    if now < report_due_at:
        log(f"⏭️ EOD report for {today} not due until {report_due_at.isoformat()}")
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
    db.record_eod_report_sent(today)
    log(f"✅ EOD report sent — {len(fills)} fill(s), {len(position_summaries)} open position(s)")


if __name__ == "__main__":
    main()
