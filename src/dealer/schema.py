from typing import Literal

from pydantic import BaseModel, Field


class Signal(BaseModel):
    """Structured output of the Dealer's LLM call — replaces gpt-trader.py's xtractjson() hack.
    The Dealer's graph branches on `action` via a deterministic edge; the LLM never calls
    Floor Broker directly."""

    symbol: str
    action: Literal["BUY", "HOLD", "SELL"]
    reasoning: str = Field(description="Explanation citing the indicators and news feed that led to this decision")
    size_hint: float = Field(default=1.0, ge=0.0, le=1.0, description="Fraction of the symbol's budget to deploy on a BUY")
