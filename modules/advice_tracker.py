"""
Advice-Following P&L Tracker — CORE + SATELLITE (2026-09-12 redesign)
====================================================================
Answers one question: **"If I had actually followed the system's daily
recommendations, would I be up or down?"**

The first 10 round-trips answered it: down. 3 winners (all cut by the time
stop, none reaching a target) against 7 losers (all stopped out) is −$11 per
trade by construction, and the account finished −2.2% while SPY was +1.5%.
The ledger is now split into two sleeves with very different jobs:

  CORE (~75%)  — SPY/QQQ, bought once and never sold on a signal. This is
                 where the money is actually made. Path C dip-buying is the
                 only mechanism that moves it.

  SATELLITE (~25%) — at most 2 single-stock positions, and the only place
                 stock-picking is allowed to cost anything. Governed by:
                   · risk-based sizing — 1% of the account per trade, so the
                     stop distance sets the position size
                   · no fixed targets — breakeven at +1R, then a 9% trail
                   · dead-money time stop at 30 days, not a profit cap
                   · pause after 2 consecutive losses
                   · exile for any name stopped out twice in 6 months
                   · a hard kill switch at −5% of the account over 6 months
                   · paper-only until it proves 20 trades of positive alpha

Alpha is measured the only way that matters: satellite return vs. simply
holding SPY with the same capital. If the picking adds nothing, it retires.

State persists in data/advice_account.json so the ledger compounds day over day.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import math

import pandas as pd

from config.config import Config

logger = logging.getLogger(__name__)

STATE_PATH = "data/advice_account.json"
SEED_PORTFOLIO = "data/portfolio.csv"
STOPOUT_REGISTRY = "data/stopout_registry.json"
# Names permanently removed from the satellite universe (2 stop-outs in 6
# months). Shared with SignalGenerator so they never reach the report either.
EXILE_REGISTRY = "data/exile_registry.json"


class AdviceTracker:
    def __init__(
        self,
        initial_capital: float = None,
        state_path: str = STATE_PATH,
        seed_portfolio_file: str = SEED_PORTFOLIO,
    ):
        if initial_capital is None:
            initial_capital = Config.TOTAL_ACCOUNT_VALUE
        self.initial_capital = initial_capital
        self.state_path = state_path
        self.seed_portfolio_file = seed_portfolio_file
        self._state = self._load_state()

    # ── State ────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load advice account state: {e}")
        return {
            "initialized": False,
            "initial_capital": self.initial_capital,
            "cash": self.initial_capital,
            "inception_date": None,
            "inception_spy": None,
            "positions": {},        # {sym: {shares, avg_cost, stop, target, source, sleeve, opened}}
            "closed_trades": [],    # realized round-trips
            "nav_history": [],      # [{date, nav, cash, realized_cum}]
            "realized_cum": 0.0,
            "satellite": self._blank_satellite(),
            "exiles": {},           # {sym: [stopout dates]} — permanent bans
        }

    @staticmethod
    def _blank_satellite() -> dict:
        """Circuit-breaker and accounting state for the stock-picking sleeve."""
        return {
            "loss_streak": 0,        # consecutive losing closes
            "paused_until": None,    # date — set by the loss-streak breaker
            "killed_until": None,    # date — set by the kill switch
            "kill_reason": "",
            "realized_cum": 0.0,     # satellite-only realized P&L
            "closed_count": 0,       # satellite-only closed trades
            "inception_spy": None,   # SPY level when the sleeve first traded
            "inception_date": None,
            "capital_base": None,    # capital the sleeve is measured against
            "spy_shadow_shares": None,   # what that capital would have bought
        }

    def _sat(self) -> dict:
        """Satellite sub-state, tolerating ledgers written before the redesign."""
        s = self._state.get("satellite")
        if not isinstance(s, dict):
            s = self._blank_satellite()
            self._state["satellite"] = s
        for k, v in self._blank_satellite().items():
            s.setdefault(k, v)
        return s

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ── Seeding ──────────────────────────────────────────────────────────────

    def _seed_from_real_holdings(self, prices: dict):
        """One-time: carry the user's real holdings in at cost, deduct from cash."""
        spent = 0.0
        seeded = []
        try:
            if os.path.exists(self.seed_portfolio_file):
                df = pd.read_csv(self.seed_portfolio_file)
                for _, r in df.iterrows():
                    sym = str(r.get("Symbol", "")).strip()
                    shares = float(r.get("Shares", 0) or 0)
                    cost = float(r.get("Purchase_Price", 0) or 0)
                    if not sym or shares <= 0 or cost <= 0:
                        continue
                    self._state["positions"][sym] = {
                        "shares": round(shares, 4),
                        "avg_cost": round(cost, 4),
                        "stop": None,        # seed holdings have no system stop/target
                        "target": None,
                        "source": "seed",
                        "opened": str(r.get("Purchase_Date", "") or ""),
                    }
                    spent += shares * cost
                    seeded.append(f"{sym}({shares:g}@${cost:g})")
        except Exception as e:
            logger.warning(f"Advice account seeding failed: {e}")

        self._state["cash"] = round(max(0.0, self.initial_capital - spent), 2)
        self._state["initialized"] = True
        self._state["inception_date"] = datetime.now().strftime("%Y-%m-%d")
        self._state["inception_spy"] = prices.get("SPY")
        logger.info(
            f"Advice account seeded: {', '.join(seeded) or 'none'} | "
            f"cost ${spent:.2f} | cash ${self._state['cash']:.2f}"
        )

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self, signals_df: pd.DataFrame, market_data: pd.DataFrame,
            market_regime: str = 'SIDEWAYS', holding_stops: dict = None) -> dict:
        """Advance the ledger one day. Returns a summary dict for the report.

        market_regime (BULL/BEAR/SIDEWAYS) caps how much of the capital may be
        invested — the account no longer sits 100% long regardless of tape.
        holding_stops {sym: stop} from PositionAdvisor gives the seeded real
        holdings a stop too; they used to be exempt from every exit rule, so a
        seed position could fall indefinitely without the account reacting.
        """
        try:
            prices, highs, lows = self._price_maps(market_data)

            if not self._state.get("initialized"):
                self._seed_from_real_holdings(prices)

            today = datetime.now().strftime("%Y-%m-%d")
            n_closed_before = len(self._state.get("closed_trades", []))

            # Tag any position written before the sleeve split so every rule
            # below can rely on pos['sleeve'] existing.
            self._migrate_sleeves()
            # One-time: clear out positions opened under the discredited rules
            # so the core sleeve can actually be funded.
            self._migrate_to_core_satellite(prices, today)

            # 0. Adopt the advisor's stops for seed holdings, then ratchet every
            #    satellite stop (breakeven at +1R, trailing thereafter).
            self._apply_holding_stops(holding_stops or {})
            self._ratchet_stops(prices, highs)

            # 1. Exit satellite positions on stop touches. CORE IS NEVER SOLD
            #    here — that is the whole point of the sleeve split.
            self._process_exits(prices, highs, lows, today)

            # 1b. Dead-money check: 30 days and going nowhere.
            self._process_time_stops(prices, today)

            # 2. Close any *satellite* name the system now flags AVOID.
            self._process_avoids(signals_df, prices, today)

            # 3. Core first: establish the index sleeve, then add on dips.
            #    Core runs every single day, including days when the satellite
            #    is paused, killed, or blocked by the regime gate.
            self._ensure_core(prices, today)
            self._process_core_adds(signals_df, prices, today)

            # 4. Satellite last, and only if every circuit breaker allows it.
            self._process_buys(signals_df, prices, today, market_regime)

            # 5. Re-check the kill switch against today's marked-to-market
            #    satellite P&L, so a drawdown trips it without waiting for a
            #    close to realise the loss.
            self._check_kill_switch(prices, today)

            # 4. Mark to market, record NAV.
            nav = self._nav(prices)
            if math.isnan(nav) or nav <= 0:
                logger.warning("Advice NAV is NaN/invalid — skipping NAV record for today")
            else:
                self._state["nav_history"] = [
                    e for e in self._state["nav_history"] if e.get("date") != today
                ]
                self._state["nav_history"].append({
                    "date": today,
                    "nav": round(nav, 2),
                    "cash": round(self._state["cash"], 2),
                    "realized_cum": round(self._state["realized_cum"], 2),
                })
            self._state["nav_history"] = self._state["nav_history"][-252:]
            self._save_state()

            summary = self._summary(nav, prices)
            summary["closed_today"] = self._state["closed_trades"][n_closed_before:]
            return summary

        except Exception as e:
            logger.error(f"Advice tracker error: {e}", exc_info=True)
            return {"error": str(e)}

    # ── Steps ────────────────────────────────────────────────────────────────

    def _apply_holding_stops(self, holding_stops: dict):
        """Give seeded real holdings the position advisor's stop.

        Seed positions were created with stop=None/target=None, which made
        _process_exits skip them unconditionally — the account would ride a
        real holding all the way down and still report it at full weight.
        """
        for sym, pos in self._state.get("positions", {}).items():
            if pos.get("source") != "seed":
                continue
            stop = holding_stops.get(sym)
            if stop is None:
                continue
            prev = pos.get("stop")
            # Advisor stops only ratchet up; mirror that here.
            if prev is None or stop > prev:
                pos["stop"] = round(float(stop), 2)

    # ── Sleeve plumbing ──────────────────────────────────────────────────────

    ARCH_VERSION = "core-satellite-2026-09-12"

    def _migrate_sleeves(self):
        """Label pre-redesign positions so sleeve-aware rules can run on them.

        Positions written before 2026-09-12 have no 'sleeve' key. Index ETFs
        become core (they are what the core sleeve holds anyway); everything
        else — including the old real-holding seeds — becomes satellite, which
        is the conservative choice because satellite rules are the strict ones.
        """
        for sym, pos in self._state.get("positions", {}).items():
            if pos.get("sleeve"):
                continue
            pos["sleeve"] = "core" if sym in Config.CORE_SYMBOLS else "satellite"
            # Legacy positions predate high-water tracking; seed it at cost so
            # the trail has a sane floor instead of reading None.
            pos.setdefault("high_water", pos.get("avg_cost"))
            pos.setdefault("initial_stop", pos.get("stop"))

    def _migrate_to_core_satellite(self, prices, today):
        """One-time transition onto the new architecture.

        The ledger carried 3 legacy stock positions worth 53% of the account,
        opened under rules that lost money and sized by a confidence score with
        no predictive power. Leaving them in place would have starved the core
        sleeve of cash indefinitely — core could only reach 47% of its 75%
        target — and silently grandfathered the old strategy's risk.

        They are closed at market and recorded as a strategy transition, not as
        signal-driven exits, so they neither pollute the loss-streak breaker nor
        earn their names a stop-out exile strike. Closed history is preserved.
        """
        if self._state.get("arch_version") == self.ARCH_VERSION:
            return
        legacy = [
            sym for sym, pos in self._state.get("positions", {}).items()
            if not self._is_core(pos)
        ]
        for sym in legacy:
            price = prices.get(sym)
            if price is None:
                # Can't mark it without a price; leave it for the next run
                # rather than closing at a stale or invented level.
                logger.warning(f"策略重构：{sym} 缺少现价，本次保留，下次运行再处理")
                continue
            self._close(sym, price, "策略重构(转核心+卫星架构)", today)
        # The transition resets the sleeve's scorecard: the 20-trade proving
        # run measures the NEW rules, and must not inherit the old record.
        self._state["satellite"] = self._blank_satellite()
        self._state["arch_version"] = self.ARCH_VERSION
        if legacy:
            logger.warning(
                f"📐 策略重构完成：平掉{len(legacy)}个旧策略持仓"
                f"（{', '.join(legacy)}），资金转入核心仓。"
                f"卫星仓计分归零，重新开始{Config.SATELLITE_PROVE_TRADES}笔验证期。"
            )

    @staticmethod
    def _is_core(pos) -> bool:
        return (pos or {}).get("sleeve") == "core"

    def _sleeve_value(self, sleeve: str, prices: dict) -> float:
        total = 0.0
        for sym, pos in self._state.get("positions", {}).items():
            if pos.get("sleeve") != sleeve:
                continue
            total += pos["shares"] * prices.get(sym, pos.get("avg_cost", 0))
        return total

    def _ensure_core(self, prices, today):
        """Establish the index core, once, in full.

        "Buy it now, in full" — sitting in cash while the core allocation is
        unfilled is itself a position, and over this account's life it was the
        wrong one: SPY rose 1.5% while the cash waited. Only ever buys the
        shortfall, so this is safe to call every day.
        """
        nav = self._nav(prices)
        if nav <= 0:
            return
        core_target_value = nav * (Config.CORE_TARGET_PCT / 100.0)
        weights = Config.CORE_SYMBOLS or {}
        wsum = sum(weights.values()) or 100.0

        for sym, w in weights.items():
            price = prices.get(sym)
            if not price or price <= 0:
                continue
            want_value = core_target_value * (w / wsum)
            pos = self._state["positions"].get(sym)
            have_value = (pos["shares"] * price) if pos else 0.0
            shortfall = want_value - have_value
            # Only act on a meaningful gap, so normal price drift doesn't
            # generate a dribble of tiny buys every morning.
            band = want_value * (Config.CORE_REBALANCE_BAND_PCT / 100.0)
            if shortfall <= max(band, 1.0):
                continue
            spend = min(shortfall, self._state["cash"])
            if spend <= 1.0:
                logger.info(
                    f"Core {sym}: 目标${want_value:.2f}，缺口${shortfall:.2f}，"
                    f"但现金仅${self._state['cash']:.2f} — 等待资金"
                )
                continue
            shares = round(spend / price, 4)
            if shares <= 0:
                continue
            cost = round(shares * price, 2)
            self._state["cash"] = round(max(0.0, self._state["cash"] - cost), 2)
            if pos:
                tot_shares = pos["shares"] + shares
                pos["avg_cost"] = round(
                    (pos["shares"] * pos["avg_cost"] + cost) / tot_shares, 4
                )
                pos["shares"] = round(tot_shares, 4)
            else:
                self._state["positions"][sym] = {
                    "shares": shares,
                    "avg_cost": round(price, 4),
                    # Core carries NO stop and NO target, by design. It is not
                    # sold on signals, stops, trends, or time.
                    "stop": None,
                    "target": None,
                    "source": "core",
                    "sleeve": "core",
                    "opened": today,
                }
            logger.info(
                f"CORE BUY {sym}: {shares}@${price:.2f} (${cost:.2f}) — "
                f"核心仓建仓/补足至目标{Config.CORE_TARGET_PCT:.0f}%"
            )

    def _process_core_adds(self, signals_df, prices, today):
        """Path C dip-buying — the only thing that moves core money.

        A BUY on an index symbol means "the market is on sale". Adds beyond the
        standing core target, capped so core cannot crowd out the satellite.
        """
        if signals_df is None or signals_df.empty or "Signal" not in signals_df.columns:
            return
        buys = signals_df[
            (signals_df["Signal"] == "BUY")
            & (signals_df["Symbol"].isin(Config.CORE_SYMBOLS.keys()))
        ]
        if buys.empty:
            return
        nav = self._nav(prices)
        # Core may stretch to its target plus one satellite sleeve's worth of
        # dry powder; past that a "dip" is just leverage on one asset.
        core_ceiling = nav * ((Config.CORE_TARGET_PCT + Config.SATELLITE_TARGET_PCT / 2) / 100.0)

        for _, r in buys.iterrows():
            sym = str(r.get("Symbol", "")).strip()
            price = prices.get(sym)
            if not sym or not price or price <= 0:
                continue
            core_value = self._sleeve_value("core", prices)
            room = min(core_ceiling - core_value, self._state["cash"])
            # One dip-buy adds a quarter of the core target at most.
            add = min(room, nav * (Config.CORE_TARGET_PCT / 100.0) * 0.25)
            if add <= 5.0:
                continue
            shares = round(add / price, 4)
            if shares <= 0:
                continue
            cost = round(shares * price, 2)
            if cost > self._state["cash"]:
                continue
            self._state["cash"] = round(self._state["cash"] - cost, 2)
            pos = self._state["positions"].get(sym)
            if pos:
                tot = pos["shares"] + shares
                pos["avg_cost"] = round(
                    (pos["shares"] * pos["avg_cost"] + cost) / tot, 4
                )
                pos["shares"] = round(tot, 4)
            else:
                self._state["positions"][sym] = {
                    "shares": shares, "avg_cost": round(price, 4),
                    "stop": None, "target": None,
                    "source": "core", "sleeve": "core", "opened": today,
                }
            logger.info(
                f"CORE ADD {sym}: {shares}@${price:.2f} (${cost:.2f}) — "
                f"大盘回调加仓（Path C）"
            )

    def _ratchet_stops(self, prices, highs=None):
        """Satellite stop management: breakeven at +1R, then trail the high.

        This is the fix for the inverted payoff. Under the old design a winner
        had one fixed target it never reached and one stop it reliably hit, so
        every gain was capped and every loss was full-sized. Now the stop only
        ever rises, and the upside is left open.
        """
        highs = highs or {}
        for sym, pos in self._state.get("positions", {}).items():
            # Core has no stop at all — it is never sold on price action.
            if self._is_core(pos):
                continue
            entry = pos.get("avg_cost")
            stop = pos.get("stop")
            price = prices.get(sym)
            if not entry or price is None:
                continue

            # Track the high-water mark reached while holding.
            hw = pos.get("high_water") or entry
            hw = max(hw, highs.get(sym, price), price)
            pos["high_water"] = round(hw, 4)

            if stop is None:
                continue

            new_stop = stop

            # Stage 1 — breakeven once the trade is up 1R.
            risk = entry - stop if stop < entry else None
            if risk and risk > 0 and price >= entry + Config.SATELLITE_BREAKEVEN_R * risk:
                new_stop = max(new_stop, entry)

            # Stage 2 — trail beneath the high-water mark. Only engages once
            # the trade is profitable, so it can never widen the initial risk.
            if hw > entry:
                trail = hw * (1 - Config.SATELLITE_TRAIL_PCT)
                if trail > entry:
                    new_stop = max(new_stop, trail)

            if new_stop > stop + 1e-9:
                pos["stop"] = round(new_stop, 2)
                logger.info(
                    f"Satellite stop↑ {sym}: ${stop:.2f}→${pos['stop']:.2f} "
                    f"(现价${price:.2f}, 高点${hw:.2f}, "
                    f"{'保本' if abs(pos['stop'] - entry) < 0.01 else f'回撤{Config.SATELLITE_TRAIL_PCT:.0%}移动止损'})"
                )

    def _process_time_stops(self, prices, today):
        """Dead-money check — NOT a profit cap.

        The old 20-trading-day rule closed every winner the account ever had:
        all 3 profitable trades were cut by the clock, META at +10.5% with the
        trend still intact. It now only fires on a position that has gone
        nowhere, so winners are left alone to trail.
        """
        for sym in list(self._state["positions"].keys()):
            pos = self._state["positions"][sym]
            if self._is_core(pos):
                continue  # core is never time-stopped; it is a permanent holding
            opened = pos.get("opened")
            price = prices.get(sym)
            if not opened or price is None:
                continue
            try:
                open_date = datetime.strptime(opened, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            held_days = (datetime.now().date() - open_date).days
            if held_days < Config.SATELLITE_TIME_STOP_DAYS:
                continue
            entry = pos.get("avg_cost") or 0
            pnl_pct = ((price - entry) / entry * 100) if entry else 0
            if pnl_pct < Config.SATELLITE_TIME_STOP_MAX_PNL:
                self._close(
                    sym, price,
                    f"时间止损({held_days}天仍无进展{pnl_pct:+.1f}%)", today,
                )

    def _process_exits(self, prices, highs, lows, today):
        for sym in list(self._state["positions"].keys()):
            pos = self._state["positions"][sym]
            # Core never exits on price. No stop, no target, no exception —
            # selling the index on a signal is what a losing satellite does.
            if self._is_core(pos):
                continue
            stop, target = pos.get("stop"), pos.get("target")
            if stop is None and target is None:
                continue
            price = prices.get(sym)
            if price is None:
                continue
            low = lows.get(sym, price)
            high = highs.get(sym, price)

            exit_price, reason = None, None
            # Stop checked first (conservative: assume worst fill if both touched).
            if stop is not None and low <= stop:
                entry = pos.get("avg_cost") or 0
                # A trailed stop above cost is a WIN being banked, not a loss.
                # Labelling it 止损 would have poisoned the loss-streak counter
                # and the stop-out exile registry with profitable exits.
                reason = "移动止盈" if (entry and stop >= entry) else "止损"
                exit_price = stop
            elif target is not None and high >= target:
                exit_price, reason = target, "止盈"
            if exit_price is not None:
                self._close(sym, exit_price, reason, today)

    def _process_avoids(self, signals_df, prices, today):
        if signals_df is None or signals_df.empty or "Signal" not in signals_df.columns:
            return
        avoid_syms = set(
            signals_df.loc[signals_df["Signal"].isin(["AVOID", "SELL"]), "Symbol"]
        )
        for sym in list(self._state["positions"].keys()):
            pos = self._state["positions"][sym]
            if self._is_core(pos):
                continue  # never sell the index on a signal
            if sym in avoid_syms:
                price = prices.get(sym)
                if price:
                    self._close(sym, price, "系统转回避", today)

    # ── Satellite circuit breakers ───────────────────────────────────────────

    def satellite_gate(self, prices=None, today=None) -> dict:
        """Whether the satellite may open a position today, and why not.

        Four independent brakes, checked hardest-first. Each exists because of
        a specific way this account lost money.
        """
        sat = self._sat()
        today = today or datetime.now().strftime("%Y-%m-%d")
        n_open = sum(
            1 for p in self._state.get("positions", {}).values()
            if not self._is_core(p)
        )

        if sat.get("killed_until") and today < sat["killed_until"]:
            return {
                "allowed": False, "state": "KILLED",
                "reason": (
                    f"熔断已触发：{sat.get('kill_reason','')}。"
                    f"卫星仓停止交易至{sat['killed_until']}，仅保留核心仓。"
                ),
                "open": n_open,
            }
        if sat.get("paused_until") and today < sat["paused_until"]:
            return {
                "allowed": False, "state": "PAUSED",
                "reason": (
                    f"连续{Config.SATELLITE_LOSS_STREAK_PAUSE}笔亏损，"
                    f"卫星仓冷静期至{sat['paused_until']}（核心仓照常运行）"
                ),
                "open": n_open,
            }
        if self.satellite_retired():
            return {
                "allowed": False, "state": "RETIRED",
                "reason": (
                    f"已完成{sat.get('closed_count',0)}笔交易但超额收益为负，"
                    "按既定规则永久退役卫星仓——选股没有创造价值。"
                ),
                "open": n_open,
            }
        if n_open >= Config.SATELLITE_MAX_POSITIONS:
            return {
                "allowed": False, "state": "FULL",
                "reason": f"卫星仓已满（{n_open}/{Config.SATELLITE_MAX_POSITIONS}）",
                "open": n_open,
            }
        return {"allowed": True, "state": "OPEN", "reason": "", "open": n_open}

    def satellite_retired(self) -> bool:
        """True once the sleeve has had its 20 trades and failed to add alpha."""
        sat = self._sat()
        if sat.get("closed_count", 0) < Config.SATELLITE_PROVE_TRADES:
            return False
        alpha = self._satellite_alpha(None)
        return alpha is not None and alpha <= Config.SATELLITE_RETIRE_ALPHA_PCT

    def _is_exiled(self, sym) -> bool:
        """A name stopped out twice inside 6 months never comes back.

        NKE was bought, stopped out, and bought again within three weeks — and
        stopped out again. The 20-day rebuy ban was not enough on its own.
        """
        dates = (self._state.get("exiles") or {}).get(sym) or []
        cutoff = datetime.now().date() - timedelta(
            days=Config.SATELLITE_EXILE_LOOKBACK_DAYS
        )
        recent = []
        for d in dates:
            try:
                if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff:
                    recent.append(d)
            except (ValueError, TypeError):
                continue
        return len(recent) >= Config.SATELLITE_EXILE_STOPOUTS

    def _record_exile_strike(self, sym, today):
        exiles = self._state.setdefault("exiles", {})
        strikes = exiles.setdefault(sym, [])
        if today not in strikes:
            strikes.append(today)
        if len(strikes) >= Config.SATELLITE_EXILE_STOPOUTS:
            logger.warning(
                f"{sym} 已在6个月内第{len(strikes)}次止损出局 — 永久移出卫星仓可选范围"
            )
        # Mirror to a shared file so SignalGenerator can drop exiled names at
        # source. Without this the report would keep printing BUY setups the
        # account is forbidden to take — advice the user might follow by hand.
        self._publish_exiles()

    def _publish_exiles(self):
        try:
            current = sorted(
                s for s in (self._state.get("exiles") or {}) if self._is_exiled(s)
            )
            os.makedirs(os.path.dirname(EXILE_REGISTRY) or ".", exist_ok=True)
            tmp = EXILE_REGISTRY + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"exiled": current}, f, indent=2, ensure_ascii=False)
            os.replace(tmp, EXILE_REGISTRY)
        except OSError as e:
            logger.warning(f"Could not publish exile registry: {e}")

    def _monthly_breakdown(self, prices):
        """Per-month satellite P&L and alpha — "track it monthly" made literal.

        A single cumulative alpha number hides whether the sleeve is improving
        or bleeding steadily. This is the table to read before deciding whether
        the 20-trade verdict is going to be worth waiting for.
        """
        months = {}
        for t in self._state.get("closed_trades", []):
            if t.get("sleeve") != "satellite":
                continue
            if "策略重构" in (t.get("reason") or ""):
                continue
            m = (t.get("closed") or "")[:7]
            if not m:
                continue
            e = months.setdefault(m, {"month": m, "trades": 0, "pnl": 0.0, "wins": 0})
            e["trades"] += 1
            e["pnl"] = round(e["pnl"] + t.get("pnl", 0.0), 2)
            if t.get("pnl", 0) > 0:
                e["wins"] += 1
        base = self._sat().get("capital_base") or 0
        out = []
        for m in sorted(months):
            e = months[m]
            e["return_pct"] = round(e["pnl"] / base * 100, 2) if base else None
            e["win_rate_pct"] = round(e["wins"] / e["trades"] * 100, 1) if e["trades"] else None
            out.append(e)
        return out[-6:]

    def _check_kill_switch(self, prices, today):
        """Hard stop on the whole sleeve at −5% of the account over 6 months.

        Decided in advance, on purpose. The failure mode this prevents is the
        one every losing account has: deciding mid-drawdown that this time is
        different and the next trade will make it back.
        """
        sat = self._sat()
        if sat.get("killed_until") and today < sat["killed_until"]:
            return
        cutoff_str = (
            datetime.now().date()
            - timedelta(days=Config.SATELLITE_KILL_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

        realized = 0.0
        for t in self._state.get("closed_trades", []):
            if t.get("sleeve") != "satellite":
                continue
            if (t.get("closed") or "") >= cutoff_str:
                realized += t.get("pnl", 0.0)
        # Open satellite P&L counts too — a drawdown shouldn't need to be
        # realised before the switch notices it.
        unrealized = 0.0
        for sym, pos in self._state.get("positions", {}).items():
            if self._is_core(pos):
                continue
            price = prices.get(sym, pos.get("avg_cost", 0))
            unrealized += (price - pos.get("avg_cost", 0)) * pos.get("shares", 0)

        total = realized + unrealized
        account = self._state.get("initial_capital", self.initial_capital) or 1
        limit = -abs(account * (Config.SATELLITE_KILL_SWITCH_PCT / 100.0))
        if total <= limit:
            year_end = f"{datetime.now().year}-12-31"
            sat["killed_until"] = year_end
            sat["kill_reason"] = (
                f"近6个月卫星仓累计亏损${total:.2f}"
                f"（已实现${realized:.2f}+浮动${unrealized:.2f}），"
                f"达到账户{Config.SATELLITE_KILL_SWITCH_PCT:.0f}%熔断线${abs(limit):.2f}"
            )
            logger.warning(f"🛑 卫星仓熔断触发：{sat['kill_reason']}，停止至{year_end}")

    def _satellite_alpha(self, prices):
        """Satellite return minus SPY-with-the-same-capital, in percent.

        The only question worth asking of a stock-picking sleeve: did picking
        beat simply owning more of the index with that money? Measured against
        the sleeve's own capital base, not the whole account, so core gains
        cannot flatter it.
        """
        sat = self._sat()
        base = sat.get("capital_base")
        inc_spy = sat.get("inception_spy")
        if not base or not inc_spy:
            return None
        realized = sat.get("realized_cum", 0.0)
        unrealized = 0.0
        if prices:
            for sym, pos in self._state.get("positions", {}).items():
                if self._is_core(pos):
                    continue
                price = prices.get(sym, pos.get("avg_cost", 0))
                unrealized += (price - pos.get("avg_cost", 0)) * pos.get("shares", 0)
        sat_return = (realized + unrealized) / base * 100

        cur_spy = (prices or {}).get("SPY")
        if not cur_spy:
            # Fall back to the last NAV-recorded SPY if today's is missing.
            cur_spy = sat.get("last_spy")
        if not cur_spy:
            return None
        sat["last_spy"] = cur_spy
        spy_return = (cur_spy - inc_spy) / inc_spy * 100
        return round(sat_return - spy_return, 2)

    def _process_buys(self, signals_df, prices, today, market_regime='SIDEWAYS'):
        """Open satellite positions — risk-sized, capped, and circuit-broken."""
        gate = self.satellite_gate(prices, today)
        if not gate["allowed"]:
            logger.info(f"卫星仓今日不开新仓：{gate['reason']}")
            return
        if signals_df is None or signals_df.empty or "Signal" not in signals_df.columns:
            return
        buys = signals_df[signals_df["Signal"] == "BUY"].copy()
        if buys.empty:
            return
        # Index symbols are core business and were already handled there.
        buys = buys[~buys["Symbol"].isin(Config.CORE_SYMBOLS.keys())]
        if buys.empty:
            return
        # Same floor the report uses: the account must never act on a signal
        # the report itself wouldn't show as a trade setup.
        if "Confidence" in buys.columns:
            buys = buys[buys["Confidence"].fillna(0) >= Config.MIN_BUY_CONFIDENCE]
            buys = buys.sort_values("Confidence", ascending=False)
        if buys.empty:
            return

        # Symbols closed today shouldn't be re-bought same day (avoids churn).
        closed_today = {t["symbol"] for t in self._state["closed_trades"] if t.get("closed") == today}
        stopouts = self._load_stopouts()

        nav = self._nav(prices)
        sat = self._sat()
        opened_today = 0

        for _, r in buys.iterrows():
            if opened_today >= Config.ADVICE_MAX_NEW_POSITIONS_PER_DAY:
                break
            # Re-check the cap inside the loop: a second name in the same batch
            # must not push the sleeve past its position limit.
            if not self.satellite_gate(prices, today)["allowed"]:
                break
            sym = str(r.get("Symbol", "")).strip()
            if not sym or sym in self._state["positions"] or sym in closed_today:
                continue
            if sym in stopouts:
                logger.info(f"Advice skip {sym}: in stop-out rebuy ban since {stopouts[sym]}")
                continue
            if self._is_exiled(sym):
                logger.info(
                    f"Advice skip {sym}: 6个月内已被止损{Config.SATELLITE_EXILE_STOPOUTS}次，"
                    "已永久移出可选范围"
                )
                continue
            entry = float(r.get("Buy_Price") or r.get("Current_Price") or prices.get(sym) or 0)
            if entry <= 0 or math.isnan(entry):
                continue

            # ── RISK-BASED SIZING ─────────────────────────────────────────
            # The stop distance sets the size, so every trade risks the same
            # dollar amount. Under the old confidence-based sizing a 44-conf
            # signal and a 64-conf signal both got ~25% of capital and a run of
            # five losses cost 10.7% of the account; this caps that at 5%.
            stop = _f(r.get("Stop_Loss"))
            if stop is None or stop <= 0 or stop >= entry:
                logger.info(f"Advice skip {sym}: 无有效止损价，无法按风险定仓")
                continue
            risk_budget = nav * (Config.SATELLITE_RISK_PER_TRADE_PCT / 100.0)
            shares = risk_budget / (entry - stop)

            # Never exceed the satellite sleeve, nor available cash.
            sleeve_cap = nav * (Config.SATELLITE_TARGET_PCT / 100.0)
            sleeve_used = self._sleeve_value("satellite", prices)
            room = min(sleeve_cap - sleeve_used, self._state["cash"])
            if room <= 1:
                logger.info(
                    f"Advice skip {sym}: 卫星仓额度已用尽"
                    f"（已用${sleeve_used:.2f}/${sleeve_cap:.2f}）"
                )
                continue
            shares = round(min(shares, room / entry), 4)
            if shares <= 0:
                continue

            cost = round(shares * entry, 2)
            if cost > self._state["cash"]:
                shares = round((self._state["cash"] * 0.999) / entry, 4)
                cost = round(shares * entry, 2)
                if shares <= 0 or cost <= 1:
                    continue
            actual_risk = round(shares * (entry - stop), 2)
            self._state["cash"] = round(max(0.0, self._state["cash"] - cost), 2)
            opened_today += 1

            # First satellite trade fixes the alpha baseline: the capital this
            # sleeve is judged on, and the SPY level it must beat.
            if not sat.get("inception_spy"):
                sat["inception_spy"] = prices.get("SPY")
                sat["inception_date"] = today
                sat["capital_base"] = round(sleeve_cap, 2)

            self._state["positions"][sym] = {
                "shares": shares,
                "avg_cost": round(entry, 4),
                "stop": stop,
                # No fixed target — the trail decides when to leave.
                "target": _f(r.get("Target_Price")) if Config.SATELLITE_USE_FIXED_TARGETS else None,
                "source": "advice",
                "sleeve": "satellite",
                "opened": today,
                "confidence": _f(r.get("Confidence")),
                "initial_stop": stop,
                "high_water": round(entry, 4),
                "risk_dollars": actual_risk,
                # Paper until the sleeve earns the right to real money.
                "paper": bool(Config.SATELLITE_PAPER_ONLY),
            }
            logger.info(
                f"SATELLITE BUY {sym}: {shares}@${entry:.2f} (${cost:.2f}) "
                f"stop=${stop:.2f} 风险${actual_risk:.2f}"
                f"({Config.SATELLITE_RISK_PER_TRADE_PCT:.0f}%账户) 移动止盈，无固定止盈"
                + ("  [纸面交易]" if Config.SATELLITE_PAPER_ONLY else "")
            )

    # ── Stop-out registry (shared with SignalGenerator) ──────────────────────

    def _load_stopouts(self) -> dict:
        """Load {symbol: date}, pruning entries older than the rebuy ban."""
        try:
            with open(STOPOUT_REGISTRY, encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        today = datetime.now().date()
        kept = {}
        for sym, dstr in raw.items():
            try:
                d = datetime.strptime(dstr, "%Y-%m-%d").date()
                elapsed = len(pd.bdate_range(start=d, end=today, inclusive="left"))
                if elapsed < Config.STOPOUT_REBUY_BAN_DAYS:
                    kept[sym] = dstr
            except Exception:
                continue
        if kept != raw:
            self._save_stopouts(kept)
        return kept

    @staticmethod
    def _save_stopouts(registry: dict):
        try:
            os.makedirs(os.path.dirname(STOPOUT_REGISTRY) or ".", exist_ok=True)
            tmp = STOPOUT_REGISTRY + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)
            os.replace(tmp, STOPOUT_REGISTRY)
        except OSError as e:
            logger.warning(f"Could not save stop-out registry: {e}")

    def _record_stopout(self, sym, today):
        registry = self._load_stopouts()
        registry[sym] = today
        self._save_stopouts(registry)

    def _close(self, sym, exit_price, reason, today):
        pos = self._state["positions"].pop(sym)
        shares = pos["shares"]
        sleeve = pos.get("sleeve") or "satellite"
        proceeds = round(shares * exit_price, 2)
        cost = shares * pos["avg_cost"]
        pnl = round(proceeds - cost, 2)
        pnl_pct = round((exit_price - pos["avg_cost"]) / pos["avg_cost"] * 100, 2) if pos["avg_cost"] else 0
        self._state["cash"] = round(self._state["cash"] + proceeds, 2)
        if reason == "止损":
            # Ban the name from re-recommendation/rebuy for a while — the old
            # 5-day cooldown let DIS get stopped 7/1 and re-signalled 7/3.
            self._record_stopout(sym, today)
            # Second strike inside 6 months removes the name permanently.
            self._record_exile_strike(sym, today)
        self._state["realized_cum"] = round(self._state["realized_cum"] + pnl, 2)

        if sleeve == "satellite":
            sat = self._sat()
            sat["realized_cum"] = round(sat.get("realized_cum", 0.0) + pnl, 2)
            sat["closed_count"] = sat.get("closed_count", 0) + 1
            # Loss-streak breaker. Counts actual losses, so a trailed exit
            # above cost (booked as 移动止盈) correctly resets the streak.
            if pnl < 0:
                sat["loss_streak"] = sat.get("loss_streak", 0) + 1
                if sat["loss_streak"] >= Config.SATELLITE_LOSS_STREAK_PAUSE:
                    resume = (
                        pd.bdate_range(
                            start=datetime.now().date(),
                            periods=Config.SATELLITE_PAUSE_DAYS + 1,
                        )[-1].date().strftime("%Y-%m-%d")
                    )
                    sat["paused_until"] = resume
                    sat["loss_streak"] = 0
                    logger.warning(
                        f"⏸ 卫星仓连续{Config.SATELLITE_LOSS_STREAK_PAUSE}笔亏损 — "
                        f"暂停开新仓至{resume}（{Config.SATELLITE_PAUSE_DAYS}个交易日），"
                        "核心仓继续运行"
                    )
            else:
                sat["loss_streak"] = 0

        self._state["closed_trades"].append({
            "symbol": sym,
            "shares": shares,
            "entry": pos["avg_cost"],
            "exit": round(exit_price, 2),
            "reason": reason,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "opened": pos.get("opened", ""),
            "closed": today,
            "sleeve": sleeve,
            "paper": pos.get("paper", False),
        })
        logger.info(
            f"{sleeve.upper()} CLOSE {sym} @ ${exit_price:.2f} [{reason}] "
            f"P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)"
        )

    # ── Valuation & summary ──────────────────────────────────────────────────

    def _nav(self, prices: dict) -> float:
        nav = self._state.get("cash", 0.0)
        for sym, pos in self._state.get("positions", {}).items():
            price = prices.get(sym, pos.get("avg_cost", 0))
            nav += pos["shares"] * price
        return nav

    def _summary(self, nav: float, prices: dict) -> dict:
        cap = self._state.get("initial_capital", self.initial_capital)
        total_return = (nav - cap) / cap if cap else 0

        positions = []
        unrealized = 0.0
        for sym, pos in self._state.get("positions", {}).items():
            price = prices.get(sym, pos.get("avg_cost", 0))
            mv = pos["shares"] * price
            pnl = (price - pos["avg_cost"]) / pos["avg_cost"] * 100 if pos["avg_cost"] else 0
            unrealized += mv - pos["shares"] * pos["avg_cost"]
            positions.append({
                "symbol": sym,
                "shares": round(pos["shares"], 4),
                "avg_cost": round(pos["avg_cost"], 2),
                "current_price": round(price, 2),
                "market_value": round(mv, 2),
                "pnl_pct": round(pnl, 2),
                "stop": pos.get("stop"),
                "target": pos.get("target"),
                "source": pos.get("source", "advice"),
                "sleeve": pos.get("sleeve", "satellite"),
                "high_water": pos.get("high_water"),
                "paper": pos.get("paper", False),
            })
        positions.sort(key=lambda x: x["market_value"], reverse=True)

        # SPY buy-and-hold benchmark of the same starting capital since inception.
        spy_return = 0.0
        inc_spy = self._state.get("inception_spy")
        cur_spy = prices.get("SPY")
        if inc_spy and cur_spy:
            spy_return = (cur_spy - inc_spy) / inc_spy

        closed = self._state.get("closed_trades", [])
        wins = [t for t in closed if t["pnl"] > 0]
        win_rate = round(len(wins) / len(closed) * 100, 1) if closed else None

        # ── Sleeve breakdown ─────────────────────────────────────────────
        sat = self._sat()
        core_value = self._sleeve_value("core", prices)
        sat_value = self._sleeve_value("satellite", prices)
        # The architecture transition closed legacy positions at market. Those
        # are not results of the new rules and must not flatter (or damage) the
        # scorecard the 20-trade proving run is judged on.
        sat_closed = [
            t for t in closed
            if t.get("sleeve") == "satellite"
            and "策略重构" not in (t.get("reason") or "")
        ]
        sat_wins = [t for t in sat_closed if t["pnl"] > 0]
        gate = self.satellite_gate(prices)
        sat_alpha = self._satellite_alpha(prices)
        trades_done = sat.get("closed_count", 0)

        sleeves = {
            "core": {
                "value": round(core_value, 2),
                "pct": round(core_value / nav * 100, 1) if nav else 0,
                "target_pct": Config.CORE_TARGET_PCT,
                "symbols": list(Config.CORE_SYMBOLS.keys()),
                "on_target": abs(
                    (core_value / nav * 100 if nav else 0) - Config.CORE_TARGET_PCT
                ) <= Config.CORE_REBALANCE_BAND_PCT,
            },
            "satellite": {
                "value": round(sat_value, 2),
                "pct": round(sat_value / nav * 100, 1) if nav else 0,
                "target_pct": Config.SATELLITE_TARGET_PCT,
                "open": gate.get("open", 0),
                "max_positions": Config.SATELLITE_MAX_POSITIONS,
                "state": gate.get("state"),
                "allowed": gate.get("allowed"),
                "gate_reason": gate.get("reason", ""),
                "paused_until": sat.get("paused_until"),
                "killed_until": sat.get("killed_until"),
                "kill_reason": sat.get("kill_reason", ""),
                "realized_pnl": round(sat.get("realized_cum", 0.0), 2),
                "trades_closed": trades_done,
                "trades_to_prove": max(0, Config.SATELLITE_PROVE_TRADES - trades_done),
                "win_rate_pct": (
                    round(len(sat_wins) / len(sat_closed) * 100, 1) if sat_closed else None
                ),
                "alpha_vs_spy_pct": sat_alpha,
                "paper_only": bool(Config.SATELLITE_PAPER_ONLY),
                "retired": self.satellite_retired(),
                "risk_per_trade": round(
                    cap * Config.SATELLITE_RISK_PER_TRADE_PCT / 100.0, 2
                ),
                "kill_switch_at": round(
                    cap * Config.SATELLITE_KILL_SWITCH_PCT / 100.0, 2
                ),
                # The verdict the whole sleeve exists to produce.
                "verdict": (
                    "RETIRE" if self.satellite_retired()
                    else "PROVING" if trades_done < Config.SATELLITE_PROVE_TRADES
                    else "EARNING" if (sat_alpha or 0) > 0 else "REVIEW"
                ),
            },
        }

        return {
            "nav": round(nav, 2),
            "initial_capital": cap,
            "cash": round(self._state.get("cash", 0), 2),
            "total_return_pct": round(total_return * 100, 2),
            "total_pnl": round(nav - cap, 2),
            "realized_pnl": round(self._state.get("realized_cum", 0), 2),
            "unrealized_pnl": round(unrealized, 2),
            "positions": positions,
            "num_positions": len(positions),
            "closed_trades": closed[-10:],
            "num_closed": len(closed),
            "win_rate_pct": win_rate,
            "spy_return_pct": round(spy_return * 100, 2),
            "alpha_pct": round((total_return - spy_return) * 100, 2),
            "inception_date": self._state.get("inception_date"),
            "nav_history": self._state.get("nav_history", [])[-30:],
            "sleeves": sleeves,
            "exiles": sorted(
                s for s in (self._state.get("exiles") or {}) if self._is_exiled(s)
            ),
            "satellite_monthly": self._monthly_breakdown(prices),
        }

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _price_maps(market_data: pd.DataFrame):
        """Return (prices, highs, lows) keyed by symbol from the daily snapshot."""
        prices, highs, lows = {}, {}, {}
        if market_data is None or market_data.empty:
            return prices, highs, lows
        for _, row in market_data.iterrows():
            sym = row.get("Symbol")
            price = row.get("Current_Price") or row.get("Close")
            # NaN is truthy and passes `float(price) <= 0` — it poisoned NAV
            # history with NaN entries (2026-06-26, 07-03). Filter explicitly.
            if not sym or price is None or pd.isna(price) or float(price) <= 0:
                continue
            p = float(price)
            prices[sym] = p
            dh, dl = row.get("Day_High"), row.get("Day_Low")
            highs[sym] = float(dh) if dh and not pd.isna(dh) and float(dh) > 0 else p
            lows[sym] = float(dl) if dl and not pd.isna(dl) and float(dl) > 0 else p
        return prices, highs, lows


def _f(v):
    """Coerce to float or None (NaN-safe) for JSON-serialisable state."""
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), 2)
    except (ValueError, TypeError):
        return None
