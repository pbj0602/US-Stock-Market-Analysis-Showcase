"""
AI Analysis Module
Generates natural language analysis reports via DeepSeek (default) or Claude.
The provider not selected as primary acts as the automatic fallback whenever
its API key is configured — see Config.AI_SERVICE.
"""

import re
import time
import pandas as pd
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
from config.config import Config

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

logger = logging.getLogger(__name__)


def _has_num(v) -> bool:
    """True for a finite, non-zero number (0 / None / NaN / junk → False)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (0.0, float('inf'), float('-inf'))

# Sizing and exits are rule-based (core+satellite). The model used to invent
# its own ("仓位控制在20-30%", "保持空仓或清仓") and contradict the rules in
# the same report, so every narrative prompt carries this constraint.
STRATEGY_GUARDRAIL = (
    '\n【策略约束】账户采用核心+卫星：75%指数核心仓（SPY/QQQ）永不因信号卖出；'
    '卫星仓每笔按1%账户风险定仓。仓位大小和卖出指令都由规则决定，'
    '禁止输出任何仓位百分比、"空仓/清仓/减仓X%"之类的建议。\n'
)

class AIAnalyzer:
    def __init__(self):
        self.claude_client = None
        self.ai_service = Config.AI_SERVICE.lower()

        # Initialize Claude client (primary when AI_SERVICE=claude, else fallback)
        if Config.ANTHROPIC_API_KEY and ANTHROPIC_AVAILABLE:
            try:
                self.claude_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
            except Exception as e:
                logger.error(f"Failed to initialize Claude client: {str(e)}")
        elif not ANTHROPIC_AVAILABLE and self.ai_service == 'claude':
            logger.warning("Anthropic package not installed but Claude service selected")

        self.deepseek_available = bool(Config.DEEPSEEK_API_KEY)

        # Resolve which provider actually serves calls. Falls through to the
        # other provider if the configured one has no usable credentials.
        if self.ai_service == 'claude' and self.claude_client:
            self.provider = 'claude'
        elif self.deepseek_available:
            self.provider = 'deepseek'
        elif self.claude_client:
            self.provider = 'claude'
        else:
            self.provider = None

        if self.provider == 'claude':
            self.model_main = Config.CLAUDE_MODEL_MAIN
            self.model_fast = Config.CLAUDE_MODEL_FAST
        else:
            self.model_main = Config.DEEPSEEK_MODEL_MAIN
            self.model_fast = Config.DEEPSEEK_MODEL_FAST

        # `client` is kept for backward compatibility: truthy means "AI usable".
        self.client = self.claude_client if self.provider == 'claude' else self.provider
        logger.info(
            "AI provider: %s (deepseek_key=%s, claude_client=%s)",
            self.provider, self.deepseek_available, bool(self.claude_client),
        )
        if not self.provider:
            logger.warning("No AI service client available")


    @staticmethod
    def _clean_llm_output(text: str) -> str:
        """
        Strip common LLM markdown artifacts from output before rendering.
        Removes orphaned --- dividers, bare ## headers, disclaimer blocks,
        and collapses excessive blank lines.
        """
        if not isinstance(text, str):
            return text
        # Remove standalone markdown dividers
        text = re.sub(r'(?m)^\s*-{3,}\s*$', '', text)
        # Remove bare ## / # header lines with no useful content
        text = re.sub(r'(?m)^\s*#{1,6}\s*$', '', text)
        # Remove disclaimer / warning blocks (> ⚠️ … or > 免责…)
        text = re.sub(r'>[ \t]*[⚠️免责][^\n]*', '', text)
        # Collapse 3+ blank lines → 2
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()
        # Warn if the text appears truncated (no sentence-ending character)
        if text and text[-1] not in '.!?。！？）)」"\'':
            logger.warning("LLM output may be truncated: …%s", text[-60:])
        return text

    def _call_deepseek(self, prompt, max_tokens=2000, temperature=0.5, model=None, system=None):
        """
        Call DeepSeek's OpenAI-compatible chat/completions endpoint.
        Retries transient failures (429 / 5xx / network) up to 3 times.
        Returns None once every attempt is exhausted.
        """
        import requests as _requests
        api_key = Config.DEEPSEEK_API_KEY
        if not api_key:
            return None

        messages = []
        if system:
            # Claude accepts a list of content blocks; DeepSeek wants plain text.
            if isinstance(system, list):
                system = '\n'.join(b.get('text', '') for b in system)
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        payload = {
            'model': model or Config.DEEPSEEK_MODEL_MAIN,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature,
        }
        url = f"{Config.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

        last_error = None
        for attempt in range(3):
            try:
                resp = _requests.post(
                    url,
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json=payload,
                    timeout=180,
                )
                # 429 rate limit / 5xx server errors are worth another try.
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise _requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                text = resp.json()['choices'][0]['message']['content']
                return self._clean_llm_output(text)
            except (_requests.HTTPError, _requests.ConnectionError, _requests.Timeout) as e:
                last_error = e
                if attempt < 2:
                    wait = 15 * (attempt + 1)
                    logger.warning(f"DeepSeek transient error (attempt {attempt+1}/3), retrying in {wait}s: {e}")
                    time.sleep(wait)
            except Exception as e:
                # Auth (401), insufficient balance (402), bad request — no retry.
                logger.error(f"DeepSeek non-retryable error: {e}")
                return None

        logger.error(f"DeepSeek failed after 3 attempts: {last_error}")
        return None

    def _call_claude(self, prompt, max_tokens=2000, temperature=0.5, model=None, system=None):
        """Call Claude with retry on transient errors. Returns None on failure."""
        if not self.claude_client:
            return None

        model = model or Config.CLAUDE_MODEL_MAIN
        retryable = (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        )
        kwargs = {'system': system} if system else {}
        last_error = None
        for attempt in range(3):
            try:
                response = self.claude_client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=120,
                    **kwargs,
                )
                return self._clean_llm_output(response.content[0].text)
            except retryable as e:
                last_error = e
                if attempt < 2:
                    wait = 15 * (attempt + 1)
                    logger.warning(f"Claude transient error (attempt {attempt+1}/3), retrying in {wait}s: {e}")
                    time.sleep(wait)
            except Exception as e:
                # Non-retryable: billing (402), auth (401/403), bad request, etc.
                logger.error(f"Claude non-retryable error: {e}")
                return None

        logger.error(f"Claude failed after 3 attempts: {last_error}")
        return None

    def _call_ai_service(self, prompt, max_tokens=2000, temperature=0.5, model=None, system=None):
        """
        Run a prompt against the primary provider, falling back to the other one
        if it fails. `model` is provider-specific — pass self.model_main /
        self.model_fast so it always matches the active provider.
        """
        if not self.provider:
            return None

        if self.provider == 'deepseek':
            primary, fallback = self._call_deepseek, self._call_claude
            fallback_model = Config.CLAUDE_MODEL_MAIN
            fallback_ready = bool(self.claude_client)
        else:
            primary, fallback = self._call_claude, self._call_deepseek
            fallback_model = Config.DEEPSEEK_MODEL_MAIN
            fallback_ready = self.deepseek_available

        result = primary(prompt, max_tokens, temperature, model=model, system=system)
        if result or not fallback_ready:
            return result

        logger.warning("%s call failed — trying the other provider", self.provider)
        # The requested model name belongs to the primary provider, so let the
        # fallback use its own default tier instead.
        return fallback(prompt, max_tokens, temperature, model=fallback_model, system=system)


    def generate_analysis(self, market_data, financial_data, macro_data, news_data, portfolio_snapshot, signals, **kwargs):
        """
        Generate comprehensive AI analysis report

        Args:
            market_data (DataFrame): Market data and technical indicators
            financial_data (DataFrame): Financial reports data
            macro_data (DataFrame): Macroeconomic indicators
            news_data (DataFrame): News data with sentiment
            portfolio_snapshot (DataFrame): Current portfolio holdings
            signals (DataFrame): Buy/sell/hold signals
            **kwargs: Additional context (market_context, premarket_data, event_analysis)

        Returns:
            dict: Analysis report with different sections
        """
        # Extract additional context from kwargs
        self.market_context = kwargs.get('market_context', {})
        self.premarket_data = kwargs.get('premarket_data', {})
        self.event_analysis = kwargs.get('event_analysis', {})
        self.macro_alerts = kwargs.get('macro_alerts', {})
        self.cross_asset_data = kwargs.get('cross_asset_data', {})
        self.options_data = kwargs.get('options_data', {})
        # One-line historical calibration ("近60日BUY信号T+5胜率42%") injected
        # into the setups prompt so the AI reviews signals against track record.
        self._signal_history_line = kwargs.get('signal_history_line', '')
        if not self.client:
            logger.error("AI client not available")
            return self._generate_fallback_analysis(market_data, portfolio_snapshot, signals)
        
        try:
            # Prepare data summaries for AI analysis
            market_summary = self._prepare_market_summary(market_data, signals)
            portfolio_summary = self._prepare_portfolio_summary(portfolio_snapshot)
            macro_summary = self._prepare_macro_summary(macro_data)
            news_summary = self._prepare_news_summary(news_data)

            analysis_sections = {}
            _warnings = []

            # ── Call 1: Market overview + stock recommendations + risk assessment (batched) ──
            market_batch = self._generate_market_batch(
                market_summary, macro_summary, news_summary, portfolio_summary
            )
            analysis_sections.update(market_batch)
            if not market_batch.get('market_overview'):
                _warnings.append('市场概览生成失败')

            # ── Calls 2–4: Run in parallel (all independent of each other) ──
            raw_buy_signals = market_summary.get('signals_summary', {}).get('buy_signals_detail', [])
            buy_signals = [s for s in raw_buy_signals if s.get('confidence', 0) >= Config.TRADE_SETUP_MIN_CONFIDENCE]

            def _task_portfolio():
                if portfolio_snapshot.empty:
                    return None
                return self._generate_portfolio_analysis(portfolio_summary, market_summary, news_summary)

            def _task_growth_setups():
                if not buy_signals:
                    no_signal_msg = (
                        f'今日买入信号置信度均低于阈值（{Config.TRADE_SETUP_MIN_CONFIDENCE}%），暂无交易机会。'
                        if raw_buy_signals else '今日无买入信号，当前保持观望。'
                    )
                    return {'growth_analysis': {}, 'trade_setups': {'setups': [], 'summary': no_signal_msg}}
                return self._generate_growth_and_setups_batch(buy_signals, news_summary, financial_data, market_summary)

            def _task_event_impact():
                headlines = news_summary.get('market_headlines', [])
                watchlist = [s['symbol'] for s in market_summary.get('all_stocks', [])]
                return self._generate_event_impact_analysis(headlines, watchlist)

            with ThreadPoolExecutor(max_workers=3) as pool:
                fut_portfolio     = pool.submit(_task_portfolio)
                fut_growth_setups = pool.submit(_task_growth_setups)
                fut_event_impact  = pool.submit(_task_event_impact)

                portfolio_result = fut_portfolio.result()
                if portfolio_result is not None:
                    analysis_sections['portfolio_analysis'] = portfolio_result
                    if not portfolio_result:
                        _warnings.append('持仓分析生成失败')

                gs = fut_growth_setups.result()
                analysis_sections['growth_analysis'] = gs.get('growth_analysis', {})
                analysis_sections['trade_setups']    = gs.get('trade_setups', {'setups': [], 'summary': '今日无买入信号。'})
                analysis_sections['ai_confidence_adjustments'] = gs.get('ai_adjustments', {})

                # Sector blurbs and extreme-RSI HOLD notes were dropped from the
                # report (2026-09-23): the sector blurbs restated the table and
                # the HOLD notes carried no action.
                analysis_sections['extreme_hold_analysis'] = {}
                analysis_sections['sector_summaries']      = {}
                analysis_sections['event_impact']          = fut_event_impact.result() or {'has_events': False, 'impact_text': ''}

            # ── Call 6: Executive summary (needs all sections above) ──
            analysis_sections['executive_summary'] = self._generate_executive_summary(
                analysis_sections, news_summary, market_summary, macro_summary
            )
            if not analysis_sections.get('executive_summary'):
                _warnings.append('执行摘要生成失败')

            if _warnings:
                analysis_sections['_warnings'] = _warnings
                logger.warning(f"Analysis completed with degraded sections: {_warnings}")

            logger.info(f"AI analysis generated successfully (5 API calls, {len(_warnings)} warning(s))")
            return analysis_sections

        except Exception as e:
            logger.error(f"Error generating AI analysis: {str(e)}")
            return self._generate_fallback_analysis(market_data, portfolio_snapshot, signals)
    
    def _prepare_market_summary(self, market_data, signals):
        """Prepare market data summary for AI analysis"""
        try:
            if market_data.empty or signals.empty:
                return {}

            md = market_data.copy()
            for field in ['Day_Change_Pct', 'Price_Change_Pct', 'day_change_pct']:
                if field in md.columns:
                    if field != 'Day_Change_Pct':
                        md['Day_Change_Pct'] = md[field]
                    break
            if 'Day_Change_Pct' not in md.columns:
                md['Day_Change_Pct'] = 0

            missing_mask = md['Day_Change_Pct'].isna()
            if missing_mask.any():
                missing_syms = md.loc[missing_mask, 'Symbol'].tolist() if 'Symbol' in md.columns else missing_mask.sum()
                logger.warning(f"Missing Day_Change_Pct for {len(missing_syms) if isinstance(missing_syms, list) else missing_syms} symbols: {missing_syms}")
            md_clean = md.dropna(subset=['Day_Change_Pct'])
            if len(md_clean) == 0:
                logger.warning("All symbols missing Day_Change_Pct — filling with 0, data may be stale or rate-limited")
                md_clean = md.fillna(0)

            top_performers = md_clean.nlargest(3, 'Day_Change_Pct')
            worst_performers = md_clean.nsmallest(3, 'Day_Change_Pct')

            # Signal summary — include HOLD signals too.
            # The generator now emits 'SELL' for 规避 (value traps); 'AVOID' is kept
            # here for backward-compat with older signal frames so the 规避 bucket
            # never silently drops a warning.
            buy_signals = signals[signals['Signal'] == 'BUY']
            sell_signals = signals[signals['Signal'].isin(['SELL', 'AVOID'])]
            hold_signals = signals[signals['Signal'] == 'HOLD']

            def _sig_detail(row):
                # Derive explicit trend direction label from underlying indicators
                pvs50    = float(row.get('Price_vs_SMA50', 0) or 0)
                sma_x    = float(row.get('SMA10_vs_SMA50', 0) or 0)
                mom10    = float(row.get('Momentum_10', 0) or 0)
                mom20    = float(row.get('Momentum_20', 0) or 0)
                macd_v   = float(row.get('MACD', 0) or 0)
                macd_sig = float(row.get('MACD_Signal', 0) or 0)
                # Uptrend: price above SMA50, SMA10 golden-crossed, positive momentum
                # Downtrend: price below SMA50, negative momentum on both timeframes
                bull_pts = sum([pvs50 > 0, sma_x > 0, mom10 > 0, mom20 > 0, macd_v > macd_sig])
                if bull_pts >= 4:
                    trend_dir = "🟢上行趋势"
                elif bull_pts <= 1:
                    trend_dir = "🔴下行趋势"
                else:
                    trend_dir = "🟡横盘震荡"

                return {
                    'symbol': row.get('Symbol', ''),
                    'signal': row.get('Signal', ''),
                    'confidence': round(float(row.get('Confidence', 0)), 1),
                    'price': round(float(row.get('Current_Price', 0) or 0), 2),
                    # None, not 0, when the setup uses a trailing exit. Feeding
                    # the model "target: $0" invited it to invent a price to
                    # replace the nonsense one — exactly the hallucination the
                    # price-anchoring block exists to prevent.
                    'target': (round(float(row['Target_Price']), 2)
                               if _has_num(row.get('Target_Price')) else None),
                    'exit_style': ('固定目标价' if _has_num(row.get('Target_Price'))
                                   else '移动止盈(无固定目标)'),
                    'stop': round(float(row.get('Stop_Loss', 0) or 0), 2),
                    'low_52w': round(float(row.get('52W_Low', 0) or 0), 2),
                    'high_52w': round(float(row.get('52W_High', 0) or 0), 2),
                    'dist_from_52w_low_pct': round(float(row.get('Dist_From_52W_Low_Pct', 0) or 0), 1),
                    'pe_ratio': row.get('PE_Ratio'),
                    'revenue_growth': row.get('Revenue_Growth'),
                    'earnings_growth': row.get('Earnings_Growth'),
                    'debt_to_equity': row.get('Debt_to_Equity'),
                    'rsi': round(float(row.get('RSI', 50) or 50), 1),
                    'trend_score': round(float(row.get('Trend_Score', 0.5) or 0.5), 2),
                    'momentum_score': round(float(row.get('Momentum_Score', 0.5) or 0.5), 2),
                    'price_vs_sma50': round(pvs50 * 100, 1),
                    'sma10_vs_sma50': round(sma_x * 100, 1),
                    'momentum_10d': round(mom10 * 100, 1),
                    'momentum_20d': round(mom20 * 100, 1),
                    'macd_bullish': macd_v > macd_sig,
                    'bb_position': round(float(row.get('BB_Position', 0.5) or 0.5), 2),
                    'trend_dir': trend_dir,
                    'volatility': round(float(row.get('Volatility', 0) or 0), 3),
                    'beta': round(float(row.get('Beta', 1) or 1), 2),
                    'position_size_pct': round(float(row.get('position_size_pct', 33) or 33), 0),
                    # NaN is truthy, so `or 0` alone let NaN through as 'nan'.
                    'rr': (round(float(row['Risk_Reward_Ratio']), 2)
                           if _has_num(row.get('Risk_Reward_Ratio')) else None),
                    'reasoning': str(row.get('Reasoning', ''))[:300],
                }

            def _mkt_detail(row):
                return {
                    'symbol': row.get('Symbol', ''),
                    'price': round(float(row.get('Current_Price', 0) or 0), 2),
                    'change_pct': round(float(row.get('Day_Change_Pct', 0) or 0), 2),
                    'rsi': round(float(row.get('RSI', 50) or 50), 1),
                    'momentum_10': round(float(row.get('Momentum_10', 0) or 0) * 100, 1),
                    'momentum_20': round(float(row.get('Momentum_20', 0) or 0) * 100, 1),
                    'price_vs_sma50': round(float(row.get('Price_vs_SMA50', 0) or 0) * 100, 1),
                    'bb_position': round(float(row.get('BB_Position', 0.5) or 0.5), 2),
                    'volume_ratio': round(float(row.get('Volume_Ratio', 1) or 1), 2),
                }

            # Full per-stock snapshot for all watchlist stocks
            all_stocks = [_mkt_detail(r) for _, r in md_clean.iterrows()]

            summary = {
                'total_symbols': len(market_data),
                'avg_day_change': md_clean['Day_Change_Pct'].mean(),
                'top_performers': [_mkt_detail(r) for _, r in top_performers.iterrows()],
                'worst_performers': [_mkt_detail(r) for _, r in worst_performers.iterrows()],
                'all_stocks': all_stocks,
                'signals_summary': {
                    'buy_count': len(buy_signals),
                    'sell_count': len(sell_signals),
                    'hold_count': len(hold_signals),
                    'top_buy_picks': buy_signals.head(3)['Symbol'].tolist() if not buy_signals.empty else [],
                    'top_sell_picks': sell_signals.head(3)['Symbol'].tolist() if not sell_signals.empty else [],
                    'avg_confidence': float(signals['Confidence'].mean()),
                    'buy_signals_detail': [_sig_detail(r) for _, r in buy_signals.head(3).iterrows()],
                    'sell_signals_detail': [_sig_detail(r) for _, r in sell_signals.head(3).iterrows()],
                    'hold_signals_detail': [_sig_detail(r) for _, r in hold_signals.head(5).iterrows()],
                },
            }

            return summary

        except Exception as e:
            logger.error(f"Error preparing market summary: {str(e)}")
            return {}
    
    def _prepare_portfolio_summary(self, portfolio_snapshot):
        """Prepare portfolio summary for AI analysis"""
        try:
            if portfolio_snapshot.empty:
                return {}
            
            portfolio_attrs = getattr(portfolio_snapshot, 'attrs', {})
            portfolio_summary_data = portfolio_attrs.get('portfolio_summary', {})
            
            # Get top and worst performing positions
            top_positions = portfolio_snapshot.nlargest(3, 'Unrealized_PnL_Pct')
            worst_positions = portfolio_snapshot.nsmallest(3, 'Unrealized_PnL_Pct')
            
            summary = {
                'total_value': portfolio_summary_data.get('total_value', 0),
                'total_pnl': portfolio_summary_data.get('total_pnl', 0),
                'portfolio_return_pct': portfolio_summary_data.get('portfolio_return_pct', 0),
                'num_positions': len(portfolio_snapshot),
                'largest_position': portfolio_summary_data.get('largest_position'),
                'largest_position_weight': portfolio_summary_data.get('largest_position_weight', 0),
                'daily_pnl': portfolio_summary_data.get('total_daily_pnl', 0),
                'top_performers': [
                    self._enrich_holding_row(row)
                    for _, row in top_positions.iterrows()
                ],
                'worst_performers': [
                    self._enrich_holding_row(row)
                    for _, row in worst_positions.iterrows()
                ],
                'all_holdings': [
                    self._enrich_holding_row(row)
                    for _, row in portfolio_snapshot.iterrows()
                ],
                'sector_allocation': portfolio_summary_data.get('sector_allocation', {})
            }
            
            return summary
            
        except Exception as e:
            logger.error(f"Error preparing portfolio summary: {str(e)}")
            return {}
    
    @staticmethod
    def _enrich_holding_row(row) -> dict:
        """Extract trend-direction data from a portfolio snapshot row."""
        pvs50  = float(row.get('Price_vs_SMA50', 0) or 0)
        sma_x  = float(row.get('SMA10_vs_SMA50', 0) or 0)
        mom10  = float(row.get('Momentum_10', 0) or 0)
        mom20  = float(row.get('Momentum_20', 0) or 0)
        macd_v = float(row.get('MACD', 0) or 0)
        macd_s = float(row.get('MACD_Signal', 0) or 0)
        bull_pts = sum([pvs50 > 0, sma_x > 0, mom10 > 0, mom20 > 0, macd_v > macd_s])
        if bull_pts >= 4:
            trend_dir = "上行趋势"
        elif bull_pts <= 1:
            trend_dir = "下行趋势"
        else:
            trend_dir = "横盘震荡"
        return {
            'symbol':        row.get('Symbol', ''),
            'pnl_pct':       round(float(row.get('Unrealized_PnL_Pct', 0) or 0), 2),
            'day_chg_pct':   round(float(row.get('Day_Change_Pct', 0) or 0), 2),
            'weight':        round(float(row.get('Weight', 0) or 0), 1),
            'current_price': round(float(row.get('Current_Price', 0) or 0), 2),
            'rsi':           round(float(row.get('RSI', 50) or 50), 1),
            'price_vs_sma50': round(pvs50 * 100, 1),
            'momentum_10d':  round(mom10 * 100, 1),
            'momentum_20d':  round(mom20 * 100, 1),
            'trend_dir':     trend_dir,
            'macd_bullish':  macd_v > macd_s,
        }

    def _prepare_macro_summary(self, macro_data):
        """Prepare macro data summary for AI analysis"""
        try:
            if macro_data.empty:
                return {}
            
            key_indicators = {
                'Federal_Funds_Rate': 'Fed Rate',
                '10Y_Treasury': '10Y Treasury',
                'CPI': 'Inflation (CPI)',
                'Unemployment_Rate': 'Unemployment',
                'VIX': 'Market Volatility',
                'Dollar_Index': 'USD Strength'
            }
            
            summary = {}
            for indicator, display_name in key_indicators.items():
                indicator_data = macro_data[macro_data['Indicator'] == indicator]
                if not indicator_data.empty:
                    row = indicator_data.iloc[0]
                    summary[indicator] = {
                        'name': display_name,
                        'value': row['Value'],
                        'change_pct': row.get('Change_Pct'),
                        'units': row.get('Units', '')
                    }
            
            return summary
            
        except Exception as e:
            logger.error(f"Error preparing macro summary: {str(e)}")
            return {}
    
    def _prepare_news_summary(self, news_data):
        """Prepare news summary for AI analysis"""
        try:
            if news_data.empty:
                return {}
            
            # Get sentiment breakdown
            sentiment_counts = news_data['Sentiment_Label'].value_counts()
            avg_sentiment = news_data['Sentiment_Score'].mean()
            
            # Get top positive and negative news
            positive_news = news_data[news_data['Sentiment_Label'] == 'Positive'].nlargest(3, 'Confidence')
            negative_news = news_data[news_data['Sentiment_Label'] == 'Negative'].nlargest(3, 'Confidence')
            
            # Extract MARKET-bucket headlines (macro/geopolitical/policy news with no symbol filter)
            market_news = news_data[news_data['Symbol'] == 'MARKET'] if 'Symbol' in news_data.columns else pd.DataFrame()
            market_headlines = (
                market_news.nlargest(10, 'Confidence')['Title'].tolist()
                if not market_news.empty else []
            )

            summary = {
                'total_articles': len(news_data),
                'avg_sentiment': avg_sentiment,
                'sentiment_breakdown': {
                    'positive': sentiment_counts.get('Positive', 0),
                    'negative': sentiment_counts.get('Negative', 0),
                    'neutral': sentiment_counts.get('Neutral', 0)
                },
                'top_positive_headlines': positive_news['Title'].tolist() if not positive_news.empty else [],
                'top_negative_headlines': negative_news['Title'].tolist() if not negative_news.empty else [],
                'symbols_with_news': news_data['Symbol'].unique().tolist(),
                'market_headlines': market_headlines,
            }
            
            return summary
            
        except Exception as e:
            logger.error(f"Error preparing news summary: {str(e)}")
            return {}
    
    def _call_ai_batch(self, sections: dict, max_tokens=4000, model=None, temperature=0.3) -> dict:
        """
        Send multiple prompts in one API call using [SECTION:name] markers.
        sections: {name: prompt_text}
        Returns: {name: response_text}

        Lower default temperature (0.3) than free-form prose: these sections are
        numeric/factual and benefit from determinism.
        """
        combined = '\n\n'.join(
            f"[SECTION:{name}]\n{prompt}" for name, prompt in sections.items()
        )
        wrapper = (
            "请按顺序回答以下各节问题。每节以 [SECTION:节名] 标记开头，严格保留标记原文不要翻译。\n\n"
            + combined
        )
        raw = self._call_ai_service(wrapper, max_tokens=max_tokens, temperature=temperature, model=model)
        if not raw:
            return {name: '' for name in sections}
        results = {}
        missing = []
        for name in sections:
            pattern = rf'\[SECTION:{re.escape(name)}\]\s*(.*?)(?=\[SECTION:|$)'
            m = re.search(pattern, raw, re.DOTALL)
            results[name] = m.group(1).strip() if m else ''
            if not results[name]:
                missing.append(name)
        if missing:
            logger.warning(f"_call_ai_batch: missing/empty sections in model output: {missing}")
        return results

    def _format_catalyst_context(self) -> str:
        """
        Build a prioritized catalyst/event-risk summary for AI prompts.
        Draws from event_analysis, macro_alerts, cross_asset_data, options_data.
        Returns a multi-line string to prepend to market_overview / executive_summary.
        """
        lines = []

        # 1. Cross-asset risk-off warning (highest priority)
        ca = self.cross_asset_data or {}
        if ca.get('risk_off_warning'):
            lines.append(f'⚠ 跨资产风险规避信号：{ca.get("risk_off_reason", "DXY/HYG异常")}')

        # 2. Today's high-impact macro event (second priority)
        ea = self.event_analysis or {}
        macro_events = ea.get('macro_events', {})
        if macro_events.get('has_high_impact_today'):
            ev_name = macro_events.get('event_name', '重大宏观数据')
            lines.append(f'🔴 今日核心催化剂：{ev_name}——市场将围绕此数据定价，技术信号置信度已系统性下调')

        # 3. Upcoming macro events within 3 days
        ma = self.macro_alerts or {}
        upcoming = ma.get('upcoming_alerts', [])[:3]
        if upcoming:
            ev_strs = [
                f"{a['event']}（{a['days_until']}天后，{a['date']}）"
                for a in upcoming
            ]
            lines.append(f'📅 近期高影响日历：{" / ".join(ev_strs)}')
        if ma.get('reduction_warning'):
            lines.append(f'⚠ {ma["reduction_warning"]}')

        # 4. Stocks reporting earnings this week
        earnings_soon = [
            f"{sym}（{data['earnings']['days_until']}天后）"
            for sym, data in ea.get('stocks', {}).items()
            if data.get('earnings', {}).get('is_this_week')
            and 0 <= data['earnings'].get('days_until', 99) <= 5
        ]
        if earnings_soon:
            lines.append(f'📊 本周财报：{", ".join(earnings_soon[:6])}')

        # 5. Options market sentiment
        opts = self.options_data or {}
        pc = opts.get('put_call', {})
        pc_ratio = pc.get('pc_ratio')
        if pc_ratio is not None:
            lines.append(f'期权情绪：P/C比率={pc_ratio:.2f}（{pc.get("signal", "")}）')
        vts_signal = opts.get('vix_term_structure', {}).get('signal', '')
        if vts_signal and vts_signal not in ('N/A', ''):
            lines.append(f'VIX期限结构：{vts_signal}')

        return '\n'.join(lines) if lines else '无高影响催化剂事件，当日驱动以技术面为主。'

    def _generate_market_batch(self, market_summary, macro_summary, news_summary, portfolio_summary) -> dict:
        """Batches market_overview + stock_recommendations + risk_assessment into one API call."""
        try:
            top_3 = market_summary.get('top_performers', [])[:3]
            worst_3 = market_summary.get('worst_performers', [])[:3]
            all_stocks = market_summary.get('all_stocks', [])
            sig = market_summary.get('signals_summary', {})
            buys = sig.get('buy_signals_detail', [])
            sells = sig.get('sell_signals_detail', [])

            rsi_values = [s['rsi'] for s in all_stocks if s.get('rsi')]
            oversold_count = sum(1 for r in rsi_values if r < 35)
            overbought_count = sum(1 for r in rsi_values if r > 70)
            above_sma50 = sum(1 for s in all_stocks if s.get('price_vs_sma50', 0) > 0)
            top_2_pos = news_summary.get('top_positive_headlines', [])[:2]
            top_2_neg = news_summary.get('top_negative_headlines', [])[:2]

            regime = self.market_context.get('market_regime', {}).get('regime', 'UNKNOWN')
            vix = self.market_context.get('vix_analysis', {}).get('current', 'N/A')
            vix_regime = self.market_context.get('vix_analysis', {}).get('regime', 'N/A')
            vix_trend = self.market_context.get('vix_analysis', {}).get('trend', 'N/A')

            macro_lines = self._format_macro_data(macro_summary) if macro_summary else "暂无宏观数据"

            def _fmt_signal(s):
                rr_str   = f"R/R={s['rr']:.1f}" if s.get('rr') else "R/R=N/A"
                tgt_str  = f"${s['target']}" if s.get('target') is not None else "无(移动止盈)"
                macd_str = "MACD多头" if s.get('macd_bullish') else "MACD空头"
                bb_str   = f"BB位={s.get('bb_position', 0.5):.2f}"
                return (
                    f"  {s['symbol']} [{s.get('trend_dir','?')}]\n"
                    f"    价格=${s['price']} 目标={tgt_str} 止损=${s['stop']} {rr_str}\n"
                    f"    RSI={s['rsi']} SMA50偏离={s['price_vs_sma50']:+.1f}% "
                    f"SMA10vsSMA50={s.get('sma10_vs_sma50',0):+.1f}% "
                    f"10日动量={s.get('momentum_10d',0):+.1f}% 20日动量={s.get('momentum_20d',0):+.1f}%\n"
                    f"    {macd_str} {bb_str} Beta={s['beta']} 波动率={s['volatility']:.3f}\n"
                    f"    仓位={s['position_size_pct']:.0f}% | 信号依据: {s['reasoning'][:150]}"
                )

            buy_lines  = '\n'.join([_fmt_signal(s) for s in buys])  if buys  else "  无"
            sell_lines = '\n'.join([_fmt_signal(s) for s in sells]) if sells else "  无"

            high_beta = [s for s in all_stocks if s.get('beta', 1) > 1.5]
            oversold_list = [s for s in all_stocks if s.get('rsi', 50) < 35]
            num_pos = portfolio_summary.get('num_positions', 0)
            port_status = (
                "空仓（无持仓）"
                if num_pos == 0
                else f"{num_pos}只持仓，最大仓位{portfolio_summary.get('largest_position_weight', 0):.0f}%，总收益{portfolio_summary.get('portfolio_return_pct', 0):.1f}%"
            )

            # Pre-compute joined strings to avoid nested f-string escaping issues
            top_gainers = ', '.join(
                p['symbol'] + ' +' + f"{p['change_pct']:.1f}% RSI=" + str(p['rsi']) for p in top_3
            )
            top_losers = ', '.join(
                p['symbol'] + ' ' + f"{p['change_pct']:.1f}% RSI=" + str(p['rsi']) for p in worst_3
            )
            high_beta_str = ', '.join(s['symbol'] for s in high_beta) or '无'
            oversold_str = ', '.join(s['symbol'] for s in oversold_list) or '无'
            regime_strength = self.market_context.get('market_regime', {}).get('strength', 'N/A')
            trade_action = self.market_context.get('trading_recommendation', {}).get('recommendation', 'N/A')
            avg_change = f"{market_summary.get('avg_day_change', 0):.2f}"
            avg_sentiment = f"{news_summary.get('avg_sentiment', 0):.2f}"
            pos_news = '; '.join(top_2_pos)
            neg_news = '; '.join(top_2_neg)
            total_syms = market_summary.get('total_symbols', 0)
            buy_c = sig.get('buy_count', 0)
            hold_c = sig.get('hold_count', 0)
            sell_c = sig.get('sell_count', 0)
            avg_conf = f"{sig.get('avg_confidence', 0):.0f}"

            _lang_guard = (
                '\n\n【分析框架】先看外部驱动（宏观数据/政策/地缘/财报/新闻/跨资产），'
                '再用技术指标（RSI/SMA/MACD/VIX）去确认或反驳该外部判断。'
                '技术指标只能作为佐证，不得脱离外部背景单独构成结论；'
                '若技术面与外部驱动背离，必须明确指出背离并以外部驱动为主。'
                '\n【语言要求】所有判断必须用概率性表述（如"概率偏大"、"风险偏高"、"当前环境有利于"）。'
                '禁止使用：毫无疑问、一定、绝对、必然、完全确定。'
                '禁止输出 Markdown 分隔符（---）、孤立标题行（##）或免责声明块。'
            )

            breadth_pct = above_sma50 / len(all_stocks) * 100 if all_stocks else 50
            breadth_note = (
                f'⚠ 广度预警：仅{above_sma50}/{len(all_stocks)}只股票站上SMA50（{breadth_pct:.0f}%），'
                '市场广度偏弱，请在分析中指出这与指数走势的潜在背离。'
                if breadth_pct < 50 else
                f'市场广度：{above_sma50}/{len(all_stocks)}只站上SMA50（{breadth_pct:.0f}%）。'
            )

            # SPY/QQQ/IWM as benchmark baseline
            benchmarks = {s['symbol']: s for s in all_stocks if s['symbol'] in ('SPY', 'QQQ', 'IWM')}
            def _bench_str(sym):
                b = benchmarks.get(sym)
                if not b:
                    return f'{sym}: N/A'
                price = b.get('price', 0)
                pvs50 = b.get('price_vs_sma50', 0)  # already in %, e.g. -3.9
                denom = 1 + pvs50 / 100
                sma50 = round(price / denom, 2) if price and abs(denom) > 0.01 else None
                sma50_str = f' SMA50≈${sma50}' if sma50 else ''
                return f'{sym}: ${price} {b.get("change_pct", 0):+.2f}% RSI={b.get("rsi", 50):.0f}{sma50_str}'
            benchmark_line = ' | '.join(_bench_str(s) for s in ('SPY', 'QQQ', 'IWM'))
            # Pre-compute SPY key levels for the prompt
            _spy = benchmarks.get('SPY', {})
            _spy_price = _spy.get('price', 0)
            _spy_pvs50 = _spy.get('price_vs_sma50', 0)
            _spy_sma50 = round(_spy_price / (1 + _spy_pvs50 / 100), 2) if _spy_price and abs(1 + _spy_pvs50 / 100) > 0.01 else None
            # Inject pre-computed levels so the model quotes facts instead of
            # hallucinating price points (#8). Support = SMA50 (derived above).
            if _spy_price and _spy_sma50:
                spy_levels_str = f'SPY现价${_spy_price}，SMA50支撑≈${_spy_sma50}'
            elif _spy_price:
                spy_levels_str = f'SPY现价${_spy_price}（SMA50数据缺失）'
            else:
                spy_levels_str = 'SPY价位数据缺失'

            catalyst_context = self._format_catalyst_context()

            sections = {
                'market_overview': (
                    '你是资深买方市场策略师，用简体中文写盘前市场解读，语言简洁。\n'
                    'RSI/SMA/MACD/VIX等常见指标直接使用，无需括号解释。\n'
                    f'【催化剂与事件风险】（首要判断依据）\n{catalyst_context}\n'
                    f'【基准指数】{benchmark_line}\n'
                    f'【技术面（共{total_syms}只）】均涨跌{avg_change}%，超卖{oversold_count}只，超买{overbought_count}只\n'
                    f'{breadth_note}\n'
                    f'领涨：{top_gainers}\n'
                    f'领跌：{top_losers}\n'
                    f'信号：{buy_c}买/{hold_c}持/{sell_c}卖（均置信度{avg_conf}%）\n'
                    f'VIX：{vix}（{vix_regime}），趋势：{regime}，建议：{trade_action}\n'
                    f'新闻情绪：{avg_sentiment}，正：{pos_news}，负：{neg_news}\n'
                    '\n'
                    '请严格按以下三行格式输出，每行以标签开头，每行不超过50字：\n'
                    '核心驱动：[今日市场交易什么事件或趋势，1句]\n'
                    '技术状态：[技术面确认还是背离上述驱动，1句]\n'
                    f'关键观察位：[只能引用以下已算好的价位，禁止编造其它点位：{spy_levels_str}，VIX={vix}。'
                    '再补一个最重要的触发条件（如VIX升破X或SPY跌破SMA50支撑）]\n'
                    '禁止在标签之外输出任何内容。'
                ),
                'stock_recommendations': (
                    '你是系统化量化交易员，用简体中文写盘前信号扫描表，语言极简，只列关键数字。\n'
                    f'市场：趋势={regime}，VIX={vix}\n'
                    f'事件风险：{catalyst_context}\n'
                    '\n'
                    '【防幻觉规则】只能分析下方列表中的股票，禁止引入任何列表外股票。\n'
                    f'如果买入信号为0（当前={buy_c}个），本节只输出一行："今日无新信号。"\n'
                    '\n'
                    '【输出格式】每个信号严格按以下单行格式，禁止多行展开：\n'
                    '买入：[代码] [趋势emoji] 入场$xx–$xx | 止损$xx | 目标$xx | R/R=x.x | [一句风险提示]\n'
                    '卖出：[代码] [趋势emoji] 规避原因一句话\n'
                    '\n'
                    f'买入信号（{buy_c}个）：\n{buy_lines}\n'
                    f'卖出/规避（{sell_c}个）：\n{sell_lines}\n'
                    '\n'
                    '最后一行：今日整体信号质量一句话评价（置信度/数量/市场环境匹配度）。'
                ),
                'risk_assessment': (
                    '你是专业风险管理人，用简体中文写风险评估，每级1-2句，给具体数值。\n'
                    f'市场：趋势={regime}（强度{regime_strength}%），'
                    f'VIX={vix}（{vix_regime}，{vix_trend}），均涨跌{avg_change}%\n'
                    f'高Beta股：{high_beta_str}，超卖股：{oversold_str}\n'
                    f'宏观：{macro_lines}\n持仓：{port_status}\n'
                    '\n'
                    '三级风险标签（每级1-2句，给具体数值）：\n'
                    '🟢 【可接受风险】：当前正常波动范围，VIX和趋势现状，对卫星仓开新仓的含义\n'
                    '🟡 【需要注意】：哪些信号若出现需要警惕？给具体数值。\n'
                    '🔴 【高风险触发条件】：什么情况让风险显著升级（VIX突破几？关键位跌破？）\n'
                    + STRATEGY_GUARDRAIL
                ),
            }
            sections = {k: v + _lang_guard for k, v in sections.items()}
            return self._call_ai_batch(sections, max_tokens=1400)

        except Exception as e:
            logger.error(f"Error generating market batch: {str(e)}")
            return {
                'market_overview': '市场概览生成失败。',
                'stock_recommendations': '信号简报生成失败。',
                'risk_assessment': '风险评估生成失败。',
            }

    def _generate_growth_and_setups_batch(self, buy_signals, news_summary, financial_data, market_summary) -> dict:
        """
        Batch: growth expectation analysis + trade setup cards for all BUY signals in one API call.
        Replaces up to 6 separate per-signal calls with 1.
        """
        try:
            if not buy_signals:
                return {'growth_analysis': {}, 'trade_setups': {'setups': [], 'summary': '今日无买入信号，当前保持观望。'}}

            regime = self.market_context.get('market_regime', {}).get('regime', 'UNKNOWN')
            vix = self.market_context.get('vix_analysis', {}).get('current', 'N/A')
            pos_headlines = '; '.join(news_summary.get('top_positive_headlines', [])[:3]) or '无'
            neg_headlines = '; '.join(news_summary.get('top_negative_headlines', [])[:3]) or '无'

            signal_blocks = []
            for s in buy_signals[:3]:
                sym = s['symbol']
                fin_row = {}
                if financial_data is not None and not financial_data.empty and 'Symbol' in financial_data.columns:
                    match = financial_data[financial_data['Symbol'] == sym]
                    if not match.empty:
                        fin_row = match.iloc[0].to_dict()

                rev_str = f"{s['revenue_growth']*100:.1f}%" if s.get('revenue_growth') is not None else 'N/A'
                earn_str = f"{s['earnings_growth']*100:.1f}%" if s.get('earnings_growth') is not None else 'N/A'
                de_str = f"{s['debt_to_equity']:.1f}x" if s.get('debt_to_equity') is not None else 'N/A'
                target_str = (f"目标：${s['target']}" if s.get('target') is not None
                              else "目标：无（移动止盈，不设固定目标价）")
                rr_str = s['rr'] if s.get('rr') is not None else 'N/A'

                signal_blocks.append(
                    f"股票：{sym} | 价格：${s['price']} | 52周低：${s['low_52w']}（偏离{s['dist_from_52w_low_pct']:.1f}%）| "
                    f"52周高：${s['high_52w']} | {target_str} | 止损：${s['stop']} | R/R：{rr_str} | "
                    f"置信度：{s['confidence']}% | RSI：{s['rsi']} | SMA50偏离：{s['price_vs_sma50']:+.1f}% | "
                    f"PE：{s.get('pe_ratio','N/A')} | 营收增速：{rev_str} | 利润增速：{earn_str} | "
                    f"负债率：{de_str} | 仓位：{s['position_size_pct']:.0f}%"
                )

            signals_text = '\n'.join(signal_blocks)

            def _signal_block_template(sym):
                return f"""
[VERDICT:{sym}]
⭐ 一句话结论：[当前是买入/观望/不适合，核心理由1句，≤30字]

[GROWTH:{sym}]
（1）当前价格下跌是暂时回调还是基本面问题？（1句，给出判断依据）
（2）综合结论：值得买入/需要再观察，并说明最关键的理由。（1句）

[SETUP:{sym}]
交易周期：[日内/波段（1-3周）/趋势（1-3月）中选一个]
入场区间：[具体价格区间]
止损价位：[具体价格 / 亏损%]
目标价位：[数据中有目标价就照抄并写盈利%；数据写"无（移动止盈）"就只写"移动止盈，不设固定目标"，严禁自拟价格，也不要拿52周高点当目标]
入场理由：[技术面触发点，1句]
最大风险：[使这笔交易失效的1个具体条件]

[ADJUST:{sym}]
下调：[0-30的整数。技术信号未计入的外部风险（负面新闻/临近财报/宏观事件/基本面恶化）该扣多少置信度分；无顾虑填0]
理由：[一句话，必须引用上方数据中的具体事实；填0时写"无"]
"""

            history_line = getattr(self, '_signal_history_line', '') or ''
            history_block = f"历史校准：{history_line}\n" if history_line else ""

            prompt = f"""你是专业分析师，用简体中文为普通投资者写分析，语言简洁直接。严格只用下方数据，不要编造。

市场环境：趋势={regime}，VIX={vix}
新闻：正面：{pos_headlines}；负面：{neg_headlines}
{history_block}
买入信号数据：
{signals_text}

你对每个信号有「置信度下调权」（ADJUST部分）：若新闻、事件或基本面显示技术信号有未计入的风险就扣分（最多30分）；
纯技术面健康、无外部隐患时填0。只能下调，不能上调。

请为每只股票按以下格式输出四部分（VERDICT必须是第一个）：
{''.join(_signal_block_template(s['symbol']) for s in buy_signals[:3])}
"""

            raw = self._call_ai_service(prompt, max_tokens=1400, temperature=0.3)
            if not raw:
                return {'growth_analysis': {}, 'trade_setups': {'setups': [], 'summary': '分析生成失败。'}}

            growth_results = {}
            setup_cards = []
            ai_adjustments = {}
            for s in buy_signals[:3]:
                sym = s['symbol']
                # Parse [VERDICT:SYM]
                v_pat = rf'\[VERDICT:{re.escape(sym)}\]\s*(.*?)(?=\[GROWTH:|\[ADJUST:|$|\[VERDICT:|$)'
                v_match = re.search(v_pat, raw, re.DOTALL)
                verdict_text = v_match.group(1).strip() if v_match else ''
                # Extract just the one-liner (first non-empty line after "⭐")
                verdict_oneliner = ''
                for line in verdict_text.splitlines():
                    line = line.strip().lstrip('⭐').strip().lstrip('一句话结论：').strip().lstrip(':').strip()
                    if line:
                        verdict_oneliner = line
                        break

                g_pat = rf'\[GROWTH:{re.escape(sym)}\]\s*(.*?)(?=\[GROWTH:|\[ADJUST:|$|\[SETUP:|$|\[VERDICT:)'
                g_match = re.search(g_pat, raw, re.DOTALL)
                growth_results[sym] = {
                    'symbol': sym,
                    'price': s['price'],
                    'dist_from_52w_low_pct': s['dist_from_52w_low_pct'],
                    'low_52w': s['low_52w'],
                    'take_profit': s['target'],
                    'stop_loss': s['stop'],
                    'verdict': g_match.group(1).strip() if g_match else '增长预期分析暂时不可用',
                }
                t_pat = rf'\[SETUP:{re.escape(sym)}\]\s*(.*?)(?=\[GROWTH:|\[ADJUST:|$|\[SETUP:|$|\[VERDICT:)'
                t_match = re.search(t_pat, raw, re.DOTALL)

                # Parse [ADJUST:SYM] — the AI's downward-only confidence review.
                # This is where external context (news / earnings / macro) gets a
                # bounded, auditable say in the final decision.
                a_pat = rf'\[ADJUST:{re.escape(sym)}\]\s*(.*?)(?=\[GROWTH:|\[SETUP:|\[VERDICT:|\[ADJUST:|$)'
                a_match = re.search(a_pat, raw, re.DOTALL)
                if a_match:
                    block = a_match.group(1)
                    d_m = re.search(r'下调\s*[:：]\s*(\d+)', block)
                    r_m = re.search(r'理由\s*[:：]\s*(.+)', block)
                    delta = max(0, min(30, int(d_m.group(1)))) if d_m else 0
                    if delta > 0:
                        ai_adjustments[sym] = {
                            'delta': -delta,
                            'reason': (r_m.group(1).strip() if r_m else '')[:120],
                        }

                setup_text = (t_match.group(1).strip() if t_match else
                              f"入场约 ${s['price']}，止损 ${s['stop']}。")
                if s['target'] is None:
                    # Prompt rules aren't enough: the model still copied the
                    # 52-week high in as a target. Overwrite the line outright.
                    setup_text = re.sub(r'目标价位\s*[:：].*', '目标价位：移动止盈，不设固定目标',
                                        setup_text)
                # A VERDICT of 观望/不适合 contradicts the card's "入场机会"
                # badge; the template shows these as watch-only.
                watch_only = bool(re.search(r'观望|不适合|不建议', verdict_oneliner))

                setup_cards.append({
                    'symbol': sym,
                    'price': s['price'],
                    'target': s['target'],
                    'stop': s['stop'],
                    'rr': s['rr'],
                    'confidence': s['confidence'],
                    'position_size_pct': s['position_size_pct'],
                    'verdict': verdict_oneliner,
                    'watch_only': watch_only,
                    'text': setup_text,
                })

            if ai_adjustments:
                logger.info(f"AI confidence adjustments: {ai_adjustments}")
            return {
                'growth_analysis': growth_results,
                'trade_setups': {'setups': setup_cards, 'summary': f"{len(setup_cards)} 个交易机会。"},
                'ai_adjustments': ai_adjustments,
            }

        except Exception as e:
            logger.error(f"Error generating growth and setups batch: {str(e)}")
            return {'growth_analysis': {}, 'trade_setups': {'setups': [], 'summary': '交易计划生成失败。'}}

    def _generate_portfolio_analysis(self, portfolio_summary, market_summary, news_summary):
        """Generate portfolio-specific analysis with trend direction for each holding."""
        try:
            all_holdings = portfolio_summary.get('all_holdings', [])
            # Fall back to top/worst if all_holdings not populated
            if not all_holdings:
                all_holdings = (
                    portfolio_summary.get('top_performers', [])
                    + portfolio_summary.get('worst_performers', [])
                )

            def _fmt_holding(h):
                trend = h.get('trend_dir', '未知')
                macd_str = "MACD多头" if h.get('macd_bullish') else "MACD空头"
                return (
                    f"  {h['symbol']}: 仓位{h['weight']:.1f}% 盈亏{h['pnl_pct']:+.1f}% 今日{h['day_chg_pct']:+.2f}% "
                    f"| 趋势={trend} RSI={h['rsi']} SMA50偏离={h['price_vs_sma50']:+.1f}% "
                    f"10日动量={h['momentum_10d']:+.1f}% 20日动量={h['momentum_20d']:+.1f}% {macd_str}"
                )

            holdings_str = '\n'.join(_fmt_holding(h) for h in all_holdings)
            downtrend_holdings = [h for h in all_holdings if h.get('trend_dir') == '下行趋势']

            prompt = (
                '你是专业投资组合经理，为自己的账户解读持仓状况，用简体中文，语言简洁直接。\n'
                '严格只用下方数据，不编造数字。\n'
                '\n'
                f'投资组合概况：总市值=${portfolio_summary.get("total_value", 0):,.0f} '
                f'今日盈亏=${portfolio_summary.get("daily_pnl", 0):,.0f} '
                f'总收益率={portfolio_summary.get("portfolio_return_pct", 0):.2f}% '
                f'持仓={portfolio_summary.get("num_positions", 0)}只\n'
                '\n'
                f'各持仓详情（含趋势方向）：\n{holdings_str}\n'
                '\n'
                + (
                    f'⚠ 下行趋势持仓：{", ".join(h["symbol"] for h in downtrend_holdings)}，'
                    '请在分析中明确提示趋势风险和具体止损参考价。\n'
                    if downtrend_holdings else ''
                )
                + '\n'
                '请按以下结构输出（每节1-2句，简洁，可快速决策）：\n'
                '\n'
                '**① 明确操作建议**\n'
                '哪只持仓最需要关注、为什么，给出关键价位锚点。\n'
                '卖出/减仓指令由系统止损规则单独给出，你不要给减仓比例或清仓建议。\n'
                '\n'
                '**② 今日表现**\n'
                '总结今日涨跌原因，哪只贡献最大，一句话。\n'
                '\n'
                '**③ 趋势风险**\n'
                '哪些持仓趋势支持持有？哪些走弱需警惕？给出具体价位参考。\n'
            )

            return self._call_ai_service(prompt, max_tokens=550)

        except Exception as e:
            logger.error(f"Error generating portfolio analysis: {str(e)}")
            return "持仓分析生成失败，请检查AI服务状态。"
    
    # ------------------------------------------------------------------
    # PRD §9.2 — Compact JSON prompt for narrative generation
    # ------------------------------------------------------------------
    def build_compact_context(
        self,
        market_summary: dict,
        macro_summary: dict,
        news_summary: dict,
        signals_summary: dict,
        risk_metrics: dict | None = None,
        cross_asset: dict | None = None,
    ) -> str:
        """
        Pre-aggregate all structured data into a compact JSON string.
        Passed as the sole user content to the AI, minimising token cost
        while preserving all decision-relevant facts (PRD §9.2).
        """
        buy_detail = signals_summary.get("buy_signals_detail", [])[:2]
        sell_detail = signals_summary.get("sell_signals_detail", [])[:2]

        compact = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "regime": self.market_context.get("market_regime", {}).get("regime", "UNKNOWN"),
            "regime_strength": self.market_context.get("market_regime", {}).get("strength"),
            "vix": self.market_context.get("vix_analysis", {}).get("current"),
            "vix_trend": self.market_context.get("vix_analysis", {}).get("trend"),
            "recommendation": self.market_context.get("trading_recommendation", {}).get("recommendation"),
            "market": {
                "avg_change_pct": round(market_summary.get("avg_day_change", 0), 2),
                "breadth_above_sma50": sum(
                    1 for s in market_summary.get("all_stocks", [])
                    if s.get("price_vs_sma50", 0) > 0
                ),
                "total_stocks": market_summary.get("total_symbols", 0),
                "top_performers": [
                    {"sym": p["symbol"], "chg": p.get("change_pct", 0), "rsi": p.get("rsi")}
                    for p in market_summary.get("top_performers", [])[:3]
                ],
            },
            "signals": {
                "buy": signals_summary.get("buy_count", 0),
                "sell": signals_summary.get("sell_count", 0),
                "hold": signals_summary.get("hold_count", 0),
                "avg_confidence": round(signals_summary.get("avg_confidence", 0), 1),
                "top_buys": [
                    {
                        "sym": s["symbol"],
                        "price": s["price"],
                        "target": s["target"],
                        "stop": s["stop"],
                        "rr": s["rr"],
                        "rsi": s["rsi"],
                        "confidence": s["confidence"],
                    }
                    for s in buy_detail
                ],
                "top_sells": [
                    {"sym": s["symbol"], "rsi": s["rsi"], "stop": s["stop"]}
                    for s in sell_detail
                ],
            },
            "sentiment": round(news_summary.get("avg_sentiment", 0), 2),
            "top_headline": (
                (news_summary.get("top_positive_headlines") or [""])[:1]
                + (news_summary.get("top_negative_headlines") or [""])[:1]
            ),
            "macro": {k: v.get("value") for k, v in (macro_summary or {}).items()},
        }
        if risk_metrics:
            compact["risk"] = {
                "var_pct": risk_metrics.get("var_95", {}).get("var_pct"),
                "var_warning": risk_metrics.get("var_warning", False),
            }
        if cross_asset:
            compact["cross_asset"] = {
                "risk_off": cross_asset.get("risk_off_warning", False),
                "dxy_5d": cross_asset.get("DXY", {}).get("change_5d_pct"),
                "hyg_5d": cross_asset.get("HYG", {}).get("change_5d_pct"),
            }
        # Embed catalyst context so executive_summary leads with the day's core driver
        catalyst = self._format_catalyst_context()
        if catalyst and '无高影响催化剂' not in catalyst:
            compact["catalyst_today"] = catalyst
        return json.dumps(compact, default=str)

    _NARRATIVE_SYSTEM_PROMPT = (
        "你是一位资深买方投资组合经理，正在为普通投资者写盘前简报。请用简体中文回复，语言通俗易懂，RSI/SMA/MACD/VIX等常见指标直接使用，无需括号解释。"
        "根据提供的JSON市场数据，严格按以下格式输出3行，每行以标签开头，每行不超过60字，禁止换行拆分同一标签：\n"
        "【核心驱动】如有catalyst_today则先说事件，再说趋势阶段（牛/熊/震荡）；无催化剂则直接说趋势。\n"
        "【最佳机会】股票代码（若有buy信号）+ 入场价区间 + 止损位 + 一句时机理由；若无信号则写'今日无个股机会'。\n"
        "【最大风险】一个最具体的数字触发条件（VIX升破XX / 价格跌破$XX / XX数据公布）。\n"
        + STRATEGY_GUARDRAIL +
        "不要加免责声明。所有判断用概率性表述（概率偏大/风险偏高/当前环境有利于）；"
        "禁止使用：毫无疑问、一定、绝对、必然；"
        "禁止输出 Markdown 分隔符（---）或孤立标题行（##）。"
    )

    def _generate_extreme_hold_analysis(self, extreme_holds: list, market_summary: dict) -> dict:
        """
        Mini-analysis for HOLD signals with extreme RSI (< RSI_OVERSOLD or > RSI_OVERBOUGHT).
        Translates cooldown/filter reasons into actionable watch conditions.
        One batch API call for all qualifying stocks.
        """
        try:
            regime = self.market_context.get('market_regime', {}).get('regime', 'UNKNOWN')
            vix = self.market_context.get('vix_analysis', {}).get('current', 'N/A')

            stock_blocks = []
            for s in extreme_holds[:5]:
                rsi_label = '极度超卖' if s['rsi'] < Config.RSI_OVERSOLD else '极度超买'
                dist = s.get('dist_from_52w_low_pct')
                dist_str = f"距52周低点{dist:.1f}%" if dist is not None else "距52周低数据缺失"
                stock_blocks.append(
                    f"{s['symbol']}: RSI={s['rsi']:.1f}({rsi_label}) | 价格=${s['price']} | "
                    f"52周低=${s['low_52w']} | 52周高=${s['high_52w']} | {dist_str} | "
                    f"SMA50偏离={s['price_vs_sma50']:+.1f}% | 趋势={s['trend_score']:.2f}"
                )

            blocks_text = '\n'.join(stock_blocks)
            lang_guard = (
                '\n禁止使用毫无疑问/一定/绝对；所有判断用概率性表述；'
                '禁止输出 Markdown 分隔符（---）或孤立标题行（##）。'
            )

            sections = {}
            for s in extreme_holds[:5]:
                sym = s['symbol']
                sections[sym] = (
                    f'{sym}（RSI={s["rsi"]:.1f}，{"极度超卖" if s["rsi"] < 30 else "极度超买"}），'
                    f'当前HOLD，市场趋势={regime}，VIX={vix}\n'
                    f'数据：{blocks_text}\n'
                    f'请用2句话回答（不超过40字/句）：'
                    f'（1）维持HOLD的核心原因，并给出转BUY所需的1个具体价格或指标条件；'
                    f'（2）最大下行风险价位，跌破该位须止损放弃。'
                    + lang_guard
                )

            raw_results = self._call_ai_batch(sections, max_tokens=500, model=self.model_fast)
            return {sym: text for sym, text in raw_results.items() if text}

        except Exception as e:
            logger.error(f"Error generating extreme hold analysis: {e}")
            return {}

    def _generate_sector_summaries(self, market_summary: dict) -> dict:
        """
        Generate a 2-sentence pre-market summary per sector using market_context
        sector_strength data cross-referenced with current signals.
        One batch API call for all sectors.
        """
        try:
            sectors = self.market_context.get('sector_strength', {}).get('sectors', [])
            if not sectors:
                return {}

            all_stocks = market_summary.get('all_stocks', [])
            sig = market_summary.get('signals_summary', {})
            buy_syms = {s['symbol'] for s in sig.get('buy_signals_detail', [])}
            sell_syms = {s['symbol'] for s in sig.get('sell_signals_detail', [])}
            stock_rsi = {s['symbol']: s.get('rsi', 50) for s in all_stocks}

            def _num(v, default):
                """Coerce to a real number, treating None/NaN as the default."""
                try:
                    f = float(v)
                    return default if pd.isna(f) else f
                except (TypeError, ValueError):
                    return default

            sections = {}
            for sector in sectors:
                name = sector.get('name', '')
                if not name:
                    continue
                mapped = sector.get('mapped_stocks') or []

                day_ret = _num(sector.get('day_return'), 0)
                week_ret = _num(sector.get('week_return'), 0)
                score = _num(sector.get('strength_score'), 0)
                rsi_val = _num(sector.get('rsi'), 50)

                # Signals within this sector
                buys_in_sector = [s for s in mapped if s in buy_syms]
                sells_in_sector = [s for s in mapped if s in sell_syms]
                extreme_rsi = [
                    f"{s}(RSI={stock_rsi[s]:.0f})"
                    for s in mapped if s in stock_rsi
                    and (stock_rsi[s] < 30 or stock_rsi[s] > 70)
                ]

                sections[name] = (
                    f'请输出一行，严格按以下格式，禁止换行或多行：\n'
                    f'[方向emoji🟢🟡🔴] {name} {day_ret:+.1f}% | [≤15字描述：最值得关注的信号或风险]\n'
                    f'数据：日涨跌{day_ret:+.2f}% 周涨跌{week_ret:+.2f}% 强度{score:+.1f} RSI={rsi_val:.1f} '
                    f'BUY={", ".join(buys_in_sector) or "无"} '
                    f'规避={", ".join(sells_in_sector) or "无"} '
                    f'极端RSI={", ".join(extreme_rsi) or "无"}'
                )

            results = self._call_ai_batch(sections, max_tokens=700, model=self.model_fast)

            # Fallback: if AI returned all empty strings, build simple summaries from raw data
            if all(not v for v in results.values()):
                logger.warning("Sector AI call returned all-empty — using data-only fallback")
                fallback = {}
                for sector in sectors:
                    name = sector.get('name', '')
                    if not name:
                        continue
                    day_ret = _num(sector.get('day_return'), 0)
                    week_ret = _num(sector.get('week_return'), 0)
                    score = _num(sector.get('strength_score'), 0)
                    rsi_val = _num(sector.get('rsi'), 50)
                    if day_ret > 0.5:
                        emoji = '🟢'
                    elif day_ret < -0.5:
                        emoji = '🔴'
                    else:
                        emoji = '🟡'
                    rsi_note = f'，RSI{rsi_val:.0f}{"超买注意" if rsi_val > 70 else ("超卖关注" if rsi_val < 30 else "中性")}'
                    fallback[name] = f'{emoji} {name} {day_ret:+.1f}% | 周{week_ret:+.1f}% 强度{score:+.1f}{rsi_note}'
                return fallback

            return results

        except Exception as e:
            logger.error(f"Error generating sector summaries: {e}")
            return {}

    def _generate_event_impact_analysis(self, market_headlines: list, watchlist_symbols: list) -> dict:
        """
        Scan today's MARKET-bucket headlines for significant unscheduled events
        (geopolitical, major IPO, policy change, etc.) and assess their impact
        on the watchlist stocks.  Runs as a parallel task alongside other sections.
        """
        if not market_headlines:
            return {'has_events': False, 'impact_text': ''}

        headlines_text = '\n'.join(f'• {hl}' for hl in market_headlines[:10])
        symbols_str = ', '.join(watchlist_symbols[:25])

        prompt = (
            '你是宏观策略分析师，用简体中文分析今日重大事件对股市的影响。\n\n'
            f'今日市场重要新闻（按相关性排序）：\n{headlines_text}\n\n'
            f'观察池股票（只分析此列表内的股票，不要引入其他股票）：{symbols_str}\n\n'
            '请完成以下任务：\n'
            '1. 识别今日最重要的1-3个市场事件（地缘政治变化/重大IPO/政策法规/突发危机等）\n'
            '2. 对每个事件，指出受影响的观察池股票及影响方向和核心逻辑\n'
            '3. 给出简要操作提示\n\n'
            '如果今日无重大事件（新闻均为常规财经报道），请直接输出这一句：\n'
            '「今日无重大突发事件，市场以技术面为主驱动。」\n\n'
            '有事件时的输出格式（每个事件3行，严格遵守）：\n'
            '📌 [事件名称，≤20字]\n'
            '影响：[代码] [利好🟢/利空🔴/中性🟡] [原因，≤25字]；[代码2] [方向] [原因]（最多3只）\n'
            '提示：[操作建议，≤30字]\n\n'
            '禁止：编造新闻中不存在的事件；分析观察池之外的股票；加免责声明；使用---分隔符。'
        )

        raw = self._call_ai_service(prompt, max_tokens=500, temperature=0.3, model=self.model_fast)
        if not raw:
            return {'has_events': False, 'impact_text': ''}

        has_events = '今日无重大突发事件' not in raw
        return {'has_events': has_events, 'impact_text': raw}

    def _generate_executive_summary(self, analysis_sections, news_summary, market_summary=None, macro_summary=None):
        """
        Generate executive summary using compact JSON input (PRD §9.2).
        Max 300 tokens output — sufficient for one clear paragraph.
        """
        try:
            market_summary = market_summary or {}
            macro_summary = macro_summary or {}
            compact_json = self.build_compact_context(
                market_summary=market_summary,
                macro_summary=macro_summary,
                news_summary=news_summary,
                signals_summary=market_summary.get("signals_summary", {}),
                risk_metrics=analysis_sections.get("risk_dashboard"),
            )

            # Use system prompt + compact JSON user message (PRD §9.2 caching).
            # On Claude the system prompt is identical across every run, so mark
            # it cacheable — repeated daily calls reuse it at a discount (#11).
            # DeepSeek caches context automatically and takes a plain string.
            if self.provider == 'claude':
                system = [{
                    "type": "text",
                    "text": self._NARRATIVE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                system = self._NARRATIVE_SYSTEM_PROMPT

            result = self._call_ai_service(
                compact_json, max_tokens=400, temperature=0.3,
                model=self.model_main, system=system,
            )
            return (result or "").strip() or "执行摘要生成失败，请检查AI服务状态。"

        except Exception as e:
            logger.error(f"Error generating executive summary: {e}")
            return "执行摘要生成失败，请检查AI服务状态。"
    
    def _format_macro_data(self, macro_summary):
        """Format macro data for prompts"""
        if not macro_summary:
            return "No macro data available"
        
        formatted_items = []
        for _, data in macro_summary.items():
            change_text = f" (change: {data.get('change_pct', 0):+.1f}%)" if data.get('change_pct') else ""
            formatted_items.append(f"- {data['name']}: {data['value']:.2f} {data.get('units', '')}{change_text}")
        
        return '\n'.join(formatted_items)
    
    def _generate_fallback_analysis(self, market_data, portfolio_snapshot, signals):
        """Generate basic analysis when AI is not available"""
        try:
            analysis = {}
            
            # Basic market overview
            if not market_data.empty:
                avg_change = market_data.get('Day_Change_Pct', pd.Series([0])).mean()
                market_direction = "上涨" if avg_change > 0 else "下跌" if avg_change < 0 else "持平"
                analysis['market_overview'] = f"市场呈{market_direction}态势，均日涨跌幅{avg_change:.2f}%。"

            if not portfolio_snapshot.empty:
                portfolio_attrs = getattr(portfolio_snapshot, 'attrs', {})
                portfolio_summary = portfolio_attrs.get('portfolio_summary', {})
                portfolio_return = portfolio_summary.get('portfolio_return_pct', 0)
                analysis['portfolio_analysis'] = f"持仓{len(portfolio_snapshot)}只，组合总收益率{portfolio_return:.2f}%。"

            if not signals.empty:
                buy_count = len(signals[signals['Signal'] == 'BUY'])
                sell_count = len(signals[signals['Signal'].isin(['SELL', 'AVOID'])])
                analysis['stock_recommendations'] = f"技术分析共生成{buy_count}个买入信号、{sell_count}个回避信号。"

            analysis['risk_assessment'] = "风险评估需要AI分析能力，当前不可用。"
            analysis['executive_summary'] = "AI服务不可用，已使用备用方法完成市场分析。"
            
            return analysis
            
        except Exception as e:
            logger.error(f"Error generating fallback analysis: {str(e)}")
            return {
                'market_overview': '市场概览生成失败。',
                'portfolio_analysis': '持仓分析生成失败。',
                'stock_recommendations': '信号简报生成失败。',
                'risk_assessment': '风险评估生成失败。',
                'executive_summary': '系统错误，分析生成失败。'
            }
    
    def save_analysis(self, analysis, filename=None):
        """Save analysis to file"""
        try:
            if filename is None:
                filename = f"ai_analysis_{datetime.now().strftime('%Y%m%d')}.json"
            
            filepath = f"data/{filename}"
            
            # Add metadata
            model_name = self.model_main or "unknown"
            analysis_with_metadata = {
                'generated_at': datetime.now().isoformat(),
                'model': model_name,
                'ai_service': self.provider or self.ai_service,
                'analysis': analysis
            }
            
            with open(filepath, 'w') as f:
                json.dump(analysis_with_metadata, f, indent=2)
            
            logger.info(f"AI analysis saved to {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Error saving AI analysis: {str(e)}")
            return None