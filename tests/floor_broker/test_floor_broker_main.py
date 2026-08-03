import pytest

from src.floor_broker import main as fb_main


class _StopLoop(Exception):
    """Raised from a mocked time.sleep() to break out of poll_bracket_fills()'s infinite loop
    after exactly one iteration, so it can be tested without actually running forever."""


def test_poll_bracket_fills_posts_slack_notification_for_each_event(monkeypatch):
    monkeypatch.setattr(
        fb_main.execution,
        "check_bracket_fills",
        lambda: [{"symbol": "MGN", "order_id": "leg-1", "reason": "take_profit", "fill_price": 15.0, "qty": 10.0}],
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
