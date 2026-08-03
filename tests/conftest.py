import os

# src/common/alpaca_client.py builds a TradingClient at import time -- it doesn't make any
# network calls until a request is issued, but it does require non-empty credentials to
# construct, so tests need dummy values present before anything under src/ is imported.
os.environ.setdefault("ALPACA_PAPER_API_KEY", "test")
os.environ.setdefault("ALPACA_PAPER_API_SECRET", "test")
