import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # API Keys
    ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
    DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
    FRED_API_KEY = os.getenv('FRED_API_KEY')
    NEWS_API_KEY = os.getenv('NEWS_API_KEY')

    # AI Service Configuration
    # 'deepseek' (default) or 'claude'. Whichever is not primary is used as the
    # automatic fallback when its API key is present.
    AI_SERVICE = os.getenv('AI_SERVICE', 'deepseek')

    # Model names — update here to roll all usages at once.
    # MAIN drives reasoning-heavy sections (overview, verdicts, exec summary,
    # risk). FAST handles extraction-style sections (sectors, event scan) where
    # a cheaper/quicker model is plenty.
    CLAUDE_MODEL_MAIN = os.getenv('CLAUDE_MODEL_MAIN', 'claude-sonnet-4-6')
    CLAUDE_MODEL_FAST = os.getenv('CLAUDE_MODEL_FAST', 'claude-haiku-4-5-20251001')

    # deepseek-reasoner is stronger on judgment but slow and pricier per run;
    # deepseek-chat is the workhorse for both tiers by default.
    DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
    DEEPSEEK_MODEL_MAIN = os.getenv('DEEPSEEK_MODEL_MAIN', 'deepseek-chat')
    DEEPSEEK_MODEL_FAST = os.getenv('DEEPSEEK_MODEL_FAST', 'deepseek-chat')

    # Telegram push (the only daily channel; read by modules/telegram_notifier.py)
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # Trading Parameters
    RSI_OVERSOLD = 30
    RSI_OVERBOUGHT = 70
    SMA_SHORT_PERIOD = 10
    SMA_LONG_PERIOD = 50
    EMA_PERIOD = 20

    # Legacy Signal Generation Weights (for backward compatibility)
    TREND_WEIGHT = 0.5
    MOMENTUM_WEIGHT = 0.3
    RSI_WEIGHT = 0.2

    # ============================================================
    # NEW: Enhanced Signal Generation Weights
    # ============================================================
    # Total Technical Analysis weight: 40%
    ENHANCED_TREND_WEIGHT = 0.20        # Trend within TA
    ENHANCED_MOMENTUM_WEIGHT = 0.12     # Momentum within TA
    ENHANCED_RSI_WEIGHT = 0.08          # RSI within TA

    # Additional signal components
    MARKET_CONTEXT_WEIGHT = 0.10        # Market regime + breadth
    SECTOR_WEIGHT = 0.10                # Sector strength
    VOLATILITY_WEIGHT = 0.10            # VIX regime
    PREMARKET_WEIGHT = 0.20             # Gap + volume analysis
    SENTIMENT_WEIGHT = 0.10             # News sentiment

    # Signal Thresholds (legacy momentum system)
    BUY_THRESHOLD = 0.6
    SELL_THRESHOLD = 0.4

    # ============================================================
    # NEW: Value + Expectation Strategy Parameters
    # ============================================================
    # 52-week low proximity filter: buy zone if within this % above 52w low
    NEAR_52W_LOW_THRESHOLD = 0.15       # Within 15% of 52-week low

    # Exit levels
    TAKE_PROFIT_PCT = 0.20              # +20% take profit
    STOP_LOSS_PCT   = 0.10             # -10% stop loss

    # Max buy recommendations per daily report
    MAX_BUY_SIGNALS = 3

    # ============================================================
    # Unified confidence floor & account discipline (2026-07 redesign)
    # ============================================================
    # ONE confidence floor for the whole system: below this a BUY is not
    # shown as a trade setup, not bought by the advice account, not paper
    # traded. Previously the report hid <30% signals but the advice account
    # still bought them (e.g. META at 17%) — that inconsistency is gone.
    MIN_BUY_CONFIDENCE = 40

    # Min confidence % for a signal to appear in Trade Setup cards
    TRADE_SETUP_MIN_CONFIDENCE = MIN_BUY_CONFIDENCE

    # A name stopped out by the advice account may not be re-recommended /
    # re-bought for this many *trading* days (DIS was stopped 7/1 and
    # re-signalled 7/3 under the old 5-day cooldown).
    STOPOUT_REBUY_BAN_DAYS = 20

    # Advice account: at most this many new positions opened per day, so one
    # bad morning can't deploy all remaining cash at once.
    ADVICE_MAX_NEW_POSITIONS_PER_DAY = 1

    # Advice account: max fraction of capital invested, by market regime.
    # Breaks the old "always 100% invested by day 3" behaviour.
    REGIME_MAX_INVESTED = {
        'BULL':     0.80,
        'SIDEWAYS': 0.55,
        'BEAR':     0.25,
    }

    # ============================================================
    # Exit management (2026-07): the system used to only ever say
    # "buy" or "don't buy". Nothing ever told the user to SELL what
    # they already own, and advice-account positions could sit open
    # forever. These drive the position advisor and the account's
    # time/trailing exits.
    # ============================================================
    # Real-holding stop: anchored to cost basis, widened/narrowed into
    # this band, and ratcheted upward only (never loosened).
    HOLDING_STOP_PCT     = 0.10
    HOLDING_MIN_STOP_PCT = 0.06
    HOLDING_MAX_STOP_PCT = 0.18

    # Once a holding is up this many R (R = initial stop distance), the
    # stop moves to breakeven; beyond that it trails the high-water mark.
    HOLDING_BREAKEVEN_R  = 1.0
    HOLDING_TRAIL_PCT    = 0.12

    # A holding that has gone nowhere for this many calendar days while
    # its trend is broken is dead money — flag it for review.
    HOLDING_TIME_STOP_DAYS   = 60
    HOLDING_TIME_STOP_MAX_PNL = 3.0    # % — "gone nowhere" band
    # Earnings inside this window on an oversized position → trim advice.
    HOLDING_EARNINGS_WARN_DAYS = 3
    # What the account was funded with, day one. Fixed — it is the baseline the
    # real account's cumulative P&L is measured against, so it must NOT be
    # rewritten when the account value moves. Set in .env.
    ACCOUNT_INITIAL_CAPITAL = float(os.getenv('ACCOUNT_INITIAL_CAPITAL', 10000))
    # Total account value (cash + holdings) used as the denominator for
    # position 'Weight' in portfolio_manager, and as the advice-tracking
    # account's starting capital. Update it in .env when the real account
    # size changes — it's read from here everywhere instead of being hardcoded.
    TOTAL_ACCOUNT_VALUE = float(os.getenv('TOTAL_ACCOUNT_VALUE', ACCOUNT_INITIAL_CAPITAL))
    # Single position above this share of the portfolio → trim advice.
    HOLDING_TRIM_WEIGHT_PCT = 40.0
    # RSI above this on a profitable holding → partial profit-taking advice.
    HOLDING_OVERBOUGHT_RSI  = 75.0

    # Advice account: a swing position that has neither stopped nor hit
    # target in this many trading days is closed at market. Without it,
    # dead positions permanently consumed the regime invested budget.
    ADVICE_TIME_STOP_DAYS = 20
    # Advice account: lift stop to entry once the position is up 1R.
    ADVICE_BREAKEVEN_R = 1.0

    # Path A (value near 52w low) is disabled entirely when market breadth
    # (share of watchlist above SMA50) drops below this — buying falling
    # knives in a weak tape was the account's main loss driver.
    VALUE_PATH_MIN_BREADTH = 0.40

    # ============================================================
    # Path C — Index ETF dip-buying (SPY/QQQ market timing)
    # ============================================================
    INDEX_TIMING_SYMBOLS = ['SPY', 'QQQ']
    INDEX_DIP_MIN_PCT = 0.04      # pullback from 52w high must exceed this
    INDEX_DIP_MAX_PCT = 0.15      # beyond this it's a downtrend, not a dip
    INDEX_MAX_RSI = 48            # only buy weakness, never chase strength
    INDEX_MIN_PRICE_VS_SMA50 = -0.05  # long trend proxy: not >5% below SMA50
    INDEX_POSITION_SIZE_PCT = 33  # indices can be sized larger than single names

    # Entry is blocked when earnings land inside this window. Was 2 days,
    # which was shorter than the 1–3 week holding period — buying 3 days
    # before a print routinely blew through the structural stop on a
    # binary event the setup never accounted for.
    EARNINGS_ENTRY_BUFFER_DAYS = 7

    # Position size scales with confidence instead of being flat per path:
    # size = base × clamp(confidence / PIVOT). A 41-confidence signal and a
    # 75-confidence signal previously received identical capital.
    # NOTE: as of the 2026-09-12 Core+Satellite redesign this only affects the
    # *displayed* suggestion. Satellite capital is sized by RISK, not by
    # confidence — see SATELLITE_RISK_PER_TRADE_PCT. The 132 logged signals
    # showed 60+ confidence winning 51% at T+20, i.e. confidence carried no
    # predictive power, so sizing by it was sizing by noise.
    CONFIDENCE_SIZING_PIVOT    = 60.0
    CONFIDENCE_SIZING_MIN_MULT = 0.60
    CONFIDENCE_SIZING_MAX_MULT = 1.25

    # ============================================================
    # CORE + SATELLITE architecture (2026-09-12 redesign)
    # ============================================================
    # Diagnosis that produced this design, from data/signal_log.db and the
    # advice account's first 10 closed trades (2026-06-16 → 2026-09-11):
    #   • Account -2.2% while SPY was +1.5%. The loss was structural, not luck.
    #   • 3 winners, ALL exited by time stop; 0 ever reached a target.
    #     7 losers, ALL exited by stop-loss. 30% × $34 − 70% × $30 = −$11/trade.
    #   • Every loser was a consumer name near its 52-week low (Path A/VALUE).
    #     MCD alone got 10 BUY signals on the way down.
    #   • T+20 ticker leaderboard: winners were ABBV/META/MSFT/V/QQQ/BRK-B
    #     (strong names); losers were NFLX/ADBE/NKE/MCD/DIS/PFE (cheap names).
    #   • Path INDEX went 2-for-2. It is the only path with a positive record.
    # Conclusion: buy strength, not cheapness; put most of the money in the
    # index; cap what stock-picking can cost.

    # Sleeve split. Core owns the base rate; satellite is the only sleeve
    # allowed to pick individual stocks.
    CORE_TARGET_PCT      = 75.0    # % of account held in index ETFs
    SATELLITE_TARGET_PCT = 25.0    # % of account available to stock picking

    # Core composition, as {symbol: % of the CORE sleeve}. Must sum to 100.
    # Set to {'SPY': 100.0} for the single-fund version.
    CORE_SYMBOLS = {'SPY': 50.0, 'QQQ': 50.0}

    # Core is bought once, in full, and never sold on a signal. Path C
    # (index dip-buying) is the ONLY mechanism that moves core money: it adds
    # on pullbacks. Rebalance only when a sleeve drifts past this band.
    CORE_REBALANCE_BAND_PCT = 5.0

    # ── Satellite: universe ─────────────────────────────────────────────
    # Relative-strength gate replacing the hand-maintained name list. A stock
    # is buyable only if it is in an uptrend AND outperforming the benchmark.
    # This single rule would have blocked every one of the 7 losing trades.
    SATELLITE_REQUIRE_UPTREND     = True   # price > SMA50 > SMA200
    SATELLITE_RS_BENCHMARK        = 'SPY'
    SATELLITE_RS_LOOKBACK_DAYS    = 126     # ≈ 6 months
    SATELLITE_RS_MIN_EXCESS       = 0.0     # 6m return must beat the benchmark

    # ── Satellite: entry ────────────────────────────────────────────────
    SATELLITE_MAX_POSITIONS = 2
    # Path A (52-week-low value) is demoted to watchlist-only: it may flag a
    # name for the day it turns, but it may never open a position again.
    VALUE_PATH_WATCHLIST_ONLY = True
    # No new satellite entries when the benchmark is below its SMA200, or when
    # breadth is thin. BEAR-regime BUYs won 29% at T+20.
    SATELLITE_REGIME_MIN_BREADTH = 0.40

    # ── Satellite: sizing (the single most important rule) ──────────────
    # Risk a fixed % of the ACCOUNT per trade. Shares = risk$ ÷ (entry − stop),
    # so the stop DISTANCE sets the position size. A 7% stop → ~$285 position,
    # a 10% stop → ~$200. Five straight losses cost 5% of the account, not the
    # 10.7% the old confidence-based sizing actually cost.
    SATELLITE_RISK_PER_TRADE_PCT = 1.0

    # ── Satellite: exit (fixes the inverted payoff) ─────────────────────
    # Fixed targets are deleted. They were never reached — ORCL's sat at +59%
    # — while the −10% stops were hit reliably, so the system booked full
    # losses and clipped its winners. Winners now trail instead.
    SATELLITE_USE_FIXED_TARGETS = False
    SATELLITE_BREAKEVEN_R       = 1.0    # at +1R the stop moves to entry
    SATELLITE_TRAIL_PCT         = 0.09   # then trails 9% under the high-water mark
    # Time stop becomes a dead-money check only, not a profit cap. META was
    # clocked out at +10.5% by the old 20-day rule.
    SATELLITE_TIME_STOP_DAYS    = 30     # calendar days
    SATELLITE_TIME_STOP_MAX_PNL = 3.0    # % — only cut if it has gone nowhere

    # ── Satellite: circuit breakers ─────────────────────────────────────
    # Two consecutive satellite losses → satellite goes to cash for 20 trading
    # days. Core keeps running throughout.
    SATELLITE_LOSS_STREAK_PAUSE = 2
    SATELLITE_PAUSE_DAYS        = 20
    # A name stopped out twice inside 6 months leaves the universe for good.
    # NKE was bought, stopped, and bought again inside three weeks.
    SATELLITE_EXILE_STOPOUTS    = 2
    SATELLITE_EXILE_LOOKBACK_DAYS = 182
    # Kill switch: if the satellite sleeve is down this much of the TOTAL
    # account over a rolling 6 months, it is done for the calendar year.
    # Decided now, while calm — not later, mid-drawdown.
    SATELLITE_KILL_SWITCH_PCT     = 5.0
    SATELLITE_KILL_LOOKBACK_DAYS  = 182

    # ── Satellite: proof before real money ──────────────────────────────
    # The satellite runs paper-only until it has produced this many closed
    # trades with positive alpha vs. simply holding SPY with the same capital.
    SATELLITE_PAPER_ONLY   = True
    SATELLITE_PROVE_TRADES = 20
    # After PROVE_TRADES closed trades, alpha ≤ 0 retires the sleeve for good.
    SATELLITE_RETIRE_ALPHA_PCT = 0.0

    # Value trap filters
    REVENUE_DECLINE_THRESHOLD = -0.10  # Flag if revenue fell >10% YoY
    DEBT_EQUITY_MAX = 5.0              # Flag if D/E ratio exceeds 5x
    VALUE_TRAP_MIN_MARKET_CAP = 1e9    # Exclude <$1B market caps in value plays (liquidity/survivorship risk)

    # Dynamic Weight Adjustments per Market Regime (reserved for future use)
    DYNAMIC_WEIGHT_BEAR = {'trend': 0.05, 'momentum': -0.03, 'volatility': 0.02}
    DYNAMIC_WEIGHT_BULL = {'momentum': 0.05, 'sector': 0.03, 'sentiment': 0.02}
    DYNAMIC_WEIGHT_HIGH_VOL = {'trend': 0.03, 'volatility': 0.05, 'momentum': -0.02}

    # VIX Tiered Score Adjustment (original_mult, neutral_mult)
    # score = original * orig_mult + 0.5 * neutral_mult
    VIX_ADJ_25_30 = (0.9, 0.1)
    VIX_ADJ_30_35 = (0.75, 0.25)
    VIX_ADJ_ABOVE_35 = (0.6, 0.4)

    # Liquidity Adjustment
    LOW_LIQUIDITY_THRESHOLD = 0.7       # Volume ratio below this triggers penalty
    LOW_LIQUIDITY_PENALTY = 0.2         # 20% score penalty

    # Risk-Reward Score Feedback
    RR_BOOST_THRESHOLD = 2.0            # R/R >= this gets score boost
    RR_BOOST_MULTIPLIER = 1.1
    RR_PENALTY_THRESHOLD = 1.0          # R/R < this gets score penalty
    RR_PENALTY_MULTIPLIER = 0.85

    # Risk Management Settings
    MIN_RISK_REWARD_RATIO = 1.5         # Minimum acceptable R/R ratio

    # Do-Not-Trade Filter Thresholds
    DNT_MIN_VOLUME_RATIO = 0.3          # Avoid very low volume
    DNT_MAX_VIX = 35                    # Avoid trading in extreme VIX
    DNT_MIN_MARKET_CAP = 1e8            # Avoid micro caps ($100M)
    DNT_MAX_SPREAD_PCT = 2.0            # Avoid high spread stocks

    # VIX Regime Thresholds
    VIX_LOW = 12                        # Low volatility regime
    VIX_NORMAL = 20                     # Normal volatility
    VIX_ELEVATED = 25                   # Elevated volatility
    VIX_HIGH = 30                       # High volatility
    VIX_EXTREME = 40                    # Extreme volatility / panic

    # Gap Classification Thresholds (percentage)
    GAP_MICRO = 0.5                     # Micro gap
    GAP_SMALL = 1.0                     # Small gap
    GAP_MEDIUM = 2.0                    # Medium gap
    GAP_LARGE = 4.0                     # Large gap
    GAP_EXTREME = 7.0                   # Extreme gap

    # Data Storage
    DATA_DIR = 'data'
    LOG_DIR = 'logs'
    REPORT_DIR = 'Stock Market Daily Report'

    # Schedule Configuration
    MARKET_TIMEZONE = 'US/Eastern'
    ANALYSIS_HOUR = 9  # 9:00 AM ET (30 mins before market open at 9:30 AM)

    # Notification Configuration
    ENABLE_DESKTOP_NOTIFICATIONS = os.getenv('ENABLE_DESKTOP_NOTIFICATIONS', 'true').lower() == 'true'
    NOTIFICATION_SOUND = os.getenv('NOTIFICATION_SOUND', 'true').lower() == 'true'

    # IBKR Dual-Account Config
    # IB Gateway ports: Live=4001, Paper=4002
    # TWS ports:        Live=7496, Paper=7497
    IBKR_HOST = os.getenv('IBKR_HOST', '127.0.0.1')
    IBKR_LIVE_PORT = int(os.getenv('IBKR_LIVE_PORT', 4001))   # IB Gateway live
    IBKR_PAPER_PORT = int(os.getenv('IBKR_PAPER_PORT', 4002)) # IB Gateway paper
    IBKR_CLIENT_ID_LIVE = int(os.getenv('IBKR_CLIENT_ID_LIVE', 1))
    IBKR_CLIENT_ID_PAPER = int(os.getenv('IBKR_CLIENT_ID_PAPER', 2))
    IBKR_READONLY_LIVE = True  # Hard-coded safety: live account is always read-only

    # Paper Trading
    ENABLE_PAPER_TRADING = os.getenv('ENABLE_PAPER_TRADING', 'false').lower() == 'true'

    # Data Pipeline
    CACHE_DIR = 'cache'
    OHLCV_DIR = 'data/ohlcv'
    DATA_FETCH_WORKERS = int(os.getenv('DATA_FETCH_WORKERS', 5))

    # Paper Trading position limits
    PAPER_POSITION_PCT = float(os.getenv('PAPER_POSITION_PCT', 0.10))  # 10% per position
    PAPER_MAX_POSITIONS = int(os.getenv('PAPER_MAX_POSITIONS', 8))
    PAPER_MAX_SINGLE_POSITION_PCT = float(os.getenv('PAPER_MAX_SINGLE_POSITION_PCT', 0.20))
    PAPER_STOP_LOSS_PCT = float(os.getenv('PAPER_STOP_LOSS_PCT', 0.05))   # -5%
    PAPER_TAKE_PROFIT_PCT = float(os.getenv('PAPER_TAKE_PROFIT_PCT', 0.10))  # +10%