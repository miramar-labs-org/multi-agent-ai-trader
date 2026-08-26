import os

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, CryptoLatestQuoteRequest, OptionLatestQuoteRequest

from src.common.symbols import canonical_crypto_symbol, is_usd_crypto_symbol

ALPACA_API_KEY = os.getenv("ALPACA_PAPER_API_KEY")
ALPACA_API_SECRET = os.getenv("ALPACA_PAPER_API_SECRET")

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_API_SECRET, paper=True)
stock_data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_API_SECRET)
crypto_data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_API_SECRET)

ALPACA_API_KEY2 = os.getenv("ALPACA_PAPER_API_KEY2")
ALPACA_API_SECRET2 = os.getenv("ALPACA_PAPER_API_SECRET2")

trading_client2 = TradingClient(ALPACA_API_KEY2, ALPACA_API_SECRET2, paper=True)
option_data_client2 = OptionHistoricalDataClient(ALPACA_API_KEY2, ALPACA_API_SECRET2)


def get_current_ask_price(symbol: str) -> float:
    if "/" in symbol or is_usd_crypto_symbol(symbol):
        symbol = canonical_crypto_symbol(symbol)
        quote = crypto_data_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
    else:
        quote = stock_data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
    return quote[symbol].ask_price


def get_current_bid_price(symbol: str) -> float:
    if "/" in symbol or is_usd_crypto_symbol(symbol):
        symbol = canonical_crypto_symbol(symbol)
        quote = crypto_data_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
    else:
        quote = stock_data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
    return quote[symbol].bid_price


def get_current_option_mid_price(contract_symbol: str) -> float:
    quote = option_data_client2.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
    )
    q = quote[contract_symbol]
    return (q.bid_price + q.ask_price) / 2


def get_current_option_ask_price(contract_symbol: str) -> float:
    quote = option_data_client2.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
    )
    return quote[contract_symbol].ask_price
