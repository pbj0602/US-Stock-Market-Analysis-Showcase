"""
Telegram push — the only notification channel (replaced WeChat + email on
2026-09-23).

The daily push is a one-screen summary; the full HTML report rides along as
a document. Facts come from the shared report model (modules/report_model.py)
and the data-health ledger, never recomputed here.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import html
import logging
import os
import re

import requests

from modules import data_health
from modules.report_validator import AREA_MARKET, AREA_POSITIONS

logger = logging.getLogger(__name__)

_API = 'https://api.telegram.org/bot{token}/{method}'
MAX_LEN = 4096          # Telegram's hard limit per message
CAPTION_MAX = 1024


def _creds():
    return os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_CHAT_ID', '')


def _call(method: str, *, data: dict, files: dict | None = None) -> bool:
    token, chat_id = _creds()
    if not token or not chat_id:
        logger.warning('[Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping')
        return False
    try:
        r = requests.post(_API.format(token=token, method=method),
                          data={'chat_id': chat_id, **data}, files=files, timeout=30)
        body = r.json()
        if body.get('ok'):
            return True
        logger.error(f"[Telegram] {method} failed: {body.get('description')}")
    except Exception as e:
        logger.error(f'[Telegram] {method} error: {e}')
    return False


def send_message(text: str, parse_mode: str = 'HTML') -> bool:
    """Send text, splitting on line boundaries if it exceeds Telegram's limit."""
    ok = True
    for chunk in _split(text):
        ok &= _call('sendMessage', data={
            'text': chunk, 'parse_mode': parse_mode, 'disable_web_page_preview': True,
        })
    return ok


def send_document(path: str, caption: str = '') -> bool:
    if not path or not os.path.exists(path):
        logger.warning(f'[Telegram] document not found: {path}')
        return False
    with open(path, 'rb') as f:
        return _call('sendDocument', data={'caption': caption[:CAPTION_MAX], 'parse_mode': 'HTML'},
                     files={'document': (os.path.basename(path), f)})


def _split(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    chunks, cur = [], ''
    for line in text.split('\n'):
        while len(line) > MAX_LEN:           # a single giant line
            chunks.append(line[:MAX_LEN])
            line = line[MAX_LEN:]
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ''
        cur = f'{cur}\n{line}' if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def markdown_to_html(md: str) -> str:
    """Enough Markdown→Telegram-HTML for the post-market report."""
    out = []
    for line in md.split('\n'):
        line = html.escape(line, quote=False)
        if re.match(r'^\s*\|?\s*:?-{3,}', line):      # table separator row
            continue
        line = re.sub(r'^#{1,6}\s*(.+)$', r'<b>\1</b>', line)
        line = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', line)
        line = re.sub(r'(?<![\w/])_(.+?)_(?![\w/])', r'<i>\1</i>', line)
        line = re.sub(r'^\s*[-*]\s+', '• ', line)
        if line.strip().startswith('|'):
            line = ' · '.join(c.strip() for c in line.strip().strip('|').split('|'))
        out.append(line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(out)).strip()


# ── Daily summary ────────────────────────────────────────────────────────────

def _e(x) -> str:
    return html.escape(str(x), quote=False)


def _money(v) -> str:
    return f'${v:,.0f}' if abs(v) >= 100 else f'${v:,.2f}'


def build_daily_summary(report_data: dict) -> str:
    m = report_data.get('model') or {}
    pa = report_data.get('position_advice') or {}
    strategy = m.get('strategy') or {}
    date = report_data.get('date', '')
    L = []

    # 1. One status line
    vix = report_data.get('vix')
    vix_s = f'VIX {vix:.1f}' if isinstance(vix, (int, float)) else 'VIX —'
    if data_health.has_critical(AREA_MARKET):
        state = '⚠️ 市场状态数据异常，今日不可信'
    else:
        state = f"{m.get('regime_emoji', '')} {m.get('regime_zh', '')}"
    L.append(f'<b>📈 美股日报 {_e(date[5:])}</b>  {state} · {vix_s}')
    spy = (report_data.get('index_timing') or {}).get('SPY') or {}
    spy_px = next((s.get('price') for s in report_data.get('all_signals', []) if s.get('ticker') == 'SPY'), None)
    if spy_px:
        dd = spy.get('drawdown_pct')
        L.append(f'SPY ${spy_px:,.2f}' + (f'（距52周高点 -{dd:.1f}%）' if dd is not None else ''))

    # 2. What to do today (real money)
    L.append('')
    L.append('<b>🚨 今天要做</b>')
    if data_health.has_critical(AREA_POSITIONS):
        L.append('• ⚠️ 持仓数据异常：今天不要按系统指令卖出，请手动核对')
    else:
        todo = [f"🔴 卖出 {_e(a['symbol'])}：{_e(a['reason_text'])}" for a in pa.get('exits') or []]
        todo += [f"🟠 减仓 {_e(a['symbol'])}：{_e(a['reason_text'])}" for a in pa.get('trims') or []]
        if strategy.get('core_target_val') and not strategy.get('core_funded'):
            todo.append(f"🎯 把 {_money(strategy['core_target_val'])} 买入核心仓"
                        f"（{'/'.join(strategy.get('core_symbols', []))}），一次买满")
        L += [f'• {t}' for t in todo[:3]] or ['• 无需操作']

    # 3. Signals
    wl = [s for s in report_data.get('all_signals', [])
          if s.get('ticker') not in {h['ticker'] for h in report_data.get('holdings', [])}]
    buys = [s for s in wl if s.get('signal') == 'BUY']
    sells = [s for s in wl if s.get('signal') == 'SELL']
    L.append('')
    L.append(f'<b>📡 信号</b>  入场 {len(buys)} · 规避 {len(sells)}')
    paper = ' [仅纸面]' if strategy.get('sat_paper_only') else ''
    for s in buys[:3]:
        if data_health.has_critical(f"信号 {s['ticker']}"):
            L.append(f"• ⚠️ {_e(s['ticker'])} 数据异常，勿用")
            continue
        tgt = f" 目标${s['target_price']:.2f}" if s.get('target_price') else ' 移动止盈'
        L.append(f"• 🟢 {_e(s['ticker'])} ${s['price']:.2f} 止损${s['stop_loss']:.2f}{tgt}{paper}")
    for s in sells[:3]:
        reason = re.sub(r'^[^:：]*[:：]\s*', '', s.get('reason') or '')[:40]
        L.append(f"• 🔴 {_e(s['ticker'])}：{_e(reason)}")
    if not buys and not sells:
        L.append('• 今日无信号')

    # 4. AI read — three labelled lines at most
    summary = report_data.get('ai_summary') or ''
    ai_lines = [ln.strip() for ln in summary.split('\n') if ln.strip().startswith('【')][:3]
    if ai_lines:
        L.append('')
        L.append('<b>🧭 解读</b>')
        L += [_e(ln) for ln in ai_lines]

    # 5. Accounts
    acct = m.get('account') or {}
    q = report_data.get('quant_summary') or {}
    L.append('')
    L.append('<b>💼 账户</b>')
    if acct.get('known'):
        pos = '空仓' if acct.get('flat') else f"持仓 {_money(acct['invested'])}"
        L.append(f"真实：{pos} · 现金 {_money(acct['cash'])} · 累计 {'+' if acct['pnl'] >= 0 else '-'}${abs(acct['pnl']):,.2f}（{acct['pnl_pct']:+.2f}%）")
    if 'nav' in q:
        sat = ((q.get('sleeves') or {}).get('satellite')) or {}
        L.append(f"追踪：{_money(q['nav'])}（{q['total_return_pct']:+.2f}%）· 相对SPY {q['alpha_pct']:+.2f}%")
        if sat:
            L.append(f"卫星仓：{sat.get('open', 0)}/{sat.get('max_positions', 2)} 持仓 · "
                     f"验证 {sat.get('trades_closed', 0)}/"
                     f"{sat.get('trades_closed', 0) + sat.get('trades_to_prove', 20)} 笔")

    # 6. Tomorrow
    tomorrow = [t for t in (m.get('checklist') or {}).get('tomorrow', []) if 'SPY/QQQ 日线' not in t][:2]
    if tomorrow:
        L.append('')
        L.append('<b>👀 明日关注</b>')
        L += [f'• {_e(t)}' for t in tomorrow]

    # 7. Health line
    L.append('')
    L.append(health_line())
    return '\n'.join(L)


def health_line() -> str:
    h = data_health.summary()
    if h['status'] == 'ok':
        return '✅ 数据健康：全部通过'
    flagged = h['critical'] + h['warnings']
    areas = '、'.join(dict.fromkeys(i['area'] for i in flagged))
    icon = '🚨' if h['status'] == 'critical' else '⚠️'
    return f'{icon} 数据健康：{len(flagged)} 项异常（{_e(areas)}）'


def build_health_alert() -> str | None:
    """Separate alert when anything critical fired; None when there's nothing to say."""
    h = data_health.summary()
    if not h['critical']:
        return None
    L = [f"<b>🚨 数据复核：{len(h['critical'])} 个严重问题</b>",
         '相关结论已在日报中屏蔽，请勿据此操作：']
    L += [f"• {_e(i['area'])}：{_e(i['message'])}" for i in h['critical']]
    if h['warnings']:
        L.append(f"另有 {len(h['warnings'])} 个一般问题，见完整报告底部。")
    return '\n'.join(L)


def notify_daily(report_data: dict, report_path: str) -> bool:
    ok = send_message(build_daily_summary(report_data))
    alert = build_health_alert()
    if alert:
        ok &= send_message(alert)
    ok &= send_document(report_path, caption='📄 完整报告（点开即可查看，表格可左右滑动）')
    logger.info(f"[Telegram] daily push {'OK' if ok else 'FAILED'}")
    return ok
