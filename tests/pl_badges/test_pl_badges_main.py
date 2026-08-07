import json
from datetime import date

from src.pl_badges import main


def _use_tmp_badges_dir(monkeypatch, tmp_path):
    badges_dir = tmp_path / "badges"
    monkeypatch.setattr(main, "BADGES_DIR", badges_dir)
    monkeypatch.setattr(main, "HISTORY_FILE", badges_dir / "pl_history.json")
    return badges_dir


def test_market_closed_leaves_badges_dir_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: False)
    badges_dir = _use_tmp_badges_dir(monkeypatch, tmp_path)

    main.main()

    assert not badges_dir.exists()


def test_open_market_writes_both_badge_files(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: True)
    badges_dir = _use_tmp_badges_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "fetch_pl_summary", lambda history_pl: {"today_pl": 50.0, "ytd_pl": -100.0})

    main.main()

    today = json.loads((badges_dir / "today-pl.json").read_text())
    ytd = json.loads((badges_dir / "ytd-pl.json").read_text())
    assert today == {"schemaVersion": 1, "label": "Today's P/L", "message": "+$50.00", "color": "brightgreen"}
    assert ytd == {"schemaVersion": 1, "label": "YTD P/L", "message": "-$100.00", "color": "red"}


def test_open_market_persists_todays_pl_into_the_history_file(monkeypatch, tmp_path):
    badges_dir = _use_tmp_badges_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: True)
    monkeypatch.setattr(main, "fetch_pl_summary", lambda history_pl: {"today_pl": 50.0, "ytd_pl": -100.0})

    main.main()

    history = json.loads((badges_dir / "pl_history.json").read_text())
    assert history == {date.today().isoformat(): 50.0}


def test_open_market_merges_todays_pl_with_existing_history(monkeypatch, tmp_path):
    badges_dir = _use_tmp_badges_dir(monkeypatch, tmp_path)
    badges_dir.mkdir()
    (badges_dir / "pl_history.json").write_text(json.dumps({"2026-08-05": -100.0, "2026-08-06": 50.0}))
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: True)
    received = {}

    def _fake_fetch(history_pl):
        received["history_pl"] = dict(history_pl)
        return {"today_pl": -20.0, "ytd_pl": -70.0}

    monkeypatch.setattr(main, "fetch_pl_summary", _fake_fetch)

    main.main()

    assert received["history_pl"] == {"2026-08-05": -100.0, "2026-08-06": 50.0}
    history = json.loads((badges_dir / "pl_history.json").read_text())
    assert history == {"2026-08-05": -100.0, "2026-08-06": 50.0, date.today().isoformat(): -20.0}
