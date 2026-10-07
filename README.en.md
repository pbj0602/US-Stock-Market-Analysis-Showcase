# US Pre-Market Stock Analysis System

[中文](README.md) | **English**

> 📌 **Showcase snapshot.** This repository is released periodically from a private one where active development happens, so its commit history reflects releases rather than day-to-day work.

A buy-side-style US equity analysis system that runs itself every trading day. Before the open (9:00 AM ET) it pulls market data, generates multi-factor signals, has an LLM (DeepSeek, with Claude as fallback) write the narrative analysis, renders an HTML report, and pushes a one-screen summary plus the full report to Telegram. After the close (4:45 PM ET) it scores the day's signals and builds a watchlist for tomorrow.

The report and push messages are in Chinese. Both the Telegram summary and the HTML report read from one shared data model (`modules/report_model.py`) and pass through a validation layer before anything is published (see below).

**Highlights**

- **Rules decide, AI explains.** Position sizing and exits are rule-based. The LLM can only *lower* a signal's confidence based on external risk it cites from the input — it can never raise it or recommend position sizes.
- **Core + satellite risk architecture**, redesigned from the system's own trade record: 75% index core that is never sold on a signal, 25% stock-picking satellite sized by 1% account risk per trade, behind four circuit breakers.
- **The system grades itself.** Every signal is logged with its final decision; T+5 / T+20 returns are back-filled daily, and an "if I had followed the advice" paper account tracks alpha vs. SPY.
- **Data is validated, not silently patched.** Bad bars, inconsistent regimes, impossible stops and AI hallucinated numbers are flagged and, if severe, block the affected conclusion.

---

## Features

### Telegram push (one screen per day)

| Block | Content |
|------|------|
| Status line | Date · bull / bear / sideways · VIX · SPY distance from 52-week high |
| 🚨 To do today | Real-money actions, max 3 (sell / trim / build core); otherwise "no action needed" |
| 📡 Signals | Entries and avoids; otherwise "no signals today" |
| 🧭 Read | Three AI lines (main driver / best opportunity / biggest risk) |
| 💼 Accounts | Real account + advice-tracking account + satellite validation progress |
| 👀 Watch tomorrow | Max 2 items |
| Data health | ✅ all checks passed / ⚠️ N issues |

The full HTML report is attached. A separate alert is sent when a severe data problem is found.

### HTML report layout

| Section | Content |
|------|------|
| Data health | Only shown when validation finds problems; lists each one |
| 🚦 Market state / TL;DR | Regime / risk level / VIX; three-sentence summary |
| 🧭 Strategy status | Core and satellite sleeves; watchlist (only names stabilising ≥ 3/4) |
| 🚨 Holding actions | Sell / trim instructions for real holdings |
| 📊 Today at a glance | Three-line AI summary |
| 🎯 Action list | Can do / not recommended / focus / watch tomorrow |
| 🏭 Sectors | Sector table (last session / 5-day / RSI / strength) |
| 💼 Real holdings | P&L and stops |
| 📡 Watchlist signals | Entries + avoids + top 5 holds by score |
| 🎯 Trade plans / 🔍 New ideas | Only when there is content |
| 📈 Index timing | One line: SPY / QQQ add-on window status |
| 💰 If I had followed the advice | Advice-tracking account; last 3 closed trades |

### AI layer (DeepSeek `deepseek-chat`, Claude fallback)

Five API calls per day; calls 2–4 run **in parallel**:

| Call | Content | Mode |
|------|------|---------|
| Call 1 | Market overview + signal brief + risk assessment (batched) | Sequential (first) |
| Call 2 | Holdings analysis (if any holdings) | **Parallel** |
| Call 3 | Growth outlook + trade-plan cards + **AI confidence review (can only lower, 0–30 pts)** | **Parallel** |
| Call 4 | Major-event impact | **Parallel** |
| Call 5 | Executive summary (compact JSON → 3 sentences) | Sequential (last) |

Every prompt carries the same strategy guardrail (`STRATEGY_GUARDRAIL`): sizing and selling are decided by rules, and the AI may not give position percentages or "go to cash / liquidate" advice. The validator strips any sentence that slips through.

**Narrative first:** the market overview and executive summary start from the day's main catalyst (high-impact macro event / cross-asset risk-off signal / earnings) and only then bring in technicals — never from RSI/SMA numbers.

### Data validation

Principle: **if something is wrong, flag it — never quietly replace it with a value that looks normal.**

1. **Input** (`modules/price_history.py`): every daily bar goes through here. Empty pre-open bars that yfinance injects are dropped; trading days missing from the daily series are filled from hourly bars; single-day moves > 25% are treated as suspect quotes. The trading calendar lives in `modules/trading_calendar.py`.
2. **Cross-checks** (`modules/report_validator.py`): the bull/bear call must agree with SPY's position vs. SMA50/200; sell/trim instructions only target real holdings and have stops > 0; BUY stops sit below the current price; sector moves agree with their constituents; identical values across several sectors raise an alarm.
3. **AI output**: sentences that break sizing rules are removed; VIX / SPY levels quoted by the AI must match the actual data; missing or truncated sections are logged.
4. **Rendering**: HTML is auto-escaped; Telegram messages over 4096 characters are split.

Issues have two severities (`modules/data_health.py`): **severe** (market state, holdings, stops) blocks the affected conclusion and sends an alert; **minor** issues are shown as usual and noted at the bottom. Each day's report data is snapshotted to `data/snapshots/` so problems can be reproduced.

### Watchlist (53 tickers)

| Sector | Tickers |
|------|------|
| Mega-cap tech | AAPL MSFT GOOGL AMZN META NVDA TSLA |
| Mid-cap tech / semis | AMD CRM ADBE ORCL QCOM AVGO |
| Healthcare | UNH LLY JNJ ABBV MRK PFE |
| Financials | JPM BAC GS V MA BRK-B |
| Energy | XOM CVX COP |
| Consumer discretionary | MCD NKE HD SBUX |
| Consumer staples | WMT COST PG KO |
| Industrials | CAT HON GE UPS BA |
| Communication | DIS NFLX VZ |
| Broad ETFs (benchmarks) | SPY QQQ IWM |
| Sector ETFs (rotation) | XLK XLF XLE XLV XLI XLP XLY |

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# AI analysis (one required; DeepSeek primary, Claude fallback)
AI_SERVICE=deepseek
DEEPSEEK_API_KEY=your_deepseek_api_key
ANTHROPIC_API_KEY=your_claude_api_key   # optional, used if DeepSeek fails

# Macro data (optional, FRED is free)
FRED_API_KEY=your_fred_key

# News (optional, NewsAPI free tier)
NEWS_API_KEY=your_newsapi_key

# Telegram push (create a bot with @BotFather, send it one message first)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# IBKR (optional, needs TWS or IB Gateway running locally)
IBKR_HOST=127.0.0.1
IBKR_LIVE_PORT=4001
IBKR_PAPER_PORT=4002

# Account size (used for position weights and the advice-tracking account)
ACCOUNT_INITIAL_CAPITAL=10000
TOTAL_ACCOUNT_VALUE=10000
```

### 3. Add holdings (optional)

`cp data/portfolio_example.csv data/portfolio.csv`, then edit:

```csv
Symbol,Shares,Purchase_Price,Purchase_Date
AAPL,10,185.50,2025-12-01
NVDA,5,110.00,2026-01-15
```

With holdings present, the report shows per-holding signals and P&L, and those tickers are excluded from the watchlist.

### 4. Run

**Pre-market analysis** (9:00 AM ET, or manually):
```bash
python3 main.py
```

**Post-market review** (4:45 PM ET, or manually):
```bash
python3 post_market_runner.py
# or review a specific date
python3 post_market_runner.py 2026-05-09
```

Reports are written to `Stock Market Daily Report/`.

---

## Architecture

### Main pipeline (`main.py`, 12 steps)

```
Step 1   MarketContext          Regime detection (SPY/QQQ trend, VIX regime, sector strength)
Step 2   PremarketAnalyzer      Pre-market gap and volume analysis
Step 3   EventRiskEngine        Earnings calendar + macro event risk (adjusts signal weights)
Step 3b  CrossAssetMonitor      Cross-asset snapshot (DXY / copper / oil / JPY / HYG)
Step 3c  OptionsSentiment       Put/call ratio + VIX term structure
Step 3d  MacroEventAlerts       FOMC / CPI / NFP within 3 days
Step 4   DataScraper            yfinance prices + indicators (SMA / EMA / RSI / MACD / BB)
Step 5   FinancialDataScraper   FRED macro data + fundamentals
Step 6   NewsScraper            News + VADER sentiment
Step 7   PortfolioManager       Holdings snapshot + P&L
Step 8   SignalGenerator        Value-reversion + trend-breakout paths + do-not-trade filter
Step 9   StockSuggester         Opportunity scan outside the watchlist (100+ tickers)
Step 10  AIAnalyzer             DeepSeek API (call 1 sequential, 2–4 parallel, 5 sequential) + validator
Step 11  ReportGenerator        Cross-checks → Jinja2 HTML report → Telegram push + snapshot
Step 12  AdviceTracker          Update the advice-tracking account
```

A failed run pushes an error alert to Telegram, so it never fails silently.

### Module map

```
main.py                          # Entry point (Telegram alert on failure)
post_market_runner.py            # Post-market review (signal hit rate + tomorrow's watchlist)
run_wrapper.py                   # launchd wrapper: timezone-safe 9:00 AM ET trigger, holiday guard
postmarket_wrapper.py            # launchd wrapper for the post-market run
web_server.py                    # Optional local server: view reports, refresh with progress bar
signal_stats.py                  # CLI: signal accuracy by regime / score bucket / ticker
config/
  config.py                      # All parameters (weights / thresholds / API config)
templates/
  daily_report.html.j2           # Jinja2 HTML report template
modules/
  data_scraper.py                # Prices + technical indicators
  financial_data_scraper.py      # FRED macro + fundamentals
  news_scraper.py                # News + sentiment
  market_context.py              # Regime / VIX / sector strength
  premarket_analyzer.py          # Pre-market gaps
  event_risk_engine.py           # Earnings event-risk engine
  cross_asset_monitor.py         # Cross-asset monitor
  options_sentiment.py           # Options sentiment (put/call)
  macro_event_alerts.py          # Macro calendar alerts
  signal_generator.py            # Multi-factor signal generation
  ai_analyzer.py                 # AI layer (DeepSeek → Claude fallback)
  report_generator.py            # HTML report generation
  portfolio_manager.py           # Holdings
  position_advisor.py            # Exit engine for real holdings (sell / trim, ratcheting stops)
  ibkr_connector.py              # IBKR market data
  ibkr_manager.py                # IBKR paper trading (live account hard-wired read-only)
  signal_tracker.py              # Signal performance tracking (SQLite)
  advice_tracker.py              # Advice-tracking paper account
  telegram_notifier.py           # Telegram push (summary + HTML attachment + alerts)
  notification_service.py        # Desktop notifications
  report_model.py                # Data model shared by both channels
  report_validator.py            # Pre-publish cross-checks + AI text filter
  data_health.py                 # Data-health log (severe / minor / fixed)
  price_history.py               # Unified daily bars (drop unfinished bars, fill gaps)
  trading_calendar.py            # US trading calendar
data/
  portfolio_example.csv          # Holdings template; copy to portfolio.csv (git-ignored)
Stock Market Daily Report/       # Generated reports (git-ignored)
```

---

## Core + satellite strategy

> **This redesign came from the system's own results, not theory.** The advice-tracking account's
> first 10 closed trades: all 3 winners were cut by the time stop and **none ever reached its target**;
> all 7 losers were **stopped out**. A 30% win rate with average win ≈ average loss is a
> **negative expectancy per trade** — not bad luck, but the design. Over the same period the account
> was −2.2% while SPY was +1.5%. **Every** loser was a consumer name near its 52-week low (path A);
> MCD alone got **10** BUY signals on the way down. The best T+20 names were ABBV/META/MSFT/V/QQQ
> (strong), the worst NFLX/ADBE/NKE/MCD/DIS/PFE (cheap). **Cheap kept getting cheaper; strong kept
> getting stronger.**

The account is split into two sleeves with completely different jobs.

### Core — 75%: earn the market
- Holds `CORE_SYMBOLS` (default SPY/QQQ 50/50; set `{'SPY': 100.0}` for a single fund)
- **Bought once, in full, never sold on a signal.** No stop, no target, no time stop; AVOID signals don't sell it
- Path C (index dip-buying) is the **only** thing that moves core money: it adds on 4–15% pullbacks
- `PositionAdvisor` returns HOLD for core symbols and applies no exit rules
- Why: path C is the only path with a positive record (2 for 2)

### Satellite — 25%: the only place allowed to pick stocks
| Rule | Setting | Why |
|------|------|--------|
| Max positions | 2 | Caps the damage stock picking can do |
| Universe | Price > SMA50 > SMA200 **and** 6-month return beats SPY | Replaces a hand-maintained list. This one rule would have blocked all 7 losers |
| Entry | Path B only: pullback to SMA50 support, or breakout on volume | Buy pullbacks in uptrends, not in downtrends |
| Regime gate | No new entries when SPY < SMA200 or breadth < 40% | BEAR-regime BUYs won 29% at T+20 |
| **Sizing** | **Risk 1% of the account per trade; shares = risk ÷ (entry − stop)** | **Stop distance sets size.** The old confidence-based sizing was sizing by noise: 60+ confidence won 51% at T+20 |
| Take profit | **No fixed targets.** At +1R the stop moves to entry, then trails 9% below the high | Targets were never reached (ORCL's sat at +59%) while stops hit reliably — the payoff was inverted |
| Time stop | Close only after 30 days *and* < +3% | The old unconditional 20-day rule cut META at +10.5% |
| Loss-streak breaker | 2 straight losses → satellite pauses 20 trading days (**core keeps running**) | — |
| Exile | Same ticker stopped out twice in 6 months → removed from the universe (`data/exile_registry.json`) | NKE was bought, stopped, bought and stopped again in three weeks |
| **Kill switch** | Satellite down 5% of the account over a rolling 6 months → **off until year end, core only** | Decided while calm, not mid-drawdown |

### Path A (near 52-week low value) → **watchlist only**
Permanently loses the right to open positions. It only flags names that may be turning: the report lists them under "👀 Watchlist (no entry)" with exactly **what is still missing before they become buyable**. Only once a name is above SMA50/SMA200 and beating SPY does path B take over.

### How it is judged — a different question
Not "is the account above its starting capital?" but "did the **satellite** beat putting the same money into SPY?" (`alpha_vs_spy_pct`), with a monthly scorecard. **If alpha is still ≤ 0 after 20 trades, the satellite is retired for good** and the system keeps only the core, exit management, regime gate and risk summary — the parts it is actually good at.

### Rollout order
1. **Put real money into the core first** — being in cash is a position too, and over the previous three months it was the wrong one (SPY +1.5%)
2. The satellite **paper trades** in the advice account first (`SATELLITE_PAPER_ONLY = True`); real money only after 20 trades with positive alpha
3. Only then fund the satellite with its 25%

> The first run performs a one-time migration: legacy positions from the old strategy are closed (tagged "strategy redesign", not counted toward loss streaks or exile), the cash moves into the core, and the satellite's score resets to start its 20-trade validation.

## Signal generation (path details)

Design principle: **fewer, better trades.** BUY signals come from three independent paths; everything else is HOLD / AVOID.

**Path A — value reversion (near 52-week low, needs reversal confirmation)**
1. Price within 15% of its 52-week low
2. **Regime gate**: no new value entries in a BEAR regime or when breadth (share of watchlist above SMA50) < 40%
3. Value-trap filters (revenue down > 10% / debt-to-equity > 5× / collapsing earnings / market cap < $1B)
4. **Falling-knife veto**: SMA10 more than 2% below SMA50 / 20-day momentum < −5% / bearish MACD — **any two veto the trade**
5. **External veto**: clearly negative recent news (mean ≤ −0.3 with ≥ 2 negative items), or earnings within 2 days
6. RSI ≤ 60 + 5-trading-day cooldown
7. **Stabilisation filter**: stabilisation score (oversold / slowing decline / MACD turning up / up on volume) **≥ 2/4 to qualify**

**Path B — trend breakout (momentum recovery)**
1. Price just crossed above SMA50 — a real cross within 3 sessions, now 0–8% above the average
2. At least 2 of: RSI 45–65 / volume > 1.2× / MACD bullish cross or 10-day momentum > 3%

**Path C — index timing (SPY / QQQ: "when to buy the index")**
- 🟢 **Add-on window**: 4–15% below the 52-week high + long-term trend intact (within 5% below SMA50) + RSI ≤ 48 + not BEAR → BUY signal (ranked ahead of single stocks)
- 🟡 Wait: pullback is there but confirmation is missing (RSI high / BEAR regime)
- 🔴 Trend broken: more than 5% below SMA50 or pullback > 15% — treated as a downtrend
- Index targets are not capped at the 52-week high (indices make new highs); the report always shows an "📈 Index timing" card

**Ranking by win rate, not by how beaten-down a name is**: candidates are sorted by confidence. The old version sorted by distance to the low, putting the worst names first.

**Stops / targets**: stop 1% below the recent swing low (20-day low); target anchored to recent resistance (60-day high) and capped at the 52-week high (except indices). Stops are clamped to [6%, 18%]; **if the structural stop is beyond 18% the trade is skipped** rather than force-clamped. Minimum R/R 1.5.

**One confidence floor for the whole system**: `MIN_BUY_CONFIDENCE = 40`. A BUY below 40% gets no trade-plan card, is not bought by the advice account, and is not paper traded.

**Stop-out ban**: a ticker stopped out by the advice account is not recommended or re-bought for 20 trading days (`data/stopout_registry.json`).

**VIX / regime / sector adjustments** (multiplicative): VIX 25–30 ×0.9, 30–35 ×0.75, > 35 ×0.6; BEAR regime ×0.75; sector relative strength ×0.90–1.10.

**Do-not-trade filter**: volume ratio < 0.3, VIX > 35, market cap < $100M, or spread > 2%.

## AI confidence review (can only lower)

In call 3 (trade-plan batch) the AI also returns a structured `[ADJUST]` verdict for each BUY: based on **external risks the technical signal does not capture** — news, upcoming earnings, macro events, deteriorating fundamentals — it may deduct 0–30 confidence points, and must cite a specific fact from the input. **No extra API call, zero added cost.**

- Rules keep the final say: a BUY is only downgraded to HOLD (and its trade card removed) if the adjusted confidence falls below the 40 floor
- The adjustment happens before the report, the advice account and the signal log, so all three see the same final decision
- Every adjustment is written into the signal's reasoning (`AI external review: confidence 55→40 (…)`) for auditability
- The prompt includes the rolling historical hit rate (e.g. "last 60 days BUY T+5 win rate 53%") as calibration

## Feedback loop (the signals' real report card)

The first version logged 116 BUY signals and zero outcomes — the system could not measure itself. Now:

- Pre-market, each signal's **final decision** (after AI review), entry price and confidence go to `signal_log.db`
- Post-market, **T+5 / T+20 returns** are back-filled daily (matching the 1–3 week swing horizon; the old same-day open→close hit rate did not match the strategy)
- The post-market report shows a rolling report card: T+5 / T+20 win rate, average return, and results by confidence bucket

---

## Catalyst-first framework

Before any AI analysis, a "catalyst context" is built in priority order:

1. **Cross-asset risk-off signals** (DXY anomaly / widening HYG credit spreads)
2. **High-impact macro events today** (CPI / NFP / FOMC) — the market prices around these
3. **Macro calendar within 3 days** (early warning)
4. **Earnings this week** (flagged per ticker; wait until after the print)
5. **Options sentiment** (put/call ratio + VIX term structure)

If a catalyst exists, the first sentence of the executive summary and market overview must say *what the market is trading today* — not start from RSI/SMA numbers.

---

## Advice-tracking account (`advice_tracker.py`)

Answers one question: **if I had followed the system's daily advice from day one, would I be up or down?**

- **Starting capital** comes from `ACCOUNT_INITIAL_CAPITAL` in `.env`; the account runs the [core + satellite](#core--satellite-strategy) structure.
- **Core (75%)**: `_ensure_core()` buys SPY/QQQ once in full; `_process_core_adds()` adds on path C signals. The core never goes through `_process_exits` / `_process_time_stops` / `_process_avoids`.
- **Satellite (25%, max 2 names)**: risk-based sizing (`shares = 1% of account ÷ (entry − stop)`), no fixed target, breakeven at +1R then a 9% trailing stop (`_ratchet_stops`), 30-day dead-money cleanup.
- **Four circuit breakers** (`satellite_gate()`, checked by severity):
  - `KILLED` — down 5% of the account over a rolling 6 months → off until year end (`_check_kill_switch`)
  - `PAUSED` — 2 straight losses → paused 20 trading days (core unaffected)
  - `RETIRED` — 20 trades with α ≤ 0 → stock picking retired permanently
  - `FULL` — already holding 2 names

  Plus **exile**: same ticker stopped out twice in 6 months (`data/exile_registry.json`; the signal layer stops recommending it too).
- **Paper validation**: with `SATELLITE_PAPER_ONLY = True`, satellite positions are tagged `paper` and the report says "do not use real money" throughout.
- Output: account value, return, P&L, win rate, sleeve breakdown (`sleeves`), **satellite alpha vs. SPY with a monthly scorecard** (`satellite_monthly`), closed trades.
- State persists in `data/advice_account.json` and compounds day over day (NaN prices are filtered so they never corrupt the equity curve).

---

## Post-market review (`post_market_runner.py`)

Runs after the close and produces:

- **Signal hit rate**: today's BUY/SELL direction accuracy + average return (vs. today's open)
- **Rolling report card**: back-filled T+5 / T+20 returns for past BUY signals; 60-day win rate, average return, results by confidence bucket
- **Tomorrow's watchlist (three tiers)**:
  - BUY signals still valid (stop not hit; watch for follow-through)
  - Extreme RSI (HOLD names with RSI < 32 or > 68; watch for reversals)
  - Earnings tomorrow (consider sizing down)
- **Report**: Markdown saved to `Stock Market Daily Report/post_market_YYYYMMDD.md`
- **Push**: converted to Telegram format and sent

---

## Scheduling

macOS launchd (plists in `~/Library/LaunchAgents/`) fires the wrappers every 15 minutes; the wrappers compute "now" in US/Eastern and run the real job once per trading day, so the schedule survives travel and timezone changes.

| Job | Time | Command |
|------|------|------|
| Pre-market analysis | 9:00 AM ET | `python3 run_wrapper.py` → `main.py` |
| Post-market review | 4:45 PM ET | `python3 postmarket_wrapper.py` → `post_market_runner.py` |

---

## Notes

- For learning and personal research only — **not investment advice**
- The IBKR live account is hard-wired read-only (`IBKR_READONLY_LIVE = True`) and never places orders
- API keys live in a local `.env` file and are not version-controlled
- IBKR requires TWS or IB Gateway running locally with the matching port open
