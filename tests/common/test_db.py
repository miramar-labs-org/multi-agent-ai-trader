from datetime import date, datetime

import pytest

from src.common import db


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnection:
    def __init__(self, rows=None, raise_on_execute=None):
        self.executed = []
        self._rows = rows or []
        self._raise_on_execute = raise_on_execute

    def execute(self, sql, params=None):
        if self._raise_on_execute:
            raise self._raise_on_execute
        self.executed.append((sql, params))

    def cursor(self, row_factory=None):
        return FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def _patch_pool(monkeypatch, conn):
    monkeypatch.setattr(db, "_schema_ready", True)
    monkeypatch.setattr(db, "_get_pool", lambda: FakePool(conn))


def test_ensure_schema_runs_only_once(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(db, "_schema_ready", False)
    monkeypatch.setattr(db, "_get_pool", lambda: FakePool(conn))

    db._ensure_schema()
    db._ensure_schema()

    assert len(conn.executed) == 1
    assert db._schema_ready is True


def test_record_dealer_decision_inserts_expected_row(monkeypatch):
    conn = FakeConnection()
    _patch_pool(monkeypatch, conn)

    db.record_dealer_decision("MGN", "BUY", "strong momentum", 5.0)

    sql, params = conn.executed[0]
    assert "INSERT INTO dealer_decisions" in sql
    assert params == ("MGN", "BUY", "strong momentum", 5.0)


def test_record_analyst_pick_inserts_expected_row(monkeypatch):
    conn = FakeConnection()
    _patch_pool(monkeypatch, conn)
    generated_at = datetime(2026, 8, 4, 9, 30)

    db.record_analyst_pick("MGN", "NASDAQ", 100.0, "screener pick", generated_at)

    sql, params = conn.executed[0]
    assert "INSERT INTO analyst_picks" in sql
    assert params == (generated_at, "MGN", "NASDAQ", 100.0, "screener pick")


def test_record_floor_broker_event_inserts_expected_row(monkeypatch):
    conn = FakeConnection()
    _patch_pool(monkeypatch, conn)

    db.record_floor_broker_event("MGN", "buy_submitted", "order abc123", qty=10, price=5.5)

    sql, params = conn.executed[0]
    assert "INSERT INTO floor_broker_events" in sql
    assert params == ("MGN", "buy_submitted", "order abc123", 10, 5.5)


def test_record_position_opened_inserts_expected_row(monkeypatch):
    conn = FakeConnection()
    _patch_pool(monkeypatch, conn)

    db.record_position_opened("MGN")

    sql, params = conn.executed[0]
    assert "INSERT INTO position_opens" in sql
    assert "ON CONFLICT (symbol) DO NOTHING" in sql
    assert params == ("MGN",)


def test_record_position_closed_deletes_expected_row(monkeypatch):
    conn = FakeConnection()
    _patch_pool(monkeypatch, conn)

    db.record_position_closed("MGN")

    sql, params = conn.executed[0]
    assert "DELETE FROM position_opens" in sql
    assert params == ("MGN",)


def test_record_eod_report_sent_inserts_expected_row(monkeypatch):
    conn = FakeConnection()
    _patch_pool(monkeypatch, conn)
    report_date = date(2026, 8, 6)

    db.record_eod_report_sent(report_date)

    sql, params = conn.executed[0]
    assert "INSERT INTO eod_report_runs" in sql
    assert "ON CONFLICT (report_date) DO NOTHING" in sql
    assert params == (report_date,)


def test_eod_report_already_sent_returns_true_when_row_exists(monkeypatch):
    conn = FakeConnection(rows=[{"?column?": 1}])
    _patch_pool(monkeypatch, conn)

    result = db.eod_report_already_sent(date(2026, 8, 6))

    assert result is True


def test_eod_report_already_sent_returns_false_when_no_row_exists(monkeypatch):
    conn = FakeConnection(rows=[])
    _patch_pool(monkeypatch, conn)

    result = db.eod_report_already_sent(date(2026, 8, 6))

    assert result is False


def test_eod_report_already_sent_fails_open_on_db_error(monkeypatch):
    monkeypatch.setattr(db, "_ensure_schema", lambda: (_ for _ in ()).throw(RuntimeError("db is down")))

    result = db.eod_report_already_sent(date(2026, 8, 6))

    assert result is False


def test_fetch_position_opened_at_returns_timestamp_when_tracked(monkeypatch):
    opened_at = datetime(2026, 8, 1, 9, 30)
    conn = FakeConnection(rows=[{"opened_at": opened_at}])
    _patch_pool(monkeypatch, conn)

    result = db.fetch_position_opened_at("MGN")

    assert result == opened_at


def test_fetch_position_opened_at_returns_none_when_untracked(monkeypatch):
    conn = FakeConnection(rows=[])
    _patch_pool(monkeypatch, conn)

    result = db.fetch_position_opened_at("MGN")

    assert result is None


@pytest.mark.parametrize(
    "record_fn,args",
    [
        (db.record_analyst_pick, ("MGN", "NASDAQ", 100.0, "rationale", datetime(2026, 8, 4))),
        (db.record_dealer_decision, ("MGN", "BUY", "reasoning", 5.0)),
        (db.record_floor_broker_event, ("MGN", "error", "boom")),
        (db.record_position_opened, ("MGN",)),
        (db.record_position_closed, ("MGN",)),
        (db.record_eod_report_sent, (date(2026, 8, 6),)),
    ],
)
def test_write_functions_swallow_exceptions(monkeypatch, record_fn, args):
    conn = FakeConnection(raise_on_execute=RuntimeError("db is down"))
    _patch_pool(monkeypatch, conn)

    record_fn(*args)  # must not raise


def test_fetch_dealer_decisions_for_date_returns_list_of_dicts(monkeypatch):
    rows = [{"id": 1, "symbol": "MGN", "action": "BUY", "reasoning": "momentum"}]
    conn = FakeConnection(rows=rows)
    _patch_pool(monkeypatch, conn)

    result = db.fetch_dealer_decisions_for_date(date(2026, 8, 4))

    assert result == rows


def test_fetch_analyst_picks_for_date_returns_list_of_dicts(monkeypatch):
    rows = [{"id": 1, "symbol": "MGN", "rationale": "screener pick"}]
    conn = FakeConnection(rows=rows)
    _patch_pool(monkeypatch, conn)

    result = db.fetch_analyst_picks_for_date(date(2026, 8, 4))

    assert result == rows


def test_fetch_floor_broker_events_for_date_returns_list_of_dicts(monkeypatch):
    rows = [{"id": 1, "symbol": "MGN", "event_type": "buy_submitted"}]
    conn = FakeConnection(rows=rows)
    _patch_pool(monkeypatch, conn)

    result = db.fetch_floor_broker_events_for_date(date(2026, 8, 4))

    assert result == rows


def test_fetch_analyst_picks_since_returns_list_of_dicts(monkeypatch):
    rows = [{"id": 1, "symbol": "MGN", "rationale": "screener pick"}]
    conn = FakeConnection(rows=rows)
    _patch_pool(monkeypatch, conn)

    result = db.fetch_analyst_picks_since(date(2026, 8, 1))

    assert result == rows


def test_fetch_dealer_decisions_since_returns_list_of_dicts(monkeypatch):
    rows = [{"id": 1, "symbol": "MGN", "action": "BUY", "reasoning": "momentum"}]
    conn = FakeConnection(rows=rows)
    _patch_pool(monkeypatch, conn)

    result = db.fetch_dealer_decisions_since(date(2026, 8, 1))

    assert result == rows


def test_fetch_floor_broker_events_since_returns_list_of_dicts(monkeypatch):
    rows = [{"id": 1, "symbol": "MGN", "event_type": "buy_submitted"}]
    conn = FakeConnection(rows=rows)
    _patch_pool(monkeypatch, conn)

    result = db.fetch_floor_broker_events_since(date(2026, 8, 1))

    assert result == rows
