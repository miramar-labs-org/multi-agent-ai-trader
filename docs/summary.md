# How this system works (plain-language explainer)

This document explains the trading system in everyday terms — no code, no infrastructure
jargon. It assumes you know trading concepts (RSI, MACD, bracket orders, stop-loss/take-profit,
paper trading) but not programming. For the technical version, see
[architecture.md](architecture.md).

## The one-sentence version

Every morning before the market opens, a program builds a short watchlist of stocks (and
optionally crypto) worth trading that day. Then, every ten minutes while the market is open,
a second program looks at each watchlist symbol's technical indicators and asks an AI model
"buy, sell, or hold?" A third program is the only one allowed to actually place an order — it
takes that decision and executes it on Alpaca's **paper** (simulated money) trading account.
A fourth program sends a plain-English recap to Slack at the end of the day.

Nothing here trades with real money. It's paper trading only, and that's not a setting anyone
can flip — it's built in.

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
- It hands all of that — news + real indicator numbers — to the AI model and asks it to pick
  up to 10 symbols worth trading today, with a dollar budget and a written rationale for each.
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
  size, it skips the trade rather than silently rounding it up past the intended budget.
- **Selling:** sells the full position at market. If Alpaca briefly rejects the sell because of
  a conflicting order, it clears the blocker and retries automatically rather than giving up.
- **Watching orders fill:** buys and sells are submitted and acknowledged immediately rather than
  waiting around for a fill, so two background checks each run every 30 seconds — one watching
  for the original buy/sell itself filling, the other watching for stop-loss or take-profit legs
  that filled on their own (which can happen hours after the original buy, with no direct
  request/response to hang a notification off of) — and post a Slack notice for each one as it's
  detected.

Every buy and sell — along with every stop-loss/take-profit fill — gets posted to Slack with
the fill price and reason, so there's a running, human-readable trail of everything the system
actually did, not just what it decided.

### 4. The EOD Report — the daily recap

Runs once a day after market close (9:30pm UTC, which covers the 4pm ET close year-round).
Makes no trading decisions at all — it just reads the account's current state and posts a
plain-English summary to Slack: today's equity, cash, buying power, profit/loss versus
yesterday's close, open positions and their unrealized P&L, and every fill that happened that
day across all the other three workers.

If today wasn't a trading day (weekend or market holiday), it posts a short "market was closed"
notice instead of a full report — so a quiet Slack channel always means "we checked, nothing
happened," never "did this even run?"

## How the pieces hand off to each other

There's no database and no message queue connecting these four workers. The handoff is
deliberately simple:

- **Analyst → Dealer:** the Analyst writes the day's watchlist to one shared, small
  structured record (symbol, budget, indicators to watch, rationale). The Dealer reads that
  same record fresh at the start of every 10-minute cycle — it never caches an old copy. If the
  Analyst hasn't run yet, or failed, the Dealer just keeps using whatever the last known
  watchlist was.
- **Dealer → Floor Broker:** a direct request, in real time, only when the decision isn't
  HOLD. Floor Broker's response (executed, skipped, or error, plus fill price if available)
  goes back to the Dealer's logs and Slack — it isn't stored anywhere further.
- **Everything → Slack:** every consequential event (morning picks, a BUY/SELL/HOLD decision,
  an execution, a bracket TP/SL fill, the EOD recap, a market-closed notice, any error) gets a
  Slack message. A BUY/SELL/HOLD decision, an execution/fill, and an error each carry their own
  Eastern-time timestamp; the morning picks and EOD recaps don't carry a top-level timestamp
  (each fill listed inside an EOD recap does carry its own). Slack is effectively the
  human-readable audit trail for the whole system today — there's no other durable log of
  trading decisions beyond raw application logs and AI-tracing data.

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

## What this system is not (yet)

- It does not remember whether its own past picks were actually good or bad — there's no
  scoring or feedback loop from yesterday's trades into today's decisions yet. That's a planned
  future improvement, not built.
- It does not backtest the AI's actual historical decision-making. There is a separate,
  already-built backtesting tool (see [backtesting.md](backtesting.md)) that checks simple,
  rule-based strategies (buy-and-hold, plain RSI/MACD rules, etc.) against historical prices as
  a sanity-check baseline — but replaying what the live AI itself would have decided on past
  days isn't possible yet, since the exact news/candidates/model behavior from a past day can't
  be reconstructed after the fact.
- It only trades what the Analyst put on the morning watchlist (plus any pre-existing position
  it discovers). It doesn't react to a brand-new, off-watchlist opportunity mid-day.
