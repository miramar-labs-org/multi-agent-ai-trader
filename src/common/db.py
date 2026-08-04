import os
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.common.logging import get_logger

log = get_logger("DB")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyst_picks (
    id SERIAL PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT,
    budget NUMERIC,
    rationale TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dealer_decisions (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    reasoning TEXT,
    size_hint NUMERIC,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS floor_broker_events (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT,
    qty NUMERIC,
    price NUMERIC,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dealer_decisions_symbol_date ON dealer_decisions (symbol, decided_at);
CREATE INDEX IF NOT EXISTS idx_floor_broker_events_symbol_date ON floor_broker_events (symbol, occurred_at);
"""

_pool: ConnectionPool | None = None
_schema_ready = False


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        database_url = os.environ["DATABASE_URL"]
        _pool = ConnectionPool(database_url, min_size=1, max_size=5, open=True)
    return _pool


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _get_pool().connection() as conn:
        conn.execute(_SCHEMA)
    _schema_ready = True


def record_analyst_pick(
    symbol: str,
    exchange: str | None,
    budget: float | None,
    rationale: str | None,
    generated_at: datetime,
) -> None:
    """Fire-and-forget insert -- never raises, so a DB outage can't block the Analyst run."""
    try:
        _ensure_schema()
        with _get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO analyst_picks (generated_at, symbol, exchange, budget, rationale)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (generated_at, symbol, exchange, budget, rationale),
            )
    except Exception as exc:
        log(f"⚠️ record_analyst_pick failed: {exc}")


def record_dealer_decision(
    symbol: str,
    action: str,
    reasoning: str | None,
    size_hint: float | None,
) -> None:
    """Fire-and-forget insert -- never raises, so a DB outage can't block a Dealer decision."""
    try:
        _ensure_schema()
        with _get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO dealer_decisions (symbol, action, reasoning, size_hint)
                VALUES (%s, %s, %s, %s)
                """,
                (symbol, action, reasoning, size_hint),
            )
    except Exception as exc:
        log(f"⚠️ record_dealer_decision failed: {exc}")


def record_floor_broker_event(
    symbol: str,
    event_type: str,
    detail: str | None,
    qty: float | None = None,
    price: float | None = None,
) -> None:
    """Fire-and-forget insert -- never raises, so a DB outage can't block order execution."""
    try:
        _ensure_schema()
        with _get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO floor_broker_events (symbol, event_type, detail, qty, price)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (symbol, event_type, detail, qty, price),
            )
    except Exception as exc:
        log(f"⚠️ record_floor_broker_event failed: {exc}")


def fetch_analyst_picks_for_date(for_date: date) -> list[dict]:
    _ensure_schema()
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM analyst_picks WHERE generated_at::date = %s ORDER BY generated_at",
                (for_date,),
            )
            return cur.fetchall()


def fetch_dealer_decisions_for_date(for_date: date) -> list[dict]:
    _ensure_schema()
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM dealer_decisions WHERE decided_at::date = %s ORDER BY decided_at",
                (for_date,),
            )
            return cur.fetchall()


def fetch_floor_broker_events_for_date(for_date: date) -> list[dict]:
    _ensure_schema()
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM floor_broker_events WHERE occurred_at::date = %s ORDER BY occurred_at",
                (for_date,),
            )
            return cur.fetchall()
