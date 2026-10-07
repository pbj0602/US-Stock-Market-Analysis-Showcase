"""
Event Risk Engine Module
Tracks earnings, dividends, splits, and macroeconomic events that impact trading signals.
Adjusts signal confidence based on upcoming events.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import requests
from config.config import Config
import pytz

logger = logging.getLogger(__name__)


class EventRiskEngine:
    """
    Tracks and evaluates event risk including:
    - Earnings announcements
    - Dividend dates (ex-date, record date, payment date)
    - Stock splits
    - Macroeconomic events (CPI, NFP, FOMC)
    """

    def __init__(self):
        self.us_tz = pytz.timezone('US/Eastern')
        self.fred_api_key = Config.FRED_API_KEY

        # Major macro events with typical impact levels
        self.macro_events = {
            'FOMC': {'impact': 'HIGH', 'volatility_multiplier': 1.5},
            'CPI': {'impact': 'HIGH', 'volatility_multiplier': 1.4},
            'NFP': {'impact': 'HIGH', 'volatility_multiplier': 1.3},  # Non-Farm Payrolls
            'PPI': {'impact': 'MEDIUM', 'volatility_multiplier': 1.2},
            'GDP': {'impact': 'MEDIUM', 'volatility_multiplier': 1.2},
            'RETAIL_SALES': {'impact': 'MEDIUM', 'volatility_multiplier': 1.15},
            'JOBLESS_CLAIMS': {'impact': 'LOW', 'volatility_multiplier': 1.05},
            'PMI': {'impact': 'MEDIUM', 'volatility_multiplier': 1.1},
            'CONSUMER_CONFIDENCE': {'impact': 'LOW', 'volatility_multiplier': 1.05}
        }

        # Signal reduction factors based on event proximity
        self.proximity_factors = {
            'same_day': 0.4,      # 60% reduction
            'next_day': 0.6,     # 40% reduction
            'this_week': 0.8,    # 20% reduction
            'next_week': 0.9,    # 10% reduction
            'none': 1.0          # No reduction
        }

    def get_full_event_analysis(self, symbols):
        """
        Get comprehensive event risk analysis for all symbols.

        Args:
            symbols (list): List of stock symbols

        Returns:
            dict: Complete event analysis
        """
        try:
            analysis = {
                'timestamp': datetime.now(self.us_tz).strftime('%Y-%m-%d %H:%M:%S %Z'),
                'stocks': {},
                'macro_events': self.get_macro_calendar(),
                'market_risk_level': 'NORMAL',
                'signal_adjustment': 1.0
            }

            # Analyze each stock
            for symbol in symbols:
                try:
                    stock_events = self._analyze_stock_events(symbol)
                    if stock_events:
                        analysis['stocks'][symbol] = stock_events
                except Exception as e:
                    logger.warning(f"Error analyzing events for {symbol}: {str(e)}")
                    continue

            # Calculate overall market risk
            analysis['market_risk_level'], analysis['signal_adjustment'] = self._calculate_market_risk(analysis)

            logger.info(f"Completed event analysis for {len(analysis['stocks'])} symbols")
            return analysis

        except Exception as e:
            logger.error(f"Error in event analysis: {str(e)}")
            return self._get_default_analysis()

    def _analyze_stock_events(self, symbol):
        """
        Analyze events for a single stock.

        Args:
            symbol (str): Stock symbol

        Returns:
            dict: Stock event analysis
        """
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            today = datetime.now(self.us_tz).date()

            events = {
                'symbol': symbol,
                'earnings': self._get_earnings_info(ticker, info, today),
                'dividends': self._get_dividend_info(ticker, info, today),
                'splits': self._get_split_info(ticker, today),
                'has_imminent_event': False,
                'risk_level': 'NORMAL',
                'signal_adjustment': 1.0
            }

            # Determine overall risk and adjustment
            events['risk_level'], events['signal_adjustment'] = self._calculate_stock_event_risk(events)
            events['has_imminent_event'] = events['signal_adjustment'] < 0.9

            return events

        except Exception as e:
            logger.warning(f"Error analyzing stock events for {symbol}: {str(e)}")
            return None

    def _get_earnings_info(self, ticker, info, today):
        """
        Get earnings event information.

        Args:
            ticker: yfinance Ticker object
            info (dict): Ticker info
            today (date): Today's date

        Returns:
            dict: Earnings information
        """
        try:
            earnings_info = {
                'next_date': None,
                'days_until': None,
                'is_today': False,
                'is_this_week': False,
                'timing': None,  # 'BMO' (Before Market Open) or 'AMC' (After Market Close)
                'estimate': None,
                'actual': None
            }

            # ETFs have no earnings; asking Yahoo for their calendar just 404s.
            if info.get('quoteType') in ('ETF', 'INDEX'):
                return earnings_info

            # Try to get earnings date from calendar
            try:
                calendar = ticker.calendar
                if calendar is not None and not calendar.empty:
                    # Handle different calendar formats
                    if isinstance(calendar, pd.DataFrame):
                        if 'Earnings Date' in calendar.columns:
                            earnings_date = calendar['Earnings Date'].iloc[0]
                        elif 0 in calendar.columns:
                            earnings_date = calendar.iloc[0, 0]
                        else:
                            earnings_date = None
                    else:
                        earnings_date = calendar.get('Earnings Date', [None])[0] if isinstance(calendar, dict) else None

                    if earnings_date:
                        if isinstance(earnings_date, str):
                            earnings_date = pd.to_datetime(earnings_date).date()
                        elif hasattr(earnings_date, 'date'):
                            earnings_date = earnings_date.date()

                        earnings_info['next_date'] = str(earnings_date)
                        days_until = (earnings_date - today).days
                        earnings_info['days_until'] = days_until
                        earnings_info['is_today'] = days_until == 0
                        earnings_info['is_this_week'] = 0 <= days_until <= 5
            except Exception as e:
                logger.debug(f"Could not get earnings calendar: {str(e)}")

            # Try to get from info as fallback
            if earnings_info['next_date'] is None:
                earnings_timestamp = info.get('earningsTimestamp')
                if earnings_timestamp:
                    try:
                        earnings_date = datetime.fromtimestamp(earnings_timestamp, tz=self.us_tz).date()
                        earnings_info['next_date'] = str(earnings_date)
                        days_until = (earnings_date - today).days
                        earnings_info['days_until'] = days_until
                        earnings_info['is_today'] = days_until == 0
                        earnings_info['is_this_week'] = 0 <= days_until <= 5
                    except:
                        pass

            # Get earnings timing if available
            earnings_timing = info.get('earningsTimestampStart', info.get('earningsTimestampEnd'))
            if earnings_timing:
                try:
                    timing_dt = datetime.fromtimestamp(earnings_timing, tz=self.us_tz)
                    if timing_dt.hour < 10:
                        earnings_info['timing'] = 'BMO'  # Before Market Open
                    else:
                        earnings_info['timing'] = 'AMC'  # After Market Close
                except:
                    pass

            # Get estimates
            earnings_info['estimate'] = info.get('earningsQuarterlyGrowth')

            return earnings_info

        except Exception as e:
            logger.warning(f"Error getting earnings info: {str(e)}")
            return {
                'next_date': None,
                'days_until': None,
                'is_today': False,
                'is_this_week': False,
                'timing': None,
                'estimate': None,
                'actual': None
            }

    def _get_dividend_info(self, ticker, info, today):
        """
        Get dividend event information.

        Args:
            ticker: yfinance Ticker object
            info (dict): Ticker info
            today (date): Today's date

        Returns:
            dict: Dividend information
        """
        try:
            dividend_info = {
                'ex_date': None,
                'days_until_ex': None,
                'is_ex_date_today': False,
                'dividend_rate': info.get('dividendRate', 0),
                'dividend_yield': info.get('dividendYield', 0),
                'payout_ratio': info.get('payoutRatio', 0)
            }

            # Try to get ex-dividend date
            ex_date = info.get('exDividendDate')
            if ex_date:
                try:
                    if isinstance(ex_date, (int, float)):
                        ex_date = datetime.fromtimestamp(ex_date, tz=self.us_tz).date()
                    elif isinstance(ex_date, str):
                        ex_date = pd.to_datetime(ex_date).date()
                    elif hasattr(ex_date, 'date'):
                        ex_date = ex_date.date()

                    dividend_info['ex_date'] = str(ex_date)
                    days_until = (ex_date - today).days
                    dividend_info['days_until_ex'] = days_until
                    dividend_info['is_ex_date_today'] = days_until == 0
                except Exception as e:
                    logger.debug(f"Could not parse ex-dividend date: {str(e)}")

            # Format dividend yield as percentage
            if dividend_info['dividend_yield']:
                dividend_info['dividend_yield'] = round(dividend_info['dividend_yield'] * 100, 2)

            return dividend_info

        except Exception as e:
            logger.warning(f"Error getting dividend info: {str(e)}")
            return {
                'ex_date': None,
                'days_until_ex': None,
                'is_ex_date_today': False,
                'dividend_rate': 0,
                'dividend_yield': 0,
                'payout_ratio': 0
            }

    def _get_split_info(self, ticker, today):
        """
        Get stock split information.

        Args:
            ticker: yfinance Ticker object
            today (date): Today's date

        Returns:
            dict: Split information
        """
        try:
            split_info = {
                'has_recent_split': False,
                'last_split_date': None,
                'last_split_ratio': None
            }

            # Get split history
            splits = ticker.splits
            if splits is not None and len(splits) > 0:
                last_split = splits.iloc[-1]
                last_split_date = splits.index[-1].date()

                # Check if split was within last 30 days
                days_since = (today - last_split_date).days

                split_info['last_split_date'] = str(last_split_date)
                split_info['last_split_ratio'] = f"{last_split}:1"
                split_info['has_recent_split'] = days_since <= 30

            return split_info

        except Exception as e:
            logger.warning(f"Error getting split info: {str(e)}")
            return {
                'has_recent_split': False,
                'last_split_date': None,
                'last_split_ratio': None
            }

    def get_macro_calendar(self):
        """
        Get upcoming macroeconomic events.
        Note: This uses hardcoded common event patterns. For real-time data,
        integrate with an economic calendar API.

        Returns:
            dict: Macro event calendar
        """
        try:
            today = datetime.now(self.us_tz)
            events = []

            # Check for common event patterns
            # FOMC meetings typically on Wednesday
            # NFP typically first Friday of month
            # CPI typically mid-month

            day_of_week = today.weekday()
            day_of_month = today.day

            # Check if today might be a high-impact day
            is_potential_fomc = day_of_week == 2  # Wednesday
            is_potential_nfp = day_of_week == 4 and day_of_month <= 7  # First Friday
            is_potential_cpi = 10 <= day_of_month <= 15  # Mid-month

            if is_potential_fomc:
                events.append({
                    'event': 'FOMC',
                    'date': today.strftime('%Y-%m-%d'),
                    'impact': 'HIGH',
                    'description': 'Potential FOMC announcement day (verify with economic calendar)',
                    'is_confirmed': False
                })

            if is_potential_nfp:
                events.append({
                    'event': 'NFP',
                    'date': today.strftime('%Y-%m-%d'),
                    'impact': 'HIGH',
                    'description': 'Potential Non-Farm Payrolls release (first Friday)',
                    'is_confirmed': False
                })

            if is_potential_cpi:
                events.append({
                    'event': 'CPI',
                    'date': today.strftime('%Y-%m-%d'),
                    'impact': 'HIGH',
                    'description': 'Potential CPI release window (verify with economic calendar)',
                    'is_confirmed': False
                })

            # Weekly jobless claims (Thursday)
            if day_of_week == 3:
                events.append({
                    'event': 'JOBLESS_CLAIMS',
                    'date': today.strftime('%Y-%m-%d'),
                    'impact': 'LOW',
                    'description': 'Weekly Initial Jobless Claims',
                    'is_confirmed': True
                })

            # Try to fetch from FRED if API key is available
            if self.fred_api_key:
                fred_events = self._fetch_fred_releases()
                events.extend(fred_events)

            # Determine if today is a high-risk day
            high_impact_today = any(e.get('impact') == 'HIGH' and e.get('date') == today.strftime('%Y-%m-%d') for e in events)

            return {
                'events': events,
                'has_high_impact_today': high_impact_today,
                'today': today.strftime('%Y-%m-%d'),
                'recommendation': 'REDUCE_EXPOSURE' if high_impact_today else 'NORMAL_TRADING'
            }

        except Exception as e:
            logger.error(f"Error getting macro calendar: {str(e)}")
            return {
                'events': [],
                'has_high_impact_today': False,
                'today': datetime.now(self.us_tz).strftime('%Y-%m-%d'),
                'recommendation': 'NORMAL_TRADING'
            }

    def _fetch_fred_releases(self):
        """
        Fetch upcoming releases from FRED API.

        Returns:
            list: FRED release events
        """
        try:
            if not self.fred_api_key:
                return []

            # FRED releases endpoint
            url = "https://api.stlouisfed.org/fred/releases/dates"
            params = {
                'api_key': self.fred_api_key,
                'file_type': 'json',
                'limit': 20,
                'offset': 0,
                'order_by': 'release_date',
                'sort_order': 'asc'
            }

            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return []

            data = response.json()
            events = []

            for release in data.get('release_dates', []):
                release_name = release.get('release_name', '').upper()

                # Map release to our event types
                impact = 'LOW'
                event_type = 'OTHER'

                if 'CPI' in release_name or 'CONSUMER PRICE' in release_name:
                    impact = 'HIGH'
                    event_type = 'CPI'
                elif 'EMPLOYMENT' in release_name or 'PAYROLL' in release_name:
                    impact = 'HIGH'
                    event_type = 'NFP'
                elif 'GDP' in release_name:
                    impact = 'MEDIUM'
                    event_type = 'GDP'
                elif 'PPI' in release_name or 'PRODUCER PRICE' in release_name:
                    impact = 'MEDIUM'
                    event_type = 'PPI'
                elif 'RETAIL' in release_name:
                    impact = 'MEDIUM'
                    event_type = 'RETAIL_SALES'

                events.append({
                    'event': event_type,
                    'date': release.get('date', ''),
                    'impact': impact,
                    'description': release.get('release_name', ''),
                    'is_confirmed': True
                })

            return events

        except Exception as e:
            logger.warning(f"Error fetching FRED releases: {str(e)}")
            return []

    def _calculate_stock_event_risk(self, events):
        """
        Calculate risk level and signal adjustment for a stock based on events.

        Args:
            events (dict): Stock events

        Returns:
            tuple: (risk_level, signal_adjustment)
        """
        try:
            adjustment = 1.0
            risk_factors = []

            # Earnings risk
            earnings = events.get('earnings', {})
            if earnings.get('is_today'):
                adjustment *= self.proximity_factors['same_day']
                risk_factors.append('EARNINGS_TODAY')
            elif earnings.get('days_until') is not None:
                days = earnings['days_until']
                if days == 1:
                    adjustment *= self.proximity_factors['next_day']
                    risk_factors.append('EARNINGS_TOMORROW')
                elif 0 < days <= 5:
                    adjustment *= self.proximity_factors['this_week']
                    risk_factors.append('EARNINGS_THIS_WEEK')

            # Ex-dividend risk
            dividends = events.get('dividends', {})
            if dividends.get('is_ex_date_today'):
                adjustment *= 0.9  # Slight reduction on ex-date
                risk_factors.append('EX_DIVIDEND_TODAY')
            elif dividends.get('days_until_ex') == 1:
                risk_factors.append('EX_DIVIDEND_TOMORROW')

            # Recent split adjustment
            splits = events.get('splits', {})
            if splits.get('has_recent_split'):
                risk_factors.append('RECENT_SPLIT')

            # Determine risk level
            if adjustment <= 0.5:
                risk_level = 'VERY_HIGH'
            elif adjustment <= 0.7:
                risk_level = 'HIGH'
            elif adjustment <= 0.9:
                risk_level = 'ELEVATED'
            else:
                risk_level = 'NORMAL'

            return risk_level, round(adjustment, 2)

        except Exception as e:
            logger.warning(f"Error calculating stock event risk: {str(e)}")
            return 'NORMAL', 1.0

    def _calculate_market_risk(self, analysis):
        """
        Calculate overall market risk level.

        Args:
            analysis (dict): Full event analysis

        Returns:
            tuple: (market_risk_level, signal_adjustment)
        """
        try:
            adjustment = 1.0

            # Macro event impact
            macro = analysis.get('macro_events', {})
            if macro.get('has_high_impact_today'):
                adjustment *= 0.6  # 40% reduction on high-impact days

            # Count high-risk stocks
            high_risk_count = sum(
                1 for s in analysis.get('stocks', {}).values()
                if s.get('risk_level') in ['HIGH', 'VERY_HIGH']
            )

            total_stocks = len(analysis.get('stocks', {}))
            if total_stocks > 0:
                high_risk_ratio = high_risk_count / total_stocks
                if high_risk_ratio > 0.3:
                    adjustment *= 0.9

            # Determine market risk level
            if adjustment <= 0.6:
                risk_level = 'HIGH'
            elif adjustment <= 0.8:
                risk_level = 'ELEVATED'
            else:
                risk_level = 'NORMAL'

            return risk_level, round(adjustment, 2)

        except Exception as e:
            logger.warning(f"Error calculating market risk: {str(e)}")
            return 'NORMAL', 1.0

    def get_signal_adjustment(self, symbol, analysis=None):
        """
        Get the signal adjustment factor for a specific symbol.

        Args:
            symbol (str): Stock symbol
            analysis (dict): Pre-computed analysis (optional)

        Returns:
            float: Signal adjustment factor (0.4 to 1.0)
        """
        try:
            if analysis is None:
                analysis = self.get_full_event_analysis([symbol])

            stock_adj = analysis.get('stocks', {}).get(symbol, {}).get('signal_adjustment', 1.0)
            market_adj = analysis.get('signal_adjustment', 1.0)

            # Combined adjustment
            final_adj = stock_adj * market_adj

            return max(0.4, min(1.0, final_adj))

        except Exception as e:
            logger.warning(f"Error getting signal adjustment for {symbol}: {str(e)}")
            return 1.0

    def should_avoid_trading(self, symbol, analysis=None):
        """
        Determine if trading should be avoided for a symbol due to events.

        Args:
            symbol (str): Stock symbol
            analysis (dict): Pre-computed analysis (optional)

        Returns:
            tuple: (should_avoid, reasons)
        """
        try:
            if analysis is None:
                analysis = self.get_full_event_analysis([symbol])

            reasons = []

            # Check stock-specific events
            stock_events = analysis.get('stocks', {}).get(symbol, {})
            if stock_events.get('earnings', {}).get('is_today'):
                reasons.append('Earnings announcement today')

            # Check market events
            if analysis.get('macro_events', {}).get('has_high_impact_today'):
                reasons.append('High-impact macro event today')

            # Check risk level
            if stock_events.get('risk_level') == 'VERY_HIGH':
                reasons.append('Very high event risk')

            return len(reasons) > 0, reasons

        except Exception as e:
            logger.warning(f"Error checking trading avoidance for {symbol}: {str(e)}")
            return False, []

    def _get_default_analysis(self):
        """Return default analysis when data is unavailable"""
        return {
            'timestamp': datetime.now(self.us_tz).strftime('%Y-%m-%d %H:%M:%S %Z'),
            'stocks': {},
            'macro_events': {
                'events': [],
                'has_high_impact_today': False,
                'today': datetime.now(self.us_tz).strftime('%Y-%m-%d'),
                'recommendation': 'NORMAL_TRADING'
            },
            'market_risk_level': 'NORMAL',
            'signal_adjustment': 1.0
        }
