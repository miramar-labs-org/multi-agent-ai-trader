import json

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from src.common.alpaca_client import get_current_ask_price, trading_client
from src.common.logging import get_logger

log = get_logger("FLOOR")

MIN_CRYPTO_NOTIONAL = 10.0  # Alpaca rejects a crypto notional below this (code 40310000)


def get_open_position(symbol: str) -> float:
    try:
        is_stock = symbol.find("/") == -1
        symbol = symbol.replace("/", "")
        position = trading_client.get_open_position(symbol)
        qty = int(float(position.qty)) if is_stock else float(position.qty)
        log(f"📈  open position: {qty} of {symbol}")
        return qty
    except APIError:
        return 0


def cancel_related_orders(order_ids: list[str]) -> None:
    for oid in order_ids:
        try:
            trading_client.cancel_order_by_id(oid)
            log(f"✅  cancelled conflicting order {oid}")
        except APIError as exc:
            if exc.code != 40410000:
                raise


def get_qty(symbol: str, budget: float) -> int:
    ask = get_current_ask_price(symbol)
    if ask == 0:
        return 0
    return int(budget // ask)


def _round_to_tick(price: float) -> float:
    # SEC Rule 612 / Alpaca: sub-$1 stocks are quoted in $0.0001 increments, not $0.01 --
    # rounding to 2dp for these can land TP/SL on the same cent as base_price and get rejected.
    return round(price, 4) if price < 1.0 else round(price, 2)


def bracket_buy_with_SLTP(
    symbol: str, budget: float, slP: float, tpP: float, base_price: float | None = None
) -> MarketOrderRequest:
    # A bracket BUY fills near the ask, not the bid/ask mid -- pricing TP/SL off mid understates
    # the real entry price and can fall below Alpaca's `base_price + 0.01` floor on wide-spread
    # symbols, causing the whole order to be rejected. `base_price`, when given, is Alpaca's own
    # rejection-supplied reference price for a retry (see `buy()`) -- it takes priority over a
    # fresh client-side quote, which can diverge from Alpaca's reference on thin, low-priced
    # symbols (e.g. our free-tier IEX-only feed missing the true NBBO).
    ask = base_price if base_price is not None else get_current_ask_price(symbol)

    # Alpaca also enforces an absolute $0.01 minimum distance between TP/SL and base_price,
    # regardless of stock price -- on sub-~$0.50 stocks, slP/tpP's percentage move (e.g. 2%/5%)
    # doesn't reach a full cent, so the percentage-based price must be clamped to that floor.
    # Clamp to $0.02, not the bare $0.01 minimum, for a small safety margin against price
    # movement in the moments between quoting `ask` and Alpaca validating the order.
    take_profit_px = max(_round_to_tick(ask * tpP), _round_to_tick(ask + 0.02))
    stop_loss_px = min(_round_to_tick(ask * slP), _round_to_tick(ask - 0.02))

    log(f"📈  ask-price {ask:.2f} => TP {take_profit_px}  |  SL {stop_loss_px}")

    return MarketOrderRequest(
        symbol=symbol,
        qty=get_qty(symbol, budget),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=stop_loss_px),
        take_profit=TakeProfitRequest(limit_price=take_profit_px),
    )


def buy(symbol: str, exchange: str, budget: float, slP: float, tpP: float) -> dict:
    op = get_open_position(symbol)

    oo = trading_client.get_orders(GetOrdersRequest(status="open"))
    matching_orders = [order for order in oo if order.symbol == symbol]

    if op != 0 or matching_orders:
        log(f"⚠️  open orders/positions exist for {symbol} - aborting BUY")
        return {"status": "skipped", "detail": "open orders/positions exist"}

    if exchange == "stocks":
        req = bracket_buy_with_SLTP(symbol, budget, slP, tpP)
    else:
        # Alpaca rejects a crypto notional with more than 2 decimal places (code 42210000); the
        # BTCUSD failure came from a `budget` value with more precision than that (e.g. a
        # merged position's market_value), so round before submitting rather than trusting the
        # caller to have already done so.
        notional = round(budget, 2)

        # Alpaca also rejects a crypto notional below its minimum order value (code 40310000,
        # "cost basis must be >= minimal amount of order 10") -- a merged position sized off a
        # shrunk market_value can fall under that floor, so clamp up to it rather than let the
        # BUY fail outright.
        if notional < MIN_CRYPTO_NOTIONAL:
            log(f"⚠️  budget {notional} below Alpaca's ${MIN_CRYPTO_NOTIONAL:.0f} crypto minimum -- clamping up")
            notional = MIN_CRYPTO_NOTIONAL

        req = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )

    try:
        order = trading_client.submit_order(req)
    except APIError as exc:
        if exchange != "stocks":
            raise
        try:
            err = json.loads(str(exc))
        except json.JSONDecodeError:
            raise

        base_price = err.get("base_price")
        if err.get("code") != 42210000 or base_price is None:
            raise

        # Our client-quoted `ask` can diverge from Alpaca's own base_price on thin, low-priced
        # symbols -- rather than guess an ever-bigger buffer around our own quote, retry once
        # using Alpaca's own authoritative reference price straight from the rejection.
        log(f"🔄  retrying BUY {symbol} priced off Alpaca's own base_price {base_price} ...")
        req = bracket_buy_with_SLTP(symbol, budget, slP, tpP, base_price=float(base_price))
        order = trading_client.submit_order(req)

    log(f"✅  buy order submitted: {order.id}")
    return {"status": "executed", "detail": f"buy order submitted: {order.id}"}


def sell(symbol: str) -> dict:
    qty = get_open_position(symbol)

    if qty <= 0:
        log(f"⚠️  no open position of {symbol} to sell")
        return {"status": "skipped", "detail": "no open position"}

    req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)

    try:
        order = trading_client.submit_order(req)
        log(f"✅  sell order submitted: {order.id}")
        return {"status": "executed", "detail": f"sell order submitted: {order.id}"}
    except APIError as exc:
        try:
            err = json.loads(str(exc))
        except json.JSONDecodeError:
            raise

        if err.get("code") != 40310000:
            raise

        log(f"⚠️  {err.get('message')} for {err.get('symbol')}")
        cancel_related_orders(err.get("related_orders", []))

        # The qty available to sell can change once the blocking orders are
        # cleared, so the retry must recompute qty and rebuild req rather than
        # resubmitting the stale req captured before cleanup.
        qty = get_open_position(symbol)
        if qty <= 0:
            log(f"⚠️  no qty of {symbol} remaining after cleanup")
            return {"status": "skipped", "detail": "no qty remaining after cleanup"}

        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)

        try:
            log("🔄  retrying after clean-up ...")
            order = trading_client.submit_order(req)
            log(f"✅  sell order submitted: {order.id}")
            return {"status": "executed", "detail": f"sell order submitted: {order.id}"}
        except APIError as retry_exc:
            log(f"💥  sell retry failed for {symbol}: {retry_exc}")
            raise
