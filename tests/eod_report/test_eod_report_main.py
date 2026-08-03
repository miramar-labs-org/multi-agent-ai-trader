from src.common import eod
from src.eod_report import main


class FakeAccount:
    def __init__(self):
        self.equity = "1050.00"
        self.last_equity = "1000.00"
        self.cash = "500.00"
        self.buying_power = "2000.00"


class FakePosition:
    def __init__(self, symbol, qty, market_value, unrealized_plpc):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value
        self.unrealized_plpc = unrealized_plpc


class FakeTradingClient:
    def __init__(self, calendar, activities=None):
        self._calendar = calendar
        self._activities = activities or []

    def get_calendar(self, request):
        return self._calendar

    def get_account(self):
        return FakeAccount()

    def get_all_positions(self):
        return [FakePosition("MGN", "3", "150.00", "0.05")]

    def get(self, path, data=None):
        return self._activities


def _silence_slack(monkeypatch):
    calls = {}
    monkeypatch.setattr(main.slack, "notify_market_closed", lambda *a, **k: calls.setdefault("market_closed", (a, k)))
    monkeypatch.setattr(main.slack, "notify_eod_report", lambda *a, **k: calls.setdefault("eod_report", (a, k)))
    monkeypatch.setattr(main.slack, "notify_error", lambda *a, **k: calls.setdefault("error", (a, k)))
    return calls


def test_market_closed_posts_notification_and_skips_report(monkeypatch):
    """Regression: previously main() only logged locally on a closed market and returned -- a
    weekend/holiday CronJob run produced zero visible signal that anything happened at all."""
    fake_client = FakeTradingClient(calendar=[])
    monkeypatch.setattr(main, "trading_client", fake_client)
    calls = _silence_slack(monkeypatch)

    main.main()

    assert "market_closed" in calls
    assert "eod_report" not in calls


def test_open_market_sends_full_eod_report(monkeypatch):
    fake_client = FakeTradingClient(calendar=["some trading day"], activities=[])
    monkeypatch.setattr(main, "trading_client", fake_client)
    monkeypatch.setattr(eod, "trading_client", fake_client)
    calls = _silence_slack(monkeypatch)

    main.main()

    assert "eod_report" in calls
    assert "market_closed" not in calls
    args, _ = calls["eod_report"]
    _report_date, account_summary, fills, position_summaries = args
    assert account_summary["equity"] == 1050.0
    assert fills == []
    assert position_summaries == [{"symbol": "MGN", "qty": 3.0, "market_value": 150.0, "unrealized_plpc": 0.05}]


def test_alpaca_failure_notifies_error_and_reraises(monkeypatch):
    class FailingTradingClient(FakeTradingClient):
        def get_account(self):
            raise RuntimeError("alpaca unavailable")

    fake_client = FailingTradingClient(calendar=["some trading day"])
    monkeypatch.setattr(main, "trading_client", fake_client)
    calls = _silence_slack(monkeypatch)

    try:
        main.main()
        raised = False
    except RuntimeError:
        raised = True

    assert raised
    assert "error" in calls
    assert "eod_report" not in calls
