from datetime import datetime

import pytz
from alpaca.trading.enums import AssetClass, PositionSide
from omegaconf import OmegaConf

from src.power_scheduler import main

EASTERN = pytz.timezone("US/Eastern")


def _cfg(minutes_before_open=60, minutes_after_close=60, flatten_crypto=True, enabled=True, manage_ollama=True,
         options_enabled=False, flatten_options=True):
    return OmegaConf.create(
        {
            "power_schedule": {
                "enabled": enabled,
                "minutes_before_open": minutes_before_open,
                "minutes_after_close": minutes_after_close,
                "flatten_crypto_before_powerdown": flatten_crypto,
                "flatten_options_before_powerdown": flatten_options,
                "manage_ollama_model": manage_ollama,
            },
            "options_trading": {"enabled": options_enabled},
            "floor_broker": {"base_url": "http://floor-broker.test:8000"},
            "llm": {"base_url": "http://ollama.test:11434/v1", "model": "qwen3.6:35b-a3b"},
        }
    )


def _dt(hour, minute):
    return EASTERN.localize(datetime(2026, 8, 10, hour, minute))


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class FakePosition:
    def __init__(self, asset_class, side=PositionSide.LONG):
        self.asset_class = asset_class
        self.side = side


# --- _target_replica_count -------------------------------------------------


def test_target_replica_count_is_zero_when_not_a_trading_day():
    assert main._target_replica_count(_dt(12, 0), None, _cfg()) == 0


def test_target_replica_count_is_zero_before_the_power_up_threshold():
    hours = (_dt(9, 30), _dt(16, 0))
    assert main._target_replica_count(_dt(8, 29), hours, _cfg(minutes_before_open=60)) == 0


def test_target_replica_count_is_one_at_the_power_up_threshold_boundary():
    hours = (_dt(9, 30), _dt(16, 0))
    assert main._target_replica_count(_dt(8, 30), hours, _cfg(minutes_before_open=60)) == 1


def test_target_replica_count_is_one_during_market_hours():
    hours = (_dt(9, 30), _dt(16, 0))
    assert main._target_replica_count(_dt(12, 0), hours, _cfg()) == 1


def test_target_replica_count_is_one_at_the_power_down_threshold_boundary():
    hours = (_dt(9, 30), _dt(16, 0))
    assert main._target_replica_count(_dt(17, 0), hours, _cfg(minutes_after_close=60)) == 1


def test_target_replica_count_is_zero_after_the_power_down_threshold():
    hours = (_dt(9, 30), _dt(16, 0))
    assert main._target_replica_count(_dt(17, 1), hours, _cfg(minutes_after_close=60)) == 0


# --- _wait_until_crypto_flat / _wait_until_floor_broker_ready --------------


def test_wait_until_crypto_flat_returns_true_immediately_when_no_crypto_positions(monkeypatch):
    monkeypatch.setattr(main.trading_client, "get_all_positions", lambda: [])
    assert main._wait_until_crypto_flat(timeout_s=5) is True


def test_wait_until_crypto_flat_returns_false_after_timeout_when_still_open(monkeypatch):
    monkeypatch.setattr(main.trading_client, "get_all_positions", lambda: [FakePosition(AssetClass.CRYPTO)])
    assert main._wait_until_crypto_flat(timeout_s=0) is False


def test_wait_until_options_flat_returns_true_immediately_when_no_option_positions(monkeypatch):
    monkeypatch.setattr(main.trading_client, "get_all_positions", lambda: [])
    assert main._wait_until_options_flat(timeout_s=5) is True


def test_wait_until_options_flat_returns_false_after_timeout_when_still_open(monkeypatch):
    monkeypatch.setattr(main.trading_client, "get_all_positions", lambda: [FakePosition(AssetClass.US_OPTION)])
    assert main._wait_until_options_flat(timeout_s=0) is False


def test_wait_until_options_flat_returns_true_when_only_short_position_open(monkeypatch):
    """An externally-created short option must not be waited on -- flatten_all_options() (called
    before this) deliberately leaves shorts alone, since selling one would open MORE short rather
    than closing it. Mirrors the LONG-only filter in execution.py's flatten_all_options()."""
    monkeypatch.setattr(
        main.trading_client, "get_all_positions", lambda: [FakePosition(AssetClass.US_OPTION, side=PositionSide.SHORT)]
    )
    assert main._wait_until_options_flat(timeout_s=5) is True


def test_wait_until_floor_broker_ready_returns_true_on_200(monkeypatch):
    monkeypatch.setattr(main.requests, "get", lambda url, timeout: FakeResponse(status_code=200))
    assert main._wait_until_floor_broker_ready(_cfg(), timeout_s=5) is True


def test_wait_until_floor_broker_ready_returns_false_after_timeout(monkeypatch):
    monkeypatch.setattr(main.requests, "get", lambda url, timeout: FakeResponse(status_code=503))
    assert main._wait_until_floor_broker_ready(_cfg(), timeout_s=0) is False


# --- _stop_ollama_model / _start_ollama_model -------------------------------


def test_ollama_native_url_strips_v1_suffix():
    assert main._ollama_native_url(_cfg()) == "http://ollama.test:11434"


def test_stop_ollama_model_posts_keep_alive_zero(monkeypatch):
    posted = {}

    def _fake_post(url, json=None, timeout=None):
        posted.update(url=url, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", _fake_post)

    main._stop_ollama_model(_cfg())

    assert posted["url"] == "http://ollama.test:11434/api/generate"
    assert posted["json"] == {"model": "qwen3.6:35b-a3b", "keep_alive": 0}


def test_start_ollama_model_posts_keep_alive_forever(monkeypatch):
    posted = {}

    def _fake_post(url, json=None, timeout=None):
        posted.update(url=url, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", _fake_post)

    main._start_ollama_model(_cfg())

    assert posted["url"] == "http://ollama.test:11434/api/generate"
    assert posted["json"] == {"model": "qwen3.6:35b-a3b", "keep_alive": -1}


def test_stop_ollama_model_notifies_but_does_not_raise_on_request_failure(monkeypatch):
    def _raise(*a, **k):
        raise main.requests.RequestException("connection refused")

    monkeypatch.setattr(main.requests, "post", _raise)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))

    main._stop_ollama_model(_cfg())  # must not raise

    assert "text" in errors


def test_start_ollama_model_notifies_but_does_not_raise_on_request_failure(monkeypatch):
    def _raise(*a, **k):
        raise main.requests.RequestException("timed out")

    monkeypatch.setattr(main.requests, "post", _raise)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))

    main._start_ollama_model(_cfg())  # must not raise

    assert "text" in errors


# --- _power_down / _power_up orchestration ----------------------------------


def test_power_down_scales_dealer_first_then_flattens_and_scales_floor_broker(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: FakeResponse(json_data={"events": [{"symbol": "BTC/USD"}]}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main, "_wait_until_options_flat", lambda: True)
    notified = {}
    monkeypatch.setattr(main.slack, "notify_power_state", lambda action, detail: notified.update(action=action, detail=detail))

    main._power_down(None, _cfg())

    assert calls[0] == ("dealer", 0)
    assert calls[-1] == ("floor-broker", 0)
    assert notified["action"] == "powered_down"


def test_power_down_aborts_and_leaves_floor_broker_up_when_crypto_never_flattens(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: FakeResponse(json_data={"events": []}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: False)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))

    main._power_down(None, _cfg())

    assert calls == [("dealer", 0)]
    assert "text" in errors


def test_power_down_skips_flatten_request_when_disabled_by_config(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    posted = {"called": False}

    def _fake_post(*a, **k):
        posted["called"] = True
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", _fake_post)
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_down(None, _cfg(flatten_crypto=False, flatten_options=False))

    assert posted["called"] is False
    assert calls[-1] == ("floor-broker", 0)


def test_power_down_aborts_when_flatten_crypto_request_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)

    def _raise(*a, **k):
        raise main.requests.RequestException("connection refused")

    monkeypatch.setattr(main.requests, "post", _raise)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))

    main._power_down(None, _cfg())

    assert calls == [("dealer", 0)]
    assert "text" in errors


def test_power_down_stops_ollama_model_after_scaling_dealer_to_zero(monkeypatch):
    order = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: order.append(("scale", name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: order.append(("stop_ollama",)))
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: FakeResponse(json_data={"events": []}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main, "_wait_until_options_flat", lambda: True)
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_down(None, _cfg())

    assert order[0] == ("scale", "dealer", 0)
    assert order[1] == ("stop_ollama",)


def test_power_down_skips_ollama_stop_when_disabled_by_config(monkeypatch):
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: None)
    called = {"stop": False}
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: called.__setitem__("stop", True))
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: FakeResponse(json_data={"events": []}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main, "_wait_until_options_flat", lambda: True)
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_down(None, _cfg(manage_ollama=False))

    assert called["stop"] is False


def test_power_down_flattens_options_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    posted_urls = []

    def _fake_post(url, timeout):
        posted_urls.append(url)
        events = [{"symbol": "AAPL250117C00200000"}] if url.endswith("/flatten-options") else []
        return FakeResponse(json_data={"events": events})

    monkeypatch.setattr(main.requests, "post", _fake_post)
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main, "_wait_until_options_flat", lambda: True)
    notified = {}
    monkeypatch.setattr(main.slack, "notify_power_state", lambda action, detail: notified.update(action=action, detail=detail))

    main._power_down(None, _cfg(options_enabled=True))

    assert any(u.endswith("/flatten-options") for u in posted_urls)
    assert calls[-1] == ("floor-broker", 0)
    assert "1 option position" in notified["detail"]


def test_power_down_flattens_options_even_when_options_trading_disabled(monkeypatch):
    """Regression: options_trading.enabled is a new-entry gate, not a protection gate -- flipping it
    off as an emergency rollback must not skip flattening any option position still open."""
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    posted_urls = []

    def _fake_post(url, timeout):
        posted_urls.append(url)
        events = [{"symbol": "AAPL250117C00200000"}] if url.endswith("/flatten-options") else []
        return FakeResponse(json_data={"events": events})

    monkeypatch.setattr(main.requests, "post", _fake_post)
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main, "_wait_until_options_flat", lambda: True)
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_down(None, _cfg(options_enabled=False))

    assert any(u.endswith("/flatten-options") for u in posted_urls)
    assert calls[-1] == ("floor-broker", 0)


def test_power_down_skips_options_flatten_when_disabled_by_config_flag(monkeypatch):
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: None)
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    posted_urls = []
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: posted_urls.append(url) or FakeResponse(json_data={"events": []}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_down(None, _cfg(options_enabled=True, flatten_options=False))

    assert not any(u.endswith("/flatten-options") for u in posted_urls)


def test_power_down_continues_when_options_never_flatten(monkeypatch):
    """Regression: unlike crypto, a failed/incomplete options flatten must never block power-down --
    options stay protected by dte_force_close/synthetic SL-TP once Floor Broker restarts, and
    retrying the flatten after the options market has closed for the day cannot succeed anyway."""
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: FakeResponse(json_data={"events": []}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    monkeypatch.setattr(main, "_wait_until_options_flat", lambda: False)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))
    notified = {}
    monkeypatch.setattr(main.slack, "notify_power_state", lambda action, detail: notified.update(action=action, detail=detail))

    main._power_down(None, _cfg(options_enabled=True))

    assert calls[0] == ("dealer", 0)
    assert calls[-1] == ("floor-broker", 0)
    assert "text" in errors  # still notified, just doesn't block
    assert notified["action"] == "powered_down"


def test_power_down_continues_when_flatten_options_request_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)

    def _fake_post(url, timeout):
        if url.endswith("/flatten-options"):
            raise main.requests.RequestException("connection refused")
        return FakeResponse(json_data={"events": []})

    monkeypatch.setattr(main.requests, "post", _fake_post)
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))
    notified = {}
    monkeypatch.setattr(main.slack, "notify_power_state", lambda action, detail: notified.update(action=action, detail=detail))

    main._power_down(None, _cfg(options_enabled=True))

    assert calls[0] == ("dealer", 0)
    assert calls[-1] == ("floor-broker", 0)
    assert "text" in errors
    assert notified["action"] == "powered_down"


def test_power_down_continues_when_wait_until_options_flat_raises(monkeypatch):
    """Regression: an uncaught exception from _wait_until_options_flat() (e.g. Alpaca's APIError on
    an outage, rate limit, or a bad account-2 credential) must not crash power-down and leave
    floor-broker stuck up with no notification -- this is the same failure mode the request-failure/
    timeout branches already guard against, just reached via an exception instead of a return."""
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_stop_ollama_model", lambda cfg: None)
    monkeypatch.setattr(main.requests, "post", lambda url, timeout: FakeResponse(json_data={"events": []}))
    monkeypatch.setattr(main, "_wait_until_crypto_flat", lambda: True)

    def _raise():
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(main, "_wait_until_options_flat", _raise)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))
    notified = {}
    monkeypatch.setattr(main.slack, "notify_power_state", lambda action, detail: notified.update(action=action, detail=detail))

    main._power_down(None, _cfg(options_enabled=True))

    assert calls[0] == ("dealer", 0)
    assert calls[-1] == ("floor-broker", 0)
    assert "text" in errors
    assert notified["action"] == "powered_down"


def test_power_up_scales_floor_broker_first_then_dealer(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_wait_until_floor_broker_ready", lambda cfg: True)
    monkeypatch.setattr(main, "_start_ollama_model", lambda cfg: None)
    notified = {}
    monkeypatch.setattr(main.slack, "notify_power_state", lambda action, detail: notified.update(action=action))

    main._power_up(None, _cfg())

    assert calls == [("floor-broker", 1), ("dealer", 1)]
    assert notified["action"] == "powered_up"


def test_power_up_leaves_dealer_at_zero_when_floor_broker_never_ready(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: calls.append((name, replicas)))
    monkeypatch.setattr(main, "_wait_until_floor_broker_ready", lambda cfg: False)
    errors = {}
    monkeypatch.setattr(main.slack, "notify_error", lambda component, text: errors.setdefault("text", text))

    main._power_up(None, _cfg())

    assert calls == [("floor-broker", 1)]
    assert "text" in errors


def test_power_up_starts_ollama_model_after_floor_broker_ready_before_dealer(monkeypatch):
    order = []
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: order.append(("scale", name, replicas)))
    monkeypatch.setattr(main, "_wait_until_floor_broker_ready", lambda cfg: True)
    monkeypatch.setattr(main, "_start_ollama_model", lambda cfg: order.append(("start_ollama",)))
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_up(None, _cfg())

    assert order == [("scale", "floor-broker", 1), ("start_ollama",), ("scale", "dealer", 1)]


def test_power_up_skips_ollama_start_when_disabled_by_config(monkeypatch):
    monkeypatch.setattr(main, "_scale", lambda apps_v1, name, replicas: None)
    monkeypatch.setattr(main, "_wait_until_floor_broker_ready", lambda cfg: True)
    called = {"start": False}
    monkeypatch.setattr(main, "_start_ollama_model", lambda cfg: called.__setitem__("start", True))
    monkeypatch.setattr(main.slack, "notify_power_state", lambda *a, **k: None)

    main._power_up(None, _cfg(manage_ollama=False))

    assert called["start"] is False


# --- main() reconcile dispatch ----------------------------------------------


def test_main_is_a_noop_when_disabled_by_config(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda: _cfg(enabled=False))
    called = {}
    monkeypatch.setattr(main, "_power_down", lambda *a: called.setdefault("power_down", True))
    monkeypatch.setattr(main, "_power_up", lambda *a: called.setdefault("power_up", True))

    main.main()

    assert called == {}


def test_main_is_a_noop_when_current_replica_count_matches_target(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda: _cfg())
    monkeypatch.setattr(main, "_now_eastern", lambda: _dt(12, 0))
    monkeypatch.setattr(main, "get_stock_market_hours", lambda day: (_dt(9, 30), _dt(16, 0)))
    monkeypatch.setattr(main, "_apps_v1", lambda: object())
    monkeypatch.setattr(main, "_get_replica_count", lambda apps_v1, name: 1)
    called = {}
    monkeypatch.setattr(main, "_power_down", lambda *a: called.setdefault("power_down", True))
    monkeypatch.setattr(main, "_power_up", lambda *a: called.setdefault("power_up", True))

    main.main()

    assert called == {}


def test_main_powers_down_when_current_is_one_and_target_is_zero(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda: _cfg())
    monkeypatch.setattr(main, "_now_eastern", lambda: _dt(20, 0))
    monkeypatch.setattr(main, "get_stock_market_hours", lambda day: (_dt(9, 30), _dt(16, 0)))
    monkeypatch.setattr(main, "_apps_v1", lambda: object())
    monkeypatch.setattr(main, "_get_replica_count", lambda apps_v1, name: 1)
    called = {}
    monkeypatch.setattr(main, "_power_down", lambda apps_v1, cfg: called.setdefault("power_down", True))
    monkeypatch.setattr(main, "_power_up", lambda apps_v1, cfg: called.setdefault("power_up", True))

    main.main()

    assert called == {"power_down": True}


def test_main_powers_up_when_current_is_zero_and_target_is_one(monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda: _cfg())
    monkeypatch.setattr(main, "_now_eastern", lambda: _dt(12, 0))
    monkeypatch.setattr(main, "get_stock_market_hours", lambda day: (_dt(9, 30), _dt(16, 0)))
    monkeypatch.setattr(main, "_apps_v1", lambda: object())
    monkeypatch.setattr(main, "_get_replica_count", lambda apps_v1, name: 0)
    called = {}
    monkeypatch.setattr(main, "_power_down", lambda apps_v1, cfg: called.setdefault("power_down", True))
    monkeypatch.setattr(main, "_power_up", lambda apps_v1, cfg: called.setdefault("power_up", True))

    main.main()

    assert called == {"power_up": True}
