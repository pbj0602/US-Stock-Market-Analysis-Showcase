"""
Premarket Data Analyzer Module
Analyzes premarket data including gap analysis, premarket volume, and relative volume.
Critical for day trading signal generation.
"""

import yfinance as yf
from modules.price_history import get_daily
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
import logging
import pytz
from config.config import Config

logger = logging.getLogger(__name__)


class PremarketAnalyzer:
    """
    Analyzes premarket conditions for stocks including:
    - Gap analysis (gap up/down vs previous close)
    - Premarket volume and relative volume
    - Premarket price action
    - Opening volatility predictions
    """

    def __init__(self):
        self.us_tz = pytz.timezone('US/Eastern')
        self.market_open = datetime.now(self.us_tz).replace(hour=9, minute=30, second=0, microsecond=0)
        self.premarket_start = datetime.now(self.us_tz).replace(hour=4, minute=0, second=0, microsecond=0)

        # Gap thresholds
        self.gap_thresholds = {
            'micro': 0.5,      # 0.5% - micro gap
            'small': 1.0,      # 1% - small gap
            'medium': 2.0,     # 2% - medium gap
            'large': 4.0,      # 4% - large gap
            'extreme': 7.0     # 7%+ - extreme gap
        }

        # Volume thresholds for unusual activity
        self.volume_thresholds = {
            'very_low': 0.3,
            'low': 0.7,
            'normal': 1.0,
            'elevated': 1.5,
            'high': 2.5,
            'very_high': 5.0,
            'extreme': 10.0
        }

    def analyze_premarket(self, symbols):
        """
        Analyze premarket conditions for given symbols.

        Args:
            symbols (list): List of stock symbols

        Returns:
            dict: Premarket analysis for all symbols
        """
        try:
            results = {}

            for symbol in symbols:
                try:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(self._analyze_symbol_premarket, symbol)
                        analysis = future.result(timeout=30)
                    if analysis:
                        results[symbol] = analysis
                except FuturesTimeoutError:
                    logger.warning(f"Premarket analysis timed out for {symbol} (>30s) — skipping")
                except Exception as e:
                    logger.warning(f"Error analyzing premarket for {symbol}: {str(e)}")
                    continue

            # Add market-wide premarket summary
            results['_market_summary'] = self._get_market_premarket_summary(results)

            logger.info(f"Completed premarket analysis for {len(results) - 1} symbols")
            return results

        except Exception as e:
            logger.error(f"Error in premarket analysis: {str(e)}")
            return {}

    def _analyze_symbol_premarket(self, symbol):
        """
        Analyze premarket data for a single symbol.

        Args:
            symbol (str): Stock symbol

        Returns:
            dict: Premarket analysis for the symbol
        """
        try:
            ticker = yf.Ticker(symbol)

            # Get current info (includes premarket data when available)
            info = ticker.info

            # Get historical data for comparison
            hist = get_daily(symbol, '1mo')

            if hist.empty:
                return None

            # Previous close
            prev_close = info.get('previousClose', hist['Close'].iloc[-1])

            # Current/premarket price
            premarket_price = info.get('preMarketPrice')
            current_price = info.get('currentPrice', info.get('regularMarketPrice', prev_close))
            open_price = info.get('open', info.get('regularMarketOpen', current_price))

            # Use premarket price if available, otherwise use current price
            analysis_price = premarket_price if premarket_price else current_price

            # Calculate gap
            gap = self._calculate_gap(prev_close, analysis_price)

            # Get volume data
            volume_data = self._analyze_volume(ticker, info, hist)

            # Get premarket-specific data
            premarket_change = info.get('preMarketChange', 0)
            premarket_change_pct = info.get('preMarketChangePercent', 0)
            premarket_volume = info.get('preMarketVolume', 0)

            # Calculate trading implications
            trading_implications = self._get_trading_implications(gap, volume_data)

            # Calculate premarket score (contribution to overall signal)
            premarket_score = self._calculate_premarket_score(gap, volume_data, premarket_change_pct)

            return {
                'symbol': symbol,
                'timestamp': datetime.now(self.us_tz).strftime('%Y-%m-%d %H:%M:%S %Z'),
                'previous_close': round(prev_close, 2) if prev_close else None,
                'premarket_price': round(premarket_price, 2) if premarket_price else None,
                'current_price': round(current_price, 2) if current_price else None,
                'open_price': round(open_price, 2) if open_price else None,
                'gap': gap,
                'volume': volume_data,
                'premarket_change': round(premarket_change, 2) if premarket_change else 0,
                'premarket_change_pct': round(premarket_change_pct * 100 if premarket_change_pct and abs(premarket_change_pct) < 1 else premarket_change_pct, 2) if premarket_change_pct else 0,
                'premarket_volume': premarket_volume,
                'trading_implications': trading_implications,
                'premarket_score': premarket_score,
                'has_premarket_data': premarket_price is not None
            }

        except Exception as e:
            logger.warning(f"Error analyzing premarket for {symbol}: {str(e)}")
            return None

    def _calculate_gap(self, prev_close, current_price):
        """
        Calculate and classify the gap.

        Args:
            prev_close (float): Previous day's closing price
            current_price (float): Current/premarket price

        Returns:
            dict: Gap analysis
        """
        try:
            if not prev_close or not current_price or prev_close == 0:
                return {
                    'value': 0,
                    'percent': 0,
                    'direction': 'FLAT',
                    'classification': 'NONE',
                    'is_significant': False
                }

            gap_value = current_price - prev_close
            gap_percent = (gap_value / prev_close) * 100

            # Determine direction
            if gap_percent > 0.1:
                direction = 'UP'
            elif gap_percent < -0.1:
                direction = 'DOWN'
            else:
                direction = 'FLAT'

            # Classify gap size
            abs_gap = abs(gap_percent)
            if abs_gap < self.gap_thresholds['micro']:
                classification = 'NONE'
            elif abs_gap < self.gap_thresholds['small']:
                classification = 'MICRO'
            elif abs_gap < self.gap_thresholds['medium']:
                classification = 'SMALL'
            elif abs_gap < self.gap_thresholds['large']:
                classification = 'MEDIUM'
            elif abs_gap < self.gap_thresholds['extreme']:
                classification = 'LARGE'
            else:
                classification = 'EXTREME'

            return {
                'value': round(gap_value, 2),
                'percent': round(gap_percent, 2),
                'direction': direction,
                'classification': classification,
                'is_significant': abs_gap >= self.gap_thresholds['small']
            }

        except Exception as e:
            logger.warning(f"Error calculating gap: {str(e)}")
            return {
                'value': 0,
                'percent': 0,
                'direction': 'FLAT',
                'classification': 'NONE',
                'is_significant': False
            }

    def _analyze_volume(self, ticker, info, hist):
        """
        Analyze volume including relative volume.

        Args:
            ticker: yfinance Ticker object
            info (dict): Ticker info
            hist (DataFrame): Historical data

        Returns:
            dict: Volume analysis
        """
        try:
            # Current volume
            current_volume = info.get('volume', info.get('regularMarketVolume', 0))
            premarket_volume = info.get('preMarketVolume', 0)

            # Average volumes
            avg_volume_10d = info.get('averageVolume10days', 0)
            avg_volume = info.get('averageVolume', 0)

            # Calculate from history if not available
            if not avg_volume and not hist.empty:
                avg_volume = hist['Volume'].mean()
            if not avg_volume_10d and len(hist) >= 10:
                avg_volume_10d = hist['Volume'].tail(10).mean()

            # Relative volume (RVOL)
            rvol = current_volume / avg_volume if avg_volume > 0 else 1
            rvol_10d = current_volume / avg_volume_10d if avg_volume_10d > 0 else 1

            # Classify volume level
            if rvol < self.volume_thresholds['very_low']:
                volume_classification = 'VERY_LOW'
            elif rvol < self.volume_thresholds['low']:
                volume_classification = 'LOW'
            elif rvol < self.volume_thresholds['elevated']:
                volume_classification = 'NORMAL'
            elif rvol < self.volume_thresholds['high']:
                volume_classification = 'ELEVATED'
            elif rvol < self.volume_thresholds['very_high']:
                volume_classification = 'HIGH'
            elif rvol < self.volume_thresholds['extreme']:
                volume_classification = 'VERY_HIGH'
            else:
                volume_classification = 'EXTREME'

            # Volume trend (is recent volume increasing?)
            if len(hist) >= 5:
                recent_avg = hist['Volume'].tail(5).mean()
                older_avg = hist['Volume'].tail(20).head(15).mean() if len(hist) >= 20 else hist['Volume'].head(len(hist) - 5).mean()
                volume_trend = 'INCREASING' if recent_avg > older_avg * 1.2 else ('DECREASING' if recent_avg < older_avg * 0.8 else 'STABLE')
            else:
                volume_trend = 'UNKNOWN'

            return {
                'current': current_volume,
                'premarket': premarket_volume,
                'average': round(avg_volume, 0) if avg_volume else 0,
                'average_10d': round(avg_volume_10d, 0) if avg_volume_10d else 0,
                'relative_volume': round(rvol, 2),
                'relative_volume_10d': round(rvol_10d, 2),
                'classification': volume_classification,
                'trend': volume_trend,
                'is_unusual': rvol >= self.volume_thresholds['high']
            }

        except Exception as e:
            logger.warning(f"Error analyzing volume: {str(e)}")
            return {
                'current': 0,
                'premarket': 0,
                'average': 0,
                'average_10d': 0,
                'relative_volume': 1,
                'relative_volume_10d': 1,
                'classification': 'UNKNOWN',
                'trend': 'UNKNOWN',
                'is_unusual': False
            }

    def _get_trading_implications(self, gap, volume_data):
        """
        Generate trading implications based on gap and volume.

        Args:
            gap (dict): Gap analysis
            volume_data (dict): Volume analysis

        Returns:
            dict: Trading implications
        """
        try:
            implications = {
                'setup': 'NONE',
                'bias': 'NEUTRAL',
                'risk_level': 'NORMAL',
                'notes': []
            }

            gap_dir = gap.get('direction', 'FLAT')
            gap_class = gap.get('classification', 'NONE')
            rvol = volume_data.get('relative_volume', 1)
            vol_unusual = volume_data.get('is_unusual', False)

            # Analyze gap + volume combinations
            if gap_class in ['LARGE', 'EXTREME']:
                implications['risk_level'] = 'HIGH'
                if gap_dir == 'UP':
                    if vol_unusual:
                        implications['setup'] = 'GAP_AND_GO'
                        implications['bias'] = 'BULLISH'
                        implications['notes'].append('Large gap up with high volume - potential continuation')
                    else:
                        implications['setup'] = 'GAP_FADE'
                        implications['bias'] = 'NEUTRAL'
                        implications['notes'].append('Large gap up but low volume - potential fade setup')
                elif gap_dir == 'DOWN':
                    if vol_unusual:
                        implications['setup'] = 'GAP_DOWN_BREAKDOWN'
                        implications['bias'] = 'BEARISH'
                        implications['notes'].append('Large gap down with high volume - potential continuation lower')
                    else:
                        implications['setup'] = 'GAP_FILL'
                        implications['bias'] = 'NEUTRAL'
                        implications['notes'].append('Large gap down but low volume - potential gap fill')

            elif gap_class in ['SMALL', 'MEDIUM']:
                if gap_dir == 'UP' and vol_unusual:
                    implications['setup'] = 'OPENING_RANGE_BREAKOUT'
                    implications['bias'] = 'BULLISH'
                    implications['notes'].append('Moderate gap up with volume - watch for ORB')
                elif gap_dir == 'DOWN' and vol_unusual:
                    implications['setup'] = 'OPENING_RANGE_BREAKDOWN'
                    implications['bias'] = 'BEARISH'
                    implications['notes'].append('Moderate gap down with volume - watch for breakdown')

            # Volume-only signals
            if vol_unusual and gap_class in ['NONE', 'MICRO']:
                implications['setup'] = 'VOLUME_ALERT'
                implications['notes'].append('Unusual volume without significant gap - potential news catalyst')

            # Low volume warnings
            if volume_data.get('classification') in ['VERY_LOW', 'LOW']:
                implications['risk_level'] = 'ELEVATED'
                implications['notes'].append('Low volume - wider spreads expected, use caution')

            return implications

        except Exception as e:
            logger.warning(f"Error getting trading implications: {str(e)}")
            return {
                'setup': 'UNKNOWN',
                'bias': 'NEUTRAL',
                'risk_level': 'UNKNOWN',
                'notes': ['Error analyzing trading implications']
            }

    def _calculate_premarket_score(self, gap, volume_data, premarket_change_pct):
        """
        Calculate premarket score contribution to overall signal.
        Score ranges from -1 (very bearish) to +1 (very bullish).

        Args:
            gap (dict): Gap analysis
            volume_data (dict): Volume analysis
            premarket_change_pct (float): Premarket change percentage

        Returns:
            dict: Premarket score and components
        """
        try:
            score = 0
            components = {}

            # Gap contribution (40% of premarket score)
            gap_pct = gap.get('percent', 0)
            if gap_pct > 3:
                gap_score = 1.0
            elif gap_pct > 1:
                gap_score = 0.5 + (gap_pct - 1) / 4
            elif gap_pct > 0:
                gap_score = gap_pct / 2
            elif gap_pct > -1:
                gap_score = gap_pct / 2
            elif gap_pct > -3:
                gap_score = -0.5 + (gap_pct + 1) / 4
            else:
                gap_score = -1.0

            components['gap'] = round(gap_score, 3)
            score += gap_score * 0.4

            # Volume contribution (30% of premarket score)
            rvol = volume_data.get('relative_volume', 1)
            if rvol > 3:
                vol_score = 0.5  # High volume is generally positive (interest)
            elif rvol > 1.5:
                vol_score = 0.3
            elif rvol > 0.7:
                vol_score = 0
            else:
                vol_score = -0.3  # Low volume is negative

            # Combine volume with gap direction
            if gap.get('direction') == 'UP' and rvol > 1.5:
                vol_score += 0.2  # Volume confirms gap up
            elif gap.get('direction') == 'DOWN' and rvol > 1.5:
                vol_score -= 0.2  # Volume confirms gap down

            components['volume'] = round(vol_score, 3)
            score += vol_score * 0.3

            # Momentum contribution (30% of premarket score)
            pm_change = premarket_change_pct if premarket_change_pct else 0
            momentum_score = max(-1, min(1, pm_change / 3))  # Normalize to -1 to 1
            components['momentum'] = round(momentum_score, 3)
            score += momentum_score * 0.3

            # Classify overall premarket sentiment
            if score > 0.5:
                sentiment = 'STRONG_BULLISH'
            elif score > 0.2:
                sentiment = 'BULLISH'
            elif score > -0.2:
                sentiment = 'NEUTRAL'
            elif score > -0.5:
                sentiment = 'BEARISH'
            else:
                sentiment = 'STRONG_BEARISH'

            return {
                'score': round(score, 3),
                'sentiment': sentiment,
                'components': components,
                'weight_recommendation': 0.2  # Suggested weight in composite signal
            }

        except Exception as e:
            logger.warning(f"Error calculating premarket score: {str(e)}")
            return {
                'score': 0,
                'sentiment': 'NEUTRAL',
                'components': {},
                'weight_recommendation': 0.2
            }

    def _get_market_premarket_summary(self, results):
        """
        Generate market-wide premarket summary.

        Args:
            results (dict): Individual symbol results

        Returns:
            dict: Market premarket summary
        """
        try:
            if len(results) <= 1:  # Only _market_summary key
                return {
                    'average_gap': 0,
                    'gap_up_count': 0,
                    'gap_down_count': 0,
                    'flat_count': 0,
                    'high_volume_count': 0,
                    'market_bias': 'NEUTRAL',
                    'overall_premarket_score': 0
                }

            gap_ups = 0
            gap_downs = 0
            flat = 0
            high_volume = 0
            total_gap = 0
            total_score = 0
            count = 0

            for symbol, data in results.items():
                if symbol.startswith('_'):
                    continue

                gap = data.get('gap', {})
                volume = data.get('volume', {})
                premarket_score = data.get('premarket_score', {})

                gap_dir = gap.get('direction', 'FLAT')
                if gap_dir == 'UP':
                    gap_ups += 1
                elif gap_dir == 'DOWN':
                    gap_downs += 1
                else:
                    flat += 1

                if volume.get('is_unusual', False):
                    high_volume += 1

                total_gap += gap.get('percent', 0)
                total_score += premarket_score.get('score', 0)
                count += 1

            if count == 0:
                return self._get_default_market_summary()

            avg_gap = total_gap / count
            avg_score = total_score / count

            # Determine market bias
            if gap_ups > gap_downs * 2:
                market_bias = 'BULLISH'
            elif gap_downs > gap_ups * 2:
                market_bias = 'BEARISH'
            elif avg_gap > 0.5:
                market_bias = 'SLIGHTLY_BULLISH'
            elif avg_gap < -0.5:
                market_bias = 'SLIGHTLY_BEARISH'
            else:
                market_bias = 'NEUTRAL'

            return {
                'average_gap': round(avg_gap, 2),
                'gap_up_count': gap_ups,
                'gap_down_count': gap_downs,
                'flat_count': flat,
                'high_volume_count': high_volume,
                'market_bias': market_bias,
                'overall_premarket_score': round(avg_score, 3),
                'total_analyzed': count
            }

        except Exception as e:
            logger.error(f"Error generating market premarket summary: {str(e)}")
            return self._get_default_market_summary()

    def _get_default_market_summary(self):
        """Return default market summary"""
        return {
            'average_gap': 0,
            'gap_up_count': 0,
            'gap_down_count': 0,
            'flat_count': 0,
            'high_volume_count': 0,
            'market_bias': 'NEUTRAL',
            'overall_premarket_score': 0,
            'total_analyzed': 0
        }

    def get_gap_trading_candidates(self, symbols, min_gap_pct=2.0, min_rvol=1.5):
        """
        Find stocks with significant gaps and volume for gap trading.

        Args:
            symbols (list): List of symbols to analyze
            min_gap_pct (float): Minimum gap percentage
            min_rvol (float): Minimum relative volume

        Returns:
            list: List of gap trading candidates
        """
        try:
            premarket_data = self.analyze_premarket(symbols)
            candidates = []

            for symbol, data in premarket_data.items():
                if symbol.startswith('_'):
                    continue

                gap = data.get('gap', {})
                volume = data.get('volume', {})

                gap_pct = abs(gap.get('percent', 0))
                rvol = volume.get('relative_volume', 0)

                if gap_pct >= min_gap_pct and rvol >= min_rvol:
                    candidates.append({
                        'symbol': symbol,
                        'gap_percent': gap.get('percent', 0),
                        'gap_direction': gap.get('direction', 'FLAT'),
                        'relative_volume': rvol,
                        'setup': data.get('trading_implications', {}).get('setup', 'UNKNOWN'),
                        'premarket_score': data.get('premarket_score', {}).get('score', 0)
                    })

            # Sort by absolute gap percentage
            candidates.sort(key=lambda x: abs(x['gap_percent']), reverse=True)

            return candidates

        except Exception as e:
            logger.error(f"Error finding gap trading candidates: {str(e)}")
            return []
