from omegaconf import OmegaConf

from src.analyst import main


class FakeGraph:
    def __init__(self, captured):
        self._captured = captured

    def invoke(self, state, config=None):
        self._captured["state"] = state
        return {"selection": {"symbols": []}}


def test_main_threads_stock_market_open_true_into_initial_state(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: True)
    monkeypatch.setattr(main, "build_graph", lambda: FakeGraph(captured))
    monkeypatch.setattr(main, "load_config", lambda: OmegaConf.create({}))
    monkeypatch.setattr(main.langsmith, "configure", lambda cfg: None)

    main.main()

    assert captured["state"]["stock_market_open"] is True


def test_main_threads_stock_market_open_false_into_initial_state_on_a_closed_day(monkeypatch):
    """Regression: Analyst must not skip its whole run on a closed stock market day -- crypto
    still trades 24/7 -- so the closed status is threaded into the graph state rather than
    short-circuiting main() the way EOD Report does."""
    captured = {}
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: False)
    monkeypatch.setattr(main, "build_graph", lambda: FakeGraph(captured))
    monkeypatch.setattr(main, "load_config", lambda: OmegaConf.create({}))
    monkeypatch.setattr(main.langsmith, "configure", lambda cfg: None)

    main.main()

    assert captured["state"]["stock_market_open"] is False
