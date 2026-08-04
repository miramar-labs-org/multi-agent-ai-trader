import pytest

from src.floor_broker import main as fb_main


class _StopLoop(Exception):
    """Raised from a mocked time.sleep() to break out of poll_bracket_fills()'s infinite loop
    after exactly one iteration, so it can be tested without actually running forever."""


def test_poll_bracket_fills_posts_slack_notification_for_each_event(monkeypatch):
    monkeypatch.setattr(
        fb_main.execution,
        "check_bracket_fills",
        lambda: [{"kind": "fill", "symbol": "MGN", "order_id": "leg-1", "reason": "take_profit", "fill_price": 15.0, "qty": 10.0}],
    )
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_floor_broker_result", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_bracket_fills()

    assert len(posted) == 1
    args, kwargs = posted[0]
    assert args[0] == "MGN"
    assert args[1] == "SELL"
    assert kwargs["reason"] == "take_profit"
    assert kwargs["fill_price"] == 15.0


def test_poll_bracket_fills_posts_no_fill_notice_for_a_terminal_event(monkeypatch):
    monkeypatch.setattr(
        fb_main.execution,
        "check_bracket_fills",
        lambda: [{"kind": "terminal", "symbol": "MGN", "order_id": "parent-1", "leg_statuses": ["canceled", "canceled"]}],
    )
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_floor_broker_result", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_bracket_fills()

    assert len(posted) == 1
    args, kwargs = posted[0]
    assert args[0] == "MGN"
    assert args[1] == "SELL"
    assert args[2] == "no_fill"


def test_poll_bracket_fills_posts_nothing_when_no_events(monkeypatch):
    monkeypatch.setattr(fb_main.execution, "check_bracket_fills", lambda: [])
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_floor_broker_result", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_bracket_fills()

    assert posted == []


def test_poll_bracket_fills_survives_an_exception_and_reaches_the_next_sleep(monkeypatch):
    """A transient Alpaca error inside one poll iteration must not kill the background thread --
    it should be caught, logged, and the loop must still reach time.sleep() to try again."""

    def _raise():
        raise RuntimeError("alpaca unavailable")

    monkeypatch.setattr(fb_main.execution, "check_bracket_fills", _raise)
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_bracket_fills()


def test_poll_pending_fills_posts_slack_notification_for_each_event(monkeypatch):
    monkeypatch.setattr(
        fb_main.execution,
        "check_pending_fills",
        lambda: [
            {
                "kind": "fill",
                "symbol": "MGN",
                "action": "BUY",
                "reason": "opening_position",
                "order_id": "order-1",
                "fill_price": 10.05,
                "sl_price": 9.8,
                "tp_price": 10.5,
            }
        ],
    )
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_floor_broker_result", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_pending_fills()

    assert len(posted) == 1
    args, kwargs = posted[0]
    assert args[0] == "MGN"
    assert args[1] == "BUY"
    assert kwargs["reason"] == "opening_position"
    assert kwargs["fill_price"] == 10.05
    assert kwargs["sl_price"] == 9.8
    assert kwargs["tp_price"] == 10.5


def test_poll_pending_fills_posts_no_fill_notice_for_a_terminal_event(monkeypatch):
    monkeypatch.setattr(
        fb_main.execution,
        "check_pending_fills",
        lambda: [
            {
                "kind": "terminal",
                "symbol": "MGN",
                "action": "BUY",
                "reason": "opening_position",
                "order_id": "order-1",
                "order_status": "rejected",
            }
        ],
    )
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_floor_broker_result", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_pending_fills()

    assert len(posted) == 1
    args, kwargs = posted[0]
    assert args[0] == "MGN"
    assert args[1] == "BUY"
    assert args[2] == "no_fill"
    assert kwargs["reason"] == "opening_position"


def test_poll_pending_fills_posts_nothing_when_no_events(monkeypatch):
    monkeypatch.setattr(fb_main.execution, "check_pending_fills", lambda: [])
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_floor_broker_result", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_pending_fills()

    assert posted == []


def test_poll_pending_fills_survives_an_exception_and_reaches_the_next_sleep(monkeypatch):
    """A transient Alpaca error inside one poll iteration must not kill the background thread --
    it should be caught, logged, and the loop must still reach time.sleep() to try again."""

    def _raise():
        raise RuntimeError("alpaca unavailable")

    monkeypatch.setattr(fb_main.execution, "check_pending_fills", _raise)
    monkeypatch.setattr(fb_main.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop))

    with pytest.raises(_StopLoop):
        fb_main.poll_pending_fills()


def _stop_after(n):
    """Lets poll_kill_switch() run for exactly `n` iterations (n calls to time.sleep()) before
    breaking out via _StopLoop, so a multi-iteration transition sequence can be tested without an
    infinite loop."""
    calls = {"count": 0}

    def _sleep(seconds):
        calls["count"] += 1
        if calls["count"] >= n:
            raise _StopLoop

    return _sleep


def test_poll_kill_switch_posts_nothing_on_first_observation(monkeypatch):
    """ROADMAP P0.5: the first poll only discovers whatever state the switch was seeded/left in
    -- that's not a transition, and must not fire a Slack notice on its own."""
    monkeypatch.setattr(fb_main.kill_switch, "buy_kill_switch_active", lambda: True)
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_buy_kill_switch", lambda active: posted.append(active))
    monkeypatch.setattr(fb_main.time, "sleep", _stop_after(1))

    with pytest.raises(_StopLoop):
        fb_main.poll_kill_switch()

    assert posted == []


def test_poll_kill_switch_notifies_only_on_transition(monkeypatch):
    states = iter([False, False, True, True, False])
    monkeypatch.setattr(fb_main.kill_switch, "buy_kill_switch_active", lambda: next(states))
    posted = []
    monkeypatch.setattr(fb_main.slack, "notify_buy_kill_switch", lambda active: posted.append(active))
    monkeypatch.setattr(fb_main.time, "sleep", _stop_after(5))

    with pytest.raises(_StopLoop):
        fb_main.poll_kill_switch()

    assert posted == [True, False], "must notify on False->True and True->False, and nothing else"


def test_poll_kill_switch_survives_an_exception_and_reaches_the_next_sleep(monkeypatch):
    def _raise():
        raise RuntimeError("apiserver unavailable")

    monkeypatch.setattr(fb_main.kill_switch, "buy_kill_switch_active", _raise)
    monkeypatch.setattr(fb_main.time, "sleep", _stop_after(1))

    with pytest.raises(_StopLoop):
        fb_main.poll_kill_switch()
