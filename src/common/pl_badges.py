from datetime import date

from alpaca.trading.requests import GetPortfolioHistoryRequest

from src.common.alpaca_client import trading_client


def fetch_pl_summary() -> dict:
    """Today's P&L is account.equity - account.last_equity -- the same day-boundary math
    execution.py's daily loss limit check already relies on. YTD P&L is equity - base_value from
    a portfolio-history request starting Jan 1 of the current year: confirmed against a live
    account that PortfolioHistory.profit_loss is a day-over-day delta series (profit_loss[i] ==
    equity[i] - equity[i-1]), not cumulative from base_value, so base_value (Alpaca's own equity
    snapshot as of the requested start date) is the only reliable YTD anchor."""
    account = trading_client.get_account()
    equity = float(account.equity)
    today_pl = equity - float(account.last_equity)

    year_start = date(date.today().year, 1, 1)
    history = trading_client.get_portfolio_history(GetPortfolioHistoryRequest(start=year_start, timeframe="1D"))
    ytd_pl = equity - float(history.base_value)

    return {"equity": equity, "today_pl": today_pl, "ytd_pl": ytd_pl}


def _format_usd(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def build_badge_payload(label: str, value: float) -> dict:
    """Shields.io endpoint-badge schema (schemaVersion 1) -- https://shields.io/badges/endpoint-badge."""
    return {
        "schemaVersion": 1,
        "label": label,
        "message": _format_usd(value),
        "color": "brightgreen" if value >= 0 else "red",
    }
