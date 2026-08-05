#!/usr/bin/env bash
# Full clean-slate reset of this side's state, for use after resetting the paper-trading Alpaca
# account itself (which happens on Alpaca's side, not here -- do that first, then export the new
# ALPACA_PAPER_API_KEY/ALPACA_PAPER_API_SECRET in ~/.zshrc before running this).
#
# 1. Truncates all four tables in _SCHEMA (src/common/db.py): analyst_picks, dealer_decisions,
#    floor_broker_events, position_opens. Pure history/bookkeeping wipe -- never touches Alpaca
#    directly. Run from inside deploy/dealer (falls back to deploy/floor-broker), since neither
#    DATABASE_URL nor psycopg is available outside the cluster -- same pattern as
#    skills/analyst-explain/SKILL.md Step 1.
# 2. Re-execs itself under zsh with ~/.zshrc sourced (once), then hands off to
#    k8s/update-secrets.sh ALPACA_PAPER_API_KEY ALPACA_PAPER_API_SECRET, which patches those two
#    keys into the mlabs-api-keys Secret from this now-refreshed environment, mirrors them to
#    GitHub Actions repo secrets, and restarts deploy/dealer + deploy/floor-broker -- covering
#    the DB truncate's own restart requirement in the same rollout, no separate restart needed
#    here.
# 3. Sanity-checks the result: prints the now-restarted pod's live account.equity/cash/positions
#    (confirming it's talking to the reset Alpaca account) and re-counts all four tables
#    (confirming they're still empty after the restart).
# 4. Triggers the pl-badges GHA workflow (workflow_dispatch) so badges/today-pl.json and
#    badges/ytd-pl.json refresh immediately instead of sitting stale until the next 21:45 UTC
#    cron -- via `gh workflow run`, not by running src/pl_badges/main.py directly (see repo
#    convention: never bypass a GHA workflow that already exists for a task).
#
# Usage:
#   ./k8s/reset-account-state.sh          # prompts for confirmation
#   ./k8s/reset-account-state.sh --yes    # skip the confirmation prompt (e.g. for scripting)

set -euo pipefail

# Re-exec under zsh with ~/.zshrc sourced, once, so a freshly-exported ALPACA_PAPER_API_KEY /
# ALPACA_PAPER_API_SECRET (set there after the Alpaca-side reset) are present in the environment
# below -- matches how update-secrets.sh is normally invoked by hand.
if [[ -z "${MLABS_RESET_REEXECED:-}" ]]; then
  exec env MLABS_RESET_REEXECED=1 zsh -c 'source ~/.zshrc && exec "$0" "$@"' "$0" "$@"
fi

NAMESPACE="multi-agent-ai-trader"
CONFIRM=true

for arg in "$@"; do
  case "$arg" in
    --yes) CONFIRM=false ;;
    *)
      echo "Usage: $0 [--yes]" >&2
      exit 1
      ;;
  esac
done

if [[ "$CONFIRM" == true ]]; then
  echo "This will PERMANENTLY DELETE all rows in analyst_picks, dealer_decisions,"
  echo "floor_broker_events, and position_opens in the $NAMESPACE Postgres DB, and push"
  echo "\$ALPACA_PAPER_API_KEY/\$ALPACA_PAPER_API_SECRET from ~/.zshrc into the mlabs-api-keys"
  echo "Secret (restarting dealer + floor-broker to pick them up)."
  echo "Make sure the Alpaca account reset is already done and those two vars are the new ones."
  read -r -p "Type 'reset' to confirm: " reply
  if [[ "$reply" != "reset" ]]; then
    echo "Aborted -- no changes made." >&2
    exit 1
  fi
fi

if kubectl get deployment/dealer -n "$NAMESPACE" >/dev/null 2>&1; then
  POD_TARGET="deploy/dealer"
elif kubectl get deployment/floor-broker -n "$NAMESPACE" >/dev/null 2>&1; then
  POD_TARGET="deploy/floor-broker"
else
  echo "ERROR: neither deploy/dealer nor deploy/floor-broker found in namespace $NAMESPACE." >&2
  exit 1
fi

kubectl exec -i -n "$NAMESPACE" "$POD_TARGET" -- python3 <<'PYEOF'
from src.common.db import _ensure_schema, _get_pool

_ensure_schema()
with _get_pool().connection() as conn:
    conn.execute(
        "TRUNCATE TABLE analyst_picks, dealer_decisions, floor_broker_events, "
        "position_opens RESTART IDENTITY"
    )
print("Truncated analyst_picks, dealer_decisions, floor_broker_events, position_opens.")
PYEOF

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/update-secrets.sh" ALPACA_PAPER_API_KEY ALPACA_PAPER_API_SECRET

echo
echo "--- Sanity check ---"
kubectl exec -i -n "$NAMESPACE" "$POD_TARGET" -- python3 <<'PYEOF'
from src.common.alpaca_client import trading_client
from src.common import db

account = trading_client.get_account()
positions = trading_client.get_all_positions()

print("account_number:", account.account_number)
print("equity:", account.equity)
print("last_equity:", account.last_equity)
print("cash:", account.cash)
print("buying_power:", account.buying_power)
print("open positions:", len(positions))
for p in positions:
    print(" -", p.symbol, p.qty, p.unrealized_pl)

with db._get_pool().connection() as conn:
    for table in ("analyst_picks", "dealer_decisions", "floor_broker_events", "position_opens"):
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"{table} row count:", count)
PYEOF

echo
echo "--- Refreshing P/L badges ---"
before_id="$(gh run list --workflow=pl-badges.yaml --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || echo "")"
gh workflow run pl-badges.yaml
run_id=""
for _ in $(seq 1 10); do
  sleep 2
  run_id="$(gh run list --workflow=pl-badges.yaml --limit 1 --json databaseId --jq '.[0].databaseId')"
  [[ -n "$run_id" && "$run_id" != "$before_id" ]] && break
  run_id=""
done
if [[ -z "$run_id" ]]; then
  echo "WARNING: couldn't find the newly-queued pl-badges run -- check 'gh run list --workflow=pl-badges.yaml' manually." >&2
else
  gh run watch "$run_id" --exit-status
fi
