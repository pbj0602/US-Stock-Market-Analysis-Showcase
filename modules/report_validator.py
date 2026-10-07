"""
Pre-publish cross-checks for the daily report.

Every rule here encodes a mistake a real report shipped with (2026-09-23):
a "BEAR 100%" regime with SPY 0.8% off its high, holding warnings with a
$0.00 stop while the account was flat, a sector "+2.89% today" that was the
prior session, AI text telling the reader to hold 20–30% against a 75% core
rule. Checks record into data_health; they never patch numbers themselves.

    check_market(...)      regime / VIX sanity
    check_positions(...)   exit/trim instructions and stops
    check_signals(...)     BUY stop/target geometry
    check_sectors(...)     sector ETF move vs its own constituents
    check_satellite(...)   scorecard shown without anything behind it
    sanitize_ai(...)       strip sizing advice, check quoted numbers
"""

import math
import re

from modules import data_health
from modules.data_health import CRITICAL, WARNING

# Areas the report keys its suppression on.
AREA_MARKET = '市场状态'
AREA_POSITIONS = '持仓操作'
AREA_SECTORS = '板块'
AREA_AI = 'AI解读'


def _bad(x) -> bool:
    try:
        return x is None or math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def check_market(regime: dict, vix: dict) -> None:
    label = regime.get('regime')
    spy = regime.get('spy_price')
    p50, p200 = regime.get('price_vs_sma50'), regime.get('price_vs_sma200')

    if _bad(spy) or float(spy) <= 0 or _bad(p50) or _bad(p200):
        data_health.record(CRITICAL, AREA_MARKET, 'SPY 数据缺失，牛熊判断是默认值，不可信')
    elif label == 'BEAR' and p50 > 0 and p200 > 0:
        data_health.record(
            CRITICAL, AREA_MARKET,
            f'判为熊市，但 SPY 在 SMA50 上方 {p50:+.1f}%、SMA200 上方 {p200:+.1f}%',
        )
    elif label == 'BULL' and p50 < 0 and p200 < 0:
        data_health.record(
            CRITICAL, AREA_MARKET,
            f'判为牛市，但 SPY 在 SMA50 下方 {p50:+.1f}%、SMA200 下方 {p200:+.1f}%',
        )

    v = vix.get('current')
    if _bad(v) or not (5 < float(v) < 90):
        data_health.record(CRITICAL, AREA_MARKET, f'VIX 数值异常：{v}')


def check_positions(holdings: list, position_advice: dict) -> None:
    acts = (position_advice.get('exits') or []) + (position_advice.get('trims') or [])
    held = {h.get('ticker') for h in holdings or []}
    for a in acts:
        if a.get('symbol') not in held:
            data_health.record(
                CRITICAL, AREA_POSITIONS,
                f"对 {a.get('symbol')} 发出{a.get('action')}指令，但它不在真实持仓里",
            )
    for a in position_advice.get('advices') or []:
        stop = a.get('stop')
        if stop is not None and (_bad(stop) or float(stop) <= 0):
            data_health.record(CRITICAL, AREA_POSITIONS, f"{a.get('symbol')} 止损价无效：{stop}")


def check_signals(all_signals: list) -> None:
    for s in all_signals or []:
        if s.get('signal') != 'BUY':
            continue
        t, price, stop, target = s.get('ticker'), s.get('price'), s.get('stop_loss'), s.get('target_price')
        if _bad(price) or _bad(stop) or not (0 < float(stop) < float(price)):
            data_health.record(CRITICAL, f'信号 {t}', f'买入信号止损价无效（价格{price}，止损{stop}）')
        elif target is not None and not _bad(target) and float(target) <= float(price):
            data_health.record(WARNING, f'信号 {t}', f'目标价 {target} 不高于现价 {price}')


def check_sectors(sector_detail: list, prices: list) -> None:
    moves = {p.get('ticker'): p.get('change_pct') for p in prices or []}
    rsis = []
    for sec in sector_detail or []:
        rsis.append(round(sec.get('rsi') or 0))
        members = [moves[m] for m in sec.get('mapped_stocks') or []
                   if m in moves and not _bad(moves[m])]
        if len(members) < 3 or _bad(sec.get('day_return')):
            continue
        avg = sum(members) / len(members)
        etf = sec['day_return']
        if etf * avg < 0 and abs(etf - avg) > 2.0:
            data_health.record(
                WARNING, AREA_SECTORS,
                f"{sec.get('etf')} 涨跌 {etf:+.2f}% 与成分股均值 {avg:+.2f}% 方向相反",
            )
    for r in set(rsis):
        if rsis.count(r) >= 4:
            data_health.record(WARNING, AREA_SECTORS, f'{rsis.count(r)} 个板块 RSI 都是 {r}，疑似默认值')


def check_satellite(sleeves: dict | None) -> None:
    sat = (sleeves or {}).get('satellite') or {}
    if (sat.get('alpha_vs_spy_pct') is not None
            and not sat.get('trades_closed') and not sat.get('open')):
        data_health.record(WARNING, '卫星仓', '无任何交易却显示超额收益')


# Sizing/liquidation advice the rules forbid the model from giving.
_FORBIDDEN = re.compile(
    r'仓位[^。；\n]{0,12}\d+\s*%|\d+\s*%\s*(?:至|到|-|–)\s*\d+\s*%仓位|空仓|清仓|减仓\s*\d+\s*%|满仓'
)
_SENTENCE = re.compile(r'[^。！？；]+[。！？；]?')
_VIX_QUOTE = re.compile(r'VIX\s*[=＝:：]\s*(\d+(?:\.\d+)?)')
_SPY_QUOTE = re.compile(r'SPY\s*现价\s*\$?\s*(\d+(?:\.\d+)?)')

AI_TEXT_KEYS = ('executive_summary', 'market_overview', 'stock_recommendations',
                'risk_assessment', 'portfolio_analysis')


def sanitize_ai(ai_analysis: dict, vix_now, spy_now) -> None:
    """Mutates ai_analysis: drops forbidden sentences, flags wrong numbers."""
    if not isinstance(ai_analysis, dict):
        return
    for w in ai_analysis.get('_warnings') or []:
        data_health.record(WARNING, AREA_AI, w)

    for key in AI_TEXT_KEYS:
        text = ai_analysis.get(key)
        if not isinstance(text, str) or not text:
            continue
        lines, dropped = [], 0
        for line in text.split('\n'):
            sents = _SENTENCE.findall(line)
            kept = [x for x in sents if not _FORBIDDEN.search(x)]
            dropped += len(sents) - len(kept)
            if ''.join(kept).strip() or not line.strip():
                lines.append(''.join(kept))
        if dropped:
            ai_analysis[key] = '\n'.join(lines).strip()
            data_health.record(WARNING, AREA_AI, f'{key} 删除了 {dropped} 句违反仓位规则的建议')

        text = ai_analysis[key]
        if not _bad(vix_now):
            for q in _VIX_QUOTE.findall(text):
                if abs(float(q) - float(vix_now)) > 0.5:
                    data_health.record(WARNING, AREA_AI, f'{key} 引用 VIX={q}，实际 {float(vix_now):.1f}')
        if not _bad(spy_now) and float(spy_now) > 0:
            for q in _SPY_QUOTE.findall(text):
                if abs(float(q) / float(spy_now) - 1) > 0.01:
                    data_health.record(WARNING, AREA_AI, f'{key} 引用 SPY ${q}，实际 ${float(spy_now):.2f}')
