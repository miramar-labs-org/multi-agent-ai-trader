from typing import List, Literal

from pydantic import BaseModel, Field


class CandidateResearch(BaseModel):
    symbol: str
    exchange: Literal["stocks"] = "stocks"  # crypto plumbed through end-to-end but out of scope for v1 screening
    budget: float
    indicators: List[str]
    rationale: str = Field(description="One-line reason this candidate made the book")


class PortfolioSelection(BaseModel):
    """Structured output of the Analyst's LLM selection node — written to the `portfolio` ConfigMap."""

    symbols: List[CandidateResearch]
