#!/usr/bin/env python3
"""
Post-Market Runner

Runs after 16:00 ET (schedule via launchd or cron at 16:30 ET).

For each signal generated this morning the runner:
  1. Batch-fetches actual closing prices from yfinance.
  2. Computes today's actual return vs signal direction (hit / miss).
  3. Fetches market summary (SPY/QQQ/IWM daily performance).
  4. Reads today's paper trades (if any) and computes impact.
  5. Calls AI (DeepSeek → Claude fallback) for a brief narrative.
  6. Checks held positions (real + satellite) against their stops at the close.
  7. Saves the full .md archive and pushes a one-screen Telegram summary.
"""

import csv
import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from typing import Optional

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

_DB_PATH = "data/signal_log.db"

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

@contextmanager
def _db():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trading calendar helpers
# ---------------------------------------------------------------------------

_US_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
}


def _is_us_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _US_HOLIDAYS_2026


def _last_us_trading_day(from_date: date) -> date:
    d = from_date
    for _ in range(10):
        if _is_us_trading_day(d):
            return d
        d -= timedelta(days=1)
    return from_date


# ---------------------------------------------------------------------------
# Batch price fetch (replaces individual per-ticker calls)
# ---------------------------------------------------------------------------

def _batch_fetch_prices(tickers: list, as_of: date) -> dict:
    """
    Batch-download open+close for all tickers in one yfinance call.
    Returns {ticker: {'open': float, 'close': float, 'prev_close': float|None}}.
    """
    import yfinance as yf

    start = str(as_of - timedelta(days=7))
    end = str(as_of + timedelta(days=1))

    try:
        df = yf.download(
            tickers, start=start, end=end,
            progress=False, auto_adjust=True,
        )
    except Exception as e:
        logger.warning(f"Batch price fetch failed: {e}")
        return {}

    if df is None or df.empty:
        return {}

    import pandas as pd
    is_multi = len(tickers) > 1

    # Find the row for as_of, or fall back to last available row
    day_mask = df.index.date == as_of
    target_pos = int(day_mask.nonzero()[0][0]) if day_mask.any() else len(df) - 1

    result = {}
    for ticker in tickers:
        try:
            closes = df['Close'][ticker] if is_multi else df['Close']
            opens = df['Open'][ticker] if is_multi else df['Open']
            close = float(closes.iloc[target_pos])
            open_ = float(opens.iloc[target_pos])
            prev = closes.iloc[:target_pos].dropna()
            prev_close = float(prev.iloc[-1]) if len(prev) else None

            if not (pd.isna(close) or pd.isna(open_)):
                result[ticker] = {
                    'open': round(open_, 2),
                    'close': round(close, 2),
                    'prev_close': round(prev_close, 2) if prev_close else None,
                }
        except Exception:
            pass

    # yfinance's batch endpoint sometimes drops a finished session for some
    # tickers (9/22: ABBV/DIS/XLK all NaN). get_daily() rebuilds it from
    # hourly bars — slower, so only for the ones that came back empty.
    for ticker in tickers:
        if ticker not in result:
            p = _repaired_bar(ticker, as_of)
            if p:
                result[ticker] = p

    return result


def _repaired_bar(ticker: str, as_of: date) -> Optional[dict]:
    try:
        from modules.price_history import get_daily
        hist = get_daily(ticker, period='1mo')
        if hist.empty:
            return None
        dates = list(hist.index.date)
        if as_of not in dates:
            return None
        i = dates.index(as_of)
        row = hist.iloc[i]
        return {
            'open': round(float(row['Open']), 2),
            'close': round(float(row['Close']), 2),
            'prev_close': round(float(hist['Close'].iloc[i - 1]), 2) if i > 0 else None,
        }
    except Exception as e:
        logger.warning(f"Repair fetch failed for {ticker}: {e}")
        return None


def _fetch_market_summary(as_of: date) -> dict:
    """Fetch daily performance for SPY / QQQ / IWM."""
    indices = ['SPY', 'QQQ', 'IWM']
    prices = _batch_fetch_prices(indices, as_of)
    summary = {}
    for idx in indices:
        p = prices.get(idx)
        # Day change vs prior close — the number every quote screen shows.
        base = p and (p.get('prev_close') or p['open'])
        if base:
            ret = (p['close'] - base) / base * 100
            summary[idx] = {'close': p['close'], 'day_return_pct': round(ret, 2)}
    return summary


# ---------------------------------------------------------------------------
# Signal retrieval
# ---------------------------------------------------------------------------

def get_todays_signals(today: date) -> list:
    if not os.path.exists(_DB_PATH):
        logger.warning("signal_log.db not found — no signals to review.")
        return []

    with _db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, signal, score, component_scores, vix_at_signal, timestamp
               FROM signal_log
               WHERE DATE(timestamp) = ?
               ORDER BY score DESC""",
            (str(today),),
        ).fetchall()

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Hit-rate evaluation
# ---------------------------------------------------------------------------

def evaluate_signals(signals: list, prices: dict) -> list:
    """Attach actual prices and compute hit/miss using pre-fetched batch prices."""
    results = []
    for s in signals:
        ticker = s['ticker']
        signal = s['signal']
        p = prices.get(ticker)

        result = dict(s)
        result['open_price'] = p['open'] if p else None
        result['close'] = p['close'] if p else None

        if p and p['open'] > 0:
            ret_pct = (p['close'] - p['open']) / p['open'] * 100
            result['day_return_pct'] = round(ret_pct, 2)
            if signal == 'BUY':
                result['hit'] = ret_pct > 0
            elif signal == 'SELL':
                result['hit'] = ret_pct < 0
            else:
                result['hit'] = None
        else:
            result['day_return_pct'] = None
            result['hit'] = None

        results.append(result)

    return results


def compute_hit_rate(evaluated: list) -> dict:
    directional = [r for r in evaluated if r['hit'] is not None]
    hits = [r for r in directional if r['hit']]
    buy_signals = [r for r in directional if r['signal'] == 'BUY']
    buy_hits = [r for r in buy_signals if r['hit']]

    return {
        'total': len(evaluated),
        'directional': len(directional),
        'hits': len(hits),
        'hit_rate_pct': round(len(hits) / len(directional) * 100, 1) if directional else None,
        'buy_total': len(buy_signals),
        'buy_hits': len(buy_hits),
        'buy_hit_rate_pct': round(len(buy_hits) / len(buy_signals) * 100, 1) if buy_signals else None,
        'avg_return_pct': round(
            sum(r['day_return_pct'] for r in directional if r['day_return_pct'] is not None)
            / len(directional), 2
        ) if directional else None,
    }


# ---------------------------------------------------------------------------
# Today's paper trades
# ---------------------------------------------------------------------------

def get_todays_trades(as_of: date) -> list:
    """Read paper_trades.csv and return rows where timestamp matches as_of (Beijing date)."""
    csv_path = os.path.join(Config.DATA_DIR, 'paper_trades.csv')
    if not os.path.exists(csv_path):
        return []

    trades = []
    with open(csv_path, newline='') as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row['timestamp'])
                if ts.date() == as_of:
                    trades.append(row)
            except (ValueError, KeyError):
                continue
    return trades


# ---------------------------------------------------------------------------
# Position check — real holdings + satellite sleeve vs their stops at the close
# ---------------------------------------------------------------------------
# Read-only: stops are owned by the morning run (PositionAdvisor ratchets
# holding_stops.json, AdviceTracker ratchets advice_account.json). Re-running
# either here would move stops on post-close data and double-count.

_NEAR_STOP_PCT = 3.0
_ADVICE_STATE = os.path.join('data', 'advice_account.json')
_HOLDING_STOPS = os.path.join('data', 'holding_stops.json')
_PORTFOLIO_CSV = os.path.join('data', 'portfolio.csv')


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_held_positions() -> list:
    """[{symbol, book: '真实'|'卫星'|'核心', stop, shares}] for everything held."""
    held = []
    stops = _load_json(_HOLDING_STOPS)
    if os.path.exists(_PORTFOLIO_CSV):
        with open(_PORTFOLIO_CSV, newline='') as f:
            for row in csv.DictReader(f):
                sym = (row.get('Symbol') or '').strip()
                if sym:
                    held.append({'symbol': sym, 'book': '真实',
                                 'stop': (stops.get(sym) or {}).get('stop'),
                                 'shares': float(row.get('Shares') or 0)})
    for sym, pos in (_load_json(_ADVICE_STATE).get('positions') or {}).items():
        book = '卫星' if pos.get('sleeve') == 'satellite' else '核心'
        held.append({'symbol': sym, 'book': book, 'stop': pos.get('stop'),
                     'shares': float(pos.get('shares') or 0)})
    return held


def check_positions(held: list, prices: dict) -> list:
    """Stop distance at the close for every position that has a stop."""
    out = []
    for h in held:
        if h['book'] == '核心':      # index core is never sold — no stop to watch
            continue
        p = prices.get(h['symbol'])
        close, stop = (p or {}).get('close'), h.get('stop')
        row = {**h, 'close': close, 'dist_pct': None}
        if close is None:
            row['status'] = 'no_price'
        elif not stop:
            row['status'] = 'no_stop'
        else:
            row['dist_pct'] = round((close - stop) / close * 100, 1)
            row['status'] = ('breached' if close <= stop
                             else 'near' if row['dist_pct'] < _NEAR_STOP_PCT else 'ok')
        out.append(row)
    return out


def tracker_day_change(prices: dict) -> dict:
    """Advice-tracking account NAV at today's close vs the prior close."""
    state = _load_json(_ADVICE_STATE)
    positions = state.get('positions') or {}
    if not positions:
        return {}
    cash = float(state.get('cash') or 0)
    nav, prev = cash, cash
    for sym, pos in positions.items():
        p = prices.get(sym)
        if not p or not p.get('prev_close'):
            return {}                 # partial NAV would be misleading
        shares = float(pos.get('shares') or 0)
        nav += shares * p['close']
        prev += shares * p['prev_close']
    initial = float(state.get('initial_capital') or Config.TOTAL_ACCOUNT_VALUE)
    return {
        'nav': round(nav, 2),
        'day_pct': round((nav - prev) / prev * 100, 2) if prev else None,
        'total_pct': round((nav - initial) / initial * 100, 2) if initial else None,
    }


# ---------------------------------------------------------------------------
# Tomorrow's watch list
# ---------------------------------------------------------------------------

def build_watch_list(evaluated: list, today: date) -> dict:
    still_valid = []
    for r in evaluated:
        if r['signal'] != 'BUY':
            continue
        ret = r.get('day_return_pct')
        if ret is None:
            note = '收盘价获取失败，请手动核对'
        elif ret <= -3.0:
            continue
        elif ret < 0:
            note = '小幅回落未破位，明日继续观察'
        else:
            note = '收盘维持强势'
        still_valid.append({
            'ticker': r['ticker'],
            'close': r['close'],
            'day_return_pct': ret,
            'score': r['score'],
            'note': note,
        })

    earnings_tmrw = []
    try:
        from modules.event_risk_engine import EventRiskEngine
        tickers = list({r['ticker'] for r in evaluated})
        ere = EventRiskEngine()
        ea = ere.get_full_event_analysis(tickers)
        for sym, data in ea.get('stocks', {}).items():
            earn = data.get('earnings', {})
            days = earn.get('days_until')
            if days == 1:
                earnings_tmrw.append({
                    'ticker': sym,
                    'date': earn.get('next_date', ''),
                    'note': '明日财报，持有者注意仓位，新仓等财报后再进',
                })
    except Exception as e:
        logger.warning(f"Earnings T+1 lookup failed: {e}")

    return {
        'still_valid': still_valid[:5],
        'earnings_tmrw': earnings_tmrw,
    }


# ---------------------------------------------------------------------------
# AI narrative (DeepSeek → Claude fallback)
# ---------------------------------------------------------------------------

def _build_narrative_prompt(
    as_of: date,
    market_summary: dict,
    hit_rate: dict,
    evaluated: list,
    trades_today: list,
) -> str:
    lines = [f"今天是 {as_of}，美股收盘后复盘。请用中文写一句话点评（不超过60字）。\n"]

    # Market
    if market_summary:
        lines.append("【大盘表现】")
        for idx, d in market_summary.items():
            sign = '+' if d['day_return_pct'] >= 0 else ''
            lines.append(f"  {idx}: 收盘 ${d['close']:.2f}，今日 {sign}{d['day_return_pct']}%")
        lines.append("")

    # Signal performance
    buy_results = [r for r in evaluated if r['signal'] == 'BUY' and r.get('day_return_pct') is not None]
    if buy_results:
        lines.append("【今日BUY信号表现】")
        for r in buy_results:
            hit_str = "命中" if r['hit'] else "未中"
            lines.append(f"  {r['ticker']}: 开盘${r['open_price']:.2f} → 收盘${r['close']:.2f}，{r['day_return_pct']:+.2f}%（{hit_str}）")
        lines.append("")

    # Trades
    if trades_today:
        lines.append("【今日操作】")
        for t in trades_today:
            lines.append(f"  {t['action']} {t['ticker']} {t['shares']}股 @ ${float(t['fill_price']):.2f}")
        lines.append("")

    lines.append(
        "只写一句：今天行情的主要特征，以及信号涨跌背后的原因。"
        "不要复述上面的价格和百分比，不要标题、不要分段、不要markdown。"
    )
    from modules.ai_analyzer import STRATEGY_GUARDRAIL
    lines.append(STRATEGY_GUARDRAIL)
    return '\n'.join(lines)


def generate_ai_narrative(
    as_of: date,
    market_summary: dict,
    hit_rate: dict,
    evaluated: list,
    trades_today: list,
) -> str:
    prompt = _build_narrative_prompt(as_of, market_summary, hit_rate, evaluated, trades_today)

    # Try DeepSeek first (primary provider)
    if Config.DEEPSEEK_API_KEY:
        try:
            import httpx
            resp = httpx.post(
                f"{Config.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions",
                headers={
                    'Authorization': f'Bearer {Config.DEEPSEEK_API_KEY}',
                    'Content-Type': 'application/json',
                },
                json={
                    'model': Config.DEEPSEEK_MODEL_FAST,
                    'messages': [{"role": "user", "content": prompt}],
                    'max_tokens': 200,
                },
                timeout=120,
            )
            resp.raise_for_status()
            text = resp.json()['choices'][0]['message']['content'].strip()
            logger.info("AI narrative generated via DeepSeek")
            return text
        except Exception as e:
            logger.warning(f"DeepSeek narrative failed ({e}), trying Claude...")

    # Fallback to Claude
    if Config.ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model=Config.CLAUDE_MODEL_FAST,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            logger.info("AI narrative generated via Claude")
            return msg.content[0].text.strip()
        except Exception as e:
            logger.warning(f"Claude narrative failed: {e}")

    return ""


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------
# Two outputs from the same facts: the Telegram push is one screen; the .md
# archive keeps the weekly-grade detail (bucket tables, still-valid list).
# Same-day hit rates are not shown anywhere — with 1-3 signals a day and a
# 1-3 week holding horizon they are noise; T+5/T+20 is the scoreboard.

_POS_STATUS = {
    'breached': '🔴 已跌破止损，明早按计划卖出',
    'near':     '🟠 接近止损',
    'ok':       '正常',
    'no_price': '⚠️ 收盘价获取失败，请手动核对',
    'no_stop':  '⚠️ 无止损记录',
}


def _index_line(market_summary: dict) -> str:
    return ' · '.join(f"{idx} {d['day_return_pct']:+.2f}%" for idx, d in market_summary.items())


def _position_lines(pos_check: list) -> list:
    flagged = [p for p in pos_check if p['status'] != 'ok']
    if not pos_check:
        return ['无持仓']
    if not flagged:
        return [f'{len(pos_check)} 个持仓全部正常（距止损均 ≥{_NEAR_STOP_PCT:.0f}%）']
    out = []
    for p in flagged:
        px = f"${p['close']:.2f}" if p['close'] is not None else ''
        dist = f" 止损${p['stop']:.2f}（距{p['dist_pct']:+.1f}%）" if p['dist_pct'] is not None else ''
        out.append(f"{p['book']} {p['symbol']} {px}{dist} {_POS_STATUS[p['status']]}")
    ok_n = len(pos_check) - len(flagged)
    if ok_n:
        out.append(f'其余 {ok_n} 个正常')
    return out


def _signal_rows(evaluated: list) -> list:
    """Directional signals only — HOLD rows have no verdict and were padding."""
    return [r for r in evaluated if r['signal'] in ('BUY', 'SELL')]


def _signal_line(r: dict) -> str:
    if r.get('day_return_pct') is None:
        return f"{r['ticker']} {r['signal']}：收盘价获取失败"
    return (f"{r['ticker']} {r['signal']} 开${r['open_price']:.2f} → 收${r['close']:.2f}"
            f"（{r['day_return_pct']:+.2f}%）")


def _scoreboard_line(s: dict) -> Optional[str]:
    if not s or not s.get('n_5d'):
        return None
    line = f"T+5 胜率 {s['win_rate_5d']:.0f}%（{s['n_5d']}笔，均{s['avg_return_5d']:+.1f}%）"
    if s.get('n_20d'):
        line += f" · T+20 {s['win_rate_20d']:.0f}%（{s['n_20d']}笔，均{s['avg_return_20d']:+.1f}%）"
    return line


def _ai_review_line(s: dict) -> Optional[str]:
    rev = (s or {}).get('ai_review') or {}
    avoided = rev.get('demoted_avg_return_5d')
    if not rev.get('n_demoted') or avoided is None:
        return None
    verdict = 'AI拦截过严' if avoided > 0 else 'AI拦截有效'
    return f"{verdict}：被拦 {rev['n_demoted']} 个，假想T+5均收益 {avoided:+.1f}%"


def _bucket_lines(s: dict) -> list:
    out = []
    for label, key in [('置信度', 'by_confidence'), ('路径', 'by_path'), ('格局', 'by_regime')]:
        rows = (s or {}).get(key) or []
        if rows:
            out.append(f'{label}：' + ' | '.join(
                f"{b['bucket']} {b['n']}笔 {b['win_rate']:.0f}% {b['avg_return']:+.1f}%" for b in rows))
    return out


def _trades_line(trades_today: list) -> Optional[str]:
    if not trades_today:
        return None
    return '模拟盘今日：' + '、'.join(
        f"{'买' if t['action'] == 'BUY' else '卖'} {t['ticker']} {t['shares']}股" for t in trades_today)


def format_report(
    today: date,
    market_summary: dict,
    watch_list: dict,
    evaluated: list,
    trades_today: list,
    ai_narrative: str,
    pos_check: list,
    tracker: dict,
    outcome_stats: Optional[dict] = None,
) -> str:
    """Full markdown archive (saved to disk, not pushed)."""
    L = [f'# 收盘复盘 · {today.strftime("%Y-%m-%d")}', '']
    if market_summary:
        L += [f'**大盘**：{_index_line(market_summary)}', '']

    L.append('## 持仓检查')
    L += [f'- {x}' for x in _position_lines(pos_check)]

    L += ['', '## 今日信号']
    rows = _signal_rows(evaluated)
    L += [f'- {_signal_line(r)}' for r in rows] or ['- 今日无买入/规避信号']

    s = outcome_stats or {}
    board = _scoreboard_line(s)
    if board:
        L += ['', '## 成绩单（近60日）', board]
        rev = _ai_review_line(s)
        if rev:
            L.append(rev)
        L += [''] + [f'- {x}' for x in _bucket_lines(s)]

    L += ['', '## 账户']
    if tracker:
        L.append(f"追踪账户：${tracker['nav']:,.2f}（今日 {tracker['day_pct']:+.2f}%，累计 {tracker['total_pct']:+.2f}%）")
    trades = _trades_line(trades_today)
    if trades:
        L.append(trades)

    if ai_narrative:
        L += ['', '## 点评', ai_narrative]

    L += ['', '## 明日']
    for e in watch_list['earnings_tmrw']:
        L.append(f"- ⚠ {e['ticker']}：{e['note']}")
    for v in watch_list['still_valid']:
        L.append(f"- {v['ticker']}：{v['note']}")
    if not watch_list['earnings_tmrw'] and not watch_list['still_valid']:
        L.append('- 暂无重点关注')
    return '\n'.join(L)


def build_telegram_summary(
    today: date,
    market_summary: dict,
    watch_list: dict,
    evaluated: list,
    trades_today: list,
    ai_narrative: str,
    pos_check: list,
    tracker: dict,
    outcome_stats: Optional[dict] = None,
) -> str:
    """One-screen push. Bucket tables only on Fridays (weekly review)."""
    from modules.telegram_notifier import _e
    L = [f'<b>📊 收盘复盘 {today.strftime("%m/%d")}</b>  {_e(_index_line(market_summary))}']

    L += ['', '<b>🚨 持仓检查</b>'] + [f'• {_e(x)}' for x in _position_lines(pos_check)]

    rows = _signal_rows(evaluated)
    L += ['', '<b>📡 今日信号</b>']
    L += [f'• {_e(_signal_line(r))}' for r in rows[:4]] or ['• 今日无买入/规避信号']

    s = outcome_stats or {}
    board = _scoreboard_line(s)
    if board:
        L += ['', '<b>📈 成绩单（近60日）</b>', _e(board)]
        rev = _ai_review_line(s)
        if rev:
            L.append(f'⚠️ {_e(rev)}' if '过严' in rev else _e(rev))
        if today.weekday() == 4:
            L += [_e(x) for x in _bucket_lines(s)]

    acct = []
    if tracker:
        acct.append(f"追踪：${tracker['nav']:,.2f}（今日 {tracker['day_pct']:+.2f}% · 累计 {tracker['total_pct']:+.2f}%）")
    trades = _trades_line(trades_today)
    if trades:
        acct.append(trades)
    if acct:
        L += ['', '<b>💼 账户</b>'] + [_e(x) for x in acct]

    if ai_narrative:
        L += ['', f'🧭 {_e(ai_narrative.strip())}']

    if watch_list['earnings_tmrw']:
        L += ['', '<b>👀 明日</b>']
        L += [f"• ⚠ {_e(e['ticker'])} 财报" for e in watch_list['earnings_tmrw']]
    return '\n'.join(L)


# ---------------------------------------------------------------------------
# Save & send
# ---------------------------------------------------------------------------

def save_report(content: str, today: date) -> str:
    os.makedirs(Config.REPORT_DIR, exist_ok=True)
    path = os.path.join(Config.REPORT_DIR, f'post_market_{today.strftime("%Y%m%d")}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.info(f"Post-market report saved: {path}")
    return path


def send_telegram(summary_html: str) -> bool:
    from modules.telegram_notifier import send_message
    return send_message(summary_html)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_post_market(trading_date: Optional[date] = None):
    """Review `trading_date`'s session (default: the latest settled US session).

    The wrapper launches at 16:45 ET, so the default is normally *today's*
    close. The old `today - 1` rule dated from a schedule that ran after
    local midnight and left every review one session behind.
    """
    from modules.trading_calendar import last_completed_session
    today = datetime.now().date()
    trading_date = trading_date or last_completed_session()
    logger.info(f"=== Post-Market Runner: runner_date={today}, trading_date={trading_date} ===")

    if not _is_us_trading_day(trading_date):
        logger.info(f"trading_date={trading_date} is not a US trading day — skipping.")
        return

    # 1. Load today's signals
    signals = get_todays_signals(trading_date)
    if not signals:
        logger.info(f"No signals found for trading_date={trading_date} — nothing to review.")
        return

    logger.info(f"Found {len(signals)} signals to review")

    # 2. Batch-fetch prices for signal tickers + everything held (one call)
    held = load_held_positions()
    tickers = sorted({s['ticker'] for s in signals} | {h['symbol'] for h in held})
    logger.info(f"Batch-fetching prices for {len(tickers)} tickers...")
    prices = _batch_fetch_prices(tickers, trading_date)
    logger.info(f"Got prices for {len(prices)}/{len(tickers)} tickers")

    # 3. Fetch market summary
    logger.info("Fetching market summary (SPY/QQQ/IWM)...")
    market_summary = _fetch_market_summary(trading_date)

    # 4. Evaluate signals
    evaluated = evaluate_signals(signals, prices)
    hit_rate = compute_hit_rate(evaluated)
    logger.info(
        f"Hit rate: {hit_rate['hit_rate_pct']}% "
        f"| BUY: {hit_rate['buy_hit_rate_pct']}% "
        f"| Avg return: {hit_rate['avg_return_pct']}%"
    )

    # 5. Positions vs stops at the close, tracking-account day change
    pos_check = check_positions(held, prices)
    tracker = tracker_day_change(prices)
    for p in pos_check:
        if p['status'] != 'ok':
            logger.warning(f"Position check {p['book']} {p['symbol']}: {p['status']} (dist {p['dist_pct']}%)")

    # 5b. Today's paper trades
    trades_today = get_todays_trades(trading_date)
    logger.info(f"Today's paper trades: {len(trades_today)}")

    # 6. Tomorrow's watch list
    watch_list = build_watch_list(evaluated, trading_date)

    # 6b. Close the feedback loop: fill realized T+5 / T+20 returns for past
    # BUY signals, then pull the rolling stats for the report. Before this,
    # 116 BUY signals had zero recorded outcomes — the system could not
    # measure itself.
    outcome_stats = {}
    try:
        from modules.signal_tracker import SignalTracker
        tracker_db = SignalTracker()
        tracker_db.update_outcomes()
        outcome_stats = tracker_db.get_outcome_stats(days=60)
    except Exception as e:
        logger.warning(f"Signal outcome update failed: {e}")

    # 7. AI narrative
    logger.info("Generating AI narrative...")
    ai_narrative = generate_ai_narrative(trading_date, market_summary, hit_rate, evaluated, trades_today)

    # 8. Format and save the full archive; push the one-screen summary
    parts = dict(
        today=trading_date, market_summary=market_summary, watch_list=watch_list,
        evaluated=evaluated, trades_today=trades_today, ai_narrative=ai_narrative,
        pos_check=pos_check, tracker=tracker, outcome_stats=outcome_stats,
    )
    save_report(format_report(**parts), trading_date)

    # 9. Push notification
    send_telegram(build_telegram_summary(**parts))

    logger.info("Post-market runner complete.")
    return {
        'hit_rate': hit_rate,
        'market_summary': market_summary,
        'watch_list': watch_list,
        'evaluated': evaluated,
        'trades_today': trades_today,
        'pos_check': pos_check,
    }


if __name__ == '__main__':
    # Optional arg: the trading date to review (YYYY-MM-DD).
    override = sys.argv[1] if len(sys.argv) > 1 else None
    if override:
        from datetime import date as _date
        run_day = _date.fromisoformat(override)
    else:
        run_day = None
    run_post_market(run_day)
