import os
from datetime import datetime, timedelta

import requests
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from bs4 import BeautifulSoup

from src.common.alpaca_client import get_current_ask_price
from src.common.logging import get_logger

log = get_logger("ANALYST")

SCREENER_BASE_URL = "https://data.alpaca.markets/v1beta1/screener/stocks"
CRYPTO_SCREENER_BASE_URL = "https://data.alpaca.markets/v1beta1/screener/crypto"
CRYPTO_WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD"]
FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"


def html_to_text(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator="\n", strip=True)


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_PAPER_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_PAPER_API_SECRET", ""),
    }


def fetch_screener_candidates(top_n: int, min_price: float = 0.0) -> list[dict]:
    # Screener endpoints aren't wrapped by alpaca-py yet, so this hits the REST API directly.
    endpoints = [
        f"{SCREENER_BASE_URL}/most-actives?by=volume&top={top_n}",
        f"{SCREENER_BASE_URL}/movers?top={top_n}",
    ]

    by_symbol: dict[str, dict] = {}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=_alpaca_headers(), timeout=10)
        except requests.RequestException as exc:
            log(f"⚠️ screener request failed for {url}: {exc}")
            continue

        if resp.status_code != 200:
            log(f"⚠️ screener returned {resp.status_code} for {url}: {resp.text[:200]}")
            continue

        payload = resp.json()
        # most-actives -> {"most_actives": [...]}, movers -> {"gainers": [...], "losers": [...]}
        raw_items = []
        for key in ("most_actives", "gainers", "losers"):
            raw_items.extend(payload.get(key, []))

        for item in raw_items:
            symbol = item.get("symbol")
            if not symbol:
                continue
            entry = by_symbol.setdefault(symbol, {"symbol": symbol})
            if "volume" in item:
                entry["volume"] = item["volume"]
            if "change" in item or "percent_change" in item:
                entry["change_pct"] = item.get("percent_change", item.get("change"))
            if "price" in item:
                entry["price"] = item["price"]

    # Only "movers" items carry a price -- "most-actives" doesn't, so a candidate sourced only from
    # most-actives needs an explicit quote lookup before it can be judged against min_price; letting
    # it through unpriced previously let sub-$1 symbols (e.g. a warrant) reach the LLM undetected.
    # Below-min-price candidates are dropped here (rather than left for Floor Broker to discover)
    # since sub-$1 stocks can violate execution.py's SL/TP bracket invariants (see
    # bracket_buy_with_SLTP's $0.02 floor/ceiling clamp) and are cheap to filter out before they
    # ever reach the LLM.
    all_candidates = list(by_symbol.values())
    for c in all_candidates:
        if c.get("price") is None:
            try:
                c["price"] = get_current_ask_price(c["symbol"])
            except Exception as exc:
                log(f"⚠️ failed to fetch a reference price for {c['symbol']}: {exc}")

    candidates = [c for c in all_candidates if c.get("price") is not None and c["price"] >= min_price]
    dropped = len(all_candidates) - len(candidates)
    if dropped:
        log(f"📉 dropped {dropped} screener candidate(s) below ${min_price:.2f} min price (or price unavailable)")
    log(f"📈 fetched {len(candidates)} screener candidates")
    return candidates


def fetch_crypto_candidates(top_n: int) -> list[dict]:
    # Alpaca's crypto screener only exposes movers (no most-actives equivalent for crypto), so a
    # small fixed watchlist guarantees baseline coverage of major pairs regardless of movers data.
    by_symbol: dict[str, dict] = {symbol: {"symbol": symbol} for symbol in CRYPTO_WATCHLIST}

    url = f"{CRYPTO_SCREENER_BASE_URL}/movers?top={top_n}"
    try:
        resp = requests.get(url, headers=_alpaca_headers(), timeout=10)
    except requests.RequestException as exc:
        log(f"⚠️ crypto screener request failed for {url}: {exc}")
        resp = None

    if resp is not None:
        if resp.status_code != 200:
            log(f"⚠️ crypto screener returned {resp.status_code} for {url}: {resp.text[:200]}")
        else:
            payload = resp.json()
            for key in ("gainers", "losers"):
                for item in payload.get(key, []):
                    symbol = item.get("symbol")
                    if not symbol:
                        continue
                    entry = by_symbol.setdefault(symbol, {"symbol": symbol})
                    if "change" in item or "percent_change" in item:
                        entry["change_pct"] = item.get("percent_change", item.get("change"))

    candidates = list(by_symbol.values())
    log(f"📈 fetched {len(candidates)} crypto screener candidates")
    return candidates


def fetch_news(days: int, limit: int = 20) -> str:
    client = NewsClient(
        api_key=os.getenv("ALPACA_PAPER_API_KEY"),
        secret_key=os.getenv("ALPACA_PAPER_API_SECRET"),
        raw_data=True,
    )

    req = NewsRequest(
        start=datetime.now() - timedelta(days=days),
        end=datetime.now(),
        limit=limit,
        include_content=True,
    )

    articles = client.get_news(req)["news"]

    feed = ""
    for a in articles:
        feed += f"{a['headline']}:{html_to_text(a['content'])}\n"

    log(f"📰 fetched {len(articles)} news items")
    return feed


def fetch_earnings_calendar(symbols: list[str], days_before: int, days_after: int) -> set[str]:
    """Returns the subset of `symbols` currently inside their earnings blackout window, i.e. any
    symbol with a scheduled Finnhub earnings report date R such that today is in
    [R - days_after, R + days_before]. Queries Finnhub's free-tier calendar/earnings endpoint
    once per call (no `symbol` param -- the market-wide calendar for the date window), rather
    than once per candidate, to stay well inside the free tier's 250 calls/day budget (this runs
    once per Analyst invocation, so ~1-2 calls/day at the default schedule). Fails soft on
    any error (missing key, network error, non-200, rate limit, bad JSON): logs a warning and
    returns an empty set, so a Finnhub outage degrades to "no earnings filter this run," never a
    crashed CronJob or a fully blocked pick list."""
    if not symbols:
        return set()

    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        log("⚠️ FINNHUB_API_KEY not set -- skipping earnings blackout filter")
        return set()

    today = datetime.now().date()
    from_date = today - timedelta(days=days_after)
    to_date = today + timedelta(days=days_before)
    params = {"from": from_date.isoformat(), "to": to_date.isoformat(), "token": api_key}

    try:
        resp = requests.get(FINNHUB_EARNINGS_URL, params=params, timeout=10)
    except requests.RequestException as exc:
        log(f"⚠️ finnhub earnings-calendar request failed: {exc}")
        return set()

    if resp.status_code == 429:
        log("⚠️ finnhub earnings-calendar rate-limited -- skipping earnings blackout filter for this run")
        return set()
    if resp.status_code != 200:
        log(f"⚠️ finnhub earnings-calendar returned {resp.status_code}: {resp.text[:200]}")
        return set()

    try:
        payload = resp.json()
    except ValueError as exc:
        log(f"⚠️ finnhub earnings-calendar returned invalid JSON: {exc}")
        return set()

    wanted = set(symbols)
    blackout = {e["symbol"] for e in payload.get("earningsCalendar", []) if e.get("symbol") in wanted}
    if blackout:
        log(f"📅 {len(blackout)}/{len(wanted)} candidate(s) in earnings blackout window ({from_date}..{to_date})")
    return blackout


def fetch_yahoo_rss_headlines(url: str, limit: int = 10) -> str:
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException as exc:
        log(f"⚠️ yahoo rss request failed: {exc}")
        return ""

    if resp.status_code != 200:
        log(f"⚠️ yahoo rss returned {resp.status_code}")
        return ""

    try:
        soup = BeautifulSoup(resp.text, "xml")
    except Exception:
        soup = BeautifulSoup(resp.text, "html.parser")

    titles = [t.get_text(strip=True) for t in soup.find_all("title")][:limit]
    return "\n".join(titles)
