import json

from src.pl_badges import main


def test_market_closed_leaves_badges_dir_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: False)
    monkeypatch.setattr(main, "BADGES_DIR", tmp_path / "badges")

    main.main()

    assert not (tmp_path / "badges").exists()


def test_open_market_writes_both_badge_files(monkeypatch, tmp_path):
    badges_dir = tmp_path / "badges"
    monkeypatch.setattr(main, "is_stock_market_open", lambda day: True)
    monkeypatch.setattr(main, "BADGES_DIR", badges_dir)
    monkeypatch.setattr(main, "fetch_pl_summary", lambda: {"today_pl": 50.0, "ytd_pl": -100.0})

    main.main()

    today = json.loads((badges_dir / "today-pl.json").read_text())
    ytd = json.loads((badges_dir / "ytd-pl.json").read_text())
    assert today == {"schemaVersion": 1, "label": "Today's P/L", "message": "+$50.00", "color": "brightgreen"}
    assert ytd == {"schemaVersion": 1, "label": "YTD P/L", "message": "-$100.00", "color": "red"}
