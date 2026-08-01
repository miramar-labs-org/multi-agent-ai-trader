import json
import os

from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

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
