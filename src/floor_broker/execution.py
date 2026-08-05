import json
import time

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import AssetClass, OrderClass, OrderSide, OrderStatus, OrderType, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from src.common import kill_switch
from src.common.alpaca_client import get_current_ask_price, get_current_bid_price, trading_client
from src.common.config import load_config
from src.common.logging import get_logger

log = get_logger("FLOOR")

MIN_CRYPTO_NOTIONAL = 10.0  # Alpaca rejects a crypto notional below this (code 40310000)

ORDER_NOT_FOUND_CODE = 40410000  # Alpaca's code for "no order exists with that id"

_TERMINAL_NO_FILL = {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}

_RECONCILE_MAX_STARTUP_ATTEMPTS = 5
_RECONCILE_BACKOFF_BASE_S = 5.0


class InvalidOrderParameters(Exception):
    """Raised when a stock bracket order's computed quantity/prices fail the invariant checks
    below before submission (ROADMAP P0.9) -- a stale/zero quote or an inverted SL/TP
    relationship should never reach Alpaca as a live order."""


class InsufficientQuantity(InvalidOrderParameters):
    """The specific InvalidOrderParameters case where the budget affords less than one whole
    share at the reference price -- not a bug, just too little budget for the current price, so
    buy() turns this into a normal status="skipped" outcome rather than propagating."""

# Tracks the parent order id of each open bracket BUY, keyed by symbol, so the fill-watcher
# (check_bracket_fills) can later find out which of its TP/SL legs eventually filled. Value is
# either a plain order-id string (the normal case) or, once a poll has hit a transient error for
# that symbol, {"order_id": str, "poll_failures": int}. In-memory only -- reconstruct_tracked_state()
# rebuilds this from Alpaca's own open-orders state on process start, but only for brackets still
# open on Alpaca; a bracket that fills in the gap between the pod dying and reconstruct running
# still produces no Slack notice for that one fill (the trade itself executes fine either way).
_tracked_brackets: dict[str, str | dict] = {}

# Tracks every order buy()/sell() itself submitted, keyed by order id, so check_pending_fills()
# can later report that order's own fill (ROADMAP P0.14) -- distinct from _tracked_brackets
# above, which is only for a bracket's *child* TP/SL legs. Same in-memory + reconstruct-on-start
# caveat as _tracked_brackets: an order that fills in the gap between the pod dying and
# reconstruct_tracked_state() running on the new pod still produces no Slack notice for that fill.
_pending_fills: dict[str, dict] = {}

# Synthetic stop-loss/take-profit for open crypto positions, keyed by symbol, value (sl_price,
# tp_price). Alpaca's bracket/OCO orders are equity-only (alpaca.trading.enums.OrderClass:
# "Crypto trading: simple (or \"\")"), so crypto has no server-side SL/TP at all -- this dict plus
# check_crypto_stops() below is the entire mechanism. In-memory only, same restart caveat as
# _tracked_brackets/_pending_fills: a Floor Broker restart drops tracking for any crypto position
# still open at the time, silently losing its stop/target until the next manual Dealer SELL.
_crypto_stops: dict[str, tuple[float, float]] = {}

# False from process start until reconcile_tracked_state_once() has succeeded at least once.
# buy() refuses new BUYs while this is False (see below) -- submitting a fresh order before
# Alpaca's live open-order state has been reconciled into _pending_fills/_tracked_brackets risks
# losing track of it exactly like the restart gap this whole mechanism exists to close.
_state_reconciled = False


def _is_order_not_found(exc: APIError) -> bool:
    """True only for a confirmed "no such order" response. Any other shape -- a genuinely
    different code, or a non-JSON/malformed error body (e.g. a raw network exception) -- must be
    treated as transient rather than assumed to mean not-found, since dropping tracked state
    should require positive confirmation, not just an unparseable error."""
    try:
        return exc.code == ORDER_NOT_FOUND_CODE
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return False


def check_pending_fills() -> list[dict]:
    """Polls every order buy()/sell() submitted for its own fill -- /execute now returns
    status="submitted" before this is known (ROADMAP P0.14), so this is the only place a
    submitted order's fill is ever observed and reported.

    A transient APIError (rate limit, timeout, Alpaca-side 5xx) must not drop the entry -- that
    would silently stop watching a live order. Only a confirmed 404 (the order genuinely no
    longer exists) removes it without a fill ever being observed."""
    events = []
    for order_id, ctx in list(_pending_fills.items()):
        try:
            order = trading_client.get_order_by_id(order_id)
        except APIError as exc:
            if _is_order_not_found(exc):
                log(f"⚠️  pending order {order_id} ({ctx['symbol']}) no longer exists on Alpaca -- dropping")
                _pending_fills.pop(order_id, None)
            else:
                ctx["poll_failures"] = ctx.get("poll_failures", 0) + 1
                log(f"💥  poll failure #{ctx['poll_failures']} for pending order {order_id} ({ctx['symbol']}): {exc}")
            continue

        ctx.pop("poll_failures", None)

        if order.filled_avg_price is not None:
            fill_price = float(order.filled_avg_price)
            if ctx.get("crypto_slP") is not None:
                _crypto_stops[ctx["symbol"]] = (fill_price * ctx["crypto_slP"], fill_price * ctx["crypto_tpP"])
                log(f"🎯  tracking synthetic stop/target for {ctx['symbol']}: {_crypto_stops[ctx['symbol']]}")
            events.append({**ctx, "kind": "fill", "order_id": order_id, "fill_price": fill_price})
            _pending_fills.pop(order_id, None)
        elif order.status in _TERMINAL_NO_FILL:
            events.append({**ctx, "kind": "terminal", "order_id": order_id, "order_status": order.status.value})
            _pending_fills.pop(order_id, None)

    return events


def check_bracket_fills() -> list[dict]:
    """Polls every tracked bracket BUY for a TP or SL leg that has since filled. A bracket's two
    child legs are OCO (one-cancels-other) on Alpaca's side -- once either fills, the other is
    auto-cancelled, so a symbol is untracked as soon as either outcome is observed.

    Same transient-vs-terminal distinction as check_pending_fills: a non-404 APIError keeps the
    symbol tracked and just records the failure."""
    events = []
    for symbol, entry in list(_tracked_brackets.items()):
        order_id = entry["order_id"] if isinstance(entry, dict) else entry
        try:
            order = trading_client.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True))
        except APIError as exc:
            if _is_order_not_found(exc):
                log(f"⚠️  tracked bracket order {order_id} ({symbol}) no longer exists on Alpaca -- dropping")
                _tracked_brackets.pop(symbol, None)
            else:
                failures = _bracket_poll_failures(symbol) + 1
                _tracked_brackets[symbol] = {"order_id": order_id, "poll_failures": failures}
                log(f"💥  poll failure #{failures} for tracked bracket {order_id} ({symbol}): {exc}")
            continue

        legs = order.legs or []
        filled_leg = next((leg for leg in legs if leg.status == OrderStatus.FILLED), None)
        if filled_leg is not None:
            events.append(
                {
                    "kind": "fill",
                    "symbol": symbol,
                    "order_id": filled_leg.id,
                    "reason": "take_profit" if filled_leg.type == OrderType.LIMIT else "stop_loss",
                    "fill_price": float(filled_leg.filled_avg_price) if filled_leg.filled_avg_price else None,
                    "qty": float(filled_leg.filled_qty) if filled_leg.filled_qty else None,
                }
            )
            _tracked_brackets.pop(symbol, None)
        elif legs and all(leg.status in _TERMINAL_NO_FILL for leg in legs):
            events.append(
                {
                    "kind": "terminal",
                    "symbol": symbol,
                    "order_id": order_id,
                    "leg_statuses": [leg.status.value for leg in legs],
                }
            )
            _tracked_brackets.pop(symbol, None)
        else:
            _tracked_brackets[symbol] = order_id

    return events


def _bracket_poll_failures(symbol: str) -> int:
    entry = _tracked_brackets.get(symbol)
    return entry["poll_failures"] if isinstance(entry, dict) else 0


def check_crypto_stops() -> list[dict]:
    """Polls every tracked crypto position's synthetic stop-loss/take-profit against its current
    bid (the price an immediate market SELL would realize). A transient price-fetch failure just
    skips that symbol this round -- it stays tracked and gets checked again on the next poll."""
    events = []
    for symbol, (sl_price, tp_price) in list(_crypto_stops.items()):
        try:
            bid = get_current_bid_price(symbol)
        except APIError as exc:
            log(f"💥  failed to fetch bid for tracked crypto stop {symbol}: {exc}")
            continue

        if bid <= sl_price or bid >= tp_price:
            reason = "stop_loss" if bid <= sl_price else "take_profit"
            _crypto_stops.pop(symbol, None)
            result = sell(symbol, reason=reason)
            events.append({"symbol": symbol, "reason": reason, "bid_price": bid, "sell_result": result})

    return events


def check_eod_flatten() -> list[dict]:
    """Feature-gated (strategy config eod_flatten.enabled, off by default). When enabled and
    Alpaca's live clock reports the market is within eod_flatten.minutes_before_close minutes of
    closing, sells every open stock position -- crypto is 24/7 so "end of day" doesn't apply to
    it. Uses the live clock (not a fixed schedule) so early/half-trading-close days are handled
    correctly with no special-casing. No "already flattened today" bookkeeping is needed: once a
    symbol is sold, trading_client.get_all_positions() simply stops returning it, so later polls
    within the same closing window are cheap no-ops."""
    cfg = load_config()  # fresh (within its own refresh window), same live-reload pattern buy() uses
    if not cfg.eod_flatten.enabled:
        return []

    clock = trading_client.get_clock()
    if not clock.is_open:
        return []

    minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
    if minutes_to_close > cfg.eod_flatten.minutes_before_close:
        return []

    events = []
    for position in trading_client.get_all_positions():
        if position.asset_class != AssetClass.US_EQUITY:
            continue  # crypto is 24/7 -- "end of day" doesn't apply, leave it alone
        result = sell(position.symbol, reason="eod_flatten")
        if result["status"] != "skipped":
            events.append({"symbol": position.symbol, "reason": "eod_flatten", "sell_result": result})
    return events


def is_state_reconciled() -> bool:
    return _state_reconciled


def reconcile_tracked_state_once() -> bool:
    """Rebuilds _pending_fills and _tracked_brackets from Alpaca's own open-orders state -- both
    dicts are in-memory only, so a Floor Broker restart otherwise loses track of every order/
    bracket that was still open at the moment it went down, silently dropping their eventual fill
    notifications. Returns True and marks state reconciled on success; False (never raises) on
    any APIError, leaving existing tracked state and is_state_reconciled() untouched so a later
    retry can still succeed.

    GetOrdersRequest(status="open") queries at the order-*family* level (per Alpaca's own
    semantics, distinct from an individual leg's OrderStatus) -- a bracket whose entry already
    filled but whose TP/SL legs are still live is still "open" here, so nested=True correctly
    surfaces it as a parent with its legs attached, the same shape check_bracket_fills expects.
    Only orders still open on Alpaca are restored -- by definition nothing has filled yet here, so
    no notification could have been missed by this reconciliation itself. An order that fills in
    the gap between the pod dying and this running is a separate, narrower gap this cannot close
    -- Alpaca no longer reports it as "open" once filled -- the trade itself is still correct at
    Alpaca, only its Slack fill notice is missed."""
    global _state_reconciled
    try:
        open_orders = trading_client.get_orders(GetOrdersRequest(status="open", nested=True))
    except APIError as exc:
        log(f"💥  failed to fetch open orders from Alpaca while reconciling tracked state: {exc}")
        return False

    restored_pending = 0
    restored_brackets = 0
    for order in open_orders:
        legs = order.legs or []
        if legs:
            if any(leg.status not in _TERMINAL_NO_FILL for leg in legs):
                _tracked_brackets[order.symbol] = order.id
                restored_brackets += 1
        elif order.filled_avg_price is None and order.status not in _TERMINAL_NO_FILL:
            _pending_fills[order.id] = {
                "symbol": order.symbol,
                "action": "BUY" if order.side == OrderSide.BUY else "SELL",
                "reason": "reconstructed_after_restart",
                "sl_price": None,
                "tp_price": None,
            }
            restored_pending += 1

    log(f"🔄  reconstructed {restored_pending} pending order(s) and {restored_brackets} bracket(s) from Alpaca")
    _state_reconciled = True
    return True


def reconstruct_tracked_state(
    max_attempts: int = _RECONCILE_MAX_STARTUP_ATTEMPTS, backoff_base_s: float = _RECONCILE_BACKOFF_BASE_S
) -> None:
    """Runs once at Floor Broker startup, before poll threads start. Retries
    reconcile_tracked_state_once() with exponential backoff -- a transient Alpaca outage at
    exactly boot time shouldn't permanently strand the service with empty tracking dicts from a
    single failed attempt. If every attempt fails, is_state_reconciled() stays False -- buy()
    refuses new BUYs (see below) until main.poll_reconciliation() succeeds in the background."""
    for attempt in range(1, max_attempts + 1):
        if reconcile_tracked_state_once():
            return
        if attempt < max_attempts:
            backoff = backoff_base_s * (2 ** (attempt - 1))
            log(f"🔄  reconciliation attempt {attempt}/{max_attempts} failed, retrying in {backoff:.0f}s")
            time.sleep(backoff)

    log(
        f"🚨  exhausted {max_attempts} startup reconciliation attempts -- "
        "BUY execution will be rejected until state reconciles; retrying in background"
    )


def _fetch_open_position(symbol: str):
    try:
        return trading_client.get_open_position(symbol.replace("/", ""))
    except APIError:
        return None


def get_open_position(symbol: str) -> float:
    is_stock = symbol.find("/") == -1
    position = _fetch_open_position(symbol)
    if position is None:
        return 0
    qty = int(float(position.qty)) if is_stock else float(position.qty)
    log(f"📈  open position: {qty} of {symbol.replace('/', '')}")
    return qty


def cancel_related_orders(order_ids: list[str]) -> None:
    for oid in order_ids:
        try:
            trading_client.cancel_order_by_id(oid)
            log(f"✅  cancelled conflicting order {oid}")
        except APIError as exc:
            if exc.code != ORDER_NOT_FOUND_CODE:
                raise


def get_qty(ask: float, budget: float) -> int:
    if ask <= 0:
        return 0
    return int(budget // ask)


def _round_to_tick(price: float) -> float:
    # SEC Rule 612 / Alpaca: sub-$1 stocks are quoted in $0.0001 increments, not $0.01 --
    # rounding to 2dp for these can land TP/SL on the same cent as base_price and get rejected.
    return round(price, 4) if price < 1.0 else round(price, 2)


def _validate_bracket_order(ask: float, qty: int, budget: float, stop_loss_px: float, take_profit_px: float) -> None:
    """Invariant checks (ROADMAP P0.9) run just before a stock bracket order is built, using the
    same single reference price (`ask`) that sized `qty` and priced `stop_loss_px`/
    `take_profit_px` -- see P0.8: these three values must all derive from one quote, not
    independent ones fetched moments apart."""
    if ask <= 0:
        raise InvalidOrderParameters(f"non-positive reference price: {ask}")
    if budget <= 0:
        raise InvalidOrderParameters(f"non-positive budget: {budget}")
    if qty < 1:
        raise InsufficientQuantity(f"budget {budget} affords < 1 share at reference price {ask}")
    if not (0 < stop_loss_px < ask < take_profit_px):
        raise InvalidOrderParameters(
            f"invalid SL/TP relationship: stop_loss={stop_loss_px} ask={ask} take_profit={take_profit_px}"
        )
    estimated_notional = qty * ask
    if estimated_notional > budget:
        raise InvalidOrderParameters(f"estimated notional {estimated_notional} exceeds authorized budget {budget}")


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

    # Quantity is sized off the same `ask` used for TP/SL above (P0.8) -- previously get_qty()
    # fetched its own independent quote, so qty and bracket prices could be based on different
    # market snapshots.
    qty = get_qty(ask, budget)

    log(f"📈  ask-price {ask:.2f} => TP {take_profit_px}  |  SL {stop_loss_px}  |  qty {qty}")

    _validate_bracket_order(ask, qty, budget, stop_loss_px, take_profit_px)

    return MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=stop_loss_px),
        take_profit=TakeProfitRequest(limit_price=take_profit_px),
    )


def buy(symbol: str, exchange: str, budget: float, slP: float, tpP: float) -> dict:
    # A restart's tracked state isn't reconciled with Alpaca yet -- submitting a fresh BUY before
    # that finishes risks the new order never being tracked if the pod dies again in the gap, so
    # refuse rather than race reconcile_tracked_state_once()/poll_reconciliation() in main.py.
    if not _state_reconciled:
        log(f"🛑  BUY {symbol} rejected -- tracked state not yet reconciled with Alpaca")
        # "skipped", not a distinct "rejected" status -- ExecuteResponse's status Literal
        # (src/floor_broker/app.py) only permits executed/submitted/skipped/error, and every
        # other "chose not to submit this BUY" outcome in this function already uses "skipped";
        # the `reason` field is what actually distinguishes this case downstream.
        return {
            "status": "skipped",
            "reason": "state_not_reconciled",
            "detail": "tracked state not yet reconciled with Alpaca after restart",
        }

    # ROADMAP P0.5: an operator-controlled runtime switch, checked fresh on every BUY (never
    # cached) so a kubectl patch takes effect on the very next request without a redeploy. SELL
    # is deliberately untouched -- the switch only ever blocks new exposure, never an exit.
    if kill_switch.buy_kill_switch_active():
        log(f"🛑  BUY kill switch active -- skipping BUY {symbol}")
        return {"status": "skipped", "reason": "buy_kill_switch_active", "detail": "BUY kill switch is active"}

    # strategy.daily_profit_target_usd/daily_loss_limit_usd (set by /configure-strategy) -- Alpaca's
    # own account.equity/last_equity already handles the trading-day boundary, so no custom
    # bookkeeping is needed. Only blocks new BUYs (halt_behavior: block_new_buys); SELL is
    # unaffected, same as the kill switch above.
    cfg = load_config()  # fresh (within its own refresh window) so a live strategy change never needs a restart
    account = trading_client.get_account()
    daily_pnl = float(account.equity) - float(account.last_equity)
    if daily_pnl >= cfg.strategy.daily_profit_target_usd:
        log(f"🛑  daily profit target reached (${daily_pnl:.2f}) -- skipping BUY {symbol}")
        return {
            "status": "skipped",
            "reason": "daily_profit_target_reached",
            "detail": f"daily P&L ${daily_pnl:.2f} >= target ${cfg.strategy.daily_profit_target_usd}",
        }
    if daily_pnl <= -cfg.strategy.daily_loss_limit_usd:
        log(f"🛑  daily loss limit reached (${daily_pnl:.2f}) -- skipping BUY {symbol}")
        return {
            "status": "skipped",
            "reason": "daily_loss_limit_reached",
            "detail": f"daily P&L ${daily_pnl:.2f} <= -limit ${cfg.strategy.daily_loss_limit_usd}",
        }

    position = _fetch_open_position(symbol)

    oo = trading_client.get_orders(GetOrdersRequest(status="open"))
    matching_orders = [order for order in oo if order.symbol == symbol]

    if matching_orders:
        log(f"⚠️  open orders exist for {symbol} - aborting BUY")
        return {"status": "skipped", "reason": "open_orders_exist", "detail": "open orders exist for symbol"}

    if position is not None:
        # `budget` is the dollar amount authorized for this symbol, not a one-shot "open a fresh
        # position" ticket -- a position sitting under its authorized budget should be allowed to
        # grow toward it rather than getting permanently stuck at whatever the first fill landed on.
        if position.market_value is None:
            # Trading-money gate: fail closed on missing data, unlike the Analyst's informational
            # P&L snapshot (summarize_positions()) which fails open -- can't safely compute
            # remaining headroom without a market_value to subtract.
            log(f"⚠️  {symbol} existing position market_value unavailable - aborting BUY")
            return {
                "status": "skipped",
                "reason": "market_value_unavailable",
                "detail": "existing position market_value unavailable",
            }

        existing_value = float(position.market_value)
        if existing_value >= budget:
            log(f"⚠️  {symbol} position (${existing_value:.2f}) already at/above budget (${budget:.2f}) - skipping BUY")
            return {
                "status": "skipped",
                "reason": "budget_exhausted",
                "detail": f"existing position value ${existing_value:.2f} >= budget ${budget:.2f}",
            }

        remaining_budget = budget - existing_value
        log(
            f"📈  {symbol} position (${existing_value:.2f}) below budget (${budget:.2f}) "
            f"- topping up ${remaining_budget:.2f}"
        )
        budget = remaining_budget

    if exchange == "stocks":
        try:
            req = bracket_buy_with_SLTP(symbol, budget, slP, tpP)
        except InsufficientQuantity as exc:
            log(f"⚠️  {exc} -- skipping BUY")
            return {"status": "skipped", "reason": "insufficient_qty", "detail": str(exc)}
        except InvalidOrderParameters as exc:
            # Catches invariants _validate_bracket_order() can't satisfy at all for a given quote
            # (e.g. a sub-$0.02 stock, where the $0.02 SL/TP floor/ceiling clamp pushes stop_loss_px
            # negative) -- these aren't a caller/config error, just an unbrokerable symbol at this
            # price, so skip cleanly rather than let it fall through to app.py's 500 handler.
            log(f"⚠️  {exc} -- skipping BUY")
            return {"status": "skipped", "reason": "invalid_order_parameters", "detail": str(exc)}
    else:
        # Alpaca rejects a crypto notional with more than 2 decimal places (code 42210000); the
        # BTCUSD failure came from a `budget` value with more precision than that (e.g. a
        # merged position's market_value), so round before submitting rather than trusting the
        # caller to have already done so.
        notional = round(budget, 2)

        # Alpaca also rejects a crypto notional below its minimum order value (code 40310000,
        # "cost basis must be >= minimal amount of order 10"). Clamping up to that floor would
        # silently submit an order larger than the caller's intended budget, so skip instead --
        # the caller (Dealer) is responsible for supplying a sufficient budget in the first place.
        if notional < MIN_CRYPTO_NOTIONAL:
            log(f"⚠️  budget {notional} below Alpaca's ${MIN_CRYPTO_NOTIONAL:.0f} crypto minimum -- skipping")
            return {
                "status": "skipped",
                "reason": "budget_below_minimum",
                "detail": f"budget {notional} below ${MIN_CRYPTO_NOTIONAL:.0f} crypto minimum",
            }

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
        try:
            req = bracket_buy_with_SLTP(symbol, budget, slP, tpP, base_price=float(base_price))
        except InsufficientQuantity as retry_exc:
            log(f"⚠️  {retry_exc} -- skipping BUY on retry")
            return {"status": "skipped", "reason": "insufficient_qty", "detail": str(retry_exc)}
        except InvalidOrderParameters as retry_exc:
            log(f"⚠️  {retry_exc} -- skipping BUY on retry")
            return {"status": "skipped", "reason": "invalid_order_parameters", "detail": str(retry_exc)}
        order = trading_client.submit_order(req)

    log(f"✅  buy order submitted: {order.id}")

    if exchange == "stocks":
        _tracked_brackets[symbol] = order.id
        sl_price = req.stop_loss.stop_price
        tp_price = req.take_profit.limit_price
        crypto_slP = crypto_tpP = None
    else:
        sl_price = None
        tp_price = None
        # No fill price is known yet for a notional market order -- store the strategy.crypto_slP/
        # crypto_tpP multipliers here so check_pending_fills() can compute the actual sl_price/
        # tp_price once the fill (and its real fill_price) is observed.
        crypto_slP = cfg.strategy.crypto_slP
        crypto_tpP = cfg.strategy.crypto_tpP

    _pending_fills[order.id] = {
        "symbol": symbol,
        "action": "BUY",
        "reason": "opening_position",
        "sl_price": sl_price,
        "tp_price": tp_price,
        "crypto_slP": crypto_slP,
        "crypto_tpP": crypto_tpP,
    }

    return {
        "status": "submitted",
        "reason": "opening_position",
        "detail": f"buy order submitted: {order.id}",
        "order_id": str(order.id),
        "sl_price": sl_price,
        "tp_price": tp_price,
    }


def sell(symbol: str, reason: str = "dealer_signal") -> dict:
    qty = get_open_position(symbol)

    if qty <= 0:
        log(f"⚠️  no open position of {symbol} to sell")
        return {"status": "skipped", "detail": "no open position"}

    req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)

    # An explicit SELL closes the position, which also cancels any still-open TP/SL bracket legs
    # on Alpaca's side -- stop watching for a bracket fill on this symbol immediately rather than
    # waiting for check_bracket_fills() to notice the legs went terminal with no fill. Same idea
    # for a tracked synthetic crypto stop/target -- a manual/Dealer-driven SELL already closes the
    # position, so check_crypto_stops() must not also try to sell it.
    _tracked_brackets.pop(symbol, None)
    _crypto_stops.pop(symbol, None)

    try:
        order = trading_client.submit_order(req)
        log(f"✅  sell order submitted: {order.id}")
        _pending_fills[order.id] = {"symbol": symbol, "action": "SELL", "reason": reason, "sl_price": None, "tp_price": None}
        return {
            "status": "submitted",
            "reason": reason,
            "detail": f"sell order submitted: {order.id}",
            "order_id": str(order.id),
        }
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
            _pending_fills[order.id] = {"symbol": symbol, "action": "SELL", "reason": reason, "sl_price": None, "tp_price": None}
            return {
                "status": "submitted",
                "reason": reason,
                "detail": f"sell order submitted: {order.id}",
                "order_id": str(order.id),
            }
        except APIError as retry_exc:
            log(f"💥  sell retry failed for {symbol}: {retry_exc}")
            raise
