---
name: analyst-explain
description: >
  Explains why today's paper-trading account is up or down, by combining live
  Alpaca P&L with the Analyst's picks, the Dealer's BUY/HOLD/SELL reasoning,
  and Floor Broker's execution outcomes recorded in Postgres. Read-only, no
  changes made. Trigger on /analyst-explain, "why is the account up/down
  today", "explain today's trades", "what did the Dealer do today and why".
---

# Explain today's account performance

Read-only — no file is written, no order is placed, no confirmation step is
needed (unlike `/configure-strategy` or `/revert-strategy`, which mutate
state).

This only works for **today**, and only for activity that happened after
Postgres persistence actually started writing (`src/common/db.py`, added in
v0.6.0, but a schema bug meant no rows were written until the v0.6.1 fix).
There is no historical backfill — decisions made before v0.6.1 are
unrecoverable, same limitation `docs/ROADMAP.md` P1.1 already documents. If
the DB tables come back empty for today, say so plainly rather than
inventing a narrative from the account numbers alone.

## Step 1 — Gather data from inside the cluster

Alpaca credentials and `DATABASE_URL` are k8s secrets (`mlabs-api-keys`) —
they're not available locally, and Postgres has no laptop tunnel (see
`miramar-platform-gcp/dgx/k3s/postgres/README.md`). Rather than wiring up
either locally, exec into a live pod that already has both plus the app's own
`alpaca-py`/`psycopg`/`src.common` code installed, and run one script there:

```bash
kubectl exec -i -n multi-agent-ai-trader deploy/dealer -- python3 <<'PYEOF'
import json
from datetime import datetime

import pytz

from src.common.alpaca_client import trading_client
from src.common.eod import fetch_fills, summarize_positions
from src.common import db

today = datetime.now(pytz.timezone("US/Eastern")).date()
today_str = today.isoformat()

account = trading_client.get_account()
positions = trading_client.get_all_positions()

out = {
    "date": today_str,
    "account": {
        "equity": float(account.equity),
        "last_equity": float(account.last_equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
    },
    "positions": summarize_positions(positions),
    "fills": fetch_fills(today_str),
    "analyst_picks": db.fetch_analyst_picks_for_date(today),
    "dealer_decisions": db.fetch_dealer_decisions_for_date(today),
    "floor_broker_events": db.fetch_floor_broker_events_for_date(today),
}
print(json.dumps(out, default=str))
PYEOF
```

If `deploy/dealer` isn't running (check `kubectl get pods -n
multi-agent-ai-trader` first), fall back to `deploy/floor-broker` — either
pod carries the same secret and code. If the exec itself fails (pod not
found, DB unreachable), report that plainly rather than guessing at account
state.

## Step 2 — Correlate decisions to outcomes

Parse the JSON from Step 1. For each symbol that appears in
`dealer_decisions` or `floor_broker_events`, build a timeline:

1. Was it an Analyst pick today? Pull its `rationale` and `budget` from
   `analyst_picks` if so — this is the "why was it even in play" context.
2. What did the Dealer decide, and why — `action` + `reasoning` from
   `dealer_decisions` (there can be several per symbol across the day's poll
   cycles; use them all, in `decided_at` order).
3. What actually happened at Floor Broker — match `floor_broker_events` to
   the nearest-preceding Dealer decision for the same symbol by timestamp
   proximity (there's no shared foreign key by design — see
   `docs/architecture.md` § Persistence). Note `event_type` (`buy_submitted`,
   `sell_submitted`, `skip`, `fill`, `no_fill`, `error`, `synthetic_*`) and
   `detail`/`price`.
4. Cross-reference against `fills` (the Alpaca-authoritative record) for the
   actual executed price/qty — the DB's `floor_broker_events.price` is
   informational, `fills` is ground truth for P&L math.

Held positions with no Dealer decision today (carried over from a prior day)
won't have a same-day row in any of the three tables — call these out
separately as "still holding from before today" rather than silently
omitting them, since they still affect today's unrealized P&L.

## Step 3 — Synthesize the narrative

Lead with the headline, in the same tone as `slack.py`'s EOD report:

- P&L: `account.equity - account.last_equity`, both as a dollar figure and a
  percentage of `last_equity`. State up or down plainly.

Then, per symbol involved today (picked, decided on, or traded — sorted by
absolute $ impact on P&L, largest first), a short paragraph: what the Analyst
saw, what the Dealer decided and why (quote the actual LLM reasoning text,
not a paraphrase), what got executed, and the resulting fill/price if any.

Close with open positions carried from before today, and a one-line note on
any `error`/`no_fill` events today that a human should probably look into.

Do not fabricate rationale for a symbol that has no matching DB row — say
"no recorded Dealer decision for this symbol today" instead.

## Step 4 — Posting to Slack (only if explicitly asked)

This skill is chat-only by default — the narrative from Step 3 is not posted
anywhere on its own. Only post it to `#miramar-trading-floor` if the user
explicitly asks to share/post it (e.g. "post that to slack", "share this with
the team"). Reuse the same pod exec pattern as Step 1, calling
`slack.notify_analyst_explain(narrative, report_date)` with the exact
narrative text already shown in chat — don't regenerate or summarize it:

```bash
kubectl exec -i -n multi-agent-ai-trader deploy/dealer -- python3 <<'PYEOF'
from src.common import slack

narrative = """<the Step 3 narrative, verbatim>"""
slack.notify_analyst_explain(narrative, "<today's ET date, YYYY-MM-DD>")
PYEOF
```
