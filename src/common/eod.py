from src.common.alpaca_client import trading_client


def fetch_fills(date: str, only_crypto: bool | None = None) -> list[dict]:
    """Fetches an account's FILL activities for one date. `only_crypto=True` keeps only crypto
    fills, `False` keeps only equity fills, `None` (default) keeps both -- matching the "/" in
    symbol convention already used for crypto elsewhere (e.g. alpaca_client.get_current_ask_price)
    since Alpaca's activities payload carries no separate asset-class field."""
    raw = trading_client.get(
        "/account/activities",
        data={"activity_types": "FILL", "date": date},
    )
    fills = [
        {
            "symbol": f["symbol"],
            "side": f["side"],
            "qty": float(f["qty"]),
            "price": float(f["price"]),
        }
        for f in raw
    ]
    if only_crypto is None:
        return fills
    return [f for f in fills if ("/" in f["symbol"]) == only_crypto]


def summarize_positions(positions, only_crypto: bool | None = None) -> list[dict]:
    """Shapes Alpaca Position objects into the plain dicts slack.notify_*_report() expects.
    `only_crypto` filters the same way as fetch_fills()."""
    if only_crypto is not None:
        positions = [p for p in positions if ("/" in p.symbol) == only_crypto]
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_plpc": float(p.unrealized_plpc),
        }
        for p in positions
    ]
