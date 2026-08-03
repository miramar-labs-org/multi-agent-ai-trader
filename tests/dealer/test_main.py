from datetime import datetime, timezone

from omegaconf import OmegaConf

from src.dealer import main as dealer_main
from src.dealer.main import market_is_open, should_process_entry


def _cfg(enable_stocks: bool, enable_crypto: bool):
    return OmegaConf.create({"trading": {"enable_stocks": enable_stocks, "enable_crypto": enable_crypto}})


def _trading_cfg(market_override=False, buffer=0):
    return OmegaConf.create({"trading": {"market_override": market_override, "buffer": buffer}})


class FakeClock:
    def __init__(self, is_open):
        self.is_open = is_open
        self.next_open = datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc)
        self.next_close = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)


class FakeTradingClient:
    def __init__(self, is_open):
        self._clock = FakeClock(is_open)

    def get_clock(self):
        return self._clock


def test_stock_entry_processed_only_when_stocks_enabled():
    entry = {"symbol": "MGN", "exchange": "stocks"}

    assert should_process_entry(entry, _cfg(enable_stocks=True, enable_crypto=True)) is True
    assert should_process_entry(entry, _cfg(enable_stocks=False, enable_crypto=True)) is False


def test_crypto_entry_processed_only_when_crypto_enabled():
    entry = {"symbol": "BTC/USD", "exchange": "binance"}

    assert should_process_entry(entry, _cfg(enable_stocks=True, enable_crypto=True)) is True
    assert should_process_entry(entry, _cfg(enable_stocks=True, enable_crypto=False)) is False


def test_market_closed_posts_slack_notice_on_first_check(monkeypatch):
    monkeypatch.setattr(dealer_main, "_last_market_open", None)
    monkeypatch.setattr(dealer_main, "trading_client", FakeTradingClient(is_open=False))
    posted = {}
    monkeypatch.setattr(dealer_main.slack, "notify_stock_market_closed", lambda next_open: posted.update(next_open=next_open))

    result = market_is_open(_trading_cfg(), log=lambda *a: None)

    assert result is False
    assert "next_open" in posted


def test_market_closed_does_not_repeat_notice_while_still_closed(monkeypatch):
    monkeypatch.setattr(dealer_main, "_last_market_open", False)
    monkeypatch.setattr(dealer_main, "trading_client", FakeTradingClient(is_open=False))
    posted = {}
    monkeypatch.setattr(dealer_main.slack, "notify_stock_market_closed", lambda next_open: posted.update(next_open=next_open))

    market_is_open(_trading_cfg(), log=lambda *a: None)

    assert posted == {}


def test_market_closed_notice_fires_again_after_reopening(monkeypatch):
    monkeypatch.setattr(dealer_main, "_last_market_open", True)
    monkeypatch.setattr(dealer_main, "trading_client", FakeTradingClient(is_open=False))
    posted = {}
    monkeypatch.setattr(dealer_main.slack, "notify_stock_market_closed", lambda next_open: posted.update(next_open=next_open))

    market_is_open(_trading_cfg(), log=lambda *a: None)

    assert "next_open" in posted


def test_market_open_does_not_post_closed_notice(monkeypatch):
    monkeypatch.setattr(dealer_main, "_last_market_open", False)
    monkeypatch.setattr(dealer_main, "trading_client", FakeTradingClient(is_open=True))
    posted = {}
    monkeypatch.setattr(dealer_main.slack, "notify_stock_market_closed", lambda next_open: posted.update(next_open=next_open))

    result = market_is_open(_trading_cfg(buffer=0), log=lambda *a: None)

    assert result is True
    assert posted == {}


def test_market_override_does_not_post_closed_notice(monkeypatch):
    monkeypatch.setattr(dealer_main, "_last_market_open", False)
    posted = {}
    monkeypatch.setattr(dealer_main.slack, "notify_stock_market_closed", lambda next_open: posted.update(next_open=next_open))

    result = market_is_open(_trading_cfg(market_override=True), log=lambda *a: None)

    assert result is True
    assert posted == {}
