"""
Shared report model — one source of truth for everything both the HTML report
and the WeChat push display.

The two channels used to derive the same facts independently: the Jinja
template computed the risk level, action checklist and TL;DR from
`market_context`, while wechat_notifier recomputed them from its own
`report_data` dict. Predictably they drifted — different risk wording,
different checklist items, a fake "stop loss" in WeChat that was just
`price × 0.93`, and sections in different orders.

Everything genuinely *shared* is computed once here. Each channel still owns
its own presentation (HTML cards vs Markdown tables); neither owns the facts.
"""

from __future__ import annotations

import math

# Presentation-neutral vocabulary shared by both channels.
RECOMMENDATION_ZH = {
    "FULL_DEPLOYMENT": "积极建仓", "AGGRESSIVE_LONG": "积极做多",
    "MODERATE_LONG": "适度做多",
    "CAUTIOUS": "谨慎操作", "REDUCE_EXPOSURE": "减仓规避",
    "AVOID": "暂停交易", "DEFENSIVE": "防御为主",
    "HOLD": "持仓观望", "NEUTRAL": "中性",
}
VIX_REGIME_ZH = {
    "Low": "低波动", "Normal": "正常", "Elevated": "偏高",
    "High": "高波动", "Extreme": "极端恐慌",
}
SECTOR_ZH = {
    "Technology": "科技", "Financials": "金融", "Energy": "能源",
    "Industrials": "工业", "Healthcare": "医疗",
    "Consumer Discretionary": "可选消费", "Consumer Staples": "必需消费",
    "Communication Services": "通信服务", "Real Estate": "房地产",
    "Utilities": "公用事业", "Materials": "原材料",
}
REGIME_ZH = {"BULL": "牛市", "BEAR": "熊市", "SIDEWAYS": "震荡"}
REGIME_EMOJI = {"BULL": "🟢", "BEAR": "🔴", "SIDEWAYS": "🟡"}
SIGNAL_ZH = {
    "BUY": "入场机会", "SELL": "规避", "HOLD": "观望",
    "FILTERED_LOW_VOLUME": "低量观望",
}


def build_report_model(
    *,
    market_regime: str,
    vix,
    vix_regime: str,
    recommendation: str,
    market_score,
    buy_count: int,
    hold_count: int,
    sell_count: int,
    buy_tickers: list,
    sell_tickers: list,
    extreme_rsi_tickers: list,
    macro_warning: str,
    cross_asset: dict,
    position_advice: dict,
    sector_detail: list,
    total_symbols: int,
    account_value=None,
    account_initial=None,
    holdings_value=None,
    sleeves=None,
    satellite_gate=None,
    watchlist=None,
    satellite_monthly=None,
) -> dict:
    """Assemble the derived facts both channels render."""
    vix_val = _num(vix, 20.0)
    cross_asset = cross_asset or {}
    position_advice = position_advice or {}

    risk = _risk_level(recommendation, vix_val)
    driver = _dominant_factor(macro_warning, cross_asset)
    # Strategy status is computed first: it outranks every signal-derived line
    # below it. An unfunded core is the most important fact of the day, and it
    # stays true whatever the watchlist happens to be flashing.
    strategy = _strategy_status(
        account_value=account_value,
        sleeves=sleeves,
        satellite_gate=satellite_gate,
        holdings_value=holdings_value,
    )
    checklist = _action_checklist(
        recommendation=recommendation,
        vix_val=vix_val,
        buy_count=buy_count,
        buy_tickers=buy_tickers,
        sell_tickers=sell_tickers,
        macro_warning=macro_warning,
        position_advice=position_advice,
        extreme_rsi_tickers=extreme_rsi_tickers,
        strategy=strategy,
    )
    tldr = _tldr(
        recommendation=recommendation,
        vix_val=vix_val,
        regime=market_regime,
        buy_count=buy_count,
        position_advice=position_advice,
        strategy=strategy,
    )

    return {
        "regime":        market_regime,
        "regime_zh":     REGIME_ZH.get(market_regime, market_regime),
        "regime_emoji":  REGIME_EMOJI.get(market_regime, "⚪"),
        "vix":           vix,
        "vix_regime_zh": VIX_REGIME_ZH.get(vix_regime, vix_regime),
        "recommendation":    recommendation,
        "recommendation_zh": RECOMMENDATION_ZH.get(recommendation, recommendation),
        "market_score":  market_score,
        "risk_label":    risk["label"],
        "risk_tone":     risk["tone"],
        "dominant_factor":  driver["factor"],
        "tech_validity":    driver["tech_validity"],
        "buy_count":     buy_count,
        "hold_count":    hold_count,
        "sell_count":    sell_count,
        "total_symbols": total_symbols,
        "tldr":          tldr,
        "checklist":     checklist,
        "account":       _account_status(account_value, account_initial, holdings_value),
        "strategy":      strategy,
        # Only names actually stabilising (≥3/4) or already turned — the rest
        # are just "cheap", which is not information.
        "watchlist":     [w for w in (watchlist or [])
                          if w.get("ready") or (w.get("stabilizing") or 0) >= 3][:8],
        "satellite_monthly": satellite_monthly or [],
        "position_advice": position_advice,
        "sector_table":  _sector_table(sector_detail),
        "selection_criteria": _selection_criteria(total_symbols, buy_count),
        "risk_off_warning": cross_asset.get("risk_off_warning"),
        "risk_off_reason":  cross_asset.get("risk_off_reason", ""),
    }


# ── Derivations ──────────────────────────────────────────────────────────────

def _account_status(account_value, account_initial, holdings_value) -> dict:
    """Where the real account actually stands: cash, exposure, cumulative P&L.

    Previously a flat account rendered as "暂无持仓，请在 data/portfolio.csv 中
    录入您的持仓" — which reads as *unconfigured* when in fact it means the last
    position was sold. A 100%-cash account is a position; say so, and say what
    the round-trip through it cost, so a losing account can't quietly present
    itself as an empty one.
    """
    total    = _num(account_value, 0.0)
    initial  = _num(account_initial, 0.0)
    invested = max(_num(holdings_value, 0.0), 0.0)
    cash     = max(total - invested, 0.0)
    pnl      = total - initial
    return {
        "total":       total,
        "initial":     initial,
        "invested":    invested,
        "cash":        cash,
        "cash_pct":    (cash / total * 100) if total else 0.0,
        "exposure_pct": (invested / total * 100) if total else 0.0,
        "pnl":         pnl,
        "pnl_pct":     (pnl / initial * 100) if initial else 0.0,
        # No holdings at all — the account sits entirely in cash.
        "flat":        invested <= 0.0,
        "known":       total > 0.0,
    }


def _strategy_status(*, account_value, sleeves, satellite_gate, holdings_value) -> dict:
    """The Core+Satellite plan, and where the real account stands against it.

    Both channels render from this, so the WeChat push and the HTML report can
    never disagree about how much belongs in the index, whether stock-picking
    is switched on today, or what the next real-money action is.
    """
    from config.config import Config

    total = _num(account_value, 0.0)
    core_target_val = total * Config.CORE_TARGET_PCT / 100.0
    sat_target_val  = total * Config.SATELLITE_TARGET_PCT / 100.0
    sleeves = sleeves or {}
    sat = sleeves.get("satellite") or {}
    core = sleeves.get("core") or {}
    gate = satellite_gate or {}

    # Real money: is the core actually funded? `holdings_value` is the real
    # portfolio's market value — 0 means the account is sitting in cash.
    real_invested = max(_num(holdings_value, 0.0), 0.0)
    core_funded = real_invested > 0

    # The single next real-money action, in priority order.
    if not core_funded:
        next_action = (
            f"把 ${core_target_val:,.0f}（{Config.CORE_TARGET_PCT:.0f}%）买入核心仓"
            f"（{'/'.join(Config.CORE_SYMBOLS)}），今天就买、一次买满。"
            "空仓本身就是一种仓位，而且过去三个月是错的那一种。"
        )
    elif Config.SATELLITE_PAPER_ONLY:
        need = sat.get("trades_to_prove", Config.SATELLITE_PROVE_TRADES)
        next_action = (
            f"核心仓已建立，继续持有不动。卫星仓仍在纸面验证期"
            f"（还需{need}笔），不要投入真钱。"
        )
    else:
        next_action = "按卫星仓规则执行，每笔风险严格控制在1%账户。"

    blocked = gate.get("blocked") or (sat.get("allowed") is False)
    block_reason = gate.get("reason") or sat.get("gate_reason") or ""

    return {
        "core_target_pct":  Config.CORE_TARGET_PCT,
        "core_target_val":  round(core_target_val, 2),
        "core_symbols":     list(Config.CORE_SYMBOLS.keys()),
        "core_split":       dict(Config.CORE_SYMBOLS),
        "core_funded":      core_funded,
        # These describe the PAPER ledger's core sleeve, not the real account.
        # Conflating them printed "目标 $1,471，当前 75.0%" directly above
        # "go buy it" — the paper account was at 75% while the real one sat in
        # cash. Named and rendered separately so the two can't be confused.
        "core_paper_pct":   core.get("pct"),
        "core_paper_ok":    core.get("on_target"),
        "core_actual_pct":  core.get("pct") if core_funded else None,
        "core_on_target":   core.get("on_target") if core_funded else False,
        "sat_target_pct":   Config.SATELLITE_TARGET_PCT,
        "sat_target_val":   round(sat_target_val, 2),
        "sat_max_positions": Config.SATELLITE_MAX_POSITIONS,
        "sat_open":         sat.get("open", 0),
        "sat_state":        sat.get("state") or ("BLOCKED" if blocked else "OPEN"),
        "sat_blocked":      bool(blocked),
        "sat_block_reason": block_reason,
        "sat_paper_only":   Config.SATELLITE_PAPER_ONLY,
        "sat_trades_done":  sat.get("trades_closed", 0),
        "sat_trades_need":  sat.get("trades_to_prove", Config.SATELLITE_PROVE_TRADES),
        "sat_alpha_pct":    sat.get("alpha_vs_spy_pct"),
        "sat_verdict":      sat.get("verdict", "PROVING"),
        "sat_killed":       bool(sat.get("killed_until")),
        "sat_kill_reason":  sat.get("kill_reason", ""),
        "risk_per_trade":   round(total * Config.SATELLITE_RISK_PER_TRADE_PCT / 100.0, 2),
        "risk_pct":         Config.SATELLITE_RISK_PER_TRADE_PCT,
        "kill_switch_at":   round(total * Config.SATELLITE_KILL_SWITCH_PCT / 100.0, 2),
        "kill_switch_pct":  Config.SATELLITE_KILL_SWITCH_PCT,
        "trail_pct":        Config.SATELLITE_TRAIL_PCT * 100,
        "time_stop_days":   Config.SATELLITE_TIME_STOP_DAYS,
        "pause_after":      Config.SATELLITE_LOSS_STREAK_PAUSE,
        "pause_days":       Config.SATELLITE_PAUSE_DAYS,
        "benchmark":        Config.SATELLITE_RS_BENCHMARK,
        "next_action":      next_action,
    }


def _risk_level(recommendation: str, vix_val: float) -> dict:
    """Single definition of the risk tier. HTML said 高风险/中等风险/正常 while
    WeChat said 高/中等/低 off the same inputs."""
    if recommendation in ("AVOID", "REDUCE_EXPOSURE") or vix_val > 25:
        return {"label": "⚠️ 高风险", "tone": "high"}
    if recommendation in ("CAUTIOUS", "DEFENSIVE") or vix_val > 18:
        return {"label": "🟡 中等风险", "tone": "medium"}
    return {"label": "🟢 正常", "tone": "low"}


def _dominant_factor(macro_warning: str, cross_asset: dict) -> dict:
    if macro_warning:
        return {"factor": "宏观事件（技术指标有效性↓）", "tech_validity": "低"}
    if cross_asset.get("risk_off_warning"):
        return {"factor": "避险情绪", "tech_validity": "中等"}
    return {"factor": "技术面 + 资金流向", "tech_validity": "正常"}


def _action_checklist(*, recommendation, vix_val, buy_count, buy_tickers,
                      sell_tickers, macro_warning, position_advice,
                      extreme_rsi_tickers, strategy=None) -> dict:
    """The four action buckets, computed once for both channels.

    Exit advice leads the DO list — telling the user what to sell out of what
    they actually own outranks any new-entry idea.
    """
    do, dont, watch, tomorrow = [], [], [], []
    strategy = strategy or {}

    exits = position_advice.get("exits") or []
    trims = position_advice.get("trims") or []

    # ── Core+Satellite: only today's state goes here ─────────────────────
    # The standing rules (sizing, "don't buy cheap", paper-only) are the same
    # every day and live in the README; the unfunded-core order is the TL;DR
    # headline. Repeating them here is what made the report say "buy $1,471
    # of SPY/QQQ" four times.
    if strategy.get("core_target_val"):
        if strategy.get("core_funded"):
            do.append(
                f"✅ 核心仓持有不动（{strategy.get('core_actual_pct') or 0:.0f}%/"
                f"{strategy.get('core_target_pct', 75):.0f}%）——只在回调时加仓，永不因信号卖出"
            )
        if strategy.get("sat_killed"):
            dont.append(f"🛑 卫星仓已熔断：{strategy.get('sat_kill_reason','')}")
        elif strategy.get("sat_blocked"):
            dont.append(
                f"⏸ 今日不开新个股仓：{strategy.get('sat_block_reason','')}（核心仓不受影响）"
            )
    for a in exits:
        do.append(
            f"🔴 卖出 {a['symbol']}（现价${a['price']:.2f}，浮动{a['pnl_pct']:+.1f}%）：{a['reason_text']}"
            if a.get("price") is not None else
            f"🔴 卖出 {a['symbol']}：{a['reason_text']}"
        )
    for a in trims:
        do.append(f"🟠 减仓 {a['symbol']}：{a['reason_text']}")

    # How a BUY candidate should be phrased. Under Core+Satellite a BUY is
    # never "a small probe at ≤30%" — it is a risk-sized satellite entry, and
    # while the sleeve is still on paper it is not a real-money action at all.
    # The old wording contradicted the sizing rule on the same page.
    def _entry_line(tickers):
        names = "、".join(tickers[:strategy.get("sat_max_positions", 3)])
        if not strategy.get("core_target_val"):
            return f"考虑入场：{names}（先设止损再建仓）"
        if strategy.get("sat_paper_only"):
            return (
                f"📝 卫星仓候选（**仅纸面记录，不要用真钱**）：{names}"
                f"——按每笔风险${strategy.get('risk_per_trade', 0):.0f}定仓位"
            )
        return (
            f"卫星仓入场：{names}——股数=${strategy.get('risk_per_trade', 0):.0f}"
            "÷(买入价−止损价)，最多同时持有"
            f"{strategy.get('sat_max_positions', 2)}只"
        )

    if recommendation == "AVOID":
        do += ["卫星仓暂停开新仓，等待风险事件落地后重新评估"]
        if strategy.get("core_target_val"):
            do.append("核心仓不动——暂停的是选股，不是指数仓")
        dont += ["新建个股仓位", "追涨或抄底"]
    elif recommendation in ("REDUCE_EXPOSURE", "DEFENSIVE"):
        # Core is exempt: reducing exposure means the satellite, never the index.
        do += ["卫星仓降至最低/空仓，检查止损位"]
        if strategy.get("core_target_val"):
            do.append("核心仓不动——降风险指的是卫星仓，不是指数仓")
        dont += ["满仓操作", "高波动标的加仓"]
    elif recommendation == "CAUTIOUS":
        if buy_tickers:
            do.append(_entry_line(buy_tickers))
        else:
            do.append("卫星仓等待高质量信号")
        dont += ["集中大仓位押注单只股票", "在宏观事件前夕加仓"]
    elif buy_count == 0:
        do += ["整理 Watchlist，做好候选清单", "检查已有持仓的止损价位"]
        dont += ["为了交易而交易", "不设止损入场"]
    else:
        do.append(_entry_line(buy_tickers))
        if not strategy.get("core_target_val"):
            do.append("保持仓位分散（单只不超过30-40%）")
        dont += ["忽视止损纪律", "过度集中于单一板块"]

    if vix_val > 22:
        dont.append(f"满仓操作（VIX={vix_val:.1f}偏高）")

    if macro_warning:
        watch.append(f"宏观事件：{macro_warning[:80]}")
    if vix_val > 25:
        watch.append(f"VIX={vix_val:.1f}（高波动），扩大止损缓冲")
    for a in position_advice.get("advices", []):
        if a["action"] == "HOLD" and a.get("stop_room_pct") is not None and a["stop_room_pct"] < 5:
            watch.append(
                f"{a['symbol']} 距止损位仅{a['stop_room_pct']:.1f}%（${a['stop']:.2f}），逼近离场线"
            )
    if sell_tickers:
        watch.append(f"规避标的：{'、'.join(sell_tickers[:3])}")
    if buy_count > 0:
        watch.append("入场后首要任务：确认止损单已设置")

    if buy_count > 0:
        tomorrow.append("追踪今日入场信号的价格走势")
    if exits:
        tomorrow.append(f"确认 {'、'.join(a['symbol'] for a in exits)} 的卖出已执行")
    if extreme_rsi_tickers:
        tomorrow.append(f"极端RSI标的：{'、'.join(extreme_rsi_tickers[:4])}（关注反转）")
    if vix_val > 20:
        tomorrow.append("VIX走向")
    tomorrow.append("SPY/QQQ 日线支撑/压力位")

    return {"do": do, "dont": dont, "watch": watch, "tomorrow": tomorrow}


def _tldr(*, recommendation, vix_val, regime, buy_count, position_advice,
          strategy=None) -> dict:
    """Three lines for a morning scan. Exit advice always wins the headline."""
    exits = position_advice.get("exits") or []
    trims = position_advice.get("trims") or []
    strategy = strategy or {}

    # An unfunded core outranks everything except an actual sell order. Buying
    # the index is the decision that determines this account's return; a
    # watchlist entry is not.
    if not exits and not strategy.get("core_funded") and strategy.get("core_target_val"):
        return {
            "headline": (
                f"🎯 首要任务：建立核心仓 "
                f"${strategy['core_target_val']:,.0f}"
                f"（{'/'.join(strategy.get('core_symbols', []))}）"
            ),
            "top_action": strategy.get("next_action", ""),
            "risk_line": (
                f"🟢 核心仓不设止损、不因信号卖出；卫星仓"
                f"{'纸面验证中，暂不投入真钱' if strategy.get('sat_paper_only') else '每笔风险1%'}"
                f"（VIX={vix_val:.1f}）"
            ),
        }

    if exits:
        headline = f"🔴 今日有 {len(exits)} 个持仓需要卖出"
        top_action = (
            f"优先执行卖出：{'、'.join(a['symbol'] for a in exits)}"
            "（见「持仓操作」区块的触发原因）"
        )
    elif trims:
        headline = f"🟠 今日有 {len(trims)} 个持仓建议减仓"
        top_action = f"减仓：{'、'.join(a['symbol'] for a in trims)}，降低集中度/事件风险"
    elif recommendation == "AVOID":
        headline = "❌ 今日卫星仓暂停"
        top_action = "不开新个股仓，核心仓不动，等待风险事件落地"
    elif recommendation in ("REDUCE_EXPOSURE", "DEFENSIVE"):
        headline = "⚠️ 今日建议降低风险敞口"
        top_action = "卫星仓降风险、检查止损位；核心仓不动"
    elif buy_count > 0:
        headline = f"✅ 今日有 {buy_count} 个入场机会"
        top_action = "查看「入场机会」，先设止损，按每笔1%账户风险定股数"
    else:
        headline = "⏸️ 今日无新信号，保持现状"
        top_action = "无需操作，可整理 Watchlist 或检查已有持仓止损位"

    if vix_val > 30:
        risk_line = f"🔴 高风险（VIX={vix_val:.1f}，卫星仓暂停）"
    elif vix_val > 22:
        risk_line = f"🟡 中等风险（VIX={vix_val:.1f}，谨慎操作）"
    else:
        risk_line = f"🟢 正常风险（VIX={vix_val:.1f}，{REGIME_ZH.get(regime, regime)}）"

    return {"headline": headline, "top_action": top_action, "risk_line": risk_line}


def _sector_table(sector_detail: list) -> list:
    """Sector rows with a status label. WeChat had this table; HTML only had
    the AI text cards, so the numbers behind them were invisible there."""
    rows = []
    for sec in (sector_detail or [])[:10]:
        rsi = _num(sec.get("rsi"), 50)
        score = _num(sec.get("strength_score"), 0)
        if rsi < 30:
            status = "🔵 超卖"
        elif rsi > 70:
            status = "🔴 超买"
        elif score > 3:
            status = "🟢 强势"
        elif score < -3:
            status = "🔻 弱势"
        else:
            status = "🟡 中性"
        name = sec.get("name", "")
        rows.append({
            "name":        name,
            "name_zh":     SECTOR_ZH.get(name, name),
            "day_return":  _num(sec.get("day_return"), 0),
            "week_return": _num(sec.get("week_return"), 0),
            "rsi":         rsi,
            "score":       score,
            "status":      status,
        })
    return rows


def _selection_criteria(total_symbols: int, buy_count: int) -> dict:
    from config.config import Config
    return {
        "total_symbols": total_symbols,
        "buy_count": buy_count,
        "rules": [
            f"股价距52周低点 ≤{Config.NEAR_52W_LOW_THRESHOLD:.0%}（价值区间）或新近上穿SMA50（趋势区间）",
            f"盈亏比 R/R ≥ {Config.MIN_RISK_REWARD_RATIO}",
            "RSI < 60（未超买）且企稳证据 ≥2/4",
            f"非5日冷却期、非止损禁买期（{Config.STOPOUT_REBUY_BAN_DAYS}个交易日）",
            f"非价值陷阱，且{Config.EARNINGS_ENTRY_BUFFER_DAYS}日内无财报",
            f"置信度 ≥{Config.MIN_BUY_CONFIDENCE}（AI外部因子复核后）",
        ],
        "empty_note": "今日无标的同时满足以上所有条件，建议等待。" if buy_count == 0 else "",
    }


def _num(v, default):
    """Coerce to a real number, treating None/NaN as the default."""
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default
