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


def test_confidence_defaults_to_1_when_omitted():
    """Older/simpler LLM outputs that don't set confidence must not be silently gated out."""
    signal = Signal(symbol="DFNS", action="BUY", reasoning="test")
    assert signal.confidence == 1.0


def test_confidence_rejects_out_of_range_values():
    with pytest.raises(ValidationError):
        Signal(symbol="DFNS", action="BUY", reasoning="test", confidence=1.5)


def test_confidence_accepts_valid_fraction():
    signal = Signal(symbol="DFNS", action="BUY", reasoning="test", confidence=0.4)
    assert signal.confidence == 0.4
