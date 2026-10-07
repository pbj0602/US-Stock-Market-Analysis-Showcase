"""
Signal Generation Module — CORE + SATELLITE strategy (2026-09-12 redesign)

The old design bought whatever looked cheapest. Measured over 132 logged
signals and the advice account's first 10 round-trips, that lost money
structurally: every single loser was a consumer name near its 52-week low,
while the names that actually worked (ABBV, META, MSFT, V, QQQ) were the
strong ones. Cheap kept getting cheaper. The paths below encode the opposite
bias — buy strength, and put most of the money in the index.

Path C — INDEX (the CORE sleeve, ~75% of the account):
  The only path that moves core money. Buys SPY/QQQ on 4–15% pullbacks from
  the 52-week high while the long trend holds. Core is never sold on a signal.
  Record to date: 2 for 2 — the only path with a positive history.

Path B — TREND (the SATELLITE sleeve, ~25%, max 2 positions):
  The only path allowed to open a single-stock position. Gated by a
  relative-strength universe test that replaces the hand-kept name list:
      price > SMA50 > SMA200  AND  6-month return > the benchmark's
  Entry is then either a pullback to rising SMA50 support, or a breakout
  confirmed by volume. No fixed target — winners trail; see AdviceTracker.
  Position size comes from risk, not conviction: shares = risk$ ÷ (entry−stop).

Path A — VALUE (near 52-week low): WATCHLIST ONLY.
  Retained purely to flag names for the day they turn. It may no longer open
  a position under any circumstance. This is the single biggest change; it
  would have blocked all 7 losing trades.
"""

import fcntl
import json
import os
import pandas as pd
from datetime import datetime
import logging
from config.config import Config

logger = logging.getLogger(__name__)


class SignalGenerator:
    def __init__(self):
        self.rsi_oversold   = Config.RSI_OVERSOLD    # 30 — boosts confidence
        self.rsi_overbought = Config.RSI_OVERBOUGHT  # 70

        self._market_context = None
        self._premarket_data = None
        self._event_analysis = None
        self._news_sentiment = None

        self._cooldown_file = 'data/signal_cooldown.json'
        self._cooldown_days = 5

        # Stop-out registry: names the advice account stopped out recently.
        # Written by AdviceTracker; a stopped name may not be re-recommended
        # for STOPOUT_REBUY_BAN_DAYS trading days (prevents DIS-style
        # stop-out-then-rebuy churn).
        self._stopout_file = 'data/stopout_registry.json'

        # Permanent exile list written by AdviceTracker: names stopped out
        # twice inside six months. A banned name must not even be *shown* as a
        # setup — printed advice gets acted on by hand.
        self._exile_file = 'data/exile_registry.json'

        # Per-run index timing status (SPY/QQQ), exposed via get_index_timing_status()
        self._index_timing = {}

        # Per-run Core+Satellite state, exposed to the report layer.
        self._watchlist = []              # Path A names being watched for a turn
        self._benchmark = {}              # SPY trend + 6m return
        self._satellite_blocked = False   # master switch on new stock picks
        self._satellite_block_reason = ''

    # ── Context setters ──────────────────────────────────────────────────────

    def set_market_context(self, context):
        self._market_context = context

    def set_premarket_data(self, data):
        self._premarket_data = data

    def set_event_analysis(self, analysis):
        self._event_analysis = analysis

    def set_news_sentiment(self, sentiment):
        self._news_sentiment = sentiment

    # ── Cooldown helpers ─────────────────────────────────────────────────────

    def _load_cooldown(self):
        """Load the cooldown registry {symbol: last_recommended_date} from disk."""
        os.makedirs(os.path.dirname(self._cooldown_file) or '.', exist_ok=True)
        lock_path = self._cooldown_file + '.lock'
        try:
            with open(lock_path, 'w') as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_SH)
                try:
                    with open(self._cooldown_file) as f:
                        return json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    return {}
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError:
            return {}

    def _save_cooldown(self, cooldown):
        """Persist the cooldown registry to disk (exclusive lock prevents corruption)."""
        os.makedirs(os.path.dirname(self._cooldown_file) or '.', exist_ok=True)
        lock_path = self._cooldown_file + '.lock'
        tmp_path = self._cooldown_file + '.tmp'
        with open(lock_path, 'w') as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                with open(tmp_path, 'w') as f:
                    json.dump(cooldown, f, indent=2)
                os.replace(tmp_path, self._cooldown_file)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    def _is_on_cooldown(self, symbol, cooldown):
        """Return True if symbol was recommended within _cooldown_days *trading* days.

        Uses pd.bdate_range to count business days so weekends don't shorten
        the effective cooldown (e.g. a Friday signal stays blocked through the
        following Thursday, not just through Monday).
        """
        last_date_str = cooldown.get(symbol)
        if not last_date_str:
            return False
        return self._within_trading_days(last_date_str, self._cooldown_days)

    @staticmethod
    def _within_trading_days(date_str: str, n_days: int) -> bool:
        """True if date_str is within the last n_days *trading* days."""
        try:
            last_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            today = datetime.now().date()
            if today <= last_date:
                return True
            trading_days_elapsed = len(pd.bdate_range(
                start=last_date, end=today, inclusive='left'
            ))
            return trading_days_elapsed < n_days
        except Exception:
            return False

    def _load_stopouts(self) -> dict:
        """Load {symbol: stopout_date} written by AdviceTracker."""
        try:
            with open(self._stopout_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _load_exiles(self) -> set:
        """Symbols permanently barred from the satellite universe."""
        try:
            with open(self._exile_file) as f:
                return set(json.load(f).get('exiled') or [])
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            return set()

    def _is_stopout_banned(self, symbol, stopouts) -> bool:
        """True if the advice account was stopped out of symbol recently."""
        date_str = stopouts.get(symbol)
        if not date_str:
            return False
        return self._within_trading_days(date_str, Config.STOPOUT_REBUY_BAN_DAYS)

    # ── Main signal generation ───────────────────────────────────────────────

    def generate_signals(self, market_data, portfolio_snapshot=None):
        """
        Value + Growth Expectation strategy:
          1. Filter watchlist for stocks within 15% of their 52-week low
          2. Exclude value traps (revenue collapsing, extreme debt, bad news)
          3. Require RSI ≤ 60 — oversold confirmation, avoids catching a still-falling knife
          4. Skip symbols on 5-day cooldown — avoids repeating same picks daily
          5. Rank remaining candidates by proximity to 52-week low
          6. Top MAX_BUY_SIGNALS → BUY  (with entry / take-profit / stop-loss prices)
          7. Everything else → HOLD
        """
        if market_data.empty:
            logger.error("No market data provided for signal generation")
            return pd.DataFrame()

        try:
            cooldown  = self._load_cooldown()
            stopouts  = self._load_stopouts()
            exiles    = self._load_exiles()
            today_str = datetime.now().strftime('%Y-%m-%d')
            if exiles:
                logger.info(f"已永久移出可选范围（6个月内2次止损）: {', '.join(sorted(exiles))}")

            # Market breadth: share of the universe above SMA50. A cheap stock
            # in a weak tape is usually a knife, so Path A shuts off entirely
            # in BEAR regime or when breadth is poor.
            pvs = market_data.get('Price_vs_SMA50')
            if pvs is not None:
                valid = pvs.dropna()
                breadth = float((valid > 0).mean()) if len(valid) else 0.5
            else:
                breadth = 0.5
            regime = self._get_market_regime_label()
            value_path_blocked = (
                regime == 'BEAR' or breadth < Config.VALUE_PATH_MIN_BREADTH
            )

            # ── Satellite master switch ───────────────────────────────────
            # No new single-stock position opens while the benchmark itself is
            # below its 200-day line, or while breadth is thin. The core sleeve
            # is unaffected and keeps running through exactly these periods.
            bench = self.benchmark_stats(market_data)
            self._benchmark = bench
            bench_mom = bench.get('mom')
            sat_block_reasons = []
            if bench.get('above_sma200') is False:
                sat_block_reasons.append(
                    f"{bench['symbol']}跌破SMA200（大盘趋势转空）"
                )
            if breadth < Config.SATELLITE_REGIME_MIN_BREADTH:
                sat_block_reasons.append(
                    f"市场广度{breadth:.0%}低于{Config.SATELLITE_REGIME_MIN_BREADTH:.0%}"
                )
            self._satellite_blocked = bool(sat_block_reasons)
            self._satellite_block_reason = '；'.join(sat_block_reasons)
            if self._satellite_blocked:
                logger.info(
                    f"卫星仓(选股)今日停开新仓：{self._satellite_block_reason}。"
                    "核心仓(指数)不受影响，继续运行。"
                )

            # Path A is watchlist-only now, so it can no longer be "blocked" —
            # it never opens a position under any condition. Kept for the log.
            if Config.VALUE_PATH_WATCHLIST_ONLY:
                logger.info(
                    "价值路径(Path A)已降级为纯观察名单：只标记转强候选，永不开仓"
                )
            elif value_path_blocked:
                logger.info(
                    f"价值路径(Path A)今日停开新仓：regime={regime}, "
                    f"广度={breadth:.0%} (阈值{Config.VALUE_PATH_MIN_BREADTH:.0%})"
                )

            self._index_timing = {}
            self._watchlist = []

            index_candidates = []   # Path C: index ETF dip-buys (rank first)
            buy_candidates   = []   # Path A: value plays (near 52w low)
            trend_candidates = []   # Path B: trend breakout (momentum recovery)
            hold_signals     = []

            for _, row in market_data.iterrows():
                symbol   = row.get('Symbol', '')
                price    = row.get('Current_Price') or row.get('Close') or 0
                low_52w  = row.get('52_Week_Low')
                high_52w = row.get('52_Week_High')

                portfolio_context = self._get_portfolio_context(symbol, portfolio_snapshot)
                risk_metrics      = self._calculate_risk_metrics(row, portfolio_context)

                # Distance from 52-week low (None if data missing)
                dist_pct = None
                if price and low_52w and low_52w > 0:
                    dist_pct = (price - low_52w) / low_52w  # 0.05 = 5% above 52w low

                # 52-week range percentile: (current - low) / (high - low)
                pct_52w_range = None
                if price and low_52w and high_52w and high_52w > low_52w:
                    pct_52w_range = round((price - low_52w) / (high_52w - low_52w) * 100, 1)

                # Compute real technical scores before building the base dict
                _trend_score    = self._calculate_trend_score(row)
                _momentum_score = self._calculate_momentum_score(row)
                _composite      = round(0.5 * _trend_score + 0.5 * _momentum_score, 3)

                base = {
                    'Symbol':           symbol,
                    'Date':             today_str,
                    'Sector_Strength':  self._get_symbol_sector(symbol),
                    'Current_Price':    round(price, 2) if price else None,
                    '52W_Low':          round(low_52w, 2) if low_52w else None,
                    '52W_High':         round(high_52w, 2) if high_52w else None,
                    'Dist_From_52W_Low_Pct': round(dist_pct * 100, 1) if dist_pct is not None else None,
                    'Pct_52W_Range':    pct_52w_range,
                    'RSI':              row.get('RSI'),
                    'Price_vs_SMA50':   row.get('Price_vs_SMA50'),
                    'Volume_Ratio':     row.get('Volume_Ratio'),
                    'Market_Cap':       row.get('Market_Cap'),
                    'PE_Ratio':         row.get('PE_Ratio'),
                    'Revenue_Growth':   row.get('Revenue_Growth'),
                    'Earnings_Growth':  row.get('Earnings_Growth'),
                    'Debt_to_Equity':   row.get('Debt_to_Equity'),
                    'Beta':             risk_metrics.get('beta'),
                    'Volatility':       risk_metrics.get('volatility'),
                    'Currently_Held':   portfolio_context.get('held', False),
                    'Unrealized_PnL_Pct': portfolio_context.get('unrealized_pnl_pct', 0),
                    'Market_Regime':    self._get_market_regime_label(),
                    'VIX_Level':        self._get_vix_level(),
                    'Trend_Score':      _trend_score,
                    'Momentum_Score':   _momentum_score,
                    # Composite is now derived from actual indicators, not hardcoded
                    'Composite_Score':  _composite,
                    'Adjusted_Score':   _composite,   # BUY paths override below
                    'Last_Updated':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }

                def _hold(reasoning, risk_flag='NORMAL'):
                    # Resistance = 52W high; Support = SMA50 price (derived from Price_vs_SMA50)
                    pvs50_raw = float(row.get('Price_vs_SMA50', 0) or 0)
                    denom = 1 + pvs50_raw
                    sma50_price = round(price / denom, 2) if price and abs(denom) > 0.01 else None
                    base.update({
                        'Signal':           'HOLD',
                        'Risk_Flag':        risk_flag,
                        'Target_Price':     None,
                        'Stop_Loss':        None,
                        'Resistance_Level': round(high_52w, 2) if high_52w else None,
                        'Support_Level':    sma50_price,
                        'Confidence':       0,
                        'Reasoning':        reasoning,
                    })
                    hold_signals.append(base)

                # ── Permanent exile (satellite names only) ─────────────────
                # Core index symbols are exempt: the core sleeve is never sold
                # and so can never be stopped out of in the first place.
                if symbol in exiles and symbol not in Config.CORE_SYMBOLS:
                    _hold(
                        f"已永久移出可选范围：6个月内被止损"
                        f"{Config.SATELLITE_EXILE_STOPOUTS}次，不再推荐"
                    )
                    continue

                # ── Stop-out rebuy ban (all paths) ─────────────────────────
                if self._is_stopout_banned(symbol, stopouts):
                    if symbol in Config.INDEX_TIMING_SYMBOLS:
                        self._index_timing[symbol] = {
                            'state': 'WATCH', 'action': '观望',
                            'detail': f'近期止损出局（{stopouts.get(symbol)}），禁买期内',
                        }
                    _hold(
                        f"近期止损出局（{stopouts.get(symbol)}），"
                        f"{Config.STOPOUT_REBUY_BAN_DAYS}个交易日内不再推荐"
                    )
                    continue

                # ── Path C: index ETF dip-buying (SPY / QQQ) ───────────────
                # Indices answer "when do I buy the market", not "which stock".
                # They never go through the single-stock value/trend paths.
                if symbol in Config.INDEX_TIMING_SYMBOLS:
                    status = self._check_index_dip(row, price, high_52w)
                    self._index_timing[symbol] = status
                    if (status['state'] == 'BUY_WINDOW'
                            and not self._is_on_cooldown(symbol, cooldown)):
                        sig = self._build_index_buy(base, row, price, high_52w, status)
                        if sig is not None:
                            index_candidates.append(sig)
                            continue
                    _hold(f"大盘择时：{status['detail']}")
                    continue

                # ── Path B (SATELLITE): the only single-stock entry ────────
                # Runs for every name regardless of where it sits in its 52-week
                # range. Previously this was nested inside "not near the 52w
                # low", which meant the trend path could only ever see the
                # leftovers the value path rejected.
                uni_ok, uni_note = self.satellite_universe_check(row, bench_mom)
                base['RS_Note'] = uni_note
                base['Universe_OK'] = uni_ok

                if uni_ok and not self._satellite_blocked and not self._is_on_cooldown(symbol, cooldown):
                    tb_ok, tb_conf, tb_reason, tb_setup = self._check_trend_entry(row)
                    if tb_ok:
                        news_bad, news_reason = self._news_veto(symbol)
                        earn_days = self._earnings_within(
                            symbol, days=Config.EARNINGS_ENTRY_BUFFER_DAYS
                        )
                        if news_bad:
                            _hold(f"趋势买点成立，但{news_reason} — 暂缓")
                            continue
                        if earn_days is not None:
                            _hold(
                                f"趋势买点成立，但{earn_days}日内财报，"
                                "二元事件风险高，等财报落地后再确认"
                            )
                            continue

                        buy_price = round(price, 2)
                        volatility = row.get('Volatility')
                        stop_loss, take_profit, stop_pct, tp_pct, stop_basis, tp_basis = self._adjusted_stops(
                            buy_price, volatility, high_52w,
                            swing_low=row.get('Recent_Swing_Low'),
                            resistance=row.get('Recent_Swing_High'),
                        )
                        if stop_loss is None:
                            _hold(
                                f"趋势买点成立但结构止损过宽（支撑距离超过{self._MAX_STOP_PCT:.0%}），"
                                "风险无法界定，放弃该笔交易"
                            )
                            continue

                        # No fixed target. The old R/R gate is gone with it:
                        # every winner was cut by the clock and no target was
                        # ever reached, so gating on a number that never
                        # materialised was filtering on fiction. Risk is bounded
                        # by the stop; reward is left open and trailed.
                        adj_conf = self._vix_confidence_adjustment(tb_conf)
                        adj_conf = self._regime_confidence_adjustment(adj_conf)
                        adj_conf = max(0, min(100, round(
                            adj_conf * self._get_sector_confidence_multiplier(symbol)
                        )))

                        # Risk-based sizing: the stop distance decides the size.
                        shares, dollar_risk, pos_value = self.risk_based_shares(
                            buy_price, stop_loss, Config.TOTAL_ACCOUNT_VALUE
                        )
                        pos_size = round(
                            pos_value / Config.TOTAL_ACCOUNT_VALUE * 100, 1
                        ) if Config.TOTAL_ACCOUNT_VALUE else 0

                        setup_zh = {'PULLBACK': '回踩支撑', 'BREAKOUT': '突破新高'}.get(
                            tb_setup, tb_setup or '')
                        full_reason = (
                            f"{tb_reason}{uni_note}。"
                            f"止损${stop_loss}（-{stop_pct:.1%}，{'锚定摆动低点' if 'swing' in (stop_basis or '') else '波动率调整'}），"
                            f"不设固定止盈——达+{Config.SATELLITE_BREAKEVEN_R:g}R后止损上移至成本价，"
                            f"其后以高点回撤{Config.SATELLITE_TRAIL_PCT:.0%}移动止盈，让利润奔跑。"
                            f"按每笔风险{Config.SATELLITE_RISK_PER_TRADE_PCT:.0f}%账户"
                            f"（${dollar_risk:.2f}）计算仓位：{shares:g}股≈${pos_value:.2f}（{pos_size:.1f}%）。"
                        )
                        base.update({
                            'Signal':              'BUY',
                            'Signal_Path':         'TREND',
                            'Sleeve':              'SATELLITE',
                            'Setup':               tb_setup,
                            'Setup_ZH':            setup_zh,
                            'Buy_Price':           buy_price,
                            # Deliberately None — satellite exits are trailed.
                            'Target_Price':        None,
                            'Stop_Loss':           stop_loss,
                            'Confidence':          adj_conf,
                            'Adjusted_Score':      adj_conf / 100 * 0.85,
                            'Risk_Flag':           'NORMAL',
                            'Reasoning':           full_reason,
                            'Risk_Reward_Ratio':   None,
                            'Risk_Dollars':        dollar_risk,
                            'Risk_Shares':         shares,
                            'Position_Value':      pos_value,
                            'position_size_pct':   pos_size,
                        })
                        trend_candidates.append(base)
                        continue

                # ── Step 1: 52-week low proximity filter (Path A) ─────────
                if dist_pct is None or dist_pct > Config.NEAR_52W_LOW_THRESHOLD:
                    range_str = (
                        f"，52周区间分位{pct_52w_range:.0f}%"
                        if pct_52w_range is not None else ""
                    )
                    if not uni_ok:
                        _hold(f"不符合卫星仓选股标准：{uni_note}")
                    elif self._satellite_blocked:
                        _hold(
                            f"符合选股标准但卫星仓今日停开新仓（{self._satellite_block_reason}）"
                        )
                    else:
                        _hold(
                            f"趋势健康但今日无买点（未回踩SMA50支撑，也未放量突破）"
                            f"{range_str}"
                        )
                    continue

                # ══════════════════════════════════════════════════════════
                # Path A (VALUE) — WATCHLIST ONLY. Never opens a position.
                # ══════════════════════════════════════════════════════════
                # This block used to end in a BUY. Every one of the advice
                # account's 7 losing trades came out of it, because "within 15%
                # of the 52-week low" selects for names the market is actively
                # repricing downward — MCD took 10 BUY signals on the way down.
                # It now produces a watch entry and nothing else: the name is
                # remembered so that if it later clears the satellite uptrend +
                # relative-strength gate, Path B can buy the turn instead of
                # the fall.
                watch_reasons = []
                disqualified = None

                is_trap, trap_reasons = self._is_value_trap(row)
                if is_trap:
                    pvs50_raw = float(row.get('Price_vs_SMA50', 0) or 0)
                    denom = 1 + pvs50_raw
                    base.update({
                        # 'SELL' is the vocabulary the report / WeChat / signal
                        # summary count as 规避.
                        'Signal':           'SELL',
                        'Risk_Flag':        'NORMAL',
                        'Target_Price':     None,
                        'Stop_Loss':        None,
                        'Resistance_Level': round(high_52w, 2) if high_52w else None,
                        'Support_Level':    round(price / denom, 2) if price and abs(denom) > 0.01 else None,
                        'Confidence':       0,
                        'Reasoning':        '价值陷阱风险: ' + '; '.join(trap_reasons),
                    })
                    hold_signals.append(base)
                    continue

                knife_signs = self.falling_knife_signs(row)
                if len(knife_signs) >= 2:
                    disqualified = f"仍在下跌趋势中（{'、'.join(knife_signs)}）"

                stab_pts, stab_reasons = self._stabilization_score(row)
                if stab_pts >= 2:
                    watch_reasons.append(
                        f"企稳迹象{stab_pts}/4（{'、'.join(stab_reasons)}）"
                    )
                elif disqualified is None:
                    disqualified = (
                        f"企稳证据不足（{stab_pts}/4：{'、'.join(stab_reasons) or '无'}）"
                    )

                # How far this name still is from being *buyable* — i.e. from
                # clearing the Path B universe gate. This is the actual watch
                # criterion: not "how cheap", but "how close to turning".
                rsi_str = f"{rsi_w:.1f}" if (rsi_w := row.get('RSI')) is not None and not pd.isna(rsi_w) else 'N/A'
                base.update({
                    'Signal':           'HOLD',
                    'Signal_Path':      'VALUE_WATCH',
                    'Sleeve':           'WATCHLIST',
                    'Watchlist':        True,
                    'Target_Price':     None,
                    'Stop_Loss':        None,
                    'Confidence':       0,
                    'Risk_Flag':        'NORMAL',
                    'Reasoning':        (
                        f"【观察名单】距52周低点{dist_pct*100:.1f}%，"
                        f"52周低${low_52w:.2f}/高${high_52w:.2f}，RSI={rsi_str}。"
                        + (f"{'、'.join(watch_reasons)}。" if watch_reasons else "")
                        + (f"尚不可买：{disqualified}。" if disqualified else "")
                        + f"转强条件：{uni_note or '需站上SMA50与SMA200并跑赢' + Config.SATELLITE_RS_BENCHMARK}。"
                        + "价值路径只观察不建仓——等它真正转强后由趋势路径接手。"
                    ),
                })
                hold_signals.append(base)
                self._watchlist.append({
                    'symbol':       symbol,
                    'price':        round(price, 2) if price else None,
                    'dist_from_low_pct': round(dist_pct * 100, 1),
                    'pct_52w_range': pct_52w_range,
                    'rsi':          row.get('RSI'),
                    'stabilizing':  stab_pts,
                    'blocker':      disqualified,
                    'turn_condition': uni_note,
                    'ready':        bool(uni_ok),
                })
                continue

            # ── Step 6: Rank, cap BUY signals, update cooldown ────────────
            # Core (index) dip-buys always rank ahead of satellite stock picks:
            # when the market itself is on sale, that beats stock-picking, and
            # it is where 75% of the capital lives.
            index_candidates.sort(key=lambda x: x.get('Confidence') or 0, reverse=True)
            trend_candidates.sort(key=lambda x: x.get('Confidence') or 0, reverse=True)

            # Satellite may never surface more names than it can actually hold.
            # Showing 3 setups into a 2-position sleeve invited over-trading.
            if len(trend_candidates) > Config.SATELLITE_MAX_POSITIONS:
                for extra in trend_candidates[Config.SATELLITE_MAX_POSITIONS:]:
                    extra['Signal'] = 'HOLD'
                    extra['Reasoning'] = (
                        f"趋势买点成立，但卫星仓最多同时持有"
                        f"{Config.SATELLITE_MAX_POSITIONS}只，今日已有更强候选排在前面"
                    )
                trend_candidates = trend_candidates[:Config.SATELLITE_MAX_POSITIONS]

            ranked = index_candidates + trend_candidates
            for i, c in enumerate(ranked):
                if i >= Config.MAX_BUY_SIGNALS:
                    c['Signal']    = 'HOLD'
                    c['Reasoning'] = f"买入候选（排名第{i+1}，今日仅推荐前{Config.MAX_BUY_SIGNALS}名）"
                c['Signal_Rank'] = i + 1

            # NOTE: cooldown is deliberately NOT written here. The AI external-
            # factor review runs after this and can demote a BUY to HOLD; a name
            # that was never actually recommended must not burn its 5-day
            # cooldown. main.py calls commit_cooldown() once signals are final.

            all_signals = index_candidates + trend_candidates + buy_candidates + hold_signals
            if not all_signals:
                return pd.DataFrame()

            signals_df = pd.DataFrame(all_signals)
            signals_df = signals_df.sort_values('Adjusted_Score', ascending=False).reset_index(drop=True)

            buy_count   = (signals_df['Signal'] == 'BUY').sum()
            hold_count  = (signals_df['Signal'] == 'HOLD').sum()
            avoid_count = (signals_df['Signal'].isin(['SELL', 'AVOID'])).sum()
            logger.info(
                f"信号生成完毕 — BUY:{buy_count} HOLD:{hold_count} 规避:{avoid_count} "
                f"(共{len(signals_df)}只)"
            )
            return signals_df

        except Exception as e:
            logger.error(f"Error generating signals: {str(e)}")
            return pd.DataFrame()

    def commit_cooldown(self, signals_df):
        """Record today's date for every symbol still carrying a BUY.

        Split out of generate_signals() so the cooldown reflects what was
        actually *recommended*, not what was merely a candidate. The AI review
        in main.py can demote a BUY to HOLD after generation; under the old
        in-loop write those demoted names silently burned a 5-day cooldown and
        were unavailable the next morning despite never having been advised.
        """
        try:
            if signals_df is None or signals_df.empty or 'Signal' not in signals_df.columns:
                return
            today_str = datetime.now().strftime('%Y-%m-%d')
            cooldown = self._load_cooldown()
            recommended = signals_df.loc[signals_df['Signal'] == 'BUY', 'Symbol'].tolist()
            for symbol in recommended:
                cooldown[symbol] = today_str
            self._save_cooldown(cooldown)
            logger.info(f"冷却期已记录 {len(recommended)} 只最终推荐标的: {', '.join(recommended) or '无'}")
        except Exception as e:
            logger.error(f"Error committing cooldown: {e}")

    # ── Strategy helpers ─────────────────────────────────────────────────────

    @staticmethod
    def falling_knife_signs(row) -> list:
        """Downtrend evidence for a name sitting at its lows.

        Two or more signs veto a value entry, and PositionAdvisor reuses the
        same test to decide whether an underwater holding should be exited —
        entry and exit must agree on what "still falling" means.
        """
        signs = []
        sma10_x = row.get('SMA10_vs_SMA50')
        mom20   = row.get('Momentum_20')
        macd_v  = row.get('MACD')
        macd_s  = row.get('MACD_Signal')
        if sma10_x is not None and not pd.isna(sma10_x) and sma10_x < -0.02:
            signs.append(f"SMA10<SMA50 {sma10_x:+.1%}")
        if mom20 is not None and not pd.isna(mom20) and mom20 < -0.05:
            signs.append(f"20日动量{mom20:+.1%}")
        if (macd_v is not None and macd_s is not None
                and not pd.isna(macd_v) and not pd.isna(macd_s) and macd_v < macd_s):
            signs.append("MACD空头")
        return signs

    def is_value_trap(self, row):
        """Public alias for the value-trap test, shared with PositionAdvisor."""
        return self._is_value_trap(row)

    @staticmethod
    def confidence_size_multiplier(confidence) -> float:
        """Scale position size by conviction, clamped to a sane band."""
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            return 1.0
        if pd.isna(conf) or conf <= 0:
            return 1.0
        raw = conf / Config.CONFIDENCE_SIZING_PIVOT
        return max(Config.CONFIDENCE_SIZING_MIN_MULT,
                   min(Config.CONFIDENCE_SIZING_MAX_MULT, raw))

    def _is_near_52w_low(self, price, low_52w):
        """Return True if price is within NEAR_52W_LOW_THRESHOLD of 52-week low."""
        if not price or not low_52w or low_52w <= 0:
            return False
        return (price - low_52w) / low_52w <= Config.NEAR_52W_LOW_THRESHOLD

    # Stop must sit within this band of the entry regardless of structure:
    # too tight gets stopped by noise, too wide blows the R/R. 6% floor —
    # the old 5% clamp got NFLX/DIS stopped by ordinary noise within days.
    _MIN_STOP_PCT = 0.06
    _MAX_STOP_PCT = 0.18

    def _adjusted_stops(self, price, volatility, high_52w=None,
                        swing_low=None, resistance=None):
        """
        Compute stop-loss and take-profit anchored to real price structure (#5).

        Stop   — placed just below the recent swing low (support). If no usable
                 swing low, falls back to a volatility-adjusted percentage. A
                 noise-tight structural stop is widened to _MIN_STOP_PCT; a
                 structural stop wider than _MAX_STOP_PCT means the trade's
                 invalidation point is too far away — the trade is REJECTED
                 (returns stop_loss=None) instead of clamped into fiction.
        Target — the nearest overhead resistance (recent swing high), capped at
                 the 52-week high. If no usable resistance, falls back to an
                 R/R-preserving multiple of the stop.

        Returns: (stop_loss, take_profit, stop_pct, tp_pct, stop_basis, tp_basis)
                 stop_loss is None when the trade should be skipped.
        """
        base_stop = Config.STOP_LOSS_PCT    # 0.10
        base_tp   = Config.TAKE_PROFIT_PCT  # 0.20
        rr_ratio  = base_tp / base_stop     # 2.0 — used only in the fallback target

        # ── Stop: anchor to swing low, else volatility ───────────────────────
        struct_stop_pct = None
        if swing_low and not pd.isna(swing_low) and 0 < swing_low < price:
            # 1% buffer below support so a clean break — not a wick — triggers it
            struct_stop_pct = (price - swing_low) / price + 0.01

        if struct_stop_pct is not None and struct_stop_pct > self._MAX_STOP_PCT:
            # Real support is >18% below entry: risk is undefinable, skip trade.
            return None, None, 0.0, 0.0, 'too_wide', ''

        if struct_stop_pct is not None:
            adj_stop = max(self._MIN_STOP_PCT, struct_stop_pct)
            stop_basis = 'swing_low' if struct_stop_pct >= self._MIN_STOP_PCT else 'swing_low_clamped'
        else:
            if volatility and not pd.isna(volatility) and volatility > 0:
                vol_stop = min(base_stop * 2, max(base_stop * 0.5, volatility * 0.25))
                adj_stop = 0.5 * base_stop + 0.5 * vol_stop
            else:
                adj_stop = base_stop
            stop_basis = 'volatility'
        adj_stop = min(self._MAX_STOP_PCT, max(self._MIN_STOP_PCT, adj_stop))

        # ── Target: anchor to resistance, else R/R multiple ──────────────────
        if resistance and not pd.isna(resistance) and resistance > price * 1.02:
            take_profit = resistance
            tp_basis = 'resistance'
        else:
            take_profit = price * (1 + adj_stop * rr_ratio)
            tp_basis = 'rr'

        # Cap take-profit at the 52-week high — a target above the annual high is
        # unrealistic for a mean-reversion entry.
        if high_52w and high_52w > price:
            if take_profit > high_52w:
                take_profit = high_52w
                tp_basis = '52w_high'

        stop_loss   = round(price * (1 - adj_stop), 2)
        take_profit = round(take_profit, 2)
        effective_stop_pct = (price - stop_loss) / price
        effective_tp_pct   = (take_profit - price) / price

        return stop_loss, take_profit, effective_stop_pct, effective_tp_pct, stop_basis, tp_basis

    def _vix_position_size(self, base_pct: float, risk_flag: str,
                           confidence=None) -> int:
        """
        Scale the suggested position size down as VIX rises, and up/down with
        the signal's own confidence.

        base_pct   : recommended pct under normal conditions (e.g. 33 or 25)
        confidence : signal confidence 0–100; a 41-confidence and a
                     75-confidence signal used to receive identical capital.
        Returns    : integer percent, minimum 5 %
        """
        vix_level = self._get_vix_level()
        try:
            vix_val = float(vix_level) if vix_level != 'N/A' else 20.0
        except (ValueError, TypeError):
            vix_val = 20.0

        if vix_val > 35:
            multiplier = 0.35
        elif vix_val > 30:
            multiplier = 0.50
        elif vix_val > 25:
            multiplier = 0.65
        elif vix_val > 20:
            multiplier = 0.80
        else:
            multiplier = 1.00

        if risk_flag == 'HIGH_CAUTION':
            multiplier *= 0.70

        if confidence is not None:
            multiplier *= self.confidence_size_multiplier(confidence)

        return max(5, round(base_pct * multiplier))

    def _vix_confidence_adjustment(self, confidence: int) -> int:
        """Apply VIX-only signal_adjustment from market_context."""
        if not self._market_context:
            return confidence
        signal_adj = self._market_context.get('vix_analysis', {}).get('signal_adjustment', 1.0)
        return max(0, min(100, round(confidence * signal_adj)))

    def _regime_confidence_adjustment(self, confidence: int) -> int:
        """
        Downward-only penalty in BEAR regime (VIX is handled separately by
        _vix_confidence_adjustment).  Captures slow-grind bear markets where
        VIX can stay low while trend is clearly down.
        """
        if not self._market_context:
            return confidence
        regime = self._market_context.get('market_regime', {}).get('regime', 'SIDEWAYS')
        multiplier = 0.75 if regime == 'BEAR' else 1.0
        return max(0, min(100, round(confidence * multiplier)))

    def _get_sector_confidence_multiplier(self, symbol: str) -> float:
        """
        Return a sector-strength multiplier (0.90–1.10) using pre-fetched
        market_context sector data — no extra API calls required.
        Stocks not in the static sector map fall back to 1.0.
        """
        if not self._market_context:
            return 1.0
        sectors = self._market_context.get('sector_strength', {}).get('sectors', [])
        for sector in sectors:
            if symbol in (sector.get('mapped_stocks') or []):
                score = sector.get('strength_score', 0)
                if score > 3:    return 1.10
                elif score > 1:  return 1.05
                elif score > -1: return 1.0
                elif score > -3: return 0.95
                else:            return 0.90
        return 1.0

    def _get_symbol_sector(self, symbol: str) -> str:
        """
        Return 'SectorName (score)' for a symbol using the sector_etfs mapping
        in market_context.  Falls back to '' if context is unavailable.
        """
        if not self._market_context:
            return ''
        sectors = self._market_context.get('sector_strength', {}).get('sectors', [])
        for sector in sectors:
            if symbol in (sector.get('mapped_stocks') or []):
                score = sector.get('strength_score', 0)
                return f"{sector.get('name', '')} ({score:+.1f})"
        return ''

    # ── Satellite universe: relative strength ────────────────────────────────

    @staticmethod
    def benchmark_stats(market_data) -> dict:
        """Benchmark trend + 6-month return, used to gate the whole satellite.

        Two separate jobs:
          • `mom` is the bar every satellite candidate must clear — a stock has
            to be *outperforming* the index to be worth owning instead of it.
          • `above_sma200` is the master switch: when the benchmark itself is
            below its 200-day line, no new satellite position opens at all.
            BEAR-regime BUYs won 29% at T+20, so this is where the money was.
        """
        out = {'symbol': Config.SATELLITE_RS_BENCHMARK, 'mom': None,
               'above_sma200': None, 'price': None}
        if market_data is None or getattr(market_data, 'empty', True):
            return out
        try:
            rows = market_data[market_data['Symbol'] == Config.SATELLITE_RS_BENCHMARK]
            if rows.empty:
                return out
            r = rows.iloc[0]
            mom = r.get('Momentum_126')
            pvs200 = r.get('Price_vs_SMA200')
            out['price'] = r.get('Current_Price') or r.get('Close')
            if mom is not None and not pd.isna(mom):
                out['mom'] = float(mom)
            if pvs200 is not None and not pd.isna(pvs200):
                out['above_sma200'] = bool(float(pvs200) > 0)
        except Exception as e:
            logger.warning(f"Benchmark stats unavailable: {e}")
        return out

    @staticmethod
    def satellite_universe_check(row, bench_mom):
        """The rule that replaces the hand-maintained name list.

        A stock is buyable only if it is in a confirmed uptrend AND beating the
        benchmark over six months. Applied to MCD/NKE on 2026-09-12 both fail
        on every clause — which is the point: this test alone would have
        blocked all seven losing trades.

        Returns (ok: bool, note: str).
        """
        if not Config.SATELLITE_REQUIRE_UPTREND:
            return True, ''
        price  = row.get('Current_Price') or row.get('Close')
        sma50  = row.get('SMA_50')
        sma200 = row.get('SMA_200')
        mom    = row.get('Momentum_126')

        def _bad(v):
            return v is None or pd.isna(v)

        if _bad(price) or _bad(sma50) or _bad(sma200):
            # Fail CLOSED. A missing SMA200 previously meant "no opinion", which
            # in an uptrend-only strategy silently re-admits everything.
            return False, '缺少SMA50/SMA200数据，趋势无法确认（保守跳过）'
        price, sma50, sma200 = float(price), float(sma50), float(sma200)
        if not (price > sma50 > sma200):
            rel50 = (price / sma50 - 1) if sma50 else 0
            rel200 = (sma50 / sma200 - 1) if sma200 else 0
            return False, (
                f'不满足多头排列 价格>SMA50>SMA200'
                f'（价格vsSMA50 {rel50:+.1%}，SMA50vsSMA200 {rel200:+.1%}）'
            )
        if _bad(mom):
            return False, '缺少6个月动量数据，相对强度无法确认（保守跳过）'
        mom = float(mom)
        if bench_mom is None:
            return False, '缺少基准动量数据，相对强度无法确认（保守跳过）'
        excess = mom - bench_mom
        if excess <= Config.SATELLITE_RS_MIN_EXCESS:
            return False, (
                f'6个月相对强度不足：本股{mom:+.1%} vs '
                f'{Config.SATELLITE_RS_BENCHMARK}{bench_mom:+.1%}（差{excess:+.1%}）'
            )
        return True, (
            f'多头排列成立，6个月跑赢{Config.SATELLITE_RS_BENCHMARK} {excess:+.1%}'
            f'（本股{mom:+.1%}）'
        )

    def _check_trend_entry(self, row):
        """
        Path B — the ONLY entry that may open a satellite position.

        Replaces the old "fresh SMA50 cross" test, which fired on names that
        had merely stopped falling. Two setups qualify, both inside a
        confirmed uptrend (the universe gate has already checked that):

          1. PULLBACK — price has come back to rising SMA50 support (within
             ±4%) without breaking it, RSI cooled to 40–60. Buying the dip in
             an uptrend, rather than the dip in a downtrend.
          2. BREAKOUT — price pushing to new near-term highs on volume
             ≥1.2× average, MACD confirming.

        Returns (ok, confidence, reasoning, setup).
        """
        try:
            price_vs_sma50 = row.get('Price_vs_SMA50')
            sma10_vs_sma50 = row.get('SMA10_vs_SMA50')
            rsi            = row.get('RSI')
            volume_ratio   = row.get('Volume_Ratio')
            macd           = row.get('MACD')
            macd_sig       = row.get('MACD_Signal')
            momentum_10    = row.get('Momentum_10')
            price          = row.get('Current_Price') or row.get('Close')
            swing_high     = row.get('Recent_Swing_High')

            def _ok(v):
                return v is not None and not pd.isna(v)

            if not _ok(price_vs_sma50):
                return False, 0, '', None
            pvs = float(price_vs_sma50)

            macd_ok = (_ok(macd) and _ok(macd_sig) and float(macd) > float(macd_sig))
            vol_ok  = _ok(volume_ratio) and float(volume_ratio) >= 1.2
            rsi_v   = float(rsi) if _ok(rsi) else None

            # ── Setup 1: pullback to rising SMA50 support ─────────────────
            # -4%..+4% of SMA50 — close enough that the moving average is the
            # actual support being tested, with a defined invalidation below it.
            if -0.04 <= pvs <= 0.04 and rsi_v is not None and 40 <= rsi_v <= 60:
                reasons = [
                    f'回踩SMA50支撑（距均线{pvs:+.1%}）',
                    f'RSI={rsi_v:.1f}已回落至可买区间',
                ]
                confidence = 50
                if macd_ok:
                    confidence += 8
                    reasons.append('MACD仍在多头')
                if _ok(sma10_vs_sma50) and float(sma10_vs_sma50) > 0:
                    confidence += 6
                    reasons.append('SMA10>SMA50短期趋势未破')
                if vol_ok:
                    confidence += 4
                    reasons.append(f'成交量{float(volume_ratio):.1f}x')
                return (True, min(75, confidence),
                        f"【趋势回踩】{'、'.join(reasons)}。", 'PULLBACK')

            # ── Setup 2: breakout on volume ───────────────────────────────
            # Requires price pressing the 60-day high, not merely rising.
            near_high = False
            if _ok(price) and _ok(swing_high) and float(swing_high) > 0:
                near_high = float(price) >= float(swing_high) * 0.98
            mom_ok = _ok(momentum_10) and float(momentum_10) > 0.03
            if near_high and vol_ok and (macd_ok or mom_ok):
                if rsi_v is not None and rsi_v > 75:
                    return False, 0, '', None      # already extended; chasing
                reasons = [
                    f'突破60日高点区间（现价${float(price):.2f}）',
                    f'成交量{float(volume_ratio):.1f}x放大确认',
                ]
                confidence = 52
                if macd_ok:
                    confidence += 8
                    reasons.append('MACD金叉向上')
                if mom_ok:
                    confidence += 6
                    reasons.append(f'10日动量{float(momentum_10):.1%}')
                return (True, min(75, confidence),
                        f"【趋势突破】{'、'.join(reasons)}。", 'BREAKOUT')

            return False, 0, '', None

        except Exception as e:
            logger.warning(f"Error in trend entry check: {e}")
            return False, 0, '', None

    @staticmethod
    def risk_based_shares(entry, stop, account_value):
        """Shares such that (entry − stop) × shares = the fixed risk budget.

        The stop DISTANCE sets the size, so a wide stop buys a small position
        and a tight stop buys a larger one — every trade loses the same amount
        when it is wrong. This is the rule that caps a losing streak.
        Returns (shares, dollar_risk, position_value) or (0, 0, 0).
        """
        try:
            entry, stop = float(entry), float(stop)
            account_value = float(account_value)
            if entry <= 0 or stop <= 0 or stop >= entry or account_value <= 0:
                return 0.0, 0.0, 0.0
            risk_budget = account_value * (Config.SATELLITE_RISK_PER_TRADE_PCT / 100.0)
            per_share_risk = entry - stop
            shares = risk_budget / per_share_risk
            # Never let one satellite name exceed the whole satellite sleeve.
            sleeve = account_value * (Config.SATELLITE_TARGET_PCT / 100.0)
            if shares * entry > sleeve:
                shares = sleeve / entry
            return round(shares, 4), round(shares * per_share_risk, 2), round(shares * entry, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0, 0.0, 0.0

    # ── Path C: index ETF dip-buying (SPY / QQQ market timing) ───────────────

    def _check_index_dip(self, row, price, high_52w):
        """
        Classify the market-timing state for an index ETF. Answers the user's
        question "when should I buy the market?" with a rule, not a feeling:

          BUY_WINDOW  — meaningful pullback (4–15% off 52w high), long trend
                        intact (not >5% below SMA50), RSI ≤ 48, regime not BEAR
          NO_DIP      — near highs; adding here is chasing
          WATCH       — dip exists but confirmation missing (RSI hot, or BEAR)
          TREND_BROKEN— >5% below SMA50 or drawdown >15%; that's a downtrend,
                        not a dip

        Returns {'state', 'action', 'detail', 'drawdown_pct', 'rsi'}.
        """
        rsi   = row.get('RSI')
        pvs50 = row.get('Price_vs_SMA50')
        rsi_v   = float(rsi) if rsi is not None and not pd.isna(rsi) else None
        pvs50_v = float(pvs50) if pvs50 is not None and not pd.isna(pvs50) else None

        if not price or not high_52w or high_52w <= 0:
            return {'state': 'NO_DATA', 'action': '观望', 'detail': '52周高点数据缺失',
                    'drawdown_pct': None, 'rsi': rsi_v}

        dd = (high_52w - price) / high_52w  # drawdown from 52w high
        dd_str = f"距52周高点-{dd*100:.1f}%"
        rsi_str = f"RSI={rsi_v:.0f}" if rsi_v is not None else "RSI=N/A"
        info = {'drawdown_pct': round(dd * 100, 1), 'rsi': rsi_v}

        if pvs50_v is not None and pvs50_v < Config.INDEX_MIN_PRICE_VS_SMA50:
            return {**info, 'state': 'TREND_BROKEN', 'action': '观望',
                    'detail': f"{dd_str}且低于SMA50 {pvs50_v:.1%}，趋势破位，等待重新站上均线"}
        if dd > Config.INDEX_DIP_MAX_PCT:
            return {**info, 'state': 'TREND_BROKEN', 'action': '观望',
                    'detail': f"{dd_str}，超出回调范围（>{Config.INDEX_DIP_MAX_PCT:.0%}），按下跌趋势对待"}
        if dd < Config.INDEX_DIP_MIN_PCT:
            return {**info, 'state': 'NO_DIP', 'action': '持有/等待回调',
                    'detail': f"{dd_str}，接近高位，无加仓窗口（回调≥{Config.INDEX_DIP_MIN_PCT:.0%}再考虑）"}
        if self._get_market_regime_label() == 'BEAR':
            return {**info, 'state': 'WATCH', 'action': '观望',
                    'detail': f"{dd_str}回调到位，但市场格局为BEAR，等待格局修复"}
        if rsi_v is not None and rsi_v > Config.INDEX_MAX_RSI:
            return {**info, 'state': 'WATCH', 'action': '观望',
                    'detail': f"{dd_str}回调到位，但{rsi_str}尚未回落（需≤{Config.INDEX_MAX_RSI}），不追反弹"}

        return {**info, 'state': 'BUY_WINDOW', 'action': '分批加仓',
                'detail': f"{dd_str}，长期趋势未破，{rsi_str}——大盘加仓窗口"}

    def _build_index_buy(self, base, row, price, high_52w, status):
        """Build a BUY signal dict for an index ETF in a confirmed dip window."""
        buy_price = round(price, 2)
        volatility = row.get('Volatility')
        # No 52w-high cap on the target: unlike a single distressed stock,
        # indices regularly make new highs, so mean-reversion capping is wrong.
        stop_loss, take_profit, stop_pct, tp_pct, stop_basis, tp_basis = self._adjusted_stops(
            buy_price, volatility, None,
            swing_low=row.get('Recent_Swing_Low'),
            resistance=row.get('Recent_Swing_High'),
        )
        if stop_loss is None:
            return None
        # Shallow dips can leave the resistance target too close; fall back to
        # a 2:1 target instead of rejecting — index dips have higher base rates
        # than single-stock value entries.
        rr = round(tp_pct / stop_pct, 2) if stop_pct > 0 else 0
        if rr < Config.MIN_RISK_REWARD_RATIO:
            tp_pct = stop_pct * 2
            take_profit = round(buy_price * (1 + tp_pct), 2)
            rr = 2.0
            tp_basis = 'rr'

        dd = status.get('drawdown_pct') or 0
        rsi_v = status.get('rsi')
        # Deeper dip + more oversold → higher confidence, then standard
        # VIX / regime / sector adjustments like every other path.
        depth_bonus = min(15, round((dd - Config.INDEX_DIP_MIN_PCT * 100) * 1.5))
        oversold_bonus = 10 if (rsi_v is not None and rsi_v < 35) else 0
        confidence = 50 + max(0, depth_bonus) + oversold_bonus
        confidence = self._vix_confidence_adjustment(confidence)
        confidence = self._regime_confidence_adjustment(confidence)
        confidence = max(0, min(100, round(confidence)))

        pos_size = self._vix_position_size(
            Config.INDEX_POSITION_SIZE_PCT, 'NORMAL', confidence
        )
        base.update({
            'Signal':            'BUY',
            'Signal_Path':       'INDEX',
            'Buy_Price':         buy_price,
            'Target_Price':      take_profit,
            'Stop_Loss':         stop_loss,
            'Confidence':        confidence,
            'Adjusted_Score':    confidence / 100,
            'Risk_Flag':         'NORMAL',
            'Risk_Reward_Ratio': rr,
            'position_size_pct': pos_size,
            'Reasoning': (
                f"【大盘择时】{status['detail']}。"
                f"止损-{stop_pct:.1%}（${stop_loss}），目标+{tp_pct:.1%}（${take_profit}），R/R={rr:.1f}。"
            ),
        })
        return base

    def get_index_timing_status(self) -> dict:
        """Per-index timing status from the latest generate_signals() run.
        {symbol: {'state','action','detail','drawdown_pct','rsi'}}"""
        return dict(self._index_timing)

    def get_watchlist(self) -> list:
        """Path A names being watched for a turn, strongest candidate first.

        These are explicitly NOT buy ideas — a watch entry means "this is cheap
        and may be bottoming", which is precisely the thesis that lost money.
        The list exists so that when one of them finally clears the uptrend +
        relative-strength gate, it is already familiar rather than a surprise.
        """
        return sorted(
            self._watchlist,
            key=lambda w: (not w.get('ready'), -(w.get('stabilizing') or 0)),
        )

    def get_satellite_gate(self) -> dict:
        """Whether new stock picks are permitted today, and why not if not."""
        return {
            'blocked': self._satellite_blocked,
            'reason': self._satellite_block_reason,
            'benchmark': dict(self._benchmark or {}),
        }

    # ETFs and fund wrappers don't have marketCap from yfinance — skip market-cap check for these
    _ETF_SYMBOLS = frozenset({
        'SPY', 'QQQ', 'IWM', 'DIA', 'VTI',
        'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLP', 'XLY', 'XLB', 'XLRE', 'XLU',
        'GLD', 'SLV', 'TLT', 'HYG', 'LQD', 'EEM', 'EFA',
        'ARKK', 'ARKG', 'ARKW', 'ARKF',
    })

    def _is_value_trap(self, row):
        """
        Detect value traps — stocks cheap for bad reasons.
        Returns (is_trap: bool, reasons: list[str])
        """
        reasons = []
        symbol = row.get('Symbol', '')

        # ETFs don't carry standard fundamental fields — skip all value-trap checks
        if symbol in self._ETF_SYMBOLS:
            return False, []

        rev_growth = row.get('Revenue_Growth')
        if rev_growth is not None and not pd.isna(rev_growth):
            if rev_growth < Config.REVENUE_DECLINE_THRESHOLD:
                reasons.append(f"营收同比下滑{rev_growth*100:.1f}%")

        de_ratio = row.get('Debt_to_Equity')
        if de_ratio is not None and not pd.isna(de_ratio):
            if de_ratio > Config.DEBT_EQUITY_MAX:
                reasons.append(f"债务/净资产比率过高({de_ratio:.1f}x)")

        earn_growth = row.get('Earnings_Growth')
        if earn_growth is not None and not pd.isna(earn_growth) and earn_growth < -0.30:
            reasons.append(f"利润大幅下滑（{earn_growth*100:.1f}%）")

        mkt_cap = row.get('Market_Cap')
        if mkt_cap is not None and not pd.isna(mkt_cap) and mkt_cap < Config.VALUE_TRAP_MIN_MARKET_CAP:
            reasons.append(f"市值过小(${mkt_cap/1e9:.1f}B)，流动性风险")

        return len(reasons) > 0, reasons

    # ── External-factor gates (concern: don't decide on technicals alone) ─────

    def _news_veto(self, symbol):
        """
        Block a technically-cheap stock when recent news is materially negative.
        A value play near its low *with* bad news is the classic value trap that
        pure price/fundamental filters miss. Returns (veto: bool, reason: str).
        """
        nd = self._news_sentiment
        if nd is None or getattr(nd, 'empty', True) or 'Symbol' not in getattr(nd, 'columns', []):
            return False, ''
        try:
            rows = nd[nd['Symbol'] == symbol]
            if len(rows) < 2 or 'Sentiment_Score' not in rows.columns:
                return False, ''
            avg = float(rows['Sentiment_Score'].mean())
            neg = int((rows['Sentiment_Label'] == 'Negative').sum()) if 'Sentiment_Label' in rows.columns else 0
            # Strongly negative cluster: mean sentiment clearly bearish and
            # at least two negative articles dominating the flow.
            if avg <= -0.30 and neg >= 2:
                return True, f"近期新闻显著偏空（均值{avg:+.2f}，{neg}条负面）"
        except Exception as e:
            logger.warning(f"News veto check failed for {symbol}: {e}")
        return False, ''

    def _earnings_within(self, symbol, days=2):
        """Return days-until-earnings if the symbol reports within `days`, else None.
        A binary earnings event invalidates a mean-reversion technical setup."""
        ea = self._event_analysis
        if not ea:
            return None
        try:
            er = (ea.get('stocks', {}).get(symbol, {}) or {}).get('earnings', {}) or {}
            du = er.get('days_until')
            if du is not None and 0 <= du <= days:
                return du
        except Exception:
            return None
        return None

    def _stabilization_score(self, row):
        """
        Score (0–4) how much a near-52w-low stock shows signs of *bottoming*,
        rather than still free-falling. Used to decouple confidence from mere
        cheapness: a stock can sit at its low and keep dropping.
        Returns (points, reasons).
        """
        pts, reasons = 0, []
        rsi   = row.get('RSI')
        mom10 = row.get('Momentum_10')
        mom20 = row.get('Momentum_20')
        macd  = row.get('MACD')
        macd_s = row.get('MACD_Signal')
        vol_r = row.get('Volume_Ratio')
        day_chg = row.get('Day_Change_Pct', row.get('Price_Change_Pct', 0)) or 0

        # 1. RSI oversold — sellers likely exhausted
        if rsi is not None and not pd.isna(rsi) and rsi <= self.rsi_oversold:
            pts += 1; reasons.append(f"RSI={rsi:.0f}超卖")
        # 2. Decline decelerating — 10d drop shallower than 20d drop
        if (mom10 is not None and mom20 is not None
                and not pd.isna(mom10) and not pd.isna(mom20) and mom10 > mom20):
            pts += 1; reasons.append("跌势放缓")
        # 3. MACD turning up
        if (macd is not None and macd_s is not None
                and not pd.isna(macd) and not pd.isna(macd_s) and macd > macd_s):
            pts += 1; reasons.append("MACD转强")
        # 4. Accumulation — up day on above-average volume
        if (vol_r is not None and not pd.isna(vol_r) and vol_r > 1.2 and day_chg > 0):
            pts += 1; reasons.append("放量上行")
        return pts, reasons

    def _calculate_trend_score(self, row):
        """Calculate trend component score (0–1). Used for legacy Trend_Score field."""
        try:
            score = 0.0
            factors = 0

            price_vs_sma50 = row.get('Price_vs_SMA50')
            if price_vs_sma50 is not None and not pd.isna(price_vs_sma50):
                if price_vs_sma50 > 0.05:    score += 1.0
                elif price_vs_sma50 > 0:     score += 0.7
                elif price_vs_sma50 > -0.05: score += 0.3
                factors += 1

            sma10_vs_sma50 = row.get('SMA10_vs_SMA50')
            if sma10_vs_sma50 is not None and not pd.isna(sma10_vs_sma50):
                if sma10_vs_sma50 > 0.02:    score += 1.0
                elif sma10_vs_sma50 > 0:     score += 0.6
                elif sma10_vs_sma50 > -0.02: score += 0.4
                factors += 1

            macd        = row.get('MACD')
            macd_signal = row.get('MACD_Signal')
            if (macd is not None and macd_signal is not None
                    and not pd.isna(macd) and not pd.isna(macd_signal)):
                if macd > macd_signal and macd > 0:  score += 1.0
                elif macd > macd_signal:             score += 0.7
                elif macd > 0:                       score += 0.3
                factors += 1

            return score / factors if factors > 0 else 0.5
        except Exception as e:
            logger.warning(f"Error calculating trend score: {e}")
            return 0.5

    def _calculate_momentum_score(self, row):
        """Calculate momentum component score (0–1). Used for legacy Momentum_Score field."""
        try:
            score = 0.0
            factors = 0

            momentum_10 = row.get('Momentum_10')
            if momentum_10 is not None and not pd.isna(momentum_10):
                if momentum_10 > 0.1:      score += 1.0
                elif momentum_10 > 0.05:   score += 0.8
                elif momentum_10 > 0:      score += 0.6
                elif momentum_10 > -0.05:  score += 0.4
                elif momentum_10 > -0.1:   score += 0.2
                factors += 1

            momentum_20 = row.get('Momentum_20')
            if momentum_20 is not None and not pd.isna(momentum_20):
                if momentum_20 > 0.15:     score += 1.0
                elif momentum_20 > 0.08:   score += 0.8
                elif momentum_20 > 0:      score += 0.6
                elif momentum_20 > -0.08:  score += 0.4
                elif momentum_20 > -0.15:  score += 0.2
                factors += 1

            volume_ratio = row.get('Volume_Ratio')
            day_change   = row.get('Day_Change_Pct', row.get('Price_Change_Pct', 0)) or 0
            if volume_ratio is not None and not pd.isna(volume_ratio):
                if volume_ratio > 2.0:    base_vol = 1.0 if day_change >= 0 else 0.0
                elif volume_ratio > 1.5:  base_vol = 0.8 if day_change >= 0 else 0.2
                elif volume_ratio > 1.2:  base_vol = 0.65 if day_change >= 0 else 0.35
                elif volume_ratio > 0.8:  base_vol = 0.5
                else:                     base_vol = 0.3
                score += base_vol
                factors += 1

            return score / factors if factors > 0 else 0.5
        except Exception as e:
            logger.warning(f"Error calculating momentum score: {e}")
            return 0.5

    def _get_portfolio_context(self, symbol, portfolio_snapshot):
        """Get context about current portfolio holdings for a symbol."""
        if portfolio_snapshot is None or portfolio_snapshot.empty:
            return {'held': False, 'shares': 0, 'unrealized_pnl_pct': 0}
        try:
            position = portfolio_snapshot[portfolio_snapshot['Symbol'] == symbol]
            if position.empty:
                return {'held': False, 'shares': 0, 'unrealized_pnl_pct': 0}
            position = position.iloc[0]
            return {
                'held':               True,
                'shares':             position.get('Shares', 0),
                'unrealized_pnl_pct': position.get('Unrealized_PnL_Pct', 0),
                'weight':             position.get('Weight', 0),
            }
        except Exception as e:
            logger.warning(f"Error getting portfolio context for {symbol}: {e}")
            return {'held': False, 'shares': 0, 'unrealized_pnl_pct': 0}

    def _calculate_risk_metrics(self, row, portfolio_context):
        """Calculate risk metrics (volatility, beta, risk level) for a position."""
        try:
            volatility = row.get('Volatility', 0)
            beta       = row.get('Beta', 1.0)

            vix_multiplier = 1.0
            if self._market_context:
                vix = self._market_context.get('vix_analysis', {}).get('current', 20)
                vix_multiplier = max(1.0, vix / 20)

            adjusted_volatility = (volatility or 0) * vix_multiplier

            if adjusted_volatility > 0.4 or (beta is not None and beta > 1.5):
                risk_level = 'High'
            elif adjusted_volatility > 0.25 or (beta is not None and beta > 1.2):
                risk_level = 'Medium'
            else:
                risk_level = 'Low'

            return {
                'volatility':          volatility,
                'adjusted_volatility': adjusted_volatility,
                'beta':                beta,
                'risk_level':          risk_level,
                'vix_multiplier':      vix_multiplier,
            }
        except Exception as e:
            logger.warning(f"Error calculating risk metrics: {e}")
            return {'volatility': 0, 'beta': 1.0, 'risk_level': 'Unknown'}

    def _get_market_regime_label(self):
        """Get current market regime label (BULL / BEAR / SIDEWAYS)."""
        if self._market_context:
            return self._market_context.get('market_regime', {}).get('regime', 'N/A')
        return 'N/A'

    def _get_vix_level(self):
        """Get current VIX value for reporting and caution checks."""
        if self._market_context:
            return self._market_context.get('vix_analysis', {}).get('current', 'N/A')
        return 'N/A'

    # ── Summary and persistence ──────────────────────────────────────────────

    def get_signal_summary(self, signals_df):
        """Return a summary dict of signal counts and top picks."""
        try:
            if signals_df.empty:
                return {}

            buy_signals  = signals_df[signals_df['Signal'] == 'BUY']
            # Accept legacy 'AVOID' alongside 'SELL' so 规避 counts never silently drop.
            sell_signals = signals_df[signals_df['Signal'].isin(['SELL', 'AVOID'])]
            hold_signals = signals_df[signals_df['Signal'] == 'HOLD']
            dnt_col      = signals_df['Do_Not_Trade'] if 'Do_Not_Trade' in signals_df.columns else None
            dnt_signals  = signals_df[dnt_col == True] if dnt_col is not None else signals_df.iloc[0:0]

            return {
                'total_signals':           len(signals_df),
                'buy_signals':             len(buy_signals),
                'sell_signals':            len(sell_signals),
                'hold_signals':            len(hold_signals),
                'do_not_trade_count':      len(dnt_signals),
                'avg_confidence':          signals_df['Confidence'].mean(),
                'top_buy_picks':           buy_signals.nlargest(3, 'Adjusted_Score')['Symbol'].tolist(),
                'top_sell_picks':          sell_signals.nsmallest(3, 'Adjusted_Score')['Symbol'].tolist(),
                'high_confidence_signals': len(signals_df[signals_df['Confidence'] > 70]),
                'market_regime':           self._get_market_regime_label(),
                'vix_level':               self._get_vix_level(),
                'avg_risk_reward':         signals_df['Risk_Reward_Ratio'].mean()
                                           if 'Risk_Reward_Ratio' in signals_df.columns else None,
            }
        except Exception as e:
            logger.error(f"Error generating signal summary: {e}")
            return {}

    def save_signals(self, signals_df, filename=None):
        """Save signals DataFrame to a dated CSV in data/."""
        try:
            if filename is None:
                filename = f"signals_{datetime.now().strftime('%Y%m%d')}.csv"
            filepath = f"data/{filename}"
            signals_df.to_csv(filepath, index=False)
            logger.info(f"Signals saved to {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving signals: {e}")
            return None
