import json

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from src.common.alpaca_client import get_current_ask_price, get_current_bid_price, trading_client
from src.common.logging import get_logger

log = get_logger("FLOOR")


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


def bracket_buy_with_SLTP(symbol: str, budget: float, slP: float, tpP: float) -> MarketOrderRequest:
    ask = get_current_ask_price(symbol)
    bid = get_current_bid_price(symbol)
    mid = (ask + bid) / 2

    take_profit_px = round(mid * tpP, 2)
    stop_loss_px = round(mid * slP, 2)

    log(f"📈  mid-price {mid:.2f} => TP {take_profit_px}  |  SL {stop_loss_px}")

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
        req = MarketOrderRequest(
            symbol=symbol,
            notional=budget,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )

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
