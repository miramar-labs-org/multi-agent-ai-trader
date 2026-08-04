import json

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType

from src.floor_broker import execution


def _api_error(payload: dict) -> APIError:
    err = APIError.__new__(APIError)
    err.args = (json.dumps(payload),)
    err._error = json.dumps(payload)
    return err


@pytest.fixture(autouse=True)
def _clear_tracked_brackets():
    """_tracked_brackets and _pending_fills are module-level state shared across every test in
    this file -- clear them before and after each test so tests can't leak tracking into one
    another."""
    execution._tracked_brackets.clear()
    execution._pending_fills.clear()
    yield
    execution._tracked_brackets.clear()
    execution._pending_fills.clear()


@pytest.fixture(autouse=True)
def _kill_switch_inactive(monkeypatch):
    """ROADMAP P0.5: every test in this file exercises buy() without a live k8s API to read the
    real ConfigMap against -- default the switch to inactive so existing BUY-path tests are
    unaffected; tests that care about the switch itself override this explicitly."""
    monkeypatch.setattr(execution.kill_switch, "buy_kill_switch_active", lambda: False)


class FakeTradingClient:
    """Stands in for alpaca-py's TradingClient. `submit_order` raises the given rejection(s)
    in order, then succeeds -- lets tests replay real Alpaca rejection payloads without any
    network access."""

    def __init__(self, rejections=()):
        self._rejections = list(rejections)
        self.submitted = []

    def get_open_position(self, symbol):
        raise APIError("no position")

    def get_orders(self, request):
        return []

    def submit_order(self, req):
        self.submitted.append(req)
        if self._rejections:
            raise _api_error(self._rejections.pop(0))
        return type("Order", (), {"id": "order-123"})()


class FakeLeg:
    def __init__(self, id, status, type_, filled_avg_price=None, filled_qty=None):
        self.id = id
        self.status = status
        self.type = type_
        self.filled_avg_price = filled_avg_price
        self.filled_qty = filled_qty


class FakeBracketOrder:
    def __init__(self, legs):
        self.legs = legs


class FakeBracketTradingClient:
    """Stands in for alpaca-py's TradingClient for check_bracket_fills() -- `orders_by_id` maps
    a parent order id to either a FakeBracketOrder or an exception instance to raise."""

    def __init__(self, orders_by_id):
        self._orders_by_id = orders_by_id

    def get_order_by_id(self, order_id, filter=None):
        result = self._orders_by_id[order_id]
        if isinstance(result, Exception):
            raise result
        return result


def test_bracket_pricing_uses_ask_not_mid(monkeypatch):
    """Regression for the HYFM rejection: a bracket BUY fills near the ask, not the bid/ask
    mid -- pricing off mid understated the real entry price and got the order rejected.
    TP/SL must be derived from the ask, not from (ask+bid)/2."""
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 2.32)

    req = execution.bracket_buy_with_SLTP("HYFM", budget=5000.0, slP=0.98, tpP=1.05)

    assert req.take_profit.limit_price == pytest.approx(2.32 * 1.05, abs=0.01)
    assert req.stop_loss.stop_price == pytest.approx(2.32 * 0.98, abs=0.01)


def test_round_to_tick_sub_dollar_precision():
    """Regression for MGN base_price=0.1616 rejection: rounding TP/SL to 2 decimals on a
    sub-$1 stock collapses distinct prices onto the same cent (SEC Rule 612 requires
    $0.0001 increments below $1); 4-decimal rounding must be used instead."""
    assert execution._round_to_tick(0.158368) == 0.1584
    assert execution._round_to_tick(2.32 * 0.98) == 2.27


def test_bracket_buy_uses_a_single_quote_for_qty_and_prices(monkeypatch):
    """Regression for P0.8: get_qty() used to fetch its own independent ask, so quantity and
    TP/SL could be priced off different market snapshots. get_current_ask_price() must now be
    called exactly once per bracket-BUY attempt, and the resulting qty must be consistent with
    that single ask."""
    calls = []

    def _fake_ask(symbol):
        calls.append(symbol)
        return 10.0

    monkeypatch.setattr(execution, "get_current_ask_price", _fake_ask)

    req = execution.bracket_buy_with_SLTP("MGN", budget=5000.0, slP=0.98, tpP=1.05)

    assert calls == ["MGN"], "get_current_ask_price must be called exactly once"
    assert req.qty == 500  # int(5000.0 // 10.0)


def test_bracket_buy_raises_on_zero_price_quote(monkeypatch):
    """P0.9: a zero-price quote must not produce an order."""
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.0)

    with pytest.raises(execution.InvalidOrderParameters):
        execution.bracket_buy_with_SLTP("MGN", budget=5000.0, slP=0.98, tpP=1.05)


def test_bracket_buy_raises_when_stop_loss_goes_non_positive(monkeypatch):
    """P0.9: on an extremely low-priced symbol, `ask - 0.02` can go to zero or negative --
    stop_loss_px must stay strictly positive and below the reference price, or the order must
    be rejected before submission rather than sent to Alpaca."""
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.01)

    with pytest.raises(execution.InvalidOrderParameters):
        execution.bracket_buy_with_SLTP("MGN", budget=5000.0, slP=0.98, tpP=1.05)


def test_buy_skips_with_insufficient_qty_reason_when_budget_affords_less_than_one_share(monkeypatch):
    """P0.9: qty < 1 is a normal, expected outcome (budget too small for the current price at
    all) rather than a bug -- must produce a status="skipped" result, not an exception, and
    must never call submit_order."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10000.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_qty"
    assert fake_client.submitted == []


def test_buy_skips_with_insufficient_qty_reason_on_retry(monkeypatch):
    """Same insufficient-qty skip, but reached via the base_price retry path -- a divergent
    Alpaca base_price can itself push the affordable quantity below one share."""
    rejection = {"base_price": "10000.00", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}
    fake_client = FakeTradingClient(rejections=[rejection])
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 1.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_qty"
    assert len(fake_client.submitted) == 1, "the initial (rejected) attempt submits once; the retry must not submit at all"


def test_get_qty_uses_the_given_ask_not_a_fresh_quote():
    assert execution.get_qty(ask=10.0, budget=5000.0) == 500
    assert execution.get_qty(ask=0.0, budget=5000.0) == 0
    assert execution.get_qty(ask=-1.0, budget=5000.0) == 0


def test_bracket_clamps_tp_sl_to_minimum_cent_distance(monkeypatch):
    """Regression for MGN base_price=0.1577 rejection: tpP/slP's percentage move (5%/2%) is
    less than $0.01 on stocks priced under ~$0.50, so the percentage-based price alone isn't
    enough -- it must be clamped to at least a $0.02 buffer past the reference price."""
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.1577)

    req = execution.bracket_buy_with_SLTP("MGN", budget=5000.0, slP=0.98, tpP=1.05)

    assert req.take_profit.limit_price >= 0.1577 + 0.01
    assert req.stop_loss.stop_price <= 0.1577 - 0.01


@pytest.mark.parametrize(
    "symbol, rejection, observed_ask",
    [
        # MGN: small drift, absorbed by the $0.02 buffer alone (kept for historical coverage).
        ("MGN", {"base_price": "0.1616", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}, 0.1616 + 0.02),
        ("MGN", {"base_price": "0.1577", "code": 42210000, "message": "take_profit.limit_price must be >= base_price + 0.01"}, 0.1577 + 0.02),
        ("MGN", {"base_price": "0.1486", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}, 0.1486 + 0.02),
        # MGN: real production drift (ask 0.16 vs base_price 0.1473) that outran the $0.02 buffer.
        ("MGN", {"base_price": "0.1473", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}, 0.16),
        # DFNS: real production drift of ~$1.25 (ask 43.94 vs base_price 42.69) -- proves the fix
        # isn't bounded to penny stocks or to any fixed-cent buffer size.
        ("DFNS", {"base_price": "42.69", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}, 43.94),
        # STKH: real production drift of ~$0.47 (ask 3.35 vs base_price 2.88).
        ("STKH", {"base_price": "2.88", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}, 3.35),
    ],
    ids=lambda v: f"{v}" if not isinstance(v, dict) else f"base_price={v['base_price']}",
)
def test_buy_retries_with_alpacas_base_price_on_bracket_rejection(monkeypatch, symbol, rejection, observed_ask):
    """Regression for repeated bracket-BUY rejections even after the tick-size and
    percentage-floor fixes: our client-quoted ask can diverge from Alpaca's own base_price on
    thin symbols (e.g. a lagging free-tier quote feed) by anywhere from a few cents (MGN) to
    over a dollar (DFNS) -- no fixed buffer size can bound this. buy() must retry once, pricing
    TP/SL off the base_price Alpaca actually reports in the rejection, regardless of the gap."""
    fake_client = FakeTradingClient(rejections=[rejection])
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: observed_ask)

    result = execution.buy(symbol, "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert len(fake_client.submitted) == 2, "expected one rejected attempt + one retry"

    retry_req = fake_client.submitted[1]
    base_price = float(rejection["base_price"])

    # Same inequalities Alpaca itself enforces server-side.
    assert retry_req.take_profit.limit_price >= base_price + 0.01
    assert retry_req.stop_loss.stop_price <= base_price - 0.01


def test_crypto_buy_rounds_notional_to_2_decimal_places(monkeypatch):
    """Regression for a live BUY BTCUSD rejection: {"code":42210000,"message":"notional value
    must be limited to 2 decimal places"} -- a `budget` with more precision than that (e.g. a
    merged position's market_value) must be rounded before being sent as notional."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 123.456789, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].notional == 123.46


def test_crypto_buy_skips_notional_below_alpacas_minimum(monkeypatch):
    """A budget below Alpaca's crypto minimum notional (code 40310000, "cost basis must be >=
    minimal amount of order 10") must be skipped, not clamped up -- clamping would silently
    submit an order larger than the caller's intended budget."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 3.5, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "budget_below_minimum"
    assert fake_client.submitted == []


def test_crypto_buy_executes_at_exactly_the_minimum_notional(monkeypatch):
    """The minimum itself must still be accepted -- only strictly-below values are skipped."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", execution.MIN_CRYPTO_NOTIONAL, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].notional == execution.MIN_CRYPTO_NOTIONAL


def test_buy_reraises_on_unrelated_api_error(monkeypatch):
    """buy()'s retry is specific to the base_price mismatch (code 42210000 with a base_price
    field) -- any other rejection must propagate, not be silently retried/swallowed."""
    other_rejection = {"code": 40310000, "message": "unrelated conflict"}
    fake_client = FakeTradingClient(rejections=[other_rejection])
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 1.0)

    with pytest.raises(APIError):
        execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert len(fake_client.submitted) == 1, "must not retry on an unrelated rejection"


def test_stock_buy_returns_reason_sl_tp_and_tracks_the_bracket_and_pending_fill(monkeypatch):
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert result["reason"] == "opening_position"
    assert result["order_id"] == "order-123"
    assert "fill_price" not in result
    assert result["sl_price"] == pytest.approx(9.8, abs=0.01)
    assert result["tp_price"] == pytest.approx(10.5, abs=0.01)
    assert execution._tracked_brackets["MGN"] == "order-123"
    assert execution._pending_fills["order-123"] == {
        "symbol": "MGN",
        "action": "BUY",
        "reason": "opening_position",
        "sl_price": pytest.approx(9.8, abs=0.01),
        "tp_price": pytest.approx(10.5, abs=0.01),
    }


def test_crypto_buy_has_no_sl_tp_price_and_is_not_tracked_as_a_bracket(monkeypatch):
    """Crypto BUYs are plain notional market orders, not brackets -- there's no TP/SL leg to
    later watch for a fill."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["sl_price"] is None
    assert result["tp_price"] is None
    assert "BTC/USD" not in execution._tracked_brackets


def test_sell_returns_dealer_signal_reason_and_untracks_bracket_and_tracks_pending_fill(monkeypatch):
    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "5"})()

        def submit_order(self, req):
            return type("Order", (), {"id": "sell-order-1"})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())
    execution._tracked_brackets["MGN"] = "parent-order-should-be-cleared"

    result = execution.sell("MGN")

    assert result["status"] == "submitted"
    assert result["reason"] == "dealer_signal"
    assert result["order_id"] == "sell-order-1"
    assert "fill_price" not in result
    assert "MGN" not in execution._tracked_brackets
    assert execution._pending_fills["sell-order-1"] == {
        "symbol": "MGN",
        "action": "SELL",
        "reason": "dealer_signal",
        "sl_price": None,
        "tp_price": None,
    }


def test_check_bracket_fills_reports_take_profit_leg_filled(monkeypatch):
    execution._tracked_brackets["MGN"] = "parent-1"
    tp_leg = FakeLeg("tp-leg-1", OrderStatus.FILLED, OrderType.LIMIT, filled_avg_price="13.50", filled_qty="10")
    sl_leg = FakeLeg("sl-leg-1", OrderStatus.CANCELED, OrderType.STOP)
    fake_client = FakeBracketTradingClient({"parent-1": FakeBracketOrder([tp_leg, sl_leg])})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == [
        {"kind": "fill", "symbol": "MGN", "order_id": "tp-leg-1", "reason": "take_profit", "fill_price": 13.50, "qty": 10.0}
    ]
    assert "MGN" not in execution._tracked_brackets


def test_check_bracket_fills_reports_stop_loss_leg_filled(monkeypatch):
    execution._tracked_brackets["MGN"] = "parent-1"
    tp_leg = FakeLeg("tp-leg-1", OrderStatus.CANCELED, OrderType.LIMIT)
    sl_leg = FakeLeg("sl-leg-1", OrderStatus.FILLED, OrderType.STOP, filled_avg_price="9.80", filled_qty="10")
    fake_client = FakeBracketTradingClient({"parent-1": FakeBracketOrder([tp_leg, sl_leg])})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == [
        {"kind": "fill", "symbol": "MGN", "order_id": "sl-leg-1", "reason": "stop_loss", "fill_price": 9.80, "qty": 10.0}
    ]
    assert "MGN" not in execution._tracked_brackets


def test_check_bracket_fills_keeps_tracking_while_both_legs_still_open(monkeypatch):
    execution._tracked_brackets["MGN"] = "parent-1"
    legs = [FakeLeg("tp-leg-1", OrderStatus.NEW, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.NEW, OrderType.STOP)]
    fake_client = FakeBracketTradingClient({"parent-1": FakeBracketOrder(legs)})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == []
    assert execution._tracked_brackets["MGN"] == "parent-1"


def test_check_bracket_fills_untracks_symbol_when_both_legs_end_without_a_fill(monkeypatch):
    """e.g. the position was closed some other way and Alpaca cancelled both legs -- must stop
    polling rather than track forever, and must report the no-fill outcome rather than going
    silent (nothing else ever covers this case)."""
    execution._tracked_brackets["MGN"] = "parent-1"
    legs = [FakeLeg("tp-leg-1", OrderStatus.CANCELED, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.CANCELED, OrderType.STOP)]
    fake_client = FakeBracketTradingClient({"parent-1": FakeBracketOrder(legs)})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == [
        {"kind": "terminal", "symbol": "MGN", "order_id": "parent-1", "leg_statuses": ["canceled", "canceled"]}
    ]
    assert "MGN" not in execution._tracked_brackets


def test_buy_skips_with_kill_switch_reason_when_active_and_touches_no_client(monkeypatch):
    """ROADMAP P0.5: an active BUY kill switch must block the BUY before any position/order
    lookup or submission -- no `trading_client` monkeypatch is set up here on purpose, so this
    test would error out trying to reach the real Alpaca API if the early-exit didn't fire
    first."""
    monkeypatch.setattr(execution.kill_switch, "buy_kill_switch_active", lambda: True)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result == {
        "status": "skipped",
        "reason": "buy_kill_switch_active",
        "detail": "BUY kill switch is active",
    }


def test_sell_still_permitted_when_kill_switch_active(monkeypatch):
    """ROADMAP P0.5: the switch only ever blocks new BUY exposure -- SELL must be completely
    unaffected by its state."""
    monkeypatch.setattr(execution.kill_switch, "buy_kill_switch_active", lambda: True)

    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "5"})()

        def submit_order(self, req):
            return type("Order", (), {"id": "sell-order-1"})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())

    result = execution.sell("MGN")

    assert result["status"] == "submitted"


def test_check_bracket_fills_untracks_symbol_on_confirmed_not_found(monkeypatch):
    """A cancelled/expired parent order can eventually 404 -- treat a *confirmed* not-found as
    nothing left to watch rather than retrying forever."""
    execution._tracked_brackets["MGN"] = "parent-1"
    fake_client = FakeBracketTradingClient({"parent-1": _api_error({"code": execution.ORDER_NOT_FOUND_CODE, "message": "order not found"})})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == []
    assert "MGN" not in execution._tracked_brackets


def test_check_bracket_fills_keeps_tracking_symbol_on_transient_api_error(monkeypatch):
    """A rate limit / 5xx / network blip must not drop tracking -- that would silently stop
    watching a still-live bracket. The symbol stays tracked and the failure is recorded so it
    can be observed, distinct from a confirmed not-found."""
    execution._tracked_brackets["MGN"] = "parent-1"
    fake_client = FakeBracketTradingClient({"parent-1": _api_error({"code": 50000000, "message": "internal server error"})})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == []
    assert execution._tracked_brackets["MGN"] == {"order_id": "parent-1", "poll_failures": 1}

    events = execution.check_bracket_fills()

    assert events == []
    assert execution._tracked_brackets["MGN"] == {"order_id": "parent-1", "poll_failures": 2}


class FakePendingOrder:
    def __init__(self, filled_avg_price=None, status=None):
        self.filled_avg_price = filled_avg_price
        self.status = status


class FakePendingFillTradingClient:
    """Stands in for alpaca-py's TradingClient for check_pending_fills() -- `orders_by_id` maps
    an order id to either a FakePendingOrder or an exception instance to raise."""

    def __init__(self, orders_by_id):
        self._orders_by_id = orders_by_id

    def get_order_by_id(self, order_id, filter=None):
        result = self._orders_by_id[order_id]
        if isinstance(result, Exception):
            raise result
        return result


def test_check_pending_fills_reports_a_filled_order(monkeypatch):
    execution._pending_fills["order-1"] = {"symbol": "MGN", "action": "BUY", "reason": "opening_position", "sl_price": 9.8, "tp_price": 10.5}
    fake_client = FakePendingFillTradingClient({"order-1": FakePendingOrder(filled_avg_price="10.05")})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_pending_fills()

    assert events == [
        {
            "symbol": "MGN",
            "action": "BUY",
            "reason": "opening_position",
            "sl_price": 9.8,
            "tp_price": 10.5,
            "kind": "fill",
            "order_id": "order-1",
            "fill_price": 10.05,
        }
    ]
    assert "order-1" not in execution._pending_fills


def test_check_pending_fills_keeps_tracking_an_unfilled_order(monkeypatch):
    execution._pending_fills["order-1"] = {"symbol": "MGN", "action": "BUY", "reason": "opening_position", "sl_price": None, "tp_price": None}
    fake_client = FakePendingFillTradingClient({"order-1": FakePendingOrder(filled_avg_price=None, status=OrderStatus.NEW)})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_pending_fills()

    assert events == []
    assert "order-1" in execution._pending_fills


def test_check_pending_fills_untracks_order_on_terminal_no_fill_status(monkeypatch):
    """Rejected/canceled/expired must be reported, not go silent -- no /execute response ever
    covers this outcome since it's only known after the fact."""
    execution._pending_fills["order-1"] = {"symbol": "MGN", "action": "BUY", "reason": "opening_position", "sl_price": None, "tp_price": None}
    fake_client = FakePendingFillTradingClient({"order-1": FakePendingOrder(filled_avg_price=None, status=OrderStatus.CANCELED)})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_pending_fills()

    assert events == [
        {
            "symbol": "MGN",
            "action": "BUY",
            "reason": "opening_position",
            "sl_price": None,
            "tp_price": None,
            "kind": "terminal",
            "order_id": "order-1",
            "order_status": "canceled",
        }
    ]
    assert "order-1" not in execution._pending_fills


def test_check_pending_fills_untracks_order_on_confirmed_not_found(monkeypatch):
    execution._pending_fills["order-1"] = {"symbol": "MGN", "action": "BUY", "reason": "opening_position", "sl_price": None, "tp_price": None}
    fake_client = FakePendingFillTradingClient({"order-1": _api_error({"code": execution.ORDER_NOT_FOUND_CODE, "message": "order not found"})})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_pending_fills()

    assert events == []
    assert "order-1" not in execution._pending_fills


def test_check_pending_fills_keeps_tracking_order_on_transient_api_error(monkeypatch):
    """A rate limit / 5xx / network blip must not drop tracking of a still-live order -- the
    entry stays and the failure count is recorded on it."""
    execution._pending_fills["order-1"] = {"symbol": "MGN", "action": "BUY", "reason": "opening_position", "sl_price": None, "tp_price": None}
    fake_client = FakePendingFillTradingClient({"order-1": _api_error({"code": 50000000, "message": "internal server error"})})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_pending_fills()

    assert events == []
    assert execution._pending_fills["order-1"]["poll_failures"] == 1

    events = execution.check_pending_fills()

    assert events == []
    assert execution._pending_fills["order-1"]["poll_failures"] == 2


def test_check_pending_fills_clears_poll_failures_once_order_is_reachable_again(monkeypatch):
    """A transient failure must not leave a stale poll_failures count behind once the order is
    successfully observed again."""
    execution._pending_fills["order-1"] = {
        "symbol": "MGN",
        "action": "BUY",
        "reason": "opening_position",
        "sl_price": None,
        "tp_price": None,
        "poll_failures": 3,
    }
    fake_client = FakePendingFillTradingClient({"order-1": FakePendingOrder(filled_avg_price=None, status=OrderStatus.NEW)})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    execution.check_pending_fills()

    assert "poll_failures" not in execution._pending_fills["order-1"]


class FakeOrder:
    def __init__(self, id, symbol, side, status, filled_avg_price=None, legs=None):
        self.id = id
        self.symbol = symbol
        self.side = side
        self.status = status
        self.filled_avg_price = filled_avg_price
        self.legs = legs


class FakeReconstructTradingClient:
    def __init__(self, open_orders):
        self._open_orders = open_orders

    def get_orders(self, request):
        return self._open_orders


def test_reconstruct_tracked_state_restores_a_still_open_pending_order(monkeypatch):
    order = FakeOrder("order-1", "BTC/USD", OrderSide.BUY, OrderStatus.NEW, filled_avg_price=None, legs=None)
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([order]))

    execution.reconstruct_tracked_state()

    assert execution._pending_fills["order-1"] == {
        "symbol": "BTC/USD",
        "action": "BUY",
        "reason": "reconstructed_after_restart",
        "sl_price": None,
        "tp_price": None,
    }


def test_reconstruct_tracked_state_restores_a_bracket_with_a_still_open_leg(monkeypatch):
    legs = [FakeLeg("tp-leg-1", OrderStatus.NEW, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.NEW, OrderType.STOP)]
    order = FakeOrder("parent-1", "MGN", OrderSide.BUY, OrderStatus.FILLED, filled_avg_price="10.05", legs=legs)
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([order]))

    execution.reconstruct_tracked_state()

    assert execution._tracked_brackets["MGN"] == "parent-1"
    assert "parent-1" not in execution._pending_fills


def test_reconstruct_tracked_state_skips_a_bracket_whose_legs_are_all_terminal(monkeypatch):
    """Shouldn't happen given the status="open" family-level query, but must not crash or
    mistrack if it ever does."""
    legs = [FakeLeg("tp-leg-1", OrderStatus.CANCELED, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.CANCELED, OrderType.STOP)]
    order = FakeOrder("parent-1", "MGN", OrderSide.BUY, OrderStatus.FILLED, filled_avg_price="10.05", legs=legs)
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([order]))

    execution.reconstruct_tracked_state()

    assert "MGN" not in execution._tracked_brackets


def test_reconstruct_tracked_state_handles_api_error_without_raising(monkeypatch):
    class FailingClient:
        def get_orders(self, request):
            raise APIError("unreachable")

    monkeypatch.setattr(execution, "trading_client", FailingClient())

    execution.reconstruct_tracked_state()

    assert execution._pending_fills == {}
    assert execution._tracked_brackets == {}
