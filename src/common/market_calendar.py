from datetime import date, datetime, time

from alpaca.trading.requests import GetCalendarRequest
import pytz

from src.common.alpaca_client import trading_client


def is_stock_market_open(day: date) -> bool:
    return bool(trading_client.get_calendar(GetCalendarRequest(start=day, end=day)))


def get_stock_market_close(day: date) -> datetime | None:
    calendar = trading_client.get_calendar(GetCalendarRequest(start=day, end=day))
    if not calendar:
        return None

    close_value = calendar[0].close
    eastern = pytz.timezone("US/Eastern")
    if isinstance(close_value, datetime):
        close_dt = close_value
    elif isinstance(close_value, time):
        close_dt = datetime.combine(day, close_value)
    else:
        close_dt = datetime.combine(day, time.fromisoformat(str(close_value)))

    if close_dt.tzinfo is None:
        return eastern.localize(close_dt)
    return close_dt.astimezone(eastern)
