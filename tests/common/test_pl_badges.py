from src.common import pl_badges


class FakeAccount:
    def __init__(self, equity, last_equity):
        self.equity = equity
        self.last_equity = last_equity


class FakeHistory:
    def __init__(self, base_value):
        self.base_value = base_value


class FakeTradingClient:
    def __init__(self, equity, last_equity, base_value):
        self._account = FakeAccount(equity, last_equity)
        self._history = FakeHistory(base_value)

    def get_account(self):
        return self._account

    def get_portfolio_history(self, request):
        return self._history


def test_fetch_pl_summary_computes_today_and_ytd_pl(monkeypatch):
    fake_client = FakeTradingClient(equity="1050.00", last_equity="1000.00", base_value="900.00")
    monkeypatch.setattr(pl_badges, "trading_client", fake_client)

    summary = pl_badges.fetch_pl_summary()

    assert summary == {"equity": 1050.0, "today_pl": 50.0, "ytd_pl": 150.0}


def test_fetch_pl_summary_handles_negative_pl(monkeypatch):
    fake_client = FakeTradingClient(equity="900.00", last_equity="1000.00", base_value="1200.00")
    monkeypatch.setattr(pl_badges, "trading_client", fake_client)

    summary = pl_badges.fetch_pl_summary()

    assert summary == {"equity": 900.0, "today_pl": -100.0, "ytd_pl": -300.0}


def test_fetch_pl_summary_falls_back_to_today_pl_when_base_value_is_none(monkeypatch):
    fake_client = FakeTradingClient(equity="999166.40", last_equity="1000000.00", base_value=None)
    monkeypatch.setattr(pl_badges, "trading_client", fake_client)

    summary = pl_badges.fetch_pl_summary()

    assert summary == {"equity": 999166.4, "today_pl": -833.6, "ytd_pl": -833.6}


def test_build_badge_payload_formats_positive_value_as_brightgreen():
    payload = pl_badges.build_badge_payload("Today's P/L", 243.359)

    assert payload == {
        "schemaVersion": 1,
        "label": "Today's P/L",
        "message": "+$243.36",
        "color": "brightgreen",
    }


def test_build_badge_payload_formats_negative_value_as_red():
    payload = pl_badges.build_badge_payload("YTD P/L", -1234.5)

    assert payload == {
        "schemaVersion": 1,
        "label": "YTD P/L",
        "message": "-$1,234.50",
        "color": "red",
    }


def test_build_badge_payload_treats_zero_as_up():
    payload = pl_badges.build_badge_payload("Today's P/L", 0.0)

    assert payload["message"] == "+$0.00"
    assert payload["color"] == "brightgreen"
