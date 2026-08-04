import re
from typing import Literal

from alpaca.common.exceptions import APIError
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.common import slack
from src.common.logging import get_logger
from src.floor_broker import execution

log = get_logger("FLOOR")

# Sanity ceiling on a single order's authorized budget (ROADMAP P0.3) -- not a business rule,
# just a last-line-of-defense guard against a units/config bug or a hallucinated Analyst budget
# (analyst/schema.py's `budget` field has no upper bound of its own) reaching Alpaca as a live
# order. 20x config.yaml's analyst.default_budget (5000).
MAX_BUDGET = 100_000.0

# Alpaca tickers: letters/digits, with an optional single "/" for crypto pairs (e.g. "BTC/USD")
# or "." for dual-class shares and warrants/units (e.g. "BRK.B", "DSX.WS") -- both come straight
# from Alpaca's own screener/assets universe, so Floor Broker must accept whatever shape Alpaca
# itself vends rather than second-guessing it; a genuinely bad symbol still gets a clean rejection
# from Alpaca's own API (caught as an APIError) instead of a client-side ValueError here.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}([./][A-Z0-9]{1,10})?$")
# "stocks", or a TAAPI venue identifier (e.g. "binance") -- config-driven (cfg.trading.
# crypto_taapi_exchange), so this validates shape, not a fixed enum of known venues.
_EXCHANGE_RE = re.compile(r"^[a-z0-9_-]+$")


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    exchange: str
    action: Literal["BUY", "SELL"]
    budget: float = Field(gt=0, le=MAX_BUDGET)
    slP: float = Field(gt=0, lt=1)
    tpP: float = Field(gt=1, lt=2)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not _SYMBOL_RE.match(v):
            raise ValueError(f"invalid symbol: {v!r}")
        return v

    @field_validator("exchange")
    @classmethod
    def _normalize_exchange(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EXCHANGE_RE.match(v):
            raise ValueError(f"invalid exchange: {v!r}")
        return v


class ExecuteResponse(BaseModel):
    status: Literal["executed", "submitted", "skipped", "error"]
    detail: str
    reason: str | None = None
    order_id: str | None = None
    fill_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None


app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest):
    try:
        if req.action == "BUY":
            result = execution.buy(req.symbol, req.exchange, req.budget, req.slP, req.tpP)
        else:
            result = execution.sell(req.symbol)
        return ExecuteResponse(**result)
    except APIError as exc:
        log(f"💥  {req.action} {req.symbol} failed: {exc}")
        return ExecuteResponse(status="error", detail=str(exc))
    except Exception as exc:
        log(f"💥  unexpected error on {req.action} {req.symbol}: {exc}")
        slack.notify_error("FLOOR", f"unexpected error on {req.action} {req.symbol}: {exc}")
        raise
