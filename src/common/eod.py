from alpaca.trading.enums import AssetClass

from src.common.alpaca_client import distinct_trading_clients, trading_client


def fetch_fills(date: str, only_crypto: bool | None = None, client=None) -> list[dict]:
    """Fetches an account's FILL activities for one date. `only_crypto=True` keeps only crypto
    fills, `False` keeps only equity fills, `None` (default) keeps both -- matching the "/" in
    symbol convention already used for crypto elsewhere (e.g. alpaca_client.get_current_ask_price)
    since Alpaca's activities payload carries no separate asset-class field. Confirmed against a
    live account: fill/activity symbols do carry the slash for crypto (e.g. "BTC/USD").

    `client` defaults to account 1's trading_client; pass another to read a different account's
    activities (see fetch_all_fills, which spans every distinct funded account)."""
    client = client or trading_client
    raw = client.get(
        "/account/activities",
        data={"activity_types": "FILL", "date": date},
    )
    fills = [
        {
            "symbol": f["symbol"],
            "side": f["side"],
            "qty": float(f["qty"]),
            "price": float(f["price"]),
            "time": f["transaction_time"],
        }
        for f in raw
    ]
    if only_crypto is None:
        return fills
    return [f for f in fills if ("/" in f["symbol"]) == only_crypto]


def fetch_all_fills(date: str, only_crypto: bool | None = None) -> list[dict]:
    """fetch_fills across every distinct funded Alpaca account -- account 1 (stocks/crypto) plus
    account 2 (options) once the two are split onto separate paper accounts. While they share
    credentials (today's default) distinct_trading_clients() yields a single client, so this
    returns exactly what fetch_fills() alone would. Used by the daily EOD Slack recap so option
    fills aren't silently dropped."""
    fills: list[dict] = []
    for client in distinct_trading_clients():
        fills.extend(fetch_fills(date, only_crypto=only_crypto, client=client))
    return fills


def fetch_all_positions() -> list:
    """Open Alpaca Position objects across every distinct funded account (see fetch_all_fills).
    Returns raw Position objects -- pass the result through summarize_positions() as before."""
    positions: list = []
    for client in distinct_trading_clients():
        positions.extend(client.get_all_positions())
    return positions


def fetch_combined_account_summary() -> dict:
    """equity / last_equity / cash / buying_power summed across every distinct funded account, in
    the shape slack.notify_eod_report() expects. Summing last_equity too keeps the recap's
    day-over-day P&L (equity - last_equity) correct once options run on a separate account."""
    fields = ("equity", "last_equity", "cash", "buying_power")
    totals = {f: 0.0 for f in fields}
    for client in distinct_trading_clients():
        account = client.get_account()
        for f in fields:
            totals[f] += float(getattr(account, f))
    return totals


def summarize_positions(positions, only_crypto: bool | None = None) -> list[dict]:
    """Shapes Alpaca Position objects into the plain dicts slack.notify_*_report() and the
    Analyst's fetch_position_pnl() expect. Unlike fetch_fills(), this filters on `p.asset_class`,
    not a "/" in the symbol -- confirmed against a live account that Alpaca's Position.symbol for
    crypto has no slash (e.g. "BTCUSD"), unlike the activities/fills payload which does (e.g.
    "BTC/USD"). Matches the asset_class check portfolio_state.merge_held_positions() already uses
    for the same reason. `unrealized_pl` and `current_price` are Optional[str] on Alpaca's own
    Position model, so they're guarded and may come back None here too -- callers must handle that."""
    if only_crypto is not None:
        positions = [p for p in positions if (p.asset_class == AssetClass.CRYPTO) == only_crypto]
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_plpc": float(p.unrealized_plpc),
            "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl is not None else None,
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price) if p.current_price is not None else None,
        }
        for p in positions
    ]
