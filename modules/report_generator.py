"""
Report Generation Module
Renders the daily HTML report; delivery is handled by modules/telegram_notifier.py.
"""

import pandas as pd
from jinja2 import Environment
from datetime import datetime, timedelta
import logging
import os
import requests
from config.config import Config

logger = logging.getLogger(__name__)

class ReportGenerator:
    def generate_and_send_report(self, market_data, portfolio_snapshot, signals, ai_analysis, **kwargs):
        """
        Generate the HTML report and save it to REPORT_DIR

        Args:
            market_data (DataFrame): Market data and technical indicators
            portfolio_snapshot (DataFrame): Portfolio snapshot
            signals (DataFrame): Buy/sell/hold signals
            ai_analysis (dict): AI-generated analysis
            **kwargs: Additional context (market_context, premarket_data, event_analysis)
        """
        # Store additional context for report generation
        self.market_context = kwargs.get('market_context', {})
        self.premarket_data = kwargs.get('premarket_data', {})
        self.event_analysis = kwargs.get('event_analysis', {})
        self.stock_suggestions = kwargs.get('stock_suggestions', {})
        self.quant_summary = kwargs.get('quant_summary', {})
        self.index_timing = kwargs.get('index_timing', {})
        self.ai_adjustments = kwargs.get('ai_adjustments', {})
        self.ai_demoted = kwargs.get('ai_demoted', [])
        # Shared model — the same object the WeChat push renders from, so the
        # two channels can no longer disagree on derived facts.
        self.report_model = kwargs.get('report_model', {})
        self.position_advice = kwargs.get('position_advice', {})
        try:
            logger.info("Generating HTML report...")
            
            # Generate HTML report
            html_content = self._generate_html_report(
                market_data, portfolio_snapshot, signals, ai_analysis
            )
            
            # Save report to file
            report_filename = f"daily_report_{datetime.now().strftime('%Y%m%d')}.html"
            os.makedirs(Config.REPORT_DIR, exist_ok=True)  # Ensure directory exists
            report_filepath = f"{Config.REPORT_DIR}/{report_filename}"
            
            with open(report_filepath, 'w', encoding='utf-8') as f:
                f.write(html_content)

            logger.info(f"Report saved to {report_filepath}")
            
            # Save CSV backups
            self._save_csv_backups(market_data, portfolio_snapshot, signals)
            
            return report_filepath
            
        except Exception as e:
            logger.error(f"Error generating and sending report: {str(e)}")
            return None
    
    def _generate_html_report(self, market_data, portfolio_snapshot, signals, ai_analysis):
        """Generate HTML report using Jinja2 template with bilingual support"""
        try:
            # Sanitise DataFrames: replace NaN/inf in price columns with None
            # so Jinja2 can use {% if val is not none %} guards cleanly.
            signals = self._sanitise_numeric(signals, [
                'Target_Price', 'Stop_Loss', 'Support_Level',
                'Resistance_Level', 'Risk_Reward_Ratio',
            ])
            market_data = self._sanitise_numeric(market_data, [
                'Current_Price', 'Market_Cap', '52_Week_Low', '52_Week_High',
                'Day_Change_Pct', 'Price_Change_Pct', 'RSI', 'PE_Ratio',
            ])

            # Format AI analysis text for better display
            formatted_ai_analysis = self._format_analysis_text(ai_analysis)

            self.formatted_ai_analysis = formatted_ai_analysis

            # Prepare data for template
            template_data = {
                'report_date': datetime.now().strftime('%Y-%m-%d'),
                'report_time': datetime.now().strftime('%H:%M:%S'),
                'market_data': market_data,
                'portfolio_snapshot': portfolio_snapshot,
                'signals': signals,
                'ai_analysis': formatted_ai_analysis,
                'portfolio_summary': self._get_portfolio_summary(portfolio_snapshot),
                'signal_summary': self._get_signal_summary(signals),
                'market_summary': self._get_market_summary(market_data),
                'trade_setups': formatted_ai_analysis.get('trade_setups', {}).get('setups', []),
                'sector_summaries': formatted_ai_analysis.get('sector_summaries', {}),
                'extreme_hold_analysis': formatted_ai_analysis.get('extreme_hold_analysis', {}),
                'stock_suggestions': self.stock_suggestions,
                'quant_summary': self.quant_summary,
                'market_context': self.market_context,
                'index_timing': self.index_timing,
                'ai_adjustments': self.ai_adjustments,
                'ai_demoted': self.ai_demoted,
                'model': self.report_model,
                'position_advice': self.position_advice,
            }

            # HTML template
            html_template = self._get_html_template()
            # Autoescape: signal reasons contain text like "SMA10<SMA50", which
            # the browser otherwise parses as a tag and truncates the row.
            template = Environment(autoescape=True).from_string(html_template)

            # Generate HTML
            html_content = template.render(**template_data)

            return html_content

        except Exception as e:
            logger.error(f"Error generating HTML report: {str(e)}")
            return self._generate_fallback_html(market_data, portfolio_snapshot, signals)

    @staticmethod
    def _sanitise_numeric(df: pd.DataFrame, cols: list) -> pd.DataFrame:
        """Replace NaN / inf in the given columns with None for clean Jinja2 rendering.

        Uses a list comprehension into an object-dtype Series so pandas cannot
        silently coerce None back to float NaN (which happens with apply on
        float64 columns and is what causes the 'nanx' display bug).
        """
        import math
        if df.empty:
            return df
        result = df.copy()
        for col in cols:
            if col not in result.columns:
                continue
            def _safe(v):
                try:
                    f = float(v)
                    return None if (math.isnan(f) or math.isinf(f)) else f
                except (TypeError, ValueError):
                    return None
            # Build object-dtype Series directly so None stays None (not NaN)
            result[col] = pd.Series(
                [_safe(v) for v in result[col]],
                index=result.index,
                dtype=object,
            )
        return result

    def _format_analysis_text(self, ai_analysis):
        """Format AI analysis text: clean LLM artifacts then add numbered-list line breaks."""
        if not ai_analysis:
            return ai_analysis

        import re
        formatted_analysis = {}
        for key, text in ai_analysis.items():
            if isinstance(text, str):
                # Strip markdown artifacts first
                text = re.sub(r'(?m)^\s*-{3,}\s*$', '', text)
                text = re.sub(r'(?m)^\s*#{1,6}\s*$', '', text)
                text = re.sub(r'>[ \t]*[⚠️免责][^\n]*', '', text)
                # Add line breaks before numbered points
                text = re.sub(r'(\d+\.\s)', r'\n\1', text)
                # Collapse excess blank lines
                text = re.sub(r'\n{3,}', '\n\n', text)
                formatted_analysis[key] = text.strip()
            else:
                formatted_analysis[key] = text

        return formatted_analysis
    
    def _get_html_template(self):
        """Load HTML template from templates/daily_report.html.j2."""
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'daily_report.html.j2',
        )
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()

    
    def _get_portfolio_summary(self, portfolio_snapshot):
        """Extract portfolio summary for template"""
        if portfolio_snapshot.empty:
            return None
        
        try:
            portfolio_attrs = getattr(portfolio_snapshot, 'attrs', {})
            portfolio_summary = portfolio_attrs.get('portfolio_summary', {})
            
            return {
                'total_value': portfolio_summary.get('total_value', 0),
                'total_return_pct': portfolio_summary.get('portfolio_return_pct', 0),
                'daily_pnl': portfolio_summary.get('total_daily_pnl', 0),
                'num_positions': len(portfolio_snapshot)
            }
        except Exception:
            return None
    
    def _get_signal_summary(self, signals):
        """Extract signal summary for template"""
        if signals.empty:
            return {'buy_count': 0, 'sell_count': 0, 'hold_count': 0}
        
        try:
            return {
                'buy_count': len(signals[signals['Signal'] == 'BUY']),
                'sell_count': len(signals[signals['Signal'] == 'SELL']),
                'hold_count': len(signals[signals['Signal'] == 'HOLD'])
            }
        except Exception:
            return {'buy_count': 0, 'sell_count': 0, 'hold_count': 0}
    
    def _get_market_summary(self, market_data):
        """Extract market summary for template"""
        if market_data.empty:
            return None
        
        try:
            avg_change = market_data.get('Day_Change_Pct', pd.Series([0])).mean()
            return {
                'total_symbols': len(market_data),
                'avg_change': avg_change
            }
        except Exception:
            return None
    
    def _save_csv_backups(self, market_data, portfolio_snapshot, signals):
        """Save CSV backups of all data"""
        try:
            date_str = datetime.now().strftime('%Y%m%d')

            # Save market data
            if not market_data.empty:
                market_data.to_csv(f'{Config.REPORT_DIR}/market_data_{date_str}.csv', index=False)

            # Save portfolio snapshot
            if not portfolio_snapshot.empty:
                portfolio_snapshot.to_csv(f'{Config.REPORT_DIR}/portfolio_snapshot_{date_str}.csv', index=False)

            # Save signals
            if not signals.empty:
                signals.to_csv(f'{Config.REPORT_DIR}/signals_{date_str}.csv', index=False)

            logger.info("CSV backups saved successfully")

        except Exception as e:
            logger.error(f"Error saving CSV backups: {str(e)}")

    def _generate_fallback_html(self, market_data, portfolio_snapshot, signals):
        """Generate basic HTML report when template fails"""
        try:
            html = f"""
            <html>
            <head>
                <title>每日股票分析报告 - {datetime.now().strftime('%Y-%m-%d')}</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    table {{ border-collapse: collapse; width: 100%; }}
                    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                    th {{ background-color: #f2f2f2; }}
                    .positive {{ color: green; }}
                    .negative {{ color: red; }}
                </style>
            </head>
            <body>
                <h1>Daily Stock Analysis Report</h1>
                <p>Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                
                <h2>Market Data</h2>
                {market_data.to_html(classes='data', table_id='market_table') if not market_data.empty else '<p>No market data available</p>'}
                
                <h2>Trading Signals</h2>
                {signals.to_html(classes='data', table_id='signals_table') if not signals.empty else '<p>No signals available</p>'}
                
                <h2>Portfolio Holdings</h2>
                {portfolio_snapshot.to_html(classes='data', table_id='portfolio_table') if not portfolio_snapshot.empty else '<p>No portfolio data available</p>'}
            </body>
            </html>
            """
            
            return html
            
        except Exception as e:
            logger.error(f"Error generating fallback HTML: {str(e)}")
            return f"<html><body><h1>Report Generation Error</h1><p>{str(e)}</p></body></html>"
    
    def send_slack_notification(self, webhook_url, message):
        """Send notification to Slack"""
        try:
            payload = {
                'text': message,
                'username': 'Stock Analysis Bot',
                'icon_emoji': ':chart_with_upwards_trend:'
            }
            
            response = requests.post(webhook_url, json=payload, timeout=10)
            
            if response.status_code == 200:
                logger.info("Slack notification sent successfully")
                return True
            else:
                logger.error(f"Slack notification failed: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending Slack notification: {str(e)}")
            return False
    
    def send_telegram_notification(self, bot_token, chat_id, message):
        """Send notification to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'Markdown'
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                logger.info("Telegram notification sent successfully")
                return True
            else:
                logger.error(f"Telegram notification failed: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending Telegram notification: {str(e)}")
            return False
    
    def generate_summary_notification(self, signals, portfolio_summary, ai_analysis):
        """Generate summary notification for Slack/Telegram"""
        try:
            if not signals.empty:
                buy_count = len(signals[signals['Signal'] == 'BUY'])
                sell_count = len(signals[signals['Signal'] == 'SELL'])
                top_buy = signals[signals['Signal'] == 'BUY'].head(1)['Symbol'].iloc[0] if buy_count > 0 else 'None'
                top_sell = signals[signals['Signal'] == 'SELL'].head(1)['Symbol'].iloc[0] if sell_count > 0 else 'None'
            else:
                buy_count = sell_count = 0
                top_buy = top_sell = 'None'
            
            portfolio_value = portfolio_summary.get('total_value', 0) if portfolio_summary else 0
            portfolio_return = portfolio_summary.get('portfolio_return_pct', 0) if portfolio_summary else 0
            
            executive_summary = ai_analysis.get('executive_summary', 'Analysis not available')
            
            message = f"""
📈 *Daily Stock Analysis Summary*
📅 Date: {datetime.now().strftime('%Y-%m-%d')}

💼 *Portfolio*:
• Total Value: ${portfolio_value:,.0f}
• Total Return: {portfolio_return:.2f}%

📊 *Signals*:
• Buy Signals: {buy_count} (Top: {top_buy})
• Sell Signals: {sell_count} (Top: {top_sell})

🤖 *AI Analysis*:
{executive_summary}

📧 Full report sent via email.
            """
            
            return message.strip()
            
        except Exception as e:
            logger.error(f"Error generating summary notification: {str(e)}")
            return f"Daily analysis completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"