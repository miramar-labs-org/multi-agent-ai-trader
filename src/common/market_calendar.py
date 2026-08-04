from datetime import date

from alpaca.trading.requests import GetCalendarRequest

from src.common.alpaca_client import trading_client


def is_stock_market_open(day: date) -> bool:
    return bool(trading_client.get_calendar(GetCalendarRequest(start=day, end=day)))
