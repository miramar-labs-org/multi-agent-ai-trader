import json
from datetime import date
from pathlib import Path

from src.common.logging import get_logger
from src.common.market_calendar import is_stock_market_open
from src.common.pl_badges import build_badge_payload, fetch_pl_summary

log = get_logger("PL-BADGES")

BADGES_DIR = Path(__file__).resolve().parents[2] / "badges"


def main():
    today = date.today()

    if not is_stock_market_open(today):
        log(f"📅 {today} was not a trading day — leaving badges unchanged.")
        return

    summary = fetch_pl_summary()
    BADGES_DIR.mkdir(exist_ok=True)
    (BADGES_DIR / "today-pl.json").write_text(json.dumps(build_badge_payload("Today's P/L", summary["today_pl"])))
    (BADGES_DIR / "ytd-pl.json").write_text(json.dumps(build_badge_payload("YTD P/L", summary["ytd_pl"])))

    log(f"✅ badges written — today {summary['today_pl']:+.2f}, YTD {summary['ytd_pl']:+.2f}")


if __name__ == "__main__":
    main()
