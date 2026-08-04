---
name: revert-strategy
description: >
  Restores config.yaml to its pre-wizard default state, undoing anything
  /configure-strategy wrote. Trigger on /revert-strategy, "revert to default config",
  "undo the strategy wizard", "reset the trading config back to default".
---

# Revert trading strategy config to default

Restores `config.yaml` to the untouched baseline captured in `config.default.yaml`,
undoing whatever `/configure-strategy` (or any manual edit) has since changed.

## Step 1 — Confirm the baseline exists

Check `config.default.yaml` is present at the repo root. It's the snapshot of
`config.yaml` taken before `/configure-strategy` ever ran for the first time. If it's
missing, stop and tell the user — do not fabricate a baseline from the current
`config.yaml`, since that could just be enshrining an already-modified state as
"default." (`/configure-strategy` is responsible for creating this snapshot on its own
first run if it doesn't already exist — see that skill's Step 7.)

## Step 2 — Diff

Run `diff config.yaml config.default.yaml`. If there's no difference, tell the user
the config is already at default and stop — nothing to do.

## Step 3 — Show the diff and confirm

Show the user exactly what will change (the diff from Step 2, read as "current →
default"). Get explicit confirmation before writing — `config.yaml` is loaded by live
k8s services (paper account, but still don't overwrite silently, per this org's "Ask
Before Acting" rule).

## Step 4 — Restore

Copy `config.default.yaml` over `config.yaml` verbatim (`cp config.default.yaml
config.yaml`). Do not hand-merge or selectively revert fields — the whole point is an
exact, unambiguous return to the known-good baseline.

## Step 5 — Log the outcome

Append a dated entry to `docs/strategy.md` noting the revert happened and what the
diff (from Step 2) contained, so the strategy history isn't silently lost even though
the config itself is back to default.
