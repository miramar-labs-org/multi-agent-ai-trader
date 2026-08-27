import time
from datetime import datetime, timedelta

import pytz
import requests
from alpaca.trading.enums import AssetClass, PositionSide
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from src.common import slack
from src.common.alpaca_client import trading_client
from src.common.config import load_config
from src.common.logging import get_logger
from src.common.market_calendar import get_stock_market_hours

log = get_logger("POWER")

NAMESPACE = "multi-agent-ai-trader"
POLL_INTERVAL_S = 5
CRYPTO_FLAT_TIMEOUT_S = 60
OPTIONS_FLAT_TIMEOUT_S = 60
FLOOR_BROKER_READY_TIMEOUT_S = 60
OLLAMA_STOP_TIMEOUT_S = 10
OLLAMA_PRELOAD_TIMEOUT_S = 60
OLLAMA_PS_TIMEOUT_S = 10


def _now_eastern() -> datetime:
    return datetime.now(pytz.timezone("US/Eastern"))


def _target_replica_count(now: datetime, hours: tuple[datetime, datetime] | None, cfg) -> int:
    """1 if `now` falls inside today's [open - minutes_before_open, close + minutes_after_close]
    window, else 0 -- including when `hours` is None (today isn't a trading day at all)."""
    if hours is None:
        return 0
    open_dt, close_dt = hours
    window_start = open_dt - timedelta(minutes=cfg.power_schedule.minutes_before_open)
    window_end = close_dt + timedelta(minutes=cfg.power_schedule.minutes_after_close)
    return 1 if window_start <= now <= window_end else 0


def _apps_v1():
    k8s_config.load_incluster_config()
    return k8s_client.AppsV1Api()


def _get_replica_count(apps_v1, name: str) -> int:
    deployment = apps_v1.read_namespaced_deployment(name, NAMESPACE)
    return deployment.spec.replicas


def _scale(apps_v1, name: str, replicas: int) -> None:
    apps_v1.patch_namespaced_deployment_scale(name, NAMESPACE, {"spec": {"replicas": replicas}})
    log(f"⚙️  scaled {name} to {replicas} replica(s)")


def _wait_until_crypto_flat(timeout_s: int = CRYPTO_FLAT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        positions = [p for p in trading_client.get_all_positions() if p.asset_class == AssetClass.CRYPTO]
        if not positions:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def _wait_until_options_flat(timeout_s: int = OPTIONS_FLAT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        positions = [
            p
            for p in trading_client.get_all_positions()
            if p.asset_class == AssetClass.US_OPTION and getattr(p, "side", PositionSide.LONG) == PositionSide.LONG
        ]
        if not positions:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def _wait_until_floor_broker_ready(cfg, timeout_s: int = FLOOR_BROKER_READY_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            resp = requests.get(f"{cfg.floor_broker.base_url}/healthz", timeout=5)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def _ollama_native_url(cfg) -> str:
    """cfg.llm.base_url is the OpenAI-compat prefix (".../v1") used for inference; Ollama's
    native keep_alive control (stop/preload) lives at the API root, not under /v1."""
    return cfg.llm.base_url.removesuffix("/v1")


def _stop_ollama_model(cfg) -> None:
    try:
        resp = requests.post(
            f"{_ollama_native_url(cfg)}/api/generate",
            json={"model": cfg.llm.model, "keep_alive": 0},
            timeout=OLLAMA_STOP_TIMEOUT_S,
        )
        resp.raise_for_status()
        log(f"🧠  stopped ollama model {cfg.llm.model}")
    except requests.RequestException as exc:
        log(f"💥  failed to stop ollama model {cfg.llm.model}: {exc}")
        slack.notify_error("POWER", f"failed to stop ollama model {cfg.llm.model}: {exc}")


def _evict_other_ollama_models(cfg) -> None:
    """Unload every model Ollama currently holds resident except cfg.llm.model.

    The DGX runs one pinned LLM at a time (keep_alive=-1). When config.yaml's llm.model
    is swapped, the previously pinned model stays loaded forever -- nothing else evicts
    it -- and on the GB10's shared 128GB unified pool that stranded model can starve the
    new one on preload (2026-08-27: nemotron-3-super left pinned, qwen preload drove
    unified memory to NV_ERR_NO_MEMORY). Clearing the stragglers here makes every
    power-up self-healing after a model swap.
    """
    try:
        resp = requests.get(
            f"{_ollama_native_url(cfg)}/api/ps",
            timeout=OLLAMA_PS_TIMEOUT_S,
        )
        resp.raise_for_status()
        loaded = [m.get("model") or m.get("name") for m in resp.json().get("models", [])]
    except requests.RequestException as exc:
        log(f"💥  failed to list loaded ollama models: {exc}")
        slack.notify_error("POWER", f"failed to list loaded ollama models: {exc}")
        return

    for model in loaded:
        if not model or model == cfg.llm.model:
            continue
        try:
            resp = requests.post(
                f"{_ollama_native_url(cfg)}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=OLLAMA_STOP_TIMEOUT_S,
            )
            resp.raise_for_status()
            log(f"🧠  evicted stale ollama model {model}")
        except requests.RequestException as exc:
            log(f"💥  failed to evict stale ollama model {model}: {exc}")
            slack.notify_error("POWER", f"failed to evict stale ollama model {model}: {exc}")


def _start_ollama_model(cfg) -> None:
    _evict_other_ollama_models(cfg)
    try:
        resp = requests.post(
            f"{_ollama_native_url(cfg)}/api/generate",
            json={"model": cfg.llm.model, "keep_alive": -1},
            timeout=OLLAMA_PRELOAD_TIMEOUT_S,
        )
        resp.raise_for_status()
        log(f"🧠  preloaded ollama model {cfg.llm.model}")
    except requests.RequestException as exc:
        log(f"💥  failed to preload ollama model {cfg.llm.model}: {exc}")
        slack.notify_error("POWER", f"failed to preload ollama model {cfg.llm.model}: {exc}")


def _power_down(apps_v1, cfg) -> None:
    """Dealer stops first (no state/positions, safe to kill immediately) so it can't fire a new
    BUY mid-flatten. Floor Broker stays up until crypto is confirmed flat -- it's the only thing
    enforcing crypto's synthetic stop-loss/take-profit, so scaling it to 0 with a crypto position
    still open would leave that position completely unprotected overnight. Options are different:
    they don't trade 24/7 and stay protected by dte_force_close/synthetic SL-TP resuming the moment
    Floor Broker is back up, so a failed/incomplete options flatten is logged and Slack-notified but
    never blocks power-down -- unlike crypto, retrying it later cannot succeed anyway once the
    options market has closed for the day (flatten_all_options() submits DAY-TIF orders)."""
    _scale(apps_v1, "dealer", 0)

    if cfg.power_schedule.manage_ollama_model:
        _stop_ollama_model(cfg)

    events = []
    if cfg.power_schedule.flatten_crypto_before_powerdown:
        try:
            resp = requests.post(f"{cfg.floor_broker.base_url}/flatten-crypto", timeout=30)
            resp.raise_for_status()
            events = resp.json().get("events", [])
        except requests.RequestException as exc:
            log(f"💥  flatten-crypto request failed: {exc}")
            slack.notify_error("POWER", f"power-down aborted -- flatten-crypto request failed: {exc}")
            return

    if not _wait_until_crypto_flat():
        log("💥  crypto positions still open after flatten timeout -- aborting power-down")
        slack.notify_error("POWER", "power-down aborted -- crypto positions still open after flatten timeout")
        return

    option_events = []
    if cfg.power_schedule.get("flatten_options_before_powerdown", False):
        try:
            resp = requests.post(f"{cfg.floor_broker.base_url}/flatten-options", timeout=30)
            resp.raise_for_status()
            option_events = resp.json().get("events", [])
            options_flat = _wait_until_options_flat()
        except Exception as exc:
            log(f"💥  flatten-options request/wait failed (power-down continuing): {exc}")
            slack.notify_error("POWER", f"flatten-options request/wait failed, power-down continuing: {exc}")
        else:
            if not options_flat:
                log(
                    "⚠️  option positions still open after flatten timeout -- power-down continuing "
                    "(they stay protected by dte_force_close/synthetic SL-TP once Floor Broker restarts)"
                )
                slack.notify_error(
                    "POWER",
                    "option positions still open after flatten timeout -- power-down continuing",
                )

    _scale(apps_v1, "floor-broker", 0)
    slack.notify_power_state(
        "powered_down",
        f"{len(events)} crypto position(s) flattened first, {len(option_events)} option position(s) flattened first.",
    )
    log(f"✅ powered down ({len(events)} crypto position(s), {len(option_events)} option position(s) flattened)")


def _power_up(apps_v1, cfg) -> None:
    _scale(apps_v1, "floor-broker", 1)
    if not _wait_until_floor_broker_ready(cfg):
        log("💥  floor-broker not ready after timeout -- dealer left at 0")
        slack.notify_error("POWER", "power-up incomplete -- floor-broker not ready after timeout, dealer left at 0")
        return

    if cfg.power_schedule.manage_ollama_model:
        _start_ollama_model(cfg)

    _scale(apps_v1, "dealer", 1)
    slack.notify_power_state("powered_up", "")
    log("✅ powered up")


def main():
    cfg = load_config()
    if not cfg.power_schedule.enabled:
        log("⏭️  power_schedule disabled")
        return

    now = _now_eastern()
    hours = get_stock_market_hours(now.date())
    target = _target_replica_count(now, hours, cfg)

    apps_v1 = _apps_v1()
    current = _get_replica_count(apps_v1, "floor-broker")
    if current == target:
        log(f"⏭️  no-op (current={current}, target={target})")
        return

    if target == 0:
        _power_down(apps_v1, cfg)
    else:
        _power_up(apps_v1, cfg)


if __name__ == "__main__":
    main()
