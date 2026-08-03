import json

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderStatus, OrderType

from src.floor_broker import execution


def _api_error(payload: dict) -> APIError:
    err = APIError.__new__(APIError)
    err.args = (json.dumps(payload),)
    return err


@pytest.fixture(autouse=True)
def _clear_tracked_brackets():
    """_tracked_brackets is module-level state shared across every test in this file -- clear it
    before and after each test so tests can't leak bracket-tracking into one another."""
    execution._tracked_brackets.clear()
    yield
    execution._tracked_brackets.clear()


class FakeTradingClient:
    """Stands in for alpaca-py's TradingClient. `submit_order` raises the given rejection(s)
    in order, then succeeds -- lets tests replay real Alpaca rejection payloads without any
    network access."""

    def __init__(self, rejections=(), fill_price=101.0):
        self._rejections = list(rejections)
        self.submitted = []
        self._fill_price = fill_price

    def get_open_position(self, symbol):
        raise APIError("no position")

    def get_orders(self, request):
        return []

    def submit_order(self, req):
        self.submitted.append(req)
        if self._rejections:
            raise _api_error(self._rejections.pop(0))
        return type("Order", (), {"id": "order-123"})()

    def get_order_by_id(self, order_id, filter=None):
        # Immediate fill by default, so _wait_for_fill returns on its first check and never
        # sleeps for real -- keeps the existing tests (which don't care about fill price) fast.
        return type("Order", (), {"filled_avg_price": self._fill_price})()


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

    assert result["status"] == "executed"
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

    assert result["status"] == "executed"
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

    assert result["status"] == "executed"
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


def test_stock_buy_returns_reason_fill_price_sl_tp_and_tracks_the_bracket(monkeypatch):
    fake_client = FakeTradingClient(fill_price=10.05)
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "executed"
    assert result["reason"] == "opening_position"
    assert result["order_id"] == "order-123"
    assert result["fill_price"] == pytest.approx(10.05)
    assert result["sl_price"] == pytest.approx(9.8, abs=0.01)
    assert result["tp_price"] == pytest.approx(10.5, abs=0.01)
    assert execution._tracked_brackets["MGN"] == "order-123"


def test_crypto_buy_has_no_sl_tp_price_and_is_not_tracked_as_a_bracket(monkeypatch):
    """Crypto BUYs are plain notional market orders, not brackets -- there's no TP/SL leg to
    later watch for a fill."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["sl_price"] is None
    assert result["tp_price"] is None
    assert "BTC/USD" not in execution._tracked_brackets


def test_wait_for_fill_gives_up_after_max_attempts_without_a_real_sleep(monkeypatch):
    class NeverFillsClient:
        def get_order_by_id(self, order_id, filter=None):
            return type("Order", (), {"filled_avg_price": None})()

    monkeypatch.setattr(execution, "trading_client", NeverFillsClient())
    sleeps = []
    monkeypatch.setattr(execution.time, "sleep", lambda s: sleeps.append(s))

    result = execution._wait_for_fill("order-999")

    assert result is None
    assert len(sleeps) == execution.FILL_POLL_ATTEMPTS


def test_sell_returns_dealer_signal_reason_fill_price_and_untracks_bracket(monkeypatch):
    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "5"})()

        def submit_order(self, req):
            return type("Order", (), {"id": "sell-order-1"})()

        def get_order_by_id(self, order_id, filter=None):
            return type("Order", (), {"filled_avg_price": "12.00"})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())
    execution._tracked_brackets["MGN"] = "parent-order-should-be-cleared"

    result = execution.sell("MGN")

    assert result["status"] == "executed"
    assert result["reason"] == "dealer_signal"
    assert result["order_id"] == "sell-order-1"
    assert result["fill_price"] == pytest.approx(12.0)
    assert "MGN" not in execution._tracked_brackets


def test_check_bracket_fills_reports_take_profit_leg_filled(monkeypatch):
    execution._tracked_brackets["MGN"] = "parent-1"
    tp_leg = FakeLeg("tp-leg-1", OrderStatus.FILLED, OrderType.LIMIT, filled_avg_price="13.50", filled_qty="10")
    sl_leg = FakeLeg("sl-leg-1", OrderStatus.CANCELED, OrderType.STOP)
    fake_client = FakeBracketTradingClient({"parent-1": FakeBracketOrder([tp_leg, sl_leg])})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == [
        {"symbol": "MGN", "order_id": "tp-leg-1", "reason": "take_profit", "fill_price": 13.50, "qty": 10.0}
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
        {"symbol": "MGN", "order_id": "sl-leg-1", "reason": "stop_loss", "fill_price": 9.80, "qty": 10.0}
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
    polling rather than track forever."""
    execution._tracked_brackets["MGN"] = "parent-1"
    legs = [FakeLeg("tp-leg-1", OrderStatus.CANCELED, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.CANCELED, OrderType.STOP)]
    fake_client = FakeBracketTradingClient({"parent-1": FakeBracketOrder(legs)})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == []
    assert "MGN" not in execution._tracked_brackets


def test_check_bracket_fills_untracks_symbol_on_api_error(monkeypatch):
    """A cancelled/expired parent order can eventually 404 -- treat that as nothing left to
    watch rather than retrying forever."""
    execution._tracked_brackets["MGN"] = "parent-1"
    fake_client = FakeBracketTradingClient({"parent-1": APIError("not found")})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_bracket_fills()

    assert events == []
    assert "MGN" not in execution._tracked_brackets
