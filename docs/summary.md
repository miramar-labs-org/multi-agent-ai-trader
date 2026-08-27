# How this system works (plain-language explainer)

This document explains the trading system in everyday terms — no code, no infrastructure
jargon. It assumes you know trading concepts (RSI, MACD, bracket orders, stop-loss/take-profit,
paper trading) but not programming. For the technical version, see
[architecture.md](architecture.md).

## The Executive Summary

Every morning before the market opens, a program builds a short watchlist of stocks (and
optionally crypto) worth trading that day. Then, every ten minutes while the market is open,
a second program looks at each watchlist symbol's technical indicators and asks an AI model
"buy, sell, or hold?" A third program is the only one allowed to actually place an order — it
takes that decision and executes it on Alpaca's **paper** (simulated money) trading account.
A fourth program sends a plain-English recap to Slack at the end of the day.

Nothing here trades with real money. It's paper trading only, and that's not a setting anyone
can flip — it's built in.

## New features

A running log of config-gated capabilities as they're added, newest first. "Status" reflects
`config.yaml` as of the date shown — check there for the current live value. Updated every time
a new feature ships.

| Date | Feature | Summary | Status |
|---|---|---|---|
| 2026-08-14 | Options trading | When on, every stock buy/sell call is expressed as a long option instead of a stock trade — a call for a buy, a put for a sell (never a short). The Dealer's AI picks a specific contract by exploring the live option chain (expiration 14–45 days out, delta 0.30–0.60), and the order goes to the same paper account as every other trade. Since the broker offers no automatic stop-loss on options, the system watches each contract itself and force-closes on a 50% loss, a 175% gain, or when expiration gets within 3 days. Crypto is unaffected. | On |
| 2026-08-14 | Configurable Alpaca account | The single paper-trading login the whole floor uses — stocks, crypto and options alike — is now a config setting that can be switched while running, instead of being fixed in code. Currently the base funded paper account; the switch target is the competition's $100k options account. | On |
| 2026-08-13 | Dealer OHLCV enrichment | Dealer prompts now include stock-only multi-timeframe Alpaca candle context (5m/1h/1d) and derived market-structure features such as return, volatility, ATR, VWAP distance, relative volume, and EMA context alongside TAAPI's current indicator snapshot. | On |
| 2026-08-12 | Analyst candidate mix | Composes the daily candidate pool as a configurable percentage split (default 40% large-cap / 30% crypto / 30% today's screener movers) instead of letting the day's movers ranking alone decide it, so large-cap names are reliably represented and always get real indicator data — still subject to the earnings blackout filter like any other candidate. | On |
| 2026-08-11 | Same-symbol stop-loss cooldown | After a symbol stops out, the Dealer pauses new BUY entries for that symbol during the configured lookback window, preventing repeated re-entry into the same failing setup. | On |
| 2026-08-11 | Dealer same-symbol memory | Dealer prompts now include recent decisions and Floor Broker outcomes for the same symbol, so the AI can see if a setup has already failed today instead of judging each poll in isolation. | On |
| 2026-08-11 | Symbol-scoped win-rate throttle | The win-rate throttle can now run per symbol instead of globally, so one cluster of bad symbols no longer freezes all BUYs across the whole portfolio. | On |
| 2026-08-11 | Analyst candidate quality filters | Stock screener candidates are filtered for extreme moves, minimum dollar volume, and warrant/unit-like suffixes before the Analyst LLM sees them. | On |
| 2026-08-11 | Bid/ask spread gate | Floor Broker skips stock BUYs when the live bid/ask spread is too wide for controlled entry risk. | On |
| 2026-08-07 | Ollama model stop/preload on power schedule | When the trading system powers down for the night, the local AI model is also unloaded from GPU memory, then reloaded just before the system wakes back up — so the machine can actually go idle overnight instead of sitting warm for a market that's closed. | On |
| 2026-08-07 | Nightly power-down/power-up | Dealer and Floor Broker are scaled off about an hour after market close and back on about an hour before the next open, instead of running around the clock for a market that's only open ~6.5 hours a day. Any open crypto position is force-closed first, since crypto's stop-loss/take-profit protection only works while Floor Broker is running. | On |
| 2026-08-07 | Position limit cap | Stops opening brand-new positions once too many are already open at the same time, so one bad stretch can't spread risk across an unbounded number of simultaneous bets. | On |
| 2026-08-07 | Risk-based position sizing | Caps how much a single stopped-out trade can lose, regardless of the budget the Analyst assigned it, by scaling the trade size down so a full stop-loss hit costs at most a fixed dollar amount. | On |
| 2026-08-07 | Confidence gate | The Dealer's AI now also reports how confident it is in each buy call, and a low-confidence buy is skipped automatically before it ever reaches the Floor Broker. | On |
| 2026-08-07 | Win-rate throttle | If the trailing win rate on recent stop-loss/take-profit exits drops too low, new buys pause automatically until it recovers — existing positions can still be sold anytime. | On |
| 2026-08-05 | Live P/L badges | Adds "Today's P/L" and "Year-to-date P/L" badges to the README, refreshed automatically after each trading day closes. | On |
| 2026-08-05 | Earnings blackout | Drops a screener candidate from the watchlist if it's about to report earnings or just did, avoiding the price swings those reports can cause. | On |
| 2026-08-05 | Macro-event blackout | Pauses new buys for the whole day on FOMC/CPI/jobs-report/PCE dates and quarterly "quad witching" days, when the whole market can move sharply. Selling is never paused. | On |
| 2026-08-05 | Conditional EOD flatten | End-of-day flatten only closes everything out if today's overall unrealized P&L is break-even or better; on a down day it holds positions overnight instead (except any held too long). | Off |
| 2026-08-05 | Midday Analyst run | A second watchlist-building run around midday, to catch stocks that moved after the morning run already happened. | On |
| 2026-08-05 | Optional end-of-day flatten ("day trading mode") | Closes out open stock positions a few minutes before market close instead of holding them overnight. | On |
| 2026-08-04 | Live position P&L snapshot | Gives the Analyst a live read of unrealized profit/loss on everything currently held, as one more input to its picks. | On |
| 2026-08-04 | Track-record feedback loop | Lets the Analyst read back its own recent picks and how they turned out, so it can notice patterns instead of judging each day in isolation. | On |

## The four workers

Think of this as a small trading floor with four roles, each running independently so a
problem in one doesn't take down the others.

### 1. The Analyst — picks today's watchlist

Runs once a day, early, before the market opens (8:55am Eastern — 35 minutes before the
9:30am bell). Its job: decide which symbols are even worth Dealer's attention today.

- It pulls a list of candidate stocks from Alpaca's "most active" and "biggest movers"
  screeners (top movers by volume and % change), and — if crypto is enabled — a small fixed
  crypto watchlist (BTC, ETH, SOL). On a day the stock market is closed (weekend/holiday), it
  skips the stock screeners but still runs the crypto side, since crypto trades 24/7.
- It reads recent news headlines and RSS feeds for those candidates.
- For the biggest movers among those candidates, it pulls real technical indicator readings
  (RSI, MACD, VWAP, Bollinger Bands, SMA, EMA) from a third-party indicator service.
- It also reads back its own recent pick history — what it picked, why, what the Dealer decided
  to do about each pick, and how the trade actually went — from a running log of past days. The
  idea is to let it notice "I keep picking this pattern and it keeps losing" rather than judging
  every day in isolation with no memory of yesterday. This is qualitative only for now (a plain
  written history, not a computed profit/loss number) — see
  [What this system is not (yet)](#what-this-system-is-not-yet) for why.
- It also pulls a live snapshot of its currently-open positions and Alpaca's own unrealized
  profit/loss for each — a point-in-time read of what the account holds right now, separate from
  the pick-history recap above, and also switchable off independently in config.
- It hands all of that — news + real indicator numbers + its own recent track record + the live
  P&L snapshot — to the AI model and asks it to pick up to 10 symbols worth trading today, with a
  dollar budget and a written rationale for each.
- Each of these inputs — news, indicators, its own track record, and the live P&L snapshot — can
  be switched off independently in config. That's a safety switch: if one input turns out to be
  feeding the AI bad information, it can be disabled on its own without touching the others.
- It double-checks the AI's picks against the actual candidate list (so the AI can't invent a
  symbol that was never a real candidate), then publishes the day's watchlist for the Dealer to
  read.
- Right after publishing, it posts a "Morning Market Report" to Slack — the day's picks,
  budgets, and rationale, plus the account's current cash/equity — so a human can see the plan
  before the market even opens. If the stock market is closed that day, the report says so up
  front, and notes that crypto trading continues 24/7 (when crypto is enabled).
- If crypto is enabled, it also posts a separate crypto end-of-day recap covering the prior
  full day's crypto activity, since crypto trades 24/7 and has no natural market-close moment
  of its own.

**What it does NOT do:** it never places an order. It only decides the watchlist. It also
doesn't follow a fixed rule (like "buy on RSI < 30") — the picks are entirely up to the AI
model's judgment, informed by the news and indicator data above. A separate, offline backtesting
tool (`docs/backtesting.md`) does implement fixed rule-based strategies, but only to give the
AI's live picks something deterministic to be compared against — it doesn't influence live
trading.

### 2. The Dealer — decides buy/hold/sell, all day

Runs continuously while the market is open, checking in every 10 minutes (and waiting 15
minutes after the opening bell before its first check, to let opening volatility settle).

For every symbol on today's watchlist, each cycle:

- Pulls that symbol's current technical indicator readings from the indicator service.
- For stock symbols, also pulls recent Alpaca OHLCV candles at multiple timeframes and turns
  them into compact context like recent return, volatility, ATR, relative volume, VWAP distance,
  distance from recent highs/lows, and moving-average context. Crypto keeps the older
  indicator-only Dealer path for now.
- Looks up recent same-symbol history from Postgres — recent Dealer calls, BUY skips/fills, and
  stop-loss/take-profit outcomes — and gives that context to the AI alongside the indicators.
- Hands those numbers to the AI model with the instruction: "you're an expert technical
  trader — based on all these indicators, should we BUY, SELL, or HOLD?"
- Every decision — including HOLD — is posted to Slack so a human can see what the Dealer
  decided. But only a BUY or SELL is actually forwarded (symbol, budget, stop-loss/take-profit
  percentages) to the Floor Broker to execute; a HOLD stops there and nothing gets traded.

If a position from before this system existed (or opened outside the daily watchlist) shows
up in the account, the Dealer folds it into its checks too — but treats it as something to
monitor for a possible SELL, not a pool of extra buying power. It won't authorize a fresh BUY
against the value of a position it didn't originally budget for.

**What it does NOT do:** it never talks to Alpaca's order-placement API directly — only the
Floor Broker does that.

### 3. The Floor Broker — the only one who actually trades

A standing service that does nothing but wait for instructions from the Dealer and execute
them. It never calls the AI model — by the time a request reaches it, the buy/sell call has
already been made. Its job is purely mechanical order execution and safety checks.

- **Buying a stock:** places a bracket order — one order with an automatic stop-loss and
  take-profit attached, sized off the current ask price. The stop-loss/take-profit percentages
  come from config (currently roughly a 2% stop and a 5% target). If a position or open order
  already exists for that symbol, it refuses the buy rather than pyramiding into it.
- **Buying crypto:** places a plain market order for a dollar amount instead (crypto doesn't
  use bracket orders here). If the dollar amount would fall under Alpaca's $10 minimum order
  size, it skips the trade rather than silently rounding it up past the intended budget. Since
  Alpaca doesn't support bracket orders for crypto at all, the Floor Broker fakes one: once the
  buy fills, it remembers a stop-loss and take-profit price for that coin (also from config) and
  checks the live price against them on the same 30-second cadence described below, selling
  automatically if either is crossed.
- **Buying an option** (only when options trading is on): the Dealer has already picked a
  specific contract by exploring the live option chain through the AI model. The Floor Broker
  re-checks the current asking price, sizes the number of contracts so the premium outlay stays
  within the per-trade risk budget, rejects it if the total cost exceeds the configured cap, and
  places a plain market order on the same single paper account everything else trades on. Alpaca has no automatic stop-loss
  for options either, so — as with crypto — the Floor Broker watches each open contract on the
  same 30-second cadence and force-closes it on a 50% loss, a 175% gain, or once expiration is
  within 3 days, whichever comes first.
- **Selling:** sells the full position at market. If Alpaca briefly rejects the sell because of
  a conflicting order, it clears the blocker and retries automatically rather than giving up.
- **Watching orders fill:** buys and sells are submitted and acknowledged immediately rather than
  waiting around for a fill, so background checks each run every 30 seconds — one watching for
  the original buy/sell itself filling, one watching for stop-loss or take-profit legs that
  filled on their own (which can happen hours after the original buy, with no direct
  request/response to hang a notification off of), and one watching tracked crypto positions
  against their stop-loss/take-profit prices — and post a Slack notice for each one as it's
  detected.
- **Daily profit/loss circuit breaker:** before any new buy, it checks today's account profit or
  loss so far. Once the day's gain hits the configured profit target, or the day's loss hits the
  configured loss limit, it stops opening new positions until the next day — existing positions
  can still be sold at any time, it just won't add new risk once the day's goal (or floor) is
  reached.

Every buy and sell — along with every stop-loss/take-profit fill — gets posted to Slack with
the fill price and reason, so there's a running, human-readable trail of everything the system
actually did, not just what it decided.

### 4. The EOD Report — the daily recap

Runs once a day 30 minutes after Alpaca's official market close. The CronJob checks several
possible close times through the afternoon, then the report code sends exactly once when the
real close+30min moment has passed — for example, 4:30pm Eastern on normal close days and
1:30pm Eastern on 1pm early-close days.
Makes no trading decisions at all — it just reads the account's current state and posts a
plain-English summary to Slack: today's equity, cash, buying power, profit/loss versus
yesterday's close, open positions and their unrealized P&L, and every fill that happened that
day across all the other three workers.

If today wasn't a trading day (weekend or market holiday), it posts a short "market was closed"
notice instead of a full report — so a quiet Slack channel always means "we checked, nothing
happened," never "did this even run?"

## How the pieces hand off to each other

There's no message queue connecting these four workers, and no database in the coordination
path — Postgres exists (see below) but is a durable log written to on the side, not how the
agents hand off to each other. The handoff itself is deliberately simple:

- **Analyst → Dealer:** the Analyst writes the day's watchlist to one shared, small
  structured record (symbol, budget, indicators to watch, rationale). The Dealer reads that
  same record fresh at the start of every 10-minute cycle — it never caches an old copy. If the
  Analyst hasn't run yet, or failed, the Dealer just keeps using whatever the last known
  watchlist was.
- **Dealer → Floor Broker:** a direct request, in real time, only when the decision isn't
  HOLD. Floor Broker's response (executed, skipped, or error, plus fill price if available)
  goes back to the Dealer's logs and Slack, same as before.
- **Everything → Slack:** every consequential event (morning picks, a BUY/SELL/HOLD decision,
  an execution, a bracket TP/SL fill, the EOD recap, a market-closed notice, any error) gets a
  Slack message. A BUY/SELL/HOLD decision, an execution/fill, and an error each carry their own
  Eastern-time timestamp; the morning picks and EOD recaps don't carry a top-level timestamp
  (each fill listed inside an EOD recap does carry its own).
- **Everything → Postgres:** since v0.6.1, Analyst picks, Dealer decisions, and Floor Broker
  execution events are also written to a shared Postgres instance (`src/common/db.py`),
  fire-and-forget so a DB outage can never block a trading decision. This backs the
  `/analyst-explain` skill, which reads the actual logged Dealer reasoning back out to explain
  a trading day rather than relying on Slack scrollback or raw logs — and, as of the track-record
  feedback loop above, the Analyst itself also reads it back each morning as part of its own
  decision-making, not just for after-the-fact human explanations. `/analyst-explain` can also
  post its narrative to Slack, but only when explicitly asked to share it — it stays chat-only
  by default.

## Where the "brain" comes from

Both the Analyst's symbol picks and the Dealer's BUY/HOLD/SELL calls are made by the same AI
language model, running on local hardware (not a third-party AI API) — so there's no
per-decision API cost and no data about the day's trading leaving the building. The model is
given the relevant numbers/news as plain text and asked to return a strictly-structured answer
(a specific set of fields, not free-form prose it has to be parsed out of), which is what keeps
its output reliably machine-usable.

## Risk controls, in trading terms

- **Paper account only** — not a setting, hardcoded. There's no path to live trading without
  someone deliberately changing the source code.
- **Every stock buy is bracketed** — a stop-loss and take-profit are placed at the same time
  as the entry, so there's never an unprotected open stock position by construction.
- **Every crypto buy gets a stop-loss/take-profit too** — Alpaca doesn't support real bracket
  orders for crypto, so the Floor Broker fakes one in software: it remembers the target prices
  after the buy fills and sells automatically if either is crossed on its 30-second price check.
- **Daily profit/loss circuit breaker** — a configurable dollar profit target and loss limit;
  once either is hit for the day, no new buys go out until the next day (existing positions can
  still be sold any time).
- **No pyramiding** — the Floor Broker refuses a new buy on a symbol that already has an open
  position or a pending order.
- **Opening-bell buffer** — the Dealer waits 15 minutes after the open before making its first
  check of the day, to avoid trading directly into opening volatility.
- **One bad symbol doesn't stop the loop** — if fetching indicators or calling the AI fails for
  one symbol on the watchlist, that failure is logged and the Dealer moves on to the next
  symbol rather than the whole cycle failing.
- **No overlapping runs** — the Analyst won't start a new daily run if the previous one is
  somehow still in flight, and there's only ever one Dealer instance active at a time, so
  there's never a race over the shared watchlist.
- **Indicator-service rate limits are respected** — indicator lookups are throttled so the
  system stays inside the third-party indicator provider's request-rate limit rather than
  risking getting throttled or blocked mid-day.
- **Earnings blackout** — a stock due to report earnings in the next couple of days, or that
  just reported, is left off the watchlist entirely, avoiding the price swings those reports can
  cause.
- **Screener quality filters** — low-quality stock candidates are filtered before the Analyst
  sees them: extreme daily movers can be excluded, candidates with insufficient dollar volume
  can be dropped when volume and price are known, and warrant/unit-like suffixes are avoided.
- **Macro-event blackout** — new buys pause for the whole day on dates with a major scheduled
  economic release (Fed rate decisions, inflation/jobs reports, and similar) or on quarterly
  "quad witching" days, when the whole market — not just one stock — tends to move sharply.
  Selling out of existing positions is never paused by this.
- **Position limit cap** — once too many positions are open at the same time (10 by default), no
  new (non-top-up) buy goes out until something closes and frees up a slot, so one bad stretch
  can't spread risk across an unbounded number of simultaneous bets.
- **Risk-based position sizing** — a stopped-out trade is capped to lose at most a fixed dollar
  amount ($100 by default), regardless of how large a budget the Analyst assigned it. This only
  ever scales a trade's size *down* from what the Analyst authorized, never up.
- **Confidence gate** — the Dealer's AI now scores its own confidence (0-100%) on every buy call,
  and a buy below the configured floor (60% by default) is skipped automatically before it's ever
  forwarded to the Floor Broker. Sells and holds are unaffected.
- **Same-symbol stop-loss cooldown** — after a symbol hits a recent stop-loss, new BUYs for
  that same symbol pause for the configured lookback window. This blocks repeated re-entry into
  the same failed intraday setup; other symbols can still trade.
- **Win-rate throttle** — if the trailing win rate on recent stop-loss/take-profit exits falls
  below a floor (30% by default, over the last several trading days), new buys pause
  automatically until it recovers. This now defaults to symbol scope, so weak performance in one
  name pauses that name rather than the whole portfolio; existing positions can still be sold at
  any time.
- **Bid/ask spread gate** — stock BUYs are skipped if the current bid/ask spread is wider than
  the configured percentage cap, avoiding thin names where a market entry and bracket stop may
  not reflect the intended risk.
- **Nightly power-down** — Dealer and Floor Broker turn off about an hour after market close and
  back on about an hour before the next open, so nothing is running (or exposed) outside the
  hours it's actually needed. Any open crypto position is force-closed first, since crypto's
  stop-loss/take-profit protection only works while Floor Broker is running.

## What this system is not (yet)

- It does not score whether its own *individual past picks* were actually good or bad in dollar
  terms. The Analyst reads back a plain-English history of its own recent picks and outcomes,
  plus a live snapshot of unrealized profit/loss for whatever it currently holds (see "The
  Analyst" above) — but neither ties a specific past pick to a specific realized dollar result: a
  symbol can be bought and sold more than once, and the live snapshot only covers positions still
  open right now. No per-trade *realized* P&L tracking or scoring exists yet outside the offline
  backtesting tool.
- It does not backtest the AI's actual historical decision-making. There is a separate,
  already-built backtesting tool (see [backtesting.md](backtesting.md)) that checks simple,
  rule-based strategies (buy-and-hold, plain RSI/MACD rules, etc.) against historical prices as
  a sanity-check baseline — but replaying what the live AI itself would have decided on past
  days isn't possible yet, since the exact news/candidates/model behavior from a past day can't
  be reconstructed after the fact.
- It only trades what the Analyst put on the morning watchlist (plus any pre-existing position
  it discovers). It doesn't react to a brand-new, off-watchlist opportunity mid-day.
- Its options trading only ever *buys* single long calls or puts — there are no spreads, no
  multi-leg structures, and nothing that sells premium. The Analyst is also not options-aware: it
  still screens and picks the same stock universe, and the options layer just re-expresses those
  picks as long contracts.
- All reporting — the end-of-day Slack recap, the README P/L badges, the Analyst's mid-day
  position-P&L note — reads the one live paper account. Stocks, crypto and options all trade
  there, so everything is covered by a single read; there is no multi-account aggregation.
