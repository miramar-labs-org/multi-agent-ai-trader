import os
from datetime import datetime, timedelta

import requests
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from bs4 import BeautifulSoup

from src.common.logging import get_logger

log = get_logger("ANALYST")

SCREENER_BASE_URL = "https://data.alpaca.markets/v1beta1/screener/stocks"


def html_to_text(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator="\n", strip=True)


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_PAPER_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_PAPER_API_SECRET", ""),
    }


def fetch_screener_candidates(top_n: int) -> list[dict]:
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

    candidates = list(by_symbol.values())
    log(f"📈 fetched {len(candidates)} screener candidates")
    return candidates


def fetch_news(days: int, limit: int = 20) -> str:
    client = NewsClient(
        api_key=os.getenv("ALPACA_PAPER_API_KEY"),
        secret_key=os.getenv("ALPACA_PAPER_API_SECRET"),
    )

    req = NewsRequest(
        start=datetime.now() - timedelta(days=days),
        end=datetime.now(),
        limit=limit,
        include_content=True,
        raw_data=True,
    )

    articles = client.get_news(req)["news"]

    feed = ""
    for a in articles:
        feed += f"{a['headline']}:{html_to_text(a['content'])}\n"

    log(f"📰 fetched {len(articles)} news items")
    return feed


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
