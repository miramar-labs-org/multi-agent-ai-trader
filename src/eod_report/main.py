from datetime import datetime

import pytz
from alpaca.trading.requests import GetCalendarRequest

from src.common import slack
from src.common.alpaca_client import trading_client
from src.common.logging import get_logger

log = get_logger("EOD")


def main():
    eastern = pytz.timezone("US/Eastern")
    today = datetime.now(eastern).date()

    calendar = trading_client.get_calendar(GetCalendarRequest(start=today, end=today))
    if not calendar:
        log(f"📅 {today} was not a trading day — skipping EOD report.")
        return

    try:
        account = trading_client.get_account()
        positions = trading_client.get_all_positions()
        fills = trading_client.get(
            "/account/activities",
            data={"activity_types": "FILL", "date": today.isoformat()},
        )
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
    position_summaries = [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_plpc": float(p.unrealized_plpc),
        }
        for p in positions
    ]
    fill_summaries = [
        {
            "symbol": f["symbol"],
            "side": f["side"],
            "qty": float(f["qty"]),
            "price": float(f["price"]),
        }
        for f in fills
    ]

    slack.notify_eod_report(today.isoformat(), account_summary, fill_summaries, position_summaries)
    log(f"✅ EOD report sent — {len(fill_summaries)} fill(s), {len(position_summaries)} open position(s)")


if __name__ == "__main__":
    main()
