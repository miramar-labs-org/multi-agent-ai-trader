import os

import requests

from src.common.config import load_config
from src.common.logging import get_logger

log = get_logger("SLACK")

_cfg = load_config()
_ENABLED = _cfg.slack.enabled
_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")


def _post(text: str) -> None:
    """Fire-and-forget POST to the Slack incoming webhook. Never raises -- a Slack
    outage must never affect a trading decision."""
    if not _ENABLED or not _WEBHOOK_URL:
        return
    try:
        resp = requests.post(_WEBHOOK_URL, json={"text": text}, timeout=10)
        if resp.status_code != 200:
            log(f"⚠️ non-200 from Slack webhook: {resp.status_code} {resp.text}")
    except requests.RequestException as exc:
        log(f"⚠️ Slack post failed: {exc}")


def notify_analyst_picks(symbols: list[dict]) -> None:
    if not symbols:
        _post("*Analyst* — daily run picked 0 symbols today.")
        return
    lines = [f"• *{s['symbol']}* (${s['budget']:.0f}) — {s['rationale']}" for s in symbols]
    _post(f"*Analyst* picked {len(symbols)} symbol(s) today:\n" + "\n".join(lines))


def notify_dealer_signal(symbol: str, action: str, reasoning: str) -> None:
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(action, "")
    _post(f"{emoji} *Dealer* — *{action}* {symbol}\n> {reasoning}")


def notify_floor_broker_result(symbol: str, action: str, status: str, detail: str) -> None:
    emoji = "✅" if status == "executed" else ("⚠️" if status == "skipped" else "❌")
    _post(f"{emoji} *Floor Broker* — {action} {symbol}: `{status}` — {detail}")


def notify_error(component: str, text: str) -> None:
    _post(f"🚨 *ERROR [{component}]* {text}")
