#!/usr/bin/env bash
# Update one or more keys in the mlabs-api-keys Secret (multi-agent-ai-trader namespace) from
# the caller's own environment -- values never appear as argv (visible to other users via `ps`)
# or get written to shell history the way `KEY=value` CLI args would. See
# k8s/secrets.example.yaml for the full key list and README.md's "Create the namespace + secret"
# section for the initial-create form.
#
# Also mirrors GHA_SECRET_KEYS (the subset of keys a GitHub Actions workflow reads directly --
# currently just ALPACA_PAPER_API_KEY/SECRET, for .github/workflows/pl-badges.yaml, which runs
# on a self-hosted runner outside the cluster and so can't read the k8s Secret) to matching
# GitHub Actions repo secrets via `gh secret set`, so the two credential stores can't drift --
# see docs/architecture.md's Secrets section.
#
# Usage:
#   export ALPACA_PAPER_API_KEY=PK...
#   export ALPACA_PAPER_API_SECRET=...
#   ./update-secrets.sh ALPACA_PAPER_API_KEY ALPACA_PAPER_API_SECRET
#   ./update-secrets.sh --restart ALPACA_PAPER_API_KEY ALPACA_PAPER_API_SECRET   # also rollout-restart
#
# With no key names given, checks every known key (see KNOWN_KEYS below) and updates whichever
# are currently set (non-empty) in the environment.
#
# kubectl patch --type=merge only touches the keys you pass -- every other key already in the
# Secret is left untouched, unlike `kubectl create ... --dry-run=client -o yaml | kubectl apply`
# which would require restating the whole key set.

set -euo pipefail

NAMESPACE="multi-agent-ai-trader"
SECRET_NAME="mlabs-api-keys"
KNOWN_KEYS=(TAAPI_API_KEY ALPACA_PAPER_API_KEY ALPACA_PAPER_API_SECRET LANGCHAIN_API_KEY SLACK_WEBHOOK_URL DATABASE_URL)
GHA_SECRET_KEYS=(ALPACA_PAPER_API_KEY ALPACA_PAPER_API_SECRET)
RESTART=true

if [[ "${1:-}" == "--restart" ]]; then
  RESTART=true
  shift
fi

# Explicit key names win; otherwise fall back to whichever KNOWN_KEYS are set in the environment.
if [[ $# -gt 0 ]]; then
  keys=("$@")
else
  keys=()
  for k in "${KNOWN_KEYS[@]}"; do
    if [[ -n "${!k:-}" ]]; then
      keys+=("$k")
    fi
  done
fi

if [[ ${#keys[@]} -eq 0 ]]; then
  echo "Usage: $0 [--restart] [KEY_NAME ...]" >&2
  echo "No key names given and none of the known keys (${KNOWN_KEYS[*]}) are set in the environment." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required (used to build the patch JSON safely)." >&2
  exit 1
fi

# Only require `gh` (and its auth) when a key that's actually mirrored to GitHub Actions is
# among the ones being updated -- e.g. a lone TAAPI_API_KEY rotation shouldn't need `gh` at all.
gha_keys=()
for key in "${keys[@]}"; do
  for gha_key in "${GHA_SECRET_KEYS[@]}"; do
    if [[ "$key" == "$gha_key" ]]; then
      gha_keys+=("$key")
    fi
  done
done

if [[ ${#gha_keys[@]} -gt 0 ]] && ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh is required to mirror ${gha_keys[*]} to GitHub Actions repo secrets." >&2
  exit 1
fi

if ! kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "ERROR: secret $SECRET_NAME not found in namespace $NAMESPACE -- create it first (see README.md)." >&2
  exit 1
fi

patch_json="{\"stringData\":{}}"
updated_keys=()
for key in "${keys[@]}"; do
  value="${!key:-}"
  if [[ -z "$value" ]]; then
    echo "ERROR: \$$key is not set (or empty) in the environment." >&2
    exit 1
  fi
  patch_json=$(jq --arg k "$key" --arg v "$value" '.stringData[$k] = $v' <<<"$patch_json")
  updated_keys+=("$key")
done

kubectl patch secret "$SECRET_NAME" -n "$NAMESPACE" --type=merge -p "$patch_json"
echo "Updated keys: ${updated_keys[*]}"

if [[ ${#gha_keys[@]} -gt 0 ]]; then
  for key in "${gha_keys[@]}"; do
    gh secret set "$key" --body "${!key}"
  done
  echo "Mirrored to GitHub Actions repo secrets: ${gha_keys[*]}"
fi

# envFrom values are only read at container start -- Dealer and Floor Broker are long-running
# Deployments that must be restarted to pick up new values. Analyst/EOD Report are CronJobs and
# pick up the new Secret naturally on their next run, so they're deliberately not restarted here.
if [[ "$RESTART" == true ]]; then
  kubectl rollout restart deployment/dealer -n "$NAMESPACE"
  kubectl rollout restart deployment/floor-broker -n "$NAMESPACE"
  kubectl rollout status deployment/dealer -n "$NAMESPACE"
  kubectl rollout status deployment/floor-broker -n "$NAMESPACE"
else
  echo "Note: Dealer/Floor Broker won't see these new values until restarted. Re-run with --restart," \
       "or manually: kubectl rollout restart deployment/dealer deployment/floor-broker -n $NAMESPACE"
fi
