#!/usr/bin/env python3
"""
Stock Analysis System - Main Entry Point
Enhanced version with market context, premarket analysis, event risk, and improved signals.

Executes the daily pre-market analysis workflow with:
- Market regime detection (SPY/QQQ trends, VIX)
- Sector strength analysis
- Premarket gap and volume analysis
- Event risk evaluation (earnings, macro events)
- Enhanced news sentiment (VADER + keyword scoring)
- Do-Not-Trade filtering
- Risk-Reward evaluation
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules.data_scraper import DataScraper
from modules.financial_data_scraper import FinancialDataScraper
from modules.news_scraper import NewsScraper
from modules.portfolio_manager import PortfolioManager
from modules.signal_generator import SignalGenerator
from modules.ai_analyzer import AIAnalyzer
from modules.report_generator import ReportGenerator
from modules.market_context import MarketContext
from modules.premarket_analyzer import PremarketAnalyzer
from modules.event_risk_engine import EventRiskEngine
from modules.stock_suggester import StockSuggester
from modules.notification_service import NotificationService
from modules.advice_tracker import AdviceTracker
from modules.position_advisor import PositionAdvisor, summarize as summarize_position_advice
from modules.report_model import build_report_model
from modules import data_health, report_validator, telegram_notifier
from modules.cross_asset_monitor import CrossAssetMonitor
from modules.options_sentiment import OptionsSentiment
from modules.macro_event_alerts import MacroEventAlerts
from modules.signal_tracker import SignalTracker
from config.config import Config
import logging
from datetime import datetime

# Setup logging
os.makedirs(Config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{Config.LOG_DIR}/analysis_{datetime.now().strftime("%Y%m%d")}.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def update_progress(step, total, description):
    """Update global progress for web server tracking"""
    try:
        # Write to progress file for web server to read
        import json
        progress_file = '.progress.json'
        progress = {
            'status': 'running' if step < total else 'completed',
            'step': step,
            'total_steps': total,
            'current_step': description,
            'start_time': datetime.now().isoformat(),
            'estimated_time': 420
        }
        with open(progress_file, 'w') as f:
            json.dump(progress, f)

        # Also try to update in-memory if web server is loaded
        try:
            import web_server
            web_server.refresh_progress.update(progress)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Could not update progress: {e}")


def _wait_for_network(timeout=120, check_host="8.8.8.8", check_port=53):
    """
    Block until a TCP connection to check_host:check_port succeeds, or until
    timeout seconds elapse.  Called at startup so that when macOS wakes from
    sleep and launchd fires the missed job, we don't start hitting APIs before
    the network stack is fully ready.
    """
    import socket, time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((check_host, check_port), timeout=3):
                logger.info("Network ready.")
                return True
        except OSError:
            remaining = int(deadline - time.time())
            logger.info(f"Waiting for network... ({remaining}s remaining)")
            time.sleep(5)
    logger.warning(f"Network not available after {timeout}s — continuing anyway.")
    return False


def _save_snapshot(report_data: dict) -> None:
    """Keep the exact data each report was rendered from, so a wrong number
    can be traced and a fix re-checked against the same inputs."""
    try:
        import json
        snap_dir = os.path.join(Config.DATA_DIR, 'snapshots')
        os.makedirs(snap_dir, exist_ok=True)
        path = os.path.join(snap_dir, f"report_data_{datetime.now().strftime('%Y%m%d')}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, ensure_ascii=False, indent=1, default=str)
        logger.info(f"  Snapshot saved: {path}")
    except Exception as e:
        logger.warning(f"  Snapshot save failed: {e}")


def main():
    """Execute the enhanced daily pre-market analysis workflow"""
    try:
        logger.info("=" * 60)
        logger.info("Starting Enhanced Daily Pre-Market Analysis...")
        logger.info("=" * 60)
        data_health.reset()

        # Wait for network before touching any external API.
        # Needed when launchd fires a missed job right after the Mac wakes from sleep.
        _wait_for_network(timeout=120)

        # Update progress immediately at start
        update_progress(0, 11, "Initializing modules and connecting to IBKR...")

        # ── Watchlist: ~50 liquid US stocks across all 11 GICS sectors ──────
        # Mega-cap tech
        tech_mega    = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'SPCX']
        # Mid-cap tech + semiconductors
        tech_mid     = ['AMD', 'CRM', 'ADBE', 'ORCL', 'QCOM', 'AVGO']
        # Healthcare
        healthcare   = ['UNH', 'LLY', 'JNJ', 'ABBV', 'MRK', 'PFE']
        # Financials
        financials   = ['JPM', 'BAC', 'GS', 'V', 'MA', 'BRK-B']
        # Energy
        energy       = ['XOM', 'CVX', 'COP']
        # Consumer Discretionary
        cons_disc    = ['MCD', 'NKE', 'HD', 'SBUX']
        # Consumer Staples
        cons_staples = ['WMT', 'COST', 'PG', 'KO']
        # Industrials
        industrials  = ['CAT', 'HON', 'GE', 'UPS', 'BA']
        # Communication Services
        comm         = ['DIS', 'NFLX', 'VZ']
        # Broad-market ETFs (benchmark + volatility proxy)
        broad_etfs   = ['SPY', 'QQQ', 'IWM']
        # Sector ETFs (sector rotation signals)
        sector_etfs  = ['XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLP', 'XLY']

        symbols = (
            tech_mega + tech_mid + healthcare + financials + energy
            + cons_disc + cons_staples + industrials + comm
            + broad_etfs + sector_etfs
        )
        # Deduplicate while preserving order (AMZN, GOOGL appear in both lists)
        seen = set()
        symbols = [s for s in symbols if not (s in seen or seen.add(s))]
        logger.info(f"Watchlist: {len(symbols)} symbols across 11 sectors")

        # Initialize all modules
        logger.info("Initializing modules...")
        data_scraper = DataScraper()
        financial_scraper = FinancialDataScraper()
        news_scraper = NewsScraper()
        market_context = MarketContext()
        premarket_analyzer = PremarketAnalyzer()
        event_risk_engine = EventRiskEngine()

        # Portfolio data: opt-in IBKR connection (set USE_IBKR=true to enable).
        # Default is to skip the Gateway attempt and go straight to portfolio.csv,
        # which avoids a TCP timeout on every run when Gateway is not open.
        portfolio_manager = None
        if os.getenv("USE_IBKR", "").strip().lower() == "true":
            try:
                from modules.ibkr_connector import IBKRConnector

                logger.info("USE_IBKR=true — attempting IBKR connection for portfolio data...")
                connector = IBKRConnector()

                if connector.connect(port=Config.IBKR_PAPER_PORT):
                    logger.info("Connected to IBKR - retrieving portfolio positions...")
                    positions = connector.get_portfolio_positions()

                    if not positions.empty:
                        connector.convert_to_portfolio_format(positions)
                        portfolio_manager = PortfolioManager('data/portfolio_ibkr.csv')
                        logger.info(f"Using IBKR portfolio with {len(positions)} positions")
                    else:
                        logger.info("IBKR account has no positions - using portfolio.csv")
                        portfolio_manager = PortfolioManager()

                    connector.disconnect()
                else:
                    logger.warning("Could not connect to IBKR - using portfolio.csv")
                    portfolio_manager = PortfolioManager()

            except Exception as e:
                logger.warning(f"IBKR connection failed: {str(e)} - using portfolio.csv")
                portfolio_manager = PortfolioManager()
        else:
            logger.info("IBKR disabled (set USE_IBKR=true to enable) — using portfolio.csv")
            portfolio_manager = PortfolioManager()

        signal_generator = SignalGenerator()
        ai_analyzer = AIAnalyzer()
        report_generator = ReportGenerator()
        stock_suggester = StockSuggester()
        notification_service = NotificationService()
        cross_asset_monitor = CrossAssetMonitor()
        options_sentiment = OptionsSentiment()
        macro_event_alerts = MacroEventAlerts()
        signal_tracker = SignalTracker()

        # ============================================================
        # STEP 1: Get Market Context (NEW)
        # ============================================================
        update_progress(1, 11, "Analyzing market context (SPY, QQQ, VIX, sectors)...")
        logger.info("-" * 40)
        logger.info("Step 1: Analyzing market context...")
        logger.info("-" * 40)

        market_context_data = market_context.get_full_market_context()

        # Log market context summary
        regime = market_context_data.get('market_regime', {})
        vix = market_context_data.get('vix_analysis', {})
        recommendation = market_context_data.get('trading_recommendation', {})

        logger.info(f"  Market Regime: {regime.get('regime', 'N/A')} (Strength: {regime.get('strength', 'N/A')}%)")
        logger.info(f"  VIX: {vix.get('current', 'N/A')} ({vix.get('regime', 'N/A')})")
        logger.info(f"  Trading Recommendation: {recommendation.get('recommendation', 'N/A')}")
        logger.info(f"  Market Score: {recommendation.get('market_score', 'N/A')}")
        # Check the regime before anything downstream leans on it.
        report_validator.check_market(regime, vix)

        # Log sector strength
        sector_data = market_context_data.get('sector_strength', {})
        if sector_data.get('top_sectors'):
            logger.info(f"  Top Sectors: {', '.join(sector_data.get('top_sectors', []))}")
            logger.info(f"  Bottom Sectors: {', '.join(sector_data.get('bottom_sectors', []))}")

        # ============================================================
        # STEP 2: Get Premarket Data (NEW)
        # ============================================================
        update_progress(2, 11, "Analyzing premarket gaps and volume patterns...")
        logger.info("-" * 40)
        logger.info("Step 2: Analyzing premarket data...")
        logger.info("-" * 40)

        premarket_data = premarket_analyzer.analyze_premarket(symbols)

        # Log premarket summary
        pm_summary = premarket_data.get('_market_summary', {})
        logger.info(f"  Gap Up Count: {pm_summary.get('gap_up_count', 0)}")
        logger.info(f"  Gap Down Count: {pm_summary.get('gap_down_count', 0)}")
        logger.info(f"  High Volume Count: {pm_summary.get('high_volume_count', 0)}")
        logger.info(f"  Market Bias: {pm_summary.get('market_bias', 'N/A')}")

        # ============================================================
        # STEP 3: Get Event Risk Analysis (NEW)
        # ============================================================
        update_progress(3, 11, "Checking earnings calendars and macro events...")
        logger.info("-" * 40)
        logger.info("Step 3: Analyzing event risk...")
        logger.info("-" * 40)

        event_analysis = event_risk_engine.get_full_event_analysis(symbols)

        logger.info(f"  Market Risk Level: {event_analysis.get('market_risk_level', 'N/A')}")
        logger.info(f"  Signal Adjustment: {event_analysis.get('signal_adjustment', 1.0):.2f}")

        macro_events = event_analysis.get('macro_events', {})
        if macro_events.get('has_high_impact_today'):
            logger.warning("  HIGH-IMPACT MACRO EVENT TODAY - Signals will be reduced")

        # Log stocks with earnings
        for symbol, data in event_analysis.get('stocks', {}).items():
            if data.get('earnings', {}).get('is_this_week'):
                logger.info(f"  {symbol}: Earnings in {data['earnings'].get('days_until', 'N/A')} days")

        # ============================================================
        # STEP 3b: Cross-Asset Monitor
        # ============================================================
        logger.info("-" * 40)
        logger.info("Step 3b: Cross-asset monitor (DXY/Copper/Oil/JPY/HYG)...")
        logger.info("-" * 40)
        try:
            cross_asset_data = cross_asset_monitor.cross_asset_snapshot()
            if cross_asset_data.get('risk_off_warning'):
                logger.warning(f"  RISK-OFF: {cross_asset_data['risk_off_reason']}")
            else:
                logger.info("  Cross-asset: no risk-off signal")
        except Exception as e:
            logger.warning(f"  Cross-asset monitor failed: {e}")
            cross_asset_data = {}

        # ============================================================
        # STEP 3c: Options Sentiment (P/C ratio + VIX term structure)
        # ============================================================
        logger.info("-" * 40)
        logger.info("Step 3c: Options sentiment (P/C ratio, VIX term structure)...")
        logger.info("-" * 40)
        try:
            options_data = options_sentiment.get_options_sentiment()
            pc = options_data.get('put_call', {})
            vts = options_data.get('vix_term_structure', {})
            logger.info(f"  P/C Ratio: {pc.get('pc_ratio', 'N/A')} → {pc.get('signal', 'N/A')}")
            logger.info(f"  VIX Term Structure: {vts.get('signal', 'N/A')}")
        except Exception as e:
            logger.warning(f"  Options sentiment failed: {e}")
            options_data = {}

        # ============================================================
        # STEP 3d: Macro Event 3-Day Alerts
        # ============================================================
        logger.info("-" * 40)
        logger.info("Step 3d: Macro event calendar alerts...")
        logger.info("-" * 40)
        try:
            vix_now = vix.get('current', 20.0)
            macro_alerts = macro_event_alerts.get_full_alert_summary(symbols, vix=vix_now)
            if macro_alerts.get('has_alerts'):
                for alert in macro_alerts.get('upcoming_alerts', []):
                    logger.warning(
                        f"  UPCOMING: {alert['event']} in {alert['days_until']} days ({alert['date']})"
                    )
            if macro_alerts.get('reduction_warning'):
                logger.warning(f"  {macro_alerts['reduction_warning']}")
        except Exception as e:
            logger.warning(f"  Macro event alerts failed: {e}")
            macro_alerts = {}

        # ============================================================
        # STEP 4: Scrape Market Data and Technical Indicators
        # ============================================================
        update_progress(4, 11, "Fetching stock prices and technical indicators...")
        logger.info("-" * 40)
        logger.info("Step 4: Scraping market data and technical indicators...")
        logger.info("-" * 40)

        market_data = data_scraper.get_market_data(symbols)

        # ============================================================
        # STEP 5: Scrape Financial Reports and Macro Data
        # ============================================================
        update_progress(5, 11, "Collecting financial reports and economic data...")
        logger.info("-" * 40)
        logger.info("Step 5: Scraping financial reports and macro data...")
        logger.info("-" * 40)

        financial_data = financial_scraper.get_financial_data(symbols)
        macro_data = financial_scraper.get_macro_data()

        # Merge the fundamental fields the value-trap / 规避 filter needs into
        # market_data. Without these, _is_value_trap() can never fire (only the
        # market-cap check has data) and AVOID stays permanently 0. Only bring in
        # columns market_data doesn't already carry, to avoid _x/_y collisions.
        if not financial_data.empty and not market_data.empty:
            fund_cols = ['Revenue_Growth', 'Earnings_Growth', 'Debt_to_Equity']
            have = [c for c in fund_cols if c in financial_data.columns
                    and c not in market_data.columns]
            if have:
                market_data = market_data.merge(
                    financial_data[['Symbol'] + have], on='Symbol', how='left'
                )
                logger.info(f"Merged fundamentals into market_data: {have}")

        # ============================================================
        # STEP 6: Scrape News with Enhanced Sentiment
        # ============================================================
        update_progress(6, 11, "Scraping news articles and analyzing sentiment...")
        logger.info("-" * 40)
        logger.info("Step 6: Scraping news with enhanced sentiment analysis...")
        logger.info("-" * 40)

        # Restrict paid NewsAPI calls to holdings only — the full watchlist gets
        # Yahoo Finance + RSS (both free/unlimited) so all 54 symbols still have
        # news coverage.  This keeps daily NewsAPI usage ≈ len(holdings) instead
        # of 54, well within the 200-request/day quota.
        holdings_symbols = (
            portfolio_manager.portfolio_data['Symbol'].unique().tolist()
            if portfolio_manager is not None and not portfolio_manager.portfolio_data.empty
            else []
        )
        logger.info(
            f"NewsAPI budget: {len(holdings_symbols)} holdings will use NewsAPI; "
            f"remaining {len(symbols) - len(holdings_symbols)} symbols use Yahoo+RSS only"
        )

        news_data = news_scraper.get_news_for_symbols(symbols, newsapi_symbols=holdings_symbols)

        # Derive sentiment summary from the already-fetched DataFrame — zero extra API calls
        news_sentiment = news_scraper.get_market_sentiment_summary(symbols, news_df=news_data)

        # Log sentiment summary
        for symbol, sentiment in news_sentiment.items():
            avg_sent = sentiment.get('Average_Sentiment', 0)
            article_count = sentiment.get('Total_Articles', 0)
            if article_count > 0:
                logger.info(f"  {symbol}: Sentiment {avg_sent:.3f} ({article_count} articles)")

        # ============================================================
        # STEP 7: Calculate Portfolio Snapshot
        # ============================================================
        update_progress(7, 11, "Updating portfolio positions and P&L...")
        logger.info("-" * 40)
        logger.info("Step 7: Calculating portfolio snapshot...")
        logger.info("-" * 40)

        portfolio_snapshot = portfolio_manager.get_portfolio_snapshot(market_data)

        # ============================================================
        # STEP 8: Generate Enhanced Buy/Sell Signals
        # ============================================================
        update_progress(8, 11, "Calculating BUY/SELL/HOLD signals with AI scoring...")
        logger.info("-" * 40)
        logger.info("Step 8: Generating enhanced buy/sell signals...")
        logger.info("-" * 40)

        # Set context data for signal generator
        signal_generator.set_market_context(market_context_data)
        signal_generator.set_premarket_data(premarket_data)
        signal_generator.set_event_analysis(event_analysis)
        signal_generator.set_news_sentiment(news_data)

        # Generate signals
        signals = signal_generator.generate_signals(market_data, portfolio_snapshot)

        # Index market-timing status (Path C): when to buy SPY/QQQ
        index_timing = signal_generator.get_index_timing_status()
        for _sym, _st in index_timing.items():
            logger.info(f"  大盘择时 {_sym}: [{_st.get('state')}] {_st.get('detail')}")

        # Core+Satellite: today's stock-picking gate and the value watchlist.
        # Path A no longer buys anything, so its output is a watch list — the
        # names that would become buyable if they turned.
        satellite_gate = signal_generator.get_satellite_gate()
        value_watchlist = signal_generator.get_watchlist()
        if satellite_gate.get('blocked'):
            logger.warning(f"  卫星仓(选股)停开新仓: {satellite_gate.get('reason')}")
        logger.info(f"  观察名单(价值路径，不建仓): {len(value_watchlist)} 只")

        # Log signal summary
        signal_summary = signal_generator.get_signal_summary(signals)

        logger.info(f"  Total Signals: {signal_summary.get('total_signals', 0)}")
        logger.info(f"  BUY: {signal_summary.get('buy_signals', 0)}")
        logger.info(f"  SELL: {signal_summary.get('sell_signals', 0)}")
        logger.info(f"  HOLD: {signal_summary.get('hold_signals', 0)}")
        logger.info(f"  Do-Not-Trade: {signal_summary.get('do_not_trade_count', 0)}")
        logger.info(f"  Avg Confidence: {signal_summary.get('avg_confidence', 0):.1f}%")

        if signal_summary.get('top_buy_picks'):
            logger.info(f"  Top Buy Picks: {', '.join(signal_summary['top_buy_picks'])}")
        if signal_summary.get('top_sell_picks'):
            logger.info(f"  Top Sell Picks: {', '.join(signal_summary['top_sell_picks'])}")

        # NOTE: signal logging moved to after Step 10 — the AI confidence
        # review can demote a BUY to HOLD, and the log must record the final
        # decision or the win-rate stats would blame trades never advised.

        # ============================================================
        # STEP 8b: Paper Trading Execution (if enabled)
        # ============================================================
        if Config.ENABLE_PAPER_TRADING and not signals.empty:
            logger.info("-" * 40)
            logger.info("Step 8b: Executing paper trades via IBKR...")
            logger.info("-" * 40)
            try:
                from modules.ibkr_manager import IBKRManager
                from modules.order_executor import OrderExecutor

                ibkr_mgr = IBKRManager()
                conn_status = ibkr_mgr.connect_all()

                if conn_status.get('paper'):
                    executor = OrderExecutor(ibkr_mgr)
                    logger.info("  IBKR paper account connected — submitting orders")
                else:
                    # Paper account not available — fall back to dry-run logging
                    executor = OrderExecutor(None)
                    logger.warning("  IBKR paper not connected — running in DRY-RUN mode")

                # Build signal list expected by OrderExecutor
                exec_signals = []
                for _, row in signals.iterrows():
                    exec_signals.append({
                        'ticker': row.get('Symbol', ''),
                        'signal': row.get('Signal', ''),
                        'score': float(row.get('Adjusted_Score', 0)),
                        'do_not_trade': bool(row.get('Do_Not_Trade', False)),
                        'current_price': float(row.get('Current_Price', 0) or 0),
                        'stop_loss': float(row['Stop_Loss']) if row.get('Stop_Loss') else None,
                        'target_price': float(row['Target_Price']) if row.get('Target_Price') else None,
                    })

                open_tickers = (
                    portfolio_snapshot['Symbol'].tolist()
                    if not portfolio_snapshot.empty else []
                )
                paper_val = ibkr_mgr.get_paper_portfolio_value() if conn_status.get('paper') else 100_000.0

                trade_results = executor.run(
                    signals=exec_signals,
                    market_data={
                        'vix': float(vix.get('current', 0) or 0),
                        'open_positions': open_tickers,
                        'portfolio_value': paper_val,
                    },
                )
                logger.info(f"  Paper trades executed: {len(trade_results)}")
                for t in trade_results:
                    logger.info(
                        f"    {t['action']} {t['shares']} {t['ticker']} "
                        f"@ ${t['fill_price']:.2f} | SL=${t['stop_loss']} TP=${t['take_profit']}"
                    )

                ibkr_mgr.disconnect_all()

            except Exception as e:
                logger.error(f"  Paper trading error: {e}")
        elif Config.ENABLE_PAPER_TRADING and signals.empty:
            logger.info("  Paper trading enabled but no signals generated — skipping")

        # ============================================================
        # STEP 9: Get New Stock Opportunity Suggestions
        # ============================================================
        update_progress(9, 11, "Scanning 100+ stocks for new opportunities...")
        logger.info("-" * 40)
        logger.info("Step 9: Finding new stock opportunities...")
        logger.info("-" * 40)

        # Get suggestions for stocks not in current watchlist
        stock_suggestions = stock_suggester.get_suggestions(
            existing_symbols=symbols,
            max_suggestions=5,
            scan_type='all'
        )

        if 'error' not in stock_suggestions:
            logger.info(f"  Scanned {stock_suggestions['scan_summary'].get('total_scanned', 0)} stocks")
            logger.info(f"  Found {len(stock_suggestions.get('momentum_leaders', []))} momentum leaders")
            logger.info(f"  Found {len(stock_suggestions.get('volume_breakouts', []))} volume breakouts")
            logger.info(f"  Found {len(stock_suggestions.get('oversold_bounces', []))} oversold bounce candidates")

        # ============================================================
        # STEP 10: Generate AI Analysis
        # ============================================================
        update_progress(10, 11, "Generating AI insights (3 focused calls)...")
        logger.info("-" * 40)
        logger.info("Step 10: Generating AI analysis...")
        logger.info("-" * 40)

        # Historical calibration line for the AI signal review (needs outcome
        # data from post-market runs; empty string until enough accumulates).
        try:
            signal_history_line = signal_tracker.get_history_line()
        except Exception as e:
            logger.warning(f"  Signal history stats unavailable: {e}")
            signal_history_line = ''

        # Add market context and stock suggestions to AI analysis
        ai_analysis = ai_analyzer.generate_analysis(
            market_data, financial_data, macro_data, news_data, portfolio_snapshot, signals,
            market_context=market_context_data,
            stock_suggestions=stock_suggestions,
            event_analysis=event_analysis,
            macro_alerts=macro_alerts,
            cross_asset_data=cross_asset_data,
            options_data=options_data,
            signal_history_line=signal_history_line,
        )

        # Strip sizing advice the rules forbid and flag misquoted numbers,
        # before any channel renders the AI text.
        report_validator.sanitize_ai(ai_analysis, vix.get('current'), regime.get('spy_price'))

        # ── Apply AI confidence review (downward-only) ──────────────────
        # The AI reviews each BUY against news / events / fundamentals and may
        # deduct up to 30 confidence points. Rules keep the final say: a BUY
        # only demotes to HOLD if the adjusted confidence falls below the
        # unified floor. Must run BEFORE the report, the advice account, and
        # the signal log so all three see the same final decision.
        ai_adjustments = ai_analysis.get('ai_confidence_adjustments') or {}
        demoted_syms = []
        if ai_adjustments and not signals.empty:
            for sym, adj in ai_adjustments.items():
                try:
                    delta = int(adj.get('delta', 0))
                    reason = str(adj.get('reason', '')).strip()
                    if delta >= 0:
                        continue  # downward-only by design
                    mask = (signals['Symbol'] == sym) & (signals['Signal'] == 'BUY')
                    if not mask.any():
                        continue
                    idx = signals.index[mask][0]
                    old_conf = float(signals.at[idx, 'Confidence'] or 0)
                    new_conf = max(0, round(old_conf + delta))
                    signals.at[idx, 'Confidence'] = new_conf
                    signals.at[idx, 'Adjusted_Score'] = new_conf / 100
                    signals.at[idx, 'Reasoning'] = (
                        str(signals.at[idx, 'Reasoning'] or '')
                        + f" ｜AI外部因子复核：置信度{old_conf:.0f}→{new_conf}（{reason}）"
                    )
                    if new_conf < Config.MIN_BUY_CONFIDENCE:
                        signals.at[idx, 'Signal'] = 'HOLD'
                        demoted_syms.append(sym)
                        logger.info(
                            f"  AI复核降级 {sym}: BUY→HOLD "
                            f"(置信度{old_conf:.0f}→{new_conf} < {Config.MIN_BUY_CONFIDENCE}: {reason})"
                        )
                    else:
                        logger.info(f"  AI复核下调 {sym}: 置信度{old_conf:.0f}→{new_conf} ({reason})")
                except Exception as e:
                    logger.warning(f"  AI adjustment for {sym} failed: {e}")
        if demoted_syms:
            setups = (ai_analysis.get('trade_setups') or {}).get('setups', [])
            ai_analysis['trade_setups']['setups'] = [
                c for c in setups if c.get('symbol') not in demoted_syms
            ]
            for sym in demoted_syms:
                (ai_analysis.get('growth_analysis') or {}).pop(sym, None)

        # Cooldown is committed only now, against the FINAL signal set. Writing
        # it inside generate_signals() meant a BUY the AI review then demoted to
        # HOLD still burned its 5-day cooldown — the name was blocked the next
        # morning despite never having been recommended.
        signal_generator.commit_cooldown(signals)

        # ============================================================
        # STEP 10b: Position Advisor — exit management for real holdings
        # ============================================================
        # The only module that answers "should I sell what I already own?".
        # Every other signal path answers "what should I buy?".
        logger.info("-" * 40)
        logger.info("Step 10b: Evaluating exits for real holdings...")
        logger.info("-" * 40)

        position_advice = {}
        holding_stops = {}
        try:
            position_advisor = PositionAdvisor()
            advices = position_advisor.evaluate(
                portfolio_snapshot=portfolio_snapshot,
                market_data=market_data,
                signals=signals,
                event_analysis=event_analysis,
                market_regime=regime.get('regime', 'SIDEWAYS'),
            )
            position_advice = summarize_position_advice(advices)
            holding_stops = position_advisor.stop_map()
            for a in advices:
                log = logger.warning if a['action'] == 'EXIT' else logger.info
                log(
                    f"  持仓建议 {a['symbol']}: [{a['action']}] "
                    f"止损${a['stop']} | {a['reason_text']}"
                )
            logger.info(
                f"  持仓操作汇总 — 卖出:{position_advice['exit_count']} "
                f"减仓:{position_advice['trim_count']} "
                f"持有:{position_advice['hold_count']}"
            )
        except Exception as e:
            logger.error(f"  Position advisor failed: {e}", exc_info=True)
            position_advice = {
                'advices': [], 'exits': [], 'trims': [],
                'exit_count': 0, 'trim_count': 0, 'hold_count': 0,
                'has_action': False,
            }

        # Log final signals (post AI review) to SQLite for performance tracking
        if not signals.empty:
            try:
                market_state_label = regime.get('regime', '')
                vix_now = vix.get('current')
                for _, row in signals.iterrows():
                    _sym = row.get('Symbol', '')
                    _adj = ai_adjustments.get(_sym) or {}
                    signal_tracker.log_signal(
                        ticker=_sym,
                        signal=row.get('Signal', ''),
                        score=row.get('Adjusted_Score', row.get('Composite_Score', 0)),
                        component_scores={
                            'trend': row.get('Trend_Score'),
                            'momentum': row.get('Momentum_Score'),
                            'rsi': row.get('RSI'),
                            'sentiment': row.get('Sentiment_Score'),
                        },
                        market_state=market_state_label,
                        vix=vix_now,
                        entry_price=row.get('Buy_Price') or row.get('Current_Price'),
                        confidence=row.get('Confidence'),
                        signal_path=row.get('Signal_Path'),
                        # AI review delta — demoted rows (BUY→HOLD) keep it so
                        # stats can price what the AI review saved/cost.
                        ai_delta=_adj.get('delta'),
                    )
                logger.info(f"  Logged {len(signals)} signals to signal_log.db")
            except Exception as e:
                logger.warning(f"  Signal tracking failed: {e}")

        # ============================================================
        # STEP 11: Generate and Send Report
        # ============================================================
        update_progress(11, 11, "Creating HTML report with visualizations...")
        logger.info("-" * 40)
        logger.info("Step 11: Generating and sending report...")
        logger.info("-" * 40)

        # ============================================================
        # STEP 12: Advice-Following Virtual Account ("if I followed the picks")
        # ============================================================
        update_progress(11, 11, "Updating advice-following P&L account...")
        logger.info("-" * 40)
        logger.info("Step 12: Updating advice-following account...")
        logger.info("-" * 40)

        try:
            advice_tracker = AdviceTracker(initial_capital=Config.TOTAL_ACCOUNT_VALUE)
            quant_summary = advice_tracker.run(
                signals_df=signals,
                market_data=market_data,
                market_regime=regime.get('regime', 'SIDEWAYS'),
                holding_stops=holding_stops,
            )
            if "error" not in quant_summary:
                logger.info(
                    f"  Advice NAV: ${quant_summary['nav']:,.2f} | "
                    f"Return: {quant_summary['total_return_pct']:+.2f}% | "
                    f"Realized: ${quant_summary['realized_pnl']:+.2f} | "
                    f"Alpha vs SPY: {quant_summary['alpha_pct']:+.2f}%"
                )
                _sl = quant_summary.get('sleeves') or {}
                _core, _sat = _sl.get('core') or {}, _sl.get('satellite') or {}
                if _core:
                    logger.info(
                        f"  核心仓: ${_core.get('value', 0):,.2f} "
                        f"({_core.get('pct', 0):.1f}%/{_core.get('target_pct', 0):.0f}%) "
                        f"{'✓达标' if _core.get('on_target') else '✗待补足'}"
                    )
                if _sat:
                    _alpha = _sat.get('alpha_vs_spy_pct')
                    logger.info(
                        f"  卫星仓: ${_sat.get('value', 0):,.2f} "
                        f"({_sat.get('pct', 0):.1f}%/{_sat.get('target_pct', 0):.0f}%) "
                        f"持仓{_sat.get('open', 0)}/{_sat.get('max_positions', 0)} "
                        f"[{_sat.get('state')}] 判定={_sat.get('verdict')} "
                        f"超额={_alpha if _alpha is not None else 'n/a'} "
                        f"已验证{_sat.get('trades_closed', 0)}笔/还需{_sat.get('trades_to_prove', 0)}笔"
                        + ("  [纸面]" if _sat.get('paper_only') else "")
                    )
                    if _sat.get('kill_reason'):
                        logger.warning(f"  🛑 {_sat['kill_reason']}")
            else:
                logger.warning(f"  Advice account error: {quant_summary['error']}")
        except Exception as e:
            logger.error(f"  Advice account failed: {e}")
            quant_summary = {"error": str(e)}

        # NOTE: the HTML report is generated further down, once the shared
        # report model exists — the template and the Telegram push both render
        # from that model, so it has to be built before either one runs.

        # ============================================================
        # Send Completion Notification
        # ============================================================
        logger.info("-" * 40)
        logger.info("Sending completion notification...")
        logger.info("-" * 40)

        # Send desktop notification
        portfolio_summary_data = None
        if not portfolio_snapshot.empty:
            portfolio_attrs = getattr(portfolio_snapshot, 'attrs', {})
            portfolio_summary_data = portfolio_attrs.get('portfolio_summary', {})

        notification_service.send_analysis_complete_notification(
            signal_summary,
            portfolio_summary_data
        )

        report_date = datetime.now().strftime('%Y%m%d')
        report_path = f"{Config.REPORT_DIR}/daily_report_{report_date}.html"

        market_state = regime.get('regime', 'RANGE')
        market_state_zh = {'BULL': '牛市', 'BEAR': '熊市'}.get(market_state, '震荡')

        # Build price list from market_data
        prices = []
        if not market_data.empty:
            for _, row in market_data.iterrows():
                prices.append({
                    'ticker': row.get('Symbol', ''),
                    'price': row.get('Current_Price') or row.get('Close', 0),
                    'change_pct': row.get('Day_Change_Pct') or row.get('Price_Change_Pct'),
                })

        # All signals with scores
        all_signals = []
        top_signals = []
        if not signals.empty:
            for _, row in signals.iterrows():
                sig_score = row.get('Adjusted_Score', row.get('Composite_Score', 0))
                # Normalize legacy 'AVOID' → 'SELL' so the report / Telegram 规避
                # buckets (which key on 'SELL') never silently drop a warning.
                sig_val = 'SELL' if row.get('Signal') == 'AVOID' else row.get('Signal', '')
                all_signals.append({
                    'ticker': row.get('Symbol', ''),
                    'signal': sig_val,
                    'score': sig_score,
                    'rsi': row.get('RSI'),
                    'price': row.get('Current_Price') or row.get('Close'),
                    'day_chg': row.get('Day_Change_Pct') or row.get('Price_Change_Pct'),
                    'volume_ratio': row.get('Volume_Ratio'),
                    'target_price': row.get('Target_Price') if row.get('Target_Price') not in (None, 'N/A') else None,
                    'stop_loss': row.get('Stop_Loss') if row.get('Stop_Loss') not in (None, 'N/A') else None,
                    'risk_reward': row.get('Risk_Reward_Ratio') if row.get('Risk_Reward_Ratio') not in (None, 'N/A') else None,
                    'low_52w': row.get('52W_Low'),
                    'high_52w': row.get('52W_High'),
                    'dist_from_52w_low_pct': row.get('Dist_From_52W_Low_Pct'),
                    'pe_ratio': row.get('PE_Ratio'),
                    'revenue_growth': row.get('Revenue_Growth'),
                    'reason': str(row.get('Reasoning', ''))[:150],
                })
                if sig_val in ('BUY', 'SELL'):
                    top_signals.append({
                        'ticker': row.get('Symbol', ''),
                        'signal': sig_val,
                        'score': sig_score,
                        'rsi': row.get('RSI'),
                        'price': row.get('Current_Price') or row.get('Close'),
                        'reason': str(row.get('Reasoning', ''))[:100],
                    })

        # AI analysis is already in Chinese; use formatted version (LLM artifacts stripped)
        zh_ai = getattr(report_generator, 'formatted_ai_analysis', {}) or {}
        if not zh_ai and isinstance(ai_analysis, dict):
            zh_ai = ai_analysis
        ai_summary = zh_ai.get('executive_summary', '') or ''

        # Portfolio PnL
        port_pnl = None
        port_val = None
        if portfolio_summary_data:
            port_pnl = portfolio_summary_data.get('total_daily_pnl')
            port_val = portfolio_summary_data.get('total_value')

        # Macro event warning (combine event_analysis + 3-day alerts)
        macro_warn = ''
        if event_analysis.get('macro_events', {}).get('has_high_impact_today'):
            macro_warn = '今日有重大宏观事件，信号已降权'
        if macro_alerts.get('reduction_warning'):
            macro_warn = (macro_warn + ' | ' if macro_warn else '') + macro_alerts['reduction_warning']
        if cross_asset_data.get('risk_off_warning'):
            macro_warn = (macro_warn + ' | ' if macro_warn else '') + f"避险信号: {cross_asset_data.get('risk_off_reason','')}"

        # Avg daily change across watchlist
        avg_daily_change = None
        if prices:
            chg_vals = [p['change_pct'] for p in prices if p.get('change_pct') is not None]
            if chg_vals:
                avg_daily_change = sum(chg_vals) / len(chg_vals)

        # Holdings list from portfolio snapshot
        holdings = []
        if not portfolio_snapshot.empty:
            for _, row in portfolio_snapshot.iterrows():
                holdings.append({
                    'ticker': row.get('Symbol', ''),
                    'shares': row.get('Shares', 0),
                    'price': row.get('Current_Price', 0),
                    'value': row.get('Position_Value', 0),
                    'pnl_pct': row.get('Unrealized_PnL_Pct'),
                    # PortfolioManager writes 'Weight'; reading 'Weight_Pct'
                    # silently defaulted every holding to 0%, which disabled
                    # every concentration warning downstream.
                    'weight': row.get('Weight', 0),
                    'day_chg': row.get('Day_Change_Pct'),
                })

        # Momentum leaders, oversold bounces, and volume breakouts from stock_suggestions
        momentum_leaders = []
        oversold_bounces = []
        volume_breakouts = []
        if 'error' not in stock_suggestions:
            for m in stock_suggestions.get('momentum_leaders', [])[:4]:
                momentum_leaders.append({
                    'ticker': m.get('symbol', ''),
                    'name': m.get('name', ''),
                    'sector': m.get('sector', ''),
                    'price': m.get('price', 0),
                    'momentum': m.get('momentum_20d'),
                    'rsi': m.get('rsi'),
                })
            for o in stock_suggestions.get('oversold_bounces', [])[:4]:
                oversold_bounces.append({
                    'ticker': o.get('symbol', ''),
                    'name': o.get('name', ''),
                    'sector': o.get('sector', ''),
                    'price': o.get('price', 0),
                    'rsi': o.get('rsi'),
                    'position_52w': o.get('pct_from_low_52w'),
                })
            for v in stock_suggestions.get('volume_breakouts', [])[:5]:
                volume_breakouts.append({
                    'ticker': v.get('symbol', ''),
                    'name': v.get('name', ''),
                    'sector': v.get('sector', ''),
                    'price': v.get('price', 0),
                    'volume_ratio': v.get('volume_ratio'),
                    'momentum': v.get('momentum_20d'),
                })

        # ── Shared report model ────────────────────────────────────────
        # Both the HTML report and the Telegram push render from this one
        # object. They used to derive risk level, action checklist and TL;DR
        # separately from different inputs, which is how they drifted apart.
        held_tickers_set = {h['ticker'] for h in holdings}
        _wl = [s for s in all_signals if s['ticker'] not in held_tickers_set]

        report_validator.check_positions(holdings, position_advice)
        report_validator.check_signals(all_signals)
        report_validator.check_sectors(sector_data.get('sectors', []), prices)
        report_validator.check_satellite((quant_summary or {}).get('sleeves'))
        report_model = build_report_model(
            market_regime=market_state,
            vix=vix.get('current'),
            vix_regime=vix.get('regime', ''),
            recommendation=recommendation.get('recommendation', ''),
            market_score=recommendation.get('market_score'),
            buy_count=len([s for s in _wl if s['signal'] == 'BUY']),
            hold_count=len([s for s in _wl if s['signal'] in ('HOLD', 'FILTERED_LOW_VOLUME')]),
            sell_count=len([s for s in _wl if s['signal'] == 'SELL']),
            buy_tickers=[s['ticker'] for s in _wl if s['signal'] == 'BUY'],
            sell_tickers=[s['ticker'] for s in _wl if s['signal'] == 'SELL'],
            extreme_rsi_tickers=[
                s['ticker'] for s in all_signals
                if (s.get('rsi') or 50) < 30 or (s.get('rsi') or 50) > 75
            ],
            macro_warning=macro_warn,
            cross_asset=cross_asset_data,
            position_advice=position_advice,
            sector_detail=sector_data.get('sectors', []),
            total_symbols=len(all_signals),
            # Real-account state. `port_val` is the market value of holdings
            # only, so when the account is flat it is None — the difference
            # against TOTAL_ACCOUNT_VALUE is what's sitting in cash.
            account_value=Config.TOTAL_ACCOUNT_VALUE,
            account_initial=Config.ACCOUNT_INITIAL_CAPITAL,
            holdings_value=port_val,
            # Core+Satellite state: the sleeve split from the advice ledger,
            # today's stock-picking gate, and the value-path watchlist.
            sleeves=(quant_summary or {}).get('sleeves'),
            satellite_gate=satellite_gate,
            watchlist=value_watchlist,
            satellite_monthly=(quant_summary or {}).get('satellite_monthly'),
        )
        # Every check has run by now; both channels show the same verdict.
        report_model['health'] = data_health.summary()

        report_data = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'model': report_model,
            'position_advice': position_advice,
            'market_state': market_state,
            'market_state_zh': market_state_zh,
            'vix': vix.get('current'),
            'vix_regime': vix.get('regime', ''),
            'recommendation': recommendation.get('recommendation', ''),
            'market_score': recommendation.get('market_score'),
            'avg_daily_change': avg_daily_change,
            'top_sectors': sector_data.get('top_sectors', []),
            'bottom_sectors': sector_data.get('bottom_sectors', []),
            'sector_detail': sector_data.get('sectors', []),
            'prices': prices,
            'all_signals': all_signals,
            'top_signals': top_signals[:3],
            # Chinese AI analysis sections (from report_generator translation)
            'ai_summary': ai_summary,
            'ai_market_overview': zh_ai.get('market_overview', ''),
            'ai_stock_recommendations': zh_ai.get('stock_recommendations', ''),
            'ai_portfolio_analysis': zh_ai.get('portfolio_analysis', ''),
            'top_risk': zh_ai.get('risk_assessment') or event_analysis.get('macro_events', {}).get('description', ''),
            'macro_event_warning': macro_warn,
            'growth_analysis': ai_analysis.get('growth_analysis', {}) if isinstance(ai_analysis, dict) else {},
            'sector_summaries': zh_ai.get('sector_summaries') or (ai_analysis.get('sector_summaries', {}) if isinstance(ai_analysis, dict) else {}),
            'trade_setups': ai_analysis.get('trade_setups', {}) if isinstance(ai_analysis, dict) else {},
            'extreme_hold_analysis': ai_analysis.get('extreme_hold_analysis', {}) if isinstance(ai_analysis, dict) else {},
            'portfolio_daily_pnl': port_pnl,
            'portfolio_total_return': portfolio_summary_data.get('portfolio_return_pct') if portfolio_summary_data else None,
            'portfolio_value': port_val,
            'holdings': holdings,
            'momentum_leaders': momentum_leaders,
            'oversold_bounces': oversold_bounces,
            'volume_breakouts': volume_breakouts,
            'quant_summary': quant_summary,
            'index_timing': index_timing,
            'ai_adjustments': ai_adjustments,
            'ai_demoted': demoted_syms,
            'cross_asset': cross_asset_data,
            'options_sentiment': options_data,
            'event_impact': ai_analysis.get('event_impact', {}) if isinstance(ai_analysis, dict) else {},
        }

        # Generate the HTML report from the same model the Telegram push uses.
        report_generator.generate_and_send_report(
            market_data, portfolio_snapshot, signals, ai_analysis,
            market_context=market_context_data,
            premarket_data=premarket_data,
            event_analysis=event_analysis,
            stock_suggestions=stock_suggestions,
            quant_summary=quant_summary,
            index_timing=index_timing,
            ai_adjustments=ai_adjustments,
            ai_demoted=demoted_syms,
            report_model=report_model,
            position_advice=position_advice,
        )

        _save_snapshot(report_data)
        telegram_notifier.notify_daily(report_data, report_path)

        logger.info("=" * 60)
        logger.info("Enhanced daily pre-market analysis completed successfully!")
        logger.info("=" * 60)

        # Print final summary to console
        print("\n" + "=" * 60)
        print("ANALYSIS COMPLETE")
        print("=" * 60)
        print(f"\nMarket Regime: {regime.get('regime', 'N/A')} | VIX: {vix.get('current', 'N/A')} ({vix.get('regime', 'N/A')})")
        print(f"Trading Recommendation: {recommendation.get('recommendation', 'N/A')}")
        print(f"\nSignals Generated: {signal_summary.get('total_signals', 0)}")
        print(f"  - BUY: {signal_summary.get('buy_signals', 0)}")
        print(f"  - SELL: {signal_summary.get('sell_signals', 0)}")
        print(f"  - HOLD: {signal_summary.get('hold_signals', 0)}")
        print(f"  - Avoid Trading: {signal_summary.get('do_not_trade_count', 0)}")

        print(f"\nHolding Actions: {position_advice.get('exit_count', 0)} SELL, "
              f"{position_advice.get('trim_count', 0)} TRIM, "
              f"{position_advice.get('hold_count', 0)} HOLD")
        for _a in position_advice.get('exits', []) + position_advice.get('trims', []):
            print(f"  - {_a['action']} {_a['symbol']}: {_a['reason_text']}")

        if signal_summary.get('top_buy_picks'):
            print(f"\nTop Buy Picks: {', '.join(signal_summary['top_buy_picks'])}")

        print("\nReport saved to: Stock Market Daily Report/")
        print("=" * 60 + "\n")

    except Exception as e:
        logger.error(f"Error during analysis: {str(e)}")
        try:
            import html
            import traceback
            last_lines = '\n'.join(traceback.format_exc().strip().splitlines()[-6:])
            telegram_notifier.send_message(
                f'<b>🚨 今日分析未完成</b>\n错误：{html.escape(str(e))}\n'
                f'<pre>{html.escape(last_lines)}</pre>\n请检查日志排查原因。'
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
