from typing import Literal

from alpaca.common.exceptions import APIError
from fastapi import FastAPI
from pydantic import BaseModel

from src.common import slack
from src.common.logging import get_logger
from src.floor_broker import execution

log = get_logger("FLOOR")


class ExecuteRequest(BaseModel):
    symbol: str
    exchange: str
    action: Literal["BUY", "SELL"]
    budget: float
    slP: float
    tpP: float


class ExecuteResponse(BaseModel):
    status: Literal["executed", "skipped", "error"]
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
