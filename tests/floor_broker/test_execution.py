import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import AssetClass, OrderSide, OrderStatus, OrderType
from omegaconf import OmegaConf

from src.common import db
from src.floor_broker import execution

# buy() now calls load_config() itself (fetched live from GitHub in production, see
# src/common/config.py) rather than reading a module-level cfg captured at import time --
# without this fixture every test in this file would attempt a real network fetch, and would be
# coupled to whatever config.yaml on `main` happens to contain (e.g. Task D's
# daily_loss_limit_usd change) instead of the fixed values these tests assert against.
_FAKE_CFG = OmegaConf.create(
    {
        "strategy": {
            "daily_profit_target_usd": 1000,
            "daily_loss_limit_usd": 500,
            "crypto_slP": 0.98,
            "crypto_tpP": 1.03,
            "max_concurrent_positions": 10,
            "position_sizing": "flat_budget",
            "risk_per_trade_usd": None,
        },
        "eod_flatten": {
            "enabled": False,
            "minutes_before_close": 10,
        },
    }
)


@pytest.fixture(autouse=True)
def _fake_cfg(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _FAKE_CFG)


def _api_error(payload: dict) -> APIError:
    err = APIError.__new__(APIError)
    err.args = (json.dumps(payload),)
    err._error = json.dumps(payload)
    return err


@pytest.fixture(autouse=True)
def _clear_tracked_brackets():
    """_tracked_brackets, _pending_fills, and _crypto_stops are module-level state shared across
    every test in this file -- clear them before and after each test so tests can't leak tracking
    into one another."""
    execution._tracked_brackets.clear()
    execution._pending_fills.clear()
    execution._crypto_stops.clear()
    yield
    execution._tracked_brackets.clear()
    execution._pending_fills.clear()
    execution._crypto_stops.clear()


@pytest.fixture(autouse=True)
def _kill_switch_inactive(monkeypatch):
    """ROADMAP P0.5: every test in this file exercises buy() without a live k8s API to read the
    real ConfigMap against -- default the switch to inactive so existing BUY-path tests are
    unaffected; tests that care about the switch itself override this explicitly."""
    monkeypatch.setattr(execution.kill_switch, "buy_kill_switch_active", lambda: False)


@pytest.fixture(autouse=True)
def _state_reconciled_by_default(monkeypatch):
    """buy() refuses to submit while tracked state hasn't been reconciled with Alpaca (see
    reconcile_tracked_state_once()) -- default every test in this file to reconciled=True so
    existing BUY-path tests are unaffected; tests that care about this gate override it
    explicitly."""
    monkeypatch.setattr(execution, "_state_reconciled", True)


class FakeAccount:
    """Stands in for alpaca-py's TradeAccount. Defaults to flat daily P&L (equity ==
    last_equity) so the daily profit/loss halt in buy() is a no-op unless a test overrides it."""

    def __init__(self, equity="100000.0", last_equity="100000.0"):
        self.equity = equity
        self.last_equity = last_equity


class FakeTradingClient:
    """Stands in for alpaca-py's TradingClient. `submit_order` raises the given rejection(s)
    in order, then succeeds -- lets tests replay real Alpaca rejection payloads without any
    network access."""

    def __init__(self, rejections=(), account=None, open_positions_count=0):
        self._rejections = list(rejections)
        self.submitted = []
        self._account = account or FakeAccount()
        self._open_positions_count = open_positions_count

    def get_account(self):
        return self._account

    def get_open_position(self, symbol):
        raise APIError("no position")

    def get_orders(self, request):
        return []

    def get_all_positions(self):
        return [object()] * self._open_positions_count

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
    """A zero-price quote means Alpaca has no executable ask; it must not be reported as an
    invalid order-parameter bug."""
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.0)

    with pytest.raises(execution.NoAskQuote):
        execution.bracket_buy_with_SLTP("MGN", budget=5000.0, slP=0.98, tpP=1.05)


def test_buy_skips_with_no_ask_quote_reason_when_quote_is_zero(monkeypatch):
    """Regression for live TDCL Slack BUY noise: Alpaca returned ask_price=0.0, which used to be
    surfaced as invalid_order_parameters. It should be a clean no-quote skip instead."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.0)

    result = execution.buy("TDCL", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_ask_quote"
    assert fake_client.submitted == []


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


def test_buy_skips_with_invalid_order_parameters_reason_when_stop_loss_goes_non_positive(monkeypatch):
    """Regression for a live ANSCW 500: bracket_buy_with_SLTP's $0.02 floor/ceiling clamp can
    push stop_loss_px negative on an extremely low-priced symbol (ask=$0.0097) -- this used to
    propagate InvalidOrderParameters straight out of buy(), which app.py's generic exception
    handler turned into a bare 500 instead of a clean skip. Must return status="skipped" and
    never reach submit_order."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.0097)

    result = execution.buy("ANSCW", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "invalid_order_parameters"
    assert fake_client.submitted == []


def test_buy_skips_with_invalid_order_parameters_reason_on_retry(monkeypatch):
    """Same invalid-order-parameters skip, but reached via the base_price retry path -- Alpaca's
    own reported base_price can itself be low enough to hit the same non-positive stop_loss
    clamp on the retry attempt, even though the initial (higher, client-quoted) ask was fine."""
    rejection = {"base_price": "0.0097", "code": 42210000, "message": "stop_loss.stop_price must be <= base_price - 0.01"}
    fake_client = FakeTradingClient(rejections=[rejection])
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 0.05)

    result = execution.buy("ANSCW", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "invalid_order_parameters"
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


def test_crypto_buy_canonicalizes_live_alpaca_position_symbol_shape(monkeypatch):
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTCUSD", "binance", 123.45, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].symbol == "BTC/USD"
    assert execution._pending_fills["order-123"]["symbol"] == "BTC/USD"


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


def test_crypto_buy_skips_non_usd_quoted_pair(monkeypatch):
    """Regression for live SHIB/USDT BUY errors: Alpaca paper accounts are funded in USD, so
    submitting a USDT-quoted pair makes Alpaca reject the order for insufficient USDT balance.
    Floor Broker must skip that stale portfolio entry before it reaches Alpaca."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("SHIB/USDT", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "non_usd_crypto_pair"
    assert fake_client.submitted == []


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


class FakePosition:
    def __init__(self, market_value, qty="1"):
        self.qty = qty
        self.market_value = market_value


class FakeExistingPositionTradingClient(FakeTradingClient):
    """Like FakeTradingClient, but get_open_position() returns an existing position instead of
    raising -- used to exercise buy()'s top-up-toward-budget branch."""

    def __init__(self, market_value, rejections=(), account=None):
        super().__init__(rejections=rejections, account=account)
        self._market_value = market_value

    def get_open_position(self, symbol):
        return FakePosition(self._market_value)


def test_buy_skips_with_open_orders_exist_reason_when_a_matching_order_is_open(monkeypatch):
    """An in-flight order for the symbol (BUY not yet filled, or a pending SELL) must still be a
    hard, unconditional skip -- layering a new BUY on top of it is racy regardless of how much
    budget headroom the caller thinks is available."""

    class FakeOpenOrderTradingClient(FakeTradingClient):
        def get_orders(self, request):
            return [type("Order", (), {"symbol": "MGN"})()]

    fake_client = FakeOpenOrderTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "open_orders_exist"
    assert fake_client.submitted == []


def test_buy_skips_with_market_value_unavailable_reason_when_position_market_value_is_none(monkeypatch):
    """market_value is Optional[str] on Alpaca's own Position model. This is a trading-money
    gate, so it must fail closed (skip) rather than guess at remaining budget headroom -- the
    opposite of the Analyst's informational P&L snapshot, which fails open on the same field."""
    fake_client = FakeExistingPositionTradingClient(market_value=None)
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "market_value_unavailable"
    assert fake_client.submitted == []


def test_buy_skips_with_budget_exhausted_reason_when_existing_position_already_meets_budget(monkeypatch):
    fake_client = FakeExistingPositionTradingClient(market_value="5000.00")
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "budget_exhausted"
    assert fake_client.submitted == []


def test_buy_tops_up_existing_stock_position_with_only_the_remaining_budget(monkeypatch):
    """Core fix: budget=$5000, existing position worth $4000 -- buy() must submit a bracket
    order sized off the $1000 remainder, not the full $5000 budget and not a skip."""
    fake_client = FakeExistingPositionTradingClient(market_value="4000.00")
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].qty == 100  # int(1000.0 // 10.0), not int(5000.0 // 10.0)


def test_buy_tops_up_existing_crypto_position_with_only_the_remaining_budget(monkeypatch):
    fake_client = FakeExistingPositionTradingClient(market_value="40.00")
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].notional == 60.0  # 100.0 - 40.0, not the full 100.0


def test_buy_skips_with_insufficient_qty_reason_when_remaining_budget_affords_less_than_one_share(monkeypatch):
    """The reduced "remaining budget" must flow into the existing InsufficientQuantity path
    unchanged -- no new sizing logic needed for this edge case."""
    fake_client = FakeExistingPositionTradingClient(market_value="4990.00")
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10000.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_qty"
    assert fake_client.submitted == []


def test_buy_skips_with_budget_below_minimum_reason_when_remaining_budget_below_crypto_minimum(monkeypatch):
    """Same idea for crypto: the remainder must flow into the existing MIN_CRYPTO_NOTIONAL check
    unchanged."""
    fake_client = FakeExistingPositionTradingClient(market_value="95.00")
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "budget_below_minimum"
    assert fake_client.submitted == []


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
        "crypto_slP": None,
        "crypto_tpP": None,
    }


def test_stock_buy_coerces_a_uuid_order_id_to_str(monkeypatch):
    """Alpaca's real SDK returns order.id as a uuid.UUID, not a str -- the returned dict's
    order_id must be a str so it validates against ExecuteResponse.order_id: str | None in
    app.py. A UUID leaking through here previously crashed /execute with a 500 on every
    successful BUY, even though the order had already been submitted and would go on to fill."""
    order_uuid = uuid.uuid4()

    class FakeUUIDOrderClient(FakeTradingClient):
        def submit_order(self, req):
            self.submitted.append(req)
            return type("Order", (), {"id": order_uuid})()

    fake_client = FakeUUIDOrderClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["order_id"] == str(order_uuid)
    assert isinstance(result["order_id"], str)


def test_crypto_buy_has_no_sl_tp_price_and_is_not_tracked_as_a_bracket(monkeypatch):
    """Crypto BUYs are plain notional market orders, not brackets -- there's no TP/SL leg to
    later watch for a fill."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["sl_price"] is None
    assert result["tp_price"] is None
    assert "BTC/USD" not in execution._tracked_brackets


def test_crypto_buy_stores_strategy_crypto_slp_tpp_for_the_pending_fill(monkeypatch):
    """Crypto has no fill price known at submission time (a plain notional market order), so the
    strategy.crypto_slP/crypto_tpP multipliers -- not yet a concrete sl_price/tp_price -- must be
    stashed on the pending-fill entry for check_pending_fills() to apply once the fill lands."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    pending = execution._pending_fills["order-123"]
    assert pending["crypto_slP"] == _FAKE_CFG.strategy.crypto_slP
    assert pending["crypto_tpP"] == _FAKE_CFG.strategy.crypto_tpP


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


def test_sell_coerces_a_uuid_order_id_to_str(monkeypatch):
    """Same UUID-leak regression as the BUY path (see test_stock_buy_coerces_a_uuid_order_id_to_str)
    -- Alpaca's real order.id is a uuid.UUID, and the returned order_id must be a str."""
    order_uuid = uuid.uuid4()

    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "5"})()

        def submit_order(self, req):
            return type("Order", (), {"id": order_uuid})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())

    result = execution.sell("MGN")

    assert result["order_id"] == str(order_uuid)
    assert isinstance(result["order_id"], str)


def test_sell_accepts_a_reason_override_and_clears_tracked_crypto_stop(monkeypatch):
    """check_crypto_stops() calls sell(symbol, reason=...) so the eventual fill notice says
    stop_loss/take_profit rather than the default dealer_signal -- and the triggering sell must
    itself clear the symbol's _crypto_stops entry so the poller can't double-sell it."""
    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "1"})()

        def submit_order(self, req):
            return type("Order", (), {"id": "sell-order-1"})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())
    execution._crypto_stops["BTC/USD"] = (98.0, 103.0)

    result = execution.sell("BTC/USD", reason="stop_loss")

    assert result["status"] == "submitted"
    assert result["reason"] == "stop_loss"
    assert execution._pending_fills["sell-order-1"]["reason"] == "stop_loss"
    assert "BTC/USD" not in execution._crypto_stops


def test_check_crypto_stops_sells_when_bid_drops_to_or_below_stop_loss(monkeypatch):
    execution._crypto_stops["BTC/USD"] = (98.0, 103.0)
    monkeypatch.setattr(execution, "get_current_bid_price", lambda symbol: 98.0)

    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "1"})()

        def submit_order(self, req):
            return type("Order", (), {"id": "sell-order-1"})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())

    events = execution.check_crypto_stops()

    assert len(events) == 1
    assert events[0]["symbol"] == "BTC/USD"
    assert events[0]["reason"] == "stop_loss"
    assert events[0]["bid_price"] == 98.0
    assert events[0]["sell_result"]["status"] == "submitted"
    assert "BTC/USD" not in execution._crypto_stops


def test_check_crypto_stops_sells_when_bid_rises_to_or_above_take_profit(monkeypatch):
    execution._crypto_stops["BTC/USD"] = (98.0, 103.0)
    monkeypatch.setattr(execution, "get_current_bid_price", lambda symbol: 103.0)

    class FakeSellClient:
        def get_open_position(self, symbol):
            return type("Position", (), {"qty": "1"})()

        def submit_order(self, req):
            return type("Order", (), {"id": "sell-order-1"})()

    monkeypatch.setattr(execution, "trading_client", FakeSellClient())

    events = execution.check_crypto_stops()

    assert events[0]["reason"] == "take_profit"
    assert "BTC/USD" not in execution._crypto_stops


def test_check_crypto_stops_keeps_tracking_when_bid_is_within_bounds(monkeypatch):
    execution._crypto_stops["BTC/USD"] = (98.0, 103.0)
    monkeypatch.setattr(execution, "get_current_bid_price", lambda symbol: 100.0)

    events = execution.check_crypto_stops()

    assert events == []
    assert execution._crypto_stops["BTC/USD"] == (98.0, 103.0)


def test_check_crypto_stops_keeps_tracking_symbol_on_transient_price_fetch_error(monkeypatch):
    execution._crypto_stops["BTC/USD"] = (98.0, 103.0)

    def _raise(symbol):
        raise APIError("unreachable")

    monkeypatch.setattr(execution, "get_current_bid_price", _raise)

    events = execution.check_crypto_stops()

    assert events == []
    assert execution._crypto_stops["BTC/USD"] == (98.0, 103.0)


def test_check_crypto_stops_skips_sell_if_stop_changed_after_snapshot(monkeypatch):
    execution._crypto_stops["BTC/USD"] = (98.0, 103.0)

    def _bid_after_concurrent_update(symbol):
        execution._crypto_stops["BTC/USD"] = (90.0, 110.0)
        return 98.0

    sells = []
    monkeypatch.setattr(execution, "get_current_bid_price", _bid_after_concurrent_update)
    monkeypatch.setattr(execution, "sell", lambda symbol, reason: sells.append((symbol, reason)))

    events = execution.check_crypto_stops()

    assert events == []
    assert sells == []
    assert execution._crypto_stops["BTC/USD"] == (90.0, 110.0)


class FakeClock:
    def __init__(self, is_open, minutes_to_close=5):
        self.is_open = is_open
        self.timestamp = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
        self.next_close = self.timestamp + timedelta(minutes=minutes_to_close)


class FakeEodPosition:
    def __init__(
        self,
        symbol,
        asset_class,
        qty="1",
        unrealized_pl=None,
        avg_entry_price=None,
        current_price=None,
    ):
        self.symbol = symbol
        self.asset_class = asset_class
        self.qty = qty
        self.unrealized_pl = unrealized_pl
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price


class FakeEodFlattenTradingClient:
    """Stands in for alpaca-py's TradingClient for check_eod_flatten() -- also backs the real
    sell() it calls per-symbol, so get_open_position() must resolve every symbol get_all_positions()
    returns."""

    def __init__(self, clock, positions):
        self._clock = clock
        self._positions = positions
        self.submitted = []

    def get_clock(self):
        return self._clock

    def get_all_positions(self):
        return self._positions

    def get_open_position(self, symbol):
        for position in self._positions:
            if position.symbol.replace("/", "") == symbol:
                return position
        raise APIError("no position")

    def submit_order(self, req):
        self.submitted.append(req)
        return type("Order", (), {"id": f"order-{req.symbol}"})()


def _eod_flatten_cfg(enabled=True, minutes_before_close=10, conditional=False, max_days_held_loss=5):
    return OmegaConf.create(
        {
            "strategy": _FAKE_CFG.strategy,
            "eod_flatten": {
                "enabled": enabled,
                "minutes_before_close": minutes_before_close,
                "conditional": conditional,
                "max_days_held_loss": max_days_held_loss,
            },
        }
    )


def test_check_eod_flatten_is_a_noop_when_disabled_by_config(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(enabled=False))
    fake_client = FakeEodFlattenTradingClient(FakeClock(is_open=True), [FakeEodPosition("MGN", AssetClass.US_EQUITY)])
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_eod_flatten()

    assert events == []
    assert fake_client.submitted == []


def test_check_eod_flatten_is_a_noop_when_market_is_closed(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg())
    fake_client = FakeEodFlattenTradingClient(FakeClock(is_open=False), [FakeEodPosition("MGN", AssetClass.US_EQUITY)])
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_eod_flatten()

    assert events == []
    assert fake_client.submitted == []


def test_check_eod_flatten_is_a_noop_when_not_yet_within_the_closing_window(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(minutes_before_close=10))
    fake_client = FakeEodFlattenTradingClient(
        FakeClock(is_open=True, minutes_to_close=30), [FakeEodPosition("MGN", AssetClass.US_EQUITY)]
    )
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_eod_flatten()

    assert events == []
    assert fake_client.submitted == []


def test_check_eod_flatten_sells_stock_positions_and_skips_crypto(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(minutes_before_close=10))
    fake_client = FakeEodFlattenTradingClient(
        FakeClock(is_open=True, minutes_to_close=5),
        [FakeEodPosition("MGN", AssetClass.US_EQUITY), FakeEodPosition("BTC/USD", AssetClass.CRYPTO)],
    )
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_eod_flatten()

    assert len(events) == 1
    assert events[0]["symbol"] == "MGN"
    assert events[0]["reason"] == "eod_flatten"
    assert events[0]["sell_result"]["status"] == "submitted"
    assert [req.symbol for req in fake_client.submitted] == ["MGN"]


def test_check_eod_flatten_excludes_a_skipped_sell_from_the_returned_events(monkeypatch):
    """sell() returns status="skipped" when get_open_position() resolves to qty<=0 -- shouldn't
    happen for a symbol get_all_positions() itself just returned, but if it does (e.g. a race with
    a fill that already closed it), it must not be reported as a flatten event."""
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(minutes_before_close=10))
    fake_client = FakeEodFlattenTradingClient(
        FakeClock(is_open=True, minutes_to_close=5), [FakeEodPosition("MGN", AssetClass.US_EQUITY, qty="0")]
    )
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_eod_flatten()

    assert events == []
    assert fake_client.submitted == []


def test_check_eod_flatten_conditional_flattens_everything_when_aggregate_pl_is_up(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(conditional=True))
    fake_client = FakeEodFlattenTradingClient(
        FakeClock(is_open=True, minutes_to_close=5),
        [
            FakeEodPosition("MGN", AssetClass.US_EQUITY, unrealized_pl="50"),
            FakeEodPosition("ACME", AssetClass.US_EQUITY, unrealized_pl="-20"),
        ],
    )
    monkeypatch.setattr(execution, "trading_client", fake_client)

    events = execution.check_eod_flatten()

    assert {e["symbol"] for e in events} == {"MGN", "ACME"}
    assert {req.symbol for req in fake_client.submitted} == {"MGN", "ACME"}


def test_check_eod_flatten_conditional_holds_everything_when_aggregate_pl_is_down(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(conditional=True, max_days_held_loss=5))
    fake_client = FakeEodFlattenTradingClient(
        FakeClock(is_open=True, minutes_to_close=5),
        [
            FakeEodPosition("MGN", AssetClass.US_EQUITY, unrealized_pl="-50"),
            FakeEodPosition("ACME", AssetClass.US_EQUITY, unrealized_pl="20"),
        ],
    )
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(db, "fetch_position_opened_at", lambda symbol: None)  # untracked -> 0 days held

    events = execution.check_eod_flatten()

    assert events == []
    assert fake_client.submitted == []


def test_check_eod_flatten_conditional_force_flattens_a_position_past_the_days_held_cap(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _eod_flatten_cfg(conditional=True, max_days_held_loss=3))
    clock = FakeClock(is_open=True, minutes_to_close=5)
    fake_client = FakeEodFlattenTradingClient(
        clock,
        [
            FakeEodPosition("MGN", AssetClass.US_EQUITY, unrealized_pl="-10"),
            FakeEodPosition("ACME", AssetClass.US_EQUITY, unrealized_pl="-5"),
        ],
    )
    monkeypatch.setattr(execution, "trading_client", fake_client)

    def fake_opened_at(symbol):
        if symbol == "MGN":
            return clock.timestamp - timedelta(days=4)  # past the 3-day cap
        return clock.timestamp - timedelta(days=1)  # under the cap

    monkeypatch.setattr(db, "fetch_position_opened_at", fake_opened_at)

    events = execution.check_eod_flatten()

    assert [e["symbol"] for e in events] == ["MGN"]
    assert [req.symbol for req in fake_client.submitted] == ["MGN"]


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


def test_buy_skips_when_daily_profit_target_reached(monkeypatch):
    account = FakeAccount(equity="101000.0", last_equity="100000.0")  # +$1000, == strategy.daily_profit_target_usd
    fake_client = FakeTradingClient(account=account)
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "daily_profit_target_reached"
    assert fake_client.submitted == []


def test_buy_skips_when_daily_loss_limit_reached(monkeypatch):
    account = FakeAccount(equity="99500.0", last_equity="100000.0")  # -$500, == strategy.daily_loss_limit_usd
    fake_client = FakeTradingClient(account=account)
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "daily_loss_limit_reached"
    assert fake_client.submitted == []


def test_buy_proceeds_when_daily_pnl_is_within_bounds(monkeypatch):
    account = FakeAccount(equity="100200.0", last_equity="100000.0")  # +$200, under target
    fake_client = FakeTradingClient(account=account)
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"


def test_buy_skips_when_max_concurrent_positions_reached(monkeypatch):
    fake_client = FakeTradingClient(open_positions_count=10)  # == _FAKE_CFG.strategy.max_concurrent_positions
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "skipped"
    assert result["reason"] == "max_concurrent_positions_reached"
    assert fake_client.submitted == []


def test_buy_proceeds_when_below_max_concurrent_positions(monkeypatch):
    fake_client = FakeTradingClient(open_positions_count=9)
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"


def test_buy_tops_up_existing_position_without_regard_to_max_concurrent_positions(monkeypatch):
    """Topping up a symbol that's already open must never be blocked by the concurrent-positions
    cap -- it isn't a new position. FakeExistingPositionTradingClient has no get_all_positions
    override, so this also proves the cap check never even calls it for a top-up."""
    fake_client = FakeExistingPositionTradingClient(market_value="4000.00")
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"


def _risk_based_cfg(risk_per_trade_usd=100):
    return OmegaConf.create(
        {
            "strategy": {
                "daily_profit_target_usd": 1000,
                "daily_loss_limit_usd": 500,
                "crypto_slP": 0.98,
                "crypto_tpP": 1.03,
                "max_concurrent_positions": 10,
                "position_sizing": "risk_based",
                "risk_per_trade_usd": risk_per_trade_usd,
            },
            "eod_flatten": {"enabled": False, "minutes_before_close": 10},
        }
    )


def test_risk_based_sizing_caps_budget_to_risk_per_trade_over_stop_distance(monkeypatch):
    """risk_per_trade_usd=100, slP=0.95 -> the stop loses 5% of the budget, so the largest budget
    that risks exactly $100 at the stop is 100 / 0.05 = $2000."""
    monkeypatch.setattr(execution, "load_config", lambda: _risk_based_cfg(risk_per_trade_usd=100))
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 10000.0, slP=0.95, tpP=1.05)

    assert result["status"] == "submitted"
    # ~200 shares (100 / 0.05 / 10) -- computed the same way the production code does rather than
    # hardcoded, since 1 - 0.95 isn't exactly representable in floating point.
    assert fake_client.submitted[0].qty == int((100 / (1 - 0.95)) // 10.0)


def test_risk_based_sizing_never_increases_budget_above_authorized(monkeypatch):
    """The risk cap only ever shrinks the requested budget -- a generous risk_per_trade_usd must
    never inflate exposure beyond what the Analyst/Dealer already authorized."""
    monkeypatch.setattr(execution, "load_config", lambda: _risk_based_cfg(risk_per_trade_usd=1000))
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 500.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].qty == 50  # int(500.0 // 10.0), unchanged -- the $50000 risk cap is far above it


def test_risk_based_sizing_uses_crypto_slp_for_crypto_buys(monkeypatch):
    monkeypatch.setattr(execution, "load_config", lambda: _risk_based_cfg(risk_per_trade_usd=1))
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)

    result = execution.buy("BTC/USD", "binance", 100.0, slP=0.98, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].notional == 50.0  # 1 / (1 - 0.98) == 50, capped down from the $100 request


def test_flat_budget_sizing_leaves_budget_unchanged(monkeypatch):
    """Default mode (_FAKE_CFG.strategy.position_sizing == "flat_budget") -- confirms the risk
    cap is a no-op unless risk_based is explicitly active, even with a stop wide enough that a
    risk cap would otherwise bite hard."""
    fake_client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", fake_client)
    monkeypatch.setattr(execution, "get_current_ask_price", lambda symbol: 10.0)

    result = execution.buy("MGN", "stocks", 5000.0, slP=0.5, tpP=1.05)

    assert result["status"] == "submitted"
    assert fake_client.submitted[0].qty == 500  # int(5000.0 // 10.0), unaffected by the wide stop


def test_sell_still_permitted_when_daily_loss_limit_reached(monkeypatch):
    """The halt only ever blocks new BUY exposure -- SELL must be completely unaffected,
    matching the existing kill-switch precedent."""
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


def test_check_pending_fills_starts_tracking_a_crypto_stop_on_buy_fill(monkeypatch):
    execution._pending_fills["order-1"] = {
        "symbol": "BTC/USD",
        "action": "BUY",
        "reason": "opening_position",
        "sl_price": None,
        "tp_price": None,
        "crypto_slP": 0.98,
        "crypto_tpP": 1.03,
    }
    fake_client = FakePendingFillTradingClient({"order-1": FakePendingOrder(filled_avg_price="100.0")})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    execution.check_pending_fills()

    assert execution._crypto_stops["BTC/USD"] == pytest.approx((98.0, 103.0))


def test_check_pending_fills_does_not_track_a_crypto_stop_for_a_stock_fill(monkeypatch):
    """A stock BUY's pending-fill entry has crypto_slP/crypto_tpP left at None (see buy()) --
    must not be mistaken for a crypto fill."""
    execution._pending_fills["order-1"] = {
        "symbol": "MGN",
        "action": "BUY",
        "reason": "opening_position",
        "sl_price": 9.8,
        "tp_price": 10.5,
        "crypto_slP": None,
        "crypto_tpP": None,
    }
    fake_client = FakePendingFillTradingClient({"order-1": FakePendingOrder(filled_avg_price="10.05")})
    monkeypatch.setattr(execution, "trading_client", fake_client)

    execution.check_pending_fills()

    assert execution._crypto_stops == {}


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
    def __init__(self, open_orders, positions=None):
        self._open_orders = open_orders
        self._positions = positions or []

    def get_orders(self, request):
        return self._open_orders

    def get_all_positions(self):
        return self._positions


def test_reconcile_tracked_state_once_restores_a_still_open_pending_order(monkeypatch):
    order = FakeOrder("order-1", "BTC/USD", OrderSide.BUY, OrderStatus.NEW, filled_avg_price=None, legs=None)
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([order]))
    monkeypatch.setattr(execution, "_state_reconciled", False)

    assert execution.reconcile_tracked_state_once() is True

    assert execution._pending_fills["order-1"] == {
        "symbol": "BTC/USD",
        "action": "BUY",
        "reason": "reconstructed_after_restart",
        "sl_price": None,
        "tp_price": None,
    }
    assert execution.is_state_reconciled() is True


def test_reconcile_tracked_state_once_restores_a_bracket_with_a_still_open_leg(monkeypatch):
    legs = [FakeLeg("tp-leg-1", OrderStatus.NEW, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.NEW, OrderType.STOP)]
    order = FakeOrder("parent-1", "MGN", OrderSide.BUY, OrderStatus.FILLED, filled_avg_price="10.05", legs=legs)
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([order]))

    assert execution.reconcile_tracked_state_once() is True

    assert execution._tracked_brackets["MGN"] == "parent-1"
    assert "parent-1" not in execution._pending_fills


def test_reconcile_tracked_state_once_skips_a_bracket_whose_legs_are_all_terminal(monkeypatch):
    """Shouldn't happen given the status="open" family-level query, but must not crash or
    mistrack if it ever does."""
    legs = [FakeLeg("tp-leg-1", OrderStatus.CANCELED, OrderType.LIMIT), FakeLeg("sl-leg-1", OrderStatus.CANCELED, OrderType.STOP)]
    order = FakeOrder("parent-1", "MGN", OrderSide.BUY, OrderStatus.FILLED, filled_avg_price="10.05", legs=legs)
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([order]))

    assert execution.reconcile_tracked_state_once() is True

    assert "MGN" not in execution._tracked_brackets


def test_reconcile_tracked_state_once_backfills_position_opens_for_open_positions(monkeypatch):
    positions = [FakeEodPosition("MGN", AssetClass.US_EQUITY), FakeEodPosition("BTC/USD", AssetClass.CRYPTO)]
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([], positions=positions))
    recorded = []
    monkeypatch.setattr(db, "record_position_opened", lambda symbol: recorded.append(symbol))

    assert execution.reconcile_tracked_state_once() is True

    assert recorded == ["MGN", "BTC/USD"]


def test_reconcile_tracked_state_once_rebuilds_crypto_stop_from_live_alpaca_position_symbol(monkeypatch):
    positions = [FakeEodPosition("BTCUSD", AssetClass.CRYPTO, avg_entry_price="100.0")]
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([], positions=positions))
    monkeypatch.setattr(db, "record_position_opened", lambda symbol: None)

    assert execution.reconcile_tracked_state_once() is True

    assert execution._crypto_stops["BTC/USD"] == pytest.approx((98.0, 103.0))


def test_reconcile_tracked_state_once_does_not_rebuild_crypto_stop_for_pending_buy(monkeypatch):
    execution._pending_fills["order-1"] = {"symbol": "BTC/USD", "action": "BUY"}
    positions = [FakeEodPosition("BTCUSD", AssetClass.CRYPTO, avg_entry_price="100.0")]
    monkeypatch.setattr(execution, "trading_client", FakeReconstructTradingClient([], positions=positions))
    monkeypatch.setattr(db, "record_position_opened", lambda symbol: None)

    assert execution.reconcile_tracked_state_once() is True

    assert execution._crypto_stops == {}


def test_reconcile_tracked_state_once_backfill_failure_does_not_block_reconciliation(monkeypatch):
    """A failure fetching positions for the backfill must not undo an otherwise-successful order
    reconciliation -- the two are independent concerns."""

    class FailingPositionsClient(FakeReconstructTradingClient):
        def get_all_positions(self):
            raise APIError("unreachable")

    monkeypatch.setattr(execution, "trading_client", FailingPositionsClient([]))
    monkeypatch.setattr(execution, "_state_reconciled", False)

    assert execution.reconcile_tracked_state_once() is True
    assert execution.is_state_reconciled() is True


def test_reconcile_tracked_state_once_handles_api_error_without_raising(monkeypatch):
    class FailingClient:
        def get_orders(self, request):
            raise APIError("unreachable")

    monkeypatch.setattr(execution, "trading_client", FailingClient())
    monkeypatch.setattr(execution, "_state_reconciled", False)

    assert execution.reconcile_tracked_state_once() is False

    assert execution._pending_fills == {}
    assert execution._tracked_brackets == {}
    assert execution.is_state_reconciled() is False


class FakeFlakyReconstructTradingClient:
    """Raises APIError on the first `fail_times` calls to get_orders(), then returns
    `open_orders` -- lets tests exercise reconstruct_tracked_state()'s retry-with-backoff loop
    without a live Alpaca outage."""

    def __init__(self, open_orders, fail_times):
        self._open_orders = open_orders
        self._fail_times = fail_times
        self.calls = 0

    def get_orders(self, request):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise APIError("unreachable")
        return self._open_orders

    def get_all_positions(self):
        return []


def test_reconstruct_tracked_state_retries_with_backoff_until_success(monkeypatch):
    client = FakeFlakyReconstructTradingClient(open_orders=[], fail_times=2)
    monkeypatch.setattr(execution, "trading_client", client)
    monkeypatch.setattr(execution, "_state_reconciled", False)
    sleeps = []
    monkeypatch.setattr(execution.time, "sleep", lambda s: sleeps.append(s))

    execution.reconstruct_tracked_state(max_attempts=5, backoff_base_s=1)

    assert client.calls == 3
    assert sleeps == [1, 2]
    assert execution.is_state_reconciled() is True


def test_reconstruct_tracked_state_gives_up_after_max_attempts_and_stays_unreconciled(monkeypatch):
    client = FakeFlakyReconstructTradingClient(open_orders=[], fail_times=99)
    monkeypatch.setattr(execution, "trading_client", client)
    monkeypatch.setattr(execution, "_state_reconciled", False)
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)

    execution.reconstruct_tracked_state(max_attempts=3, backoff_base_s=1)

    assert client.calls == 3
    assert execution.is_state_reconciled() is False


def test_buy_skips_when_state_not_reconciled(monkeypatch):
    # status="skipped", not a distinct "rejected" -- ExecuteResponse's status Literal
    # (src/floor_broker/app.py) doesn't permit "rejected"; see test_app.py for the
    # end-to-end regression test covering that API-contract constraint directly.
    monkeypatch.setattr(execution, "_state_reconciled", False)
    client = FakeTradingClient()
    monkeypatch.setattr(execution, "trading_client", client)

    result = execution.buy("MGN", "stocks", budget=100.0, slP=0.98, tpP=1.05)

    assert result == {
        "status": "skipped",
        "reason": "state_not_reconciled",
        "detail": "tracked state not yet reconciled with Alpaca after restart",
    }
    assert client.submitted == []
