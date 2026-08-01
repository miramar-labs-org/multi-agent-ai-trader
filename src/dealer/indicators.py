import os

import requests


def build_props(indicators_cfg, indicator_name: str) -> str:
    for indicator in indicators_cfg:
        if indicator["name"] == indicator_name:
            return "".join(f"&{k}={v}" for k, v in indicator["properties"].items())
    return ""


def _taapi_url(ind: str, secret: str, symbol: str, exchange: str, props: str) -> str:
    if exchange != "stocks":
        return f"https://api.taapi.io/{ind}?secret={secret}&exchange={exchange}&symbol={symbol}{props}"
    return f"https://api.taapi.io/{ind}?secret={secret}&type=stocks&symbol={symbol}{props}"


def fetch_rsi(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "rsi"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        log(f"📈 rsi: {response.json()['value']}")
        return f"The current Relative Strength Index (RSI) for {symbol} is {response.json()['value']}"
    e = f"⚠️ rsi error: {response.status_code}"
    log(e)
    return e


def fetch_sma(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "sma"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        log(f"📈 sma: {response.json()['value']}")
        return f"The current Simple Moving Average (SMA) for {symbol} is {response.json()['value']}"
    e = f"⚠️ sma error: {response.status_code}"
    log(e)
    return e


def fetch_ema(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "ema"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        log(f"📈 ema: {response.json()['value']}")
        return f"The current Exponential Moving Average (EMA) for {symbol} is {response.json()['value']}"
    e = f"⚠️ ema error: {response.status_code}"
    log(e)
    return e


def fetch_macd(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "macd"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        body = response.json()
        log(f"📈 macd: {body['valueMACD']}")
        log(f"📈 macdsig: {body['valueMACDSignal']}")
        log(f"📈 macdhist: {body['valueMACDHist']}")
        return (
            f"The current Moving Average convergence Divergence (MACD) values for {symbol} are "
            f"MACD {body['valueMACD']}, MACD Signal {body['valueMACDSignal']}, MACD History{body['valueMACDHist']}"
        )
    e = f"⚠️ macd error: {response.status_code}"
    log(e)
    return e


def fetch_bbands(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "bbands"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        body = response.json()
        log(f"📈 bbandupper: {body['valueUpperBand']}")
        log(f"📈 bbandmiddle: {body['valueMiddleBand']}")
        log(f"📈 bbandlower: {body['valueLowerBand']}")
        return (
            f"The current Bollinger Bands for {symbol} are: Lower {body['valueLowerBand']}, "
            f"LowMiddleer {body['valueMiddleBand']}, Upper {body['valueUpperBand']}"
        )
    e = f"⚠️ bband error: {response.status_code}"
    log(e)
    return e


def fetch_volume(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "volume"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        log(f"📈 vol: {response.json()['value']}")
        return f"The current Volume for {symbol} is {response.json()['value']}"
    e = f"⚠️ vol error: {response.status_code}"
    log(e)
    return e


def fetch_vosc(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "vosc"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        log(f"📈 vosc: {response.json()['value']}")
        return f"The current Volume Oscillator for {symbol} is {response.json()['value']}"
    e = f"⚠️ vosc error: {response.status_code}"
    log(e)
    return e


def fetch_vwap(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "vwap"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        log(f"📈 vwap: {response.json()['value']}")
        return f"The current Volume Weighted Average Price (VWAP) for {symbol} is {response.json()['value']}"
    e = f"⚠️ vwap error: {response.status_code}"
    log(e)
    return e


def fetch_stochrsi(indicators_cfg, symbol: str, exchange: str, log) -> str:
    ind = "stochrsi"
    secret = os.getenv("TAAPI_API_KEY")
    props = build_props(indicators_cfg, ind)
    url = _taapi_url(ind, secret, symbol, exchange, props)

    response = requests.get(url)
    if response.status_code == 200:
        body = response.json()
        log(f"📈 srsiFK: {body['valueFastK']}")
        log(f"📈 srsiFD: {body['valueFastD']}")
        return (
            f"The current Stochastic Relative Strength Index values for {symbol} are: "
            f"FastK {body['valueFastK']}, FastD {body['valueFastD']}"
        )
    e = f"⚠️ srsi error: {response.status_code}"
    log(e)
    return e


INDICATOR_MAP = {
    "rsi": fetch_rsi,
    "stochrsi": fetch_stochrsi,
    "sma": fetch_sma,
    "ema": fetch_ema,
    "volume": fetch_volume,
    "vosc": fetch_vosc,
    "macd": fetch_macd,
    "bbands": fetch_bbands,
    "vwap": fetch_vwap,
}
