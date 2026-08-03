import pytest
from pydantic import ValidationError

from src.dealer.schema import Signal


def test_size_hint_rejects_out_of_range_values():
    """Regression for the DFNS crash: the local LLM once returned size_hint=1000 (a share
    count/dollar amount, not a 0-1 fraction) and Pydantic raised uncaught mid-poll. size_hint
    must stay bounded to a fraction of the symbol's budget."""
    with pytest.raises(ValidationError):
        Signal(symbol="DFNS", action="BUY", reasoning="test", size_hint=1000)


def test_size_hint_accepts_valid_fraction():
    signal = Signal(symbol="DFNS", action="BUY", reasoning="test", size_hint=0.5)
    assert signal.size_hint == 0.5
