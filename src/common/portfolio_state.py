import json
import os

from alpaca.trading.enums import AssetClass
from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from src.common.alpaca_client import trading_client

NAMESPACE = os.getenv("POD_NAMESPACE", "multi-agent-ai-trader")
CONFIGMAP_NAME = "portfolio"
DATA_KEY = "portfolio.json"


def _load_k8s_config() -> None:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def read_portfolio() -> dict:
    """Reads the `portfolio` ConfigMap fresh — no caching, matches the Dealer's poll cadence."""
    _load_k8s_config()
    v1 = client.CoreV1Api()
    cm = v1.read_namespaced_config_map(CONFIGMAP_NAME, NAMESPACE)
    return json.loads(cm.data.get(DATA_KEY, "{}"))


def merge_held_positions(portfolio: dict, cfg) -> dict:
    """Adds any Alpaca position not already in the watchlist -- e.g. one opened before this app
    existed -- so Dealer keeps deciding BUY/HOLD/SELL on it instead of leaving it unmanaged
    forever. Stock positions are only merged in when `cfg.trading.enable_stocks` is set, and
    crypto positions only when `cfg.trading.enable_crypto` is set, matching Dealer's own
    exchange-filter gate."""
    symbols = portfolio.get("symbols", [])
    known = {entry["symbol"] for entry in symbols}

    for position in trading_client.get_all_positions():
        if position.symbol in known:
            continue

        if position.asset_class == AssetClass.US_EQUITY and cfg.trading.enable_stocks:
            exchange = "stocks"
        elif position.asset_class == AssetClass.CRYPTO and cfg.trading.enable_crypto:
            exchange = cfg.trading.crypto_taapi_exchange
        else:
            continue

        symbols.append(
            {
                "symbol": position.symbol,
                "exchange": exchange,
                "budget": float(position.market_value),
                "indicators": ["ALL"],
            }
        )

    return {**portfolio, "symbols": symbols}


def write_portfolio(portfolio: dict) -> None:
    """Patches the `portfolio` ConfigMap. Called once per Analyst CronJob run."""
    _load_k8s_config()
    v1 = client.CoreV1Api()
    body = {"data": {DATA_KEY: json.dumps(portfolio, indent=2)}}
    try:
        v1.patch_namespaced_config_map(CONFIGMAP_NAME, NAMESPACE, body)
    except ApiException as exc:
        if exc.status != 404:
            raise
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, namespace=NAMESPACE),
            data={DATA_KEY: json.dumps(portfolio, indent=2)},
        )
        v1.create_namespaced_config_map(NAMESPACE, cm)
