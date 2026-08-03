import json

import pytest
from alpaca.common.exceptions import APIError

from src.floor_broker import execution


def _api_error(payload: dict) -> APIError:
    err = APIError.__new__(APIError)
    err.args = (json.dumps(payload),)
    return err


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
