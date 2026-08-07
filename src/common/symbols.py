USD_CRYPTO_SUFFIX = "USD"
KNOWN_USD_CRYPTO_BASES = {
    "AAVE",
    "AVAX",
    "BCH",
    "BTC",
    "DOGE",
    "ETH",
    "LINK",
    "LTC",
    "SHIB",
    "SOL",
    "UNI",
}


def canonical_crypto_symbol(symbol: str) -> str:
    """Return the app's canonical Alpaca USD crypto pair form, e.g. BTCUSD -> BTC/USD."""
    normalized = symbol.strip().upper()
    if "/" in normalized:
        return normalized
    if normalized.endswith(USD_CRYPTO_SUFFIX) and len(normalized) > len(USD_CRYPTO_SUFFIX):
        base = normalized[: -len(USD_CRYPTO_SUFFIX)]
        if base in KNOWN_USD_CRYPTO_BASES:
            return f"{base}/{USD_CRYPTO_SUFFIX}"
    return normalized


def alpaca_order_symbol(symbol: str) -> str:
    """Return Alpaca Trading API's position/order lookup form for any canonical app symbol."""
    return symbol.replace("/", "").upper()


def is_usd_crypto_symbol(symbol: str) -> bool:
    return canonical_crypto_symbol(symbol).endswith("/USD")
