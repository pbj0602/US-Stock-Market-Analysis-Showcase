"""
Position Advisor — exit management for the user's *real* holdings.

Fills the biggest hole in the system: every other module answers "what should
I buy?". Nothing answered "should I sell what I already own?". A `SELL` signal
from SignalGenerator means "this cheap-looking stock is a value trap, don't
buy it" — it was never an instruction to exit a position, and real holdings
were explicitly exempted from the advice account's stop/target processing.

This module evaluates each holding in data/portfolio.csv every morning and
emits one of three actions:

  EXIT — a hard rule fired: the ratcheting stop was breached, the trend broke
         down while underwater, the name became a value trap, or the position
         has been dead money past the time stop.
  TRIM — the thesis still holds but the position is oversized, overbought, or
         carrying binary event risk into earnings.
  HOLD — nothing to do; the stop price is still reported so the user always
         knows where the position gets closed.

Stop levels live in data/holding_stops.json and **only ever move up**:
  1. Anchor  — cost basis minus HOLDING_STOP_PCT, widened to the recent swing
               low when real support sits lower, clamped into the min/max band.
  2. Breakeven — once the position is up HOLDING_BREAKEVEN_R, the stop lifts
               to the cost basis so a winner cannot round-trip into a loss.
  3. Trail   — beyond that, the stop trails HOLDING_TRAIL_PCT below the
               high-water mark reached while holding.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import pandas as pd

from config.config import Config

logger = logging.getLogger(__name__)

STATE_PATH = "data/holding_stops.json"


class PositionAdvisor:
    def __init__(self, state_path: str = STATE_PATH):
        self.state_path = state_path
        self._state = self._load_state()

    # ── State ────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning(f"Could not save holding stops: {e}")

    # ── Entry point ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        portfolio_snapshot: pd.DataFrame,
        market_data: pd.DataFrame,
        signals: pd.DataFrame = None,
        event_analysis: dict = None,
        market_regime: str = "SIDEWAYS",
    ) -> list:
        """Return one advice dict per holding, EXIT first then TRIM then HOLD."""
        if portfolio_snapshot is None or portfolio_snapshot.empty:
            return []

        market_rows = self._index_by_symbol(market_data)
        signal_rows = self._index_by_symbol(signals)
        today = datetime.now().strftime("%Y-%m-%d")
        advices = []

        for _, pos in portfolio_snapshot.iterrows():
            try:
                advices.append(self._evaluate_one(
                    pos, market_rows, signal_rows, event_analysis or {},
                    market_regime, today,
                ))
            except Exception as e:
                logger.warning(f"Position advice failed for {pos.get('Symbol')}: {e}")

        self._prune_state({str(s.get("Symbol", "")) for _, s in portfolio_snapshot.iterrows()})
        self._save_state()

        order = {"EXIT": 0, "TRIM": 1, "HOLD": 2}
        advices.sort(key=lambda a: (order.get(a["action"], 9), -(a.get("weight") or 0)))
        return advices

    def _evaluate_one(self, pos, market_rows, signal_rows, event_analysis,
                      market_regime, today) -> dict:
        symbol = str(pos.get("Symbol", ""))
        price = _f(pos.get("Current_Price"))
        cost = _f(pos.get("Purchase_Price"))
        pnl_pct = _f(pos.get("Unrealized_PnL_Pct")) or 0.0
        weight = _f(pos.get("Weight")) or 0.0
        shares = _f(pos.get("Shares")) or 0.0
        held_days = _f(pos.get("Holding_Days")) or 0.0
        rsi = _f(pos.get("RSI"))
        row = market_rows.get(symbol, {})

        # ── CORE holdings are exempt from every exit rule ────────────────────
        # The index sleeve is a permanent holding, not a trade. Stops, trend
        # breaks, value-trap tests and time stops all exist to get out of a
        # *stock* whose thesis broke; an index has no thesis to break, and
        # selling it on a signal is precisely how a buy-and-hold return gets
        # converted into a trading loss. It is rebalanced, never exited.
        if symbol in Config.CORE_SYMBOLS:
            core_reason = (
                f"{symbol} 属于核心仓（目标{Config.CORE_TARGET_PCT:.0f}%），"
                "长期持有、不因信号卖出。回调只加不减（Path C），"
                "仅在偏离目标权重时再平衡。"
            )
            return {
                "symbol":        symbol,
                "action":        "HOLD",
                "action_zh":     "继续持有(核心仓)",
                "price":         round(price, 2) if price is not None else None,
                "cost":          round(cost, 2) if cost is not None else None,
                "stop":          None,
                "stop_basis":    "core",
                "stop_basis_zh": "核心仓不设止损",
                "stop_room_pct": None,
                "pnl_pct":       round(pnl_pct, 2),
                "weight":        round(weight, 1),
                "shares":        shares,
                "held_days":     int(held_days),
                "rsi":           round(rsi, 1) if rsi is not None else None,
                "exit_value":    None,
                "signal":        signal_rows.get(symbol, {}).get("Signal", ""),
                "reasons":       [core_reason],
                "reason_text":   core_reason,
                "is_core":       True,
            }

        stop, stop_basis = self._update_stop(symbol, price, cost, row, today)

        reasons = []
        action = "HOLD"

        # ── EXIT rules ───────────────────────────────────────────────────────
        if price is not None and stop is not None and price <= stop:
            action = "EXIT"
            reasons.append(
                f"现价${price:.2f}已跌破止损位${stop:.2f}（{_STOP_LABEL.get(stop_basis, stop_basis)}）"
            )

        knife = _falling_knife_signs(row)
        if action != "EXIT" and len(knife) >= 2 and pnl_pct < 0:
            action = "EXIT"
            reasons.append(
                f"趋势转空且已浮亏{pnl_pct:.1f}%（{'、'.join(knife)}），下跌趋势中不宜续抱"
            )

        trap, trap_reasons = _value_trap_reasons(row)
        if action != "EXIT" and trap:
            action = "EXIT"
            reasons.append("基本面转为价值陷阱: " + "; ".join(trap_reasons))

        if (action != "EXIT"
                and held_days >= Config.HOLDING_TIME_STOP_DAYS
                and abs(pnl_pct) <= Config.HOLDING_TIME_STOP_MAX_PNL
                and len(knife) >= 1):
            action = "EXIT"
            reasons.append(
                f"时间止损：持有{held_days:.0f}天仅{pnl_pct:+.1f}%且趋势未转强，"
                "资金占用无回报，建议换仓"
            )

        # ── TRIM rules (only when not already exiting) ───────────────────────
        if action == "HOLD":
            if weight > Config.HOLDING_TRIM_WEIGHT_PCT:
                action = "TRIM"
                reasons.append(
                    f"单只占仓{weight:.0f}%，超过{Config.HOLDING_TRIM_WEIGHT_PCT:.0f}%集中度上限，"
                    "建议减仓分散"
                )
            earn_days = _earnings_within(
                symbol, event_analysis, Config.HOLDING_EARNINGS_WARN_DAYS
            )
            if earn_days is not None and weight > 25:
                action = "TRIM"
                reasons.append(
                    f"{earn_days}日内财报，占仓{weight:.0f}%，二元事件风险过大，建议先减半"
                )
            if (rsi is not None and rsi >= Config.HOLDING_OVERBOUGHT_RSI
                    and pnl_pct > 0):
                action = "TRIM"
                reasons.append(
                    f"RSI={rsi:.0f}超买且浮盈{pnl_pct:+.1f}%，建议部分止盈锁定利润"
                )

        # ── HOLD narrative ───────────────────────────────────────────────────
        if action == "HOLD" and not reasons:
            if price is not None and stop is not None and price > 0:
                room = (price - stop) / price * 100
                reasons.append(
                    f"未触发任何退出条件，距止损位${stop:.2f}还有{room:.1f}%缓冲"
                )
            else:
                reasons.append("未触发任何退出条件")

        sig = signal_rows.get(symbol, {})
        return {
            "symbol":       symbol,
            "action":       action,
            "action_zh":    _ACTION_ZH[action],
            "price":        round(price, 2) if price is not None else None,
            "cost":         round(cost, 2) if cost is not None else None,
            "stop":         round(stop, 2) if stop is not None else None,
            "stop_basis":   stop_basis,
            "stop_basis_zh": _STOP_LABEL.get(stop_basis, stop_basis),
            "stop_room_pct": (round((price - stop) / price * 100, 1)
                              if price and stop and price > 0 else None),
            "pnl_pct":      round(pnl_pct, 2),
            "weight":       round(weight, 1),
            "shares":       shares,
            "held_days":    int(held_days),
            "rsi":          round(rsi, 1) if rsi is not None else None,
            "exit_value":   round(shares * price, 2) if (shares and price) else None,
            "signal":       sig.get("Signal", ""),
            "reasons":      reasons,
            "reason_text":  "；".join(reasons),
        }

    # ── Ratcheting stop ──────────────────────────────────────────────────────

    def _update_stop(self, symbol, price, cost, row, today):
        """Return (stop_price, basis). The stop never moves down."""
        if price is None or price <= 0:
            prev = self._state.get(symbol) or {}
            return prev.get("stop"), prev.get("basis", "none")

        prev = self._state.get(symbol) or {}
        anchor = cost if (cost and cost > 0) else price

        # 1. Initial anchor: cost basis less the base stop, extended down to
        #    real support (recent swing low) when support sits lower, then
        #    clamped so the risk stays definable.
        base_stop = anchor * (1 - Config.HOLDING_STOP_PCT)
        basis = "cost"
        swing_low = _f(row.get("Recent_Swing_Low"))
        if swing_low and 0 < swing_low < price:
            structural = swing_low * 0.99  # 1% buffer below support
            if structural < base_stop:
                base_stop = structural
                basis = "swing_low"

        floor = anchor * (1 - Config.HOLDING_MAX_STOP_PCT)
        ceiling = anchor * (1 - Config.HOLDING_MIN_STOP_PCT)
        base_stop = min(ceiling, max(floor, base_stop))

        # 2. High-water mark drives breakeven and trailing.
        high_water = max(_f(prev.get("high_water")) or 0.0, price)
        risk = anchor - base_stop
        stop = base_stop
        if risk > 0 and high_water >= anchor + Config.HOLDING_BREAKEVEN_R * risk:
            stop = max(stop, anchor)
            basis = "breakeven"
            trailed = high_water * (1 - Config.HOLDING_TRAIL_PCT)
            if trailed > stop:
                stop = trailed
                basis = "trailing"

        # 3. Ratchet: a stop that has been raised never comes back down.
        prev_stop = _f(prev.get("stop"))
        if prev_stop is not None and prev_stop > stop:
            stop, basis = prev_stop, prev.get("basis", basis)

        self._state[symbol] = {
            "stop": round(stop, 2),
            "basis": basis,
            "anchor": round(anchor, 2),
            "high_water": round(high_water, 2),
            "updated": today,
        }
        return round(stop, 2), basis

    def _prune_state(self, live_symbols: set):
        for sym in list(self._state):
            if sym not in live_symbols:
                self._state.pop(sym, None)

    def stop_map(self) -> dict:
        """{symbol: stop_price} — lets the advice account apply the same stops
        to its seed positions instead of exempting them from exits entirely."""
        return {s: v.get("stop") for s, v in self._state.items() if v.get("stop")}

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _index_by_symbol(df) -> dict:
        if df is None or getattr(df, "empty", True):
            return {}
        out = {}
        for _, r in df.iterrows():
            sym = r.get("Symbol")
            if sym:
                out[str(sym)] = r
        return out


_ACTION_ZH = {"EXIT": "🔴 卖出", "TRIM": "🟠 减仓", "HOLD": "🟢 继续持有"}

_STOP_LABEL = {
    "cost":      "成本价下方固定止损",
    "swing_low": "锚定近端支撑位",
    "breakeven": "已上移至成本价（保本）",
    "trailing":  "跟踪止损（跟随最高价）",
    "none":      "暂无",
}


def summarize(advices: list) -> dict:
    """Counts + headline used by the report, WeChat title and logs."""
    exits = [a for a in advices if a["action"] == "EXIT"]
    trims = [a for a in advices if a["action"] == "TRIM"]
    return {
        "advices":    advices,
        "exits":      exits,
        "trims":      trims,
        "exit_count": len(exits),
        "trim_count": len(trims),
        "hold_count": len([a for a in advices if a["action"] == "HOLD"]),
        "has_action": bool(exits or trims),
    }


def _f(v):
    """Coerce to float, treating None/NaN/junk as None."""
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _falling_knife_signs(row) -> list:
    """Shared with SignalGenerator: downtrend evidence, 2+ signs = knife."""
    from modules.signal_generator import SignalGenerator
    return SignalGenerator.falling_knife_signs(row)


def _value_trap_reasons(row):
    from modules.signal_generator import SignalGenerator
    return SignalGenerator().is_value_trap(row)


def _earnings_within(symbol, event_analysis, days):
    try:
        er = (event_analysis.get("stocks", {}).get(symbol, {}) or {}).get("earnings", {}) or {}
        du = er.get("days_until")
        if du is not None and 0 <= du <= days:
            return du
    except Exception:
        return None
    return None
