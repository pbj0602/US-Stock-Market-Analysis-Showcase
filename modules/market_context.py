"""
Market Context Module
Provides market regime analysis, sector strength, VIX regime, and overall market context
for improving signal quality and adapting to market conditions.
"""

import yfinance as yf
from modules.price_history import get_daily
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import requests
from config.config import Config

logger = logging.getLogger(__name__)


class MarketContext:
    """
    Analyzes overall market conditions to provide context for individual stock signals.
    Includes market indices, sector strength, VIX regime, and market breadth.
    """

    def __init__(self):
        # Market index ETFs
        self.market_indices = {
            'SPY': 'S&P 500',
            'QQQ': 'NASDAQ 100',
            'DIA': 'Dow Jones',
            'IWM': 'Russell 2000'
        }

        # Sector ETFs with stock mappings
        self.sector_etfs = {
            'XLK': {'name': 'Technology', 'stocks': ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'META', 'AVGO', 'ORCL', 'CRM', 'ADBE', 'AMD']},
            'XLF': {'name': 'Financials', 'stocks': ['JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'BLK', 'SCHW', 'AXP', 'USB']},
            'XLE': {'name': 'Energy', 'stocks': ['XOM', 'CVX', 'COP', 'SLB', 'EOG', 'MPC', 'PSX', 'VLO', 'PXD', 'OXY']},
            'XLI': {'name': 'Industrials', 'stocks': ['CAT', 'UNP', 'HON', 'UPS', 'BA', 'RTX', 'DE', 'LMT', 'GE', 'MMM']},
            'XLV': {'name': 'Healthcare', 'stocks': ['UNH', 'JNJ', 'LLY', 'PFE', 'ABBV', 'MRK', 'TMO', 'ABT', 'DHR', 'BMY']},
            'XLY': {'name': 'Consumer Discretionary', 'stocks': ['AMZN', 'TSLA', 'HD', 'MCD', 'NKE', 'LOW', 'SBUX', 'TJX', 'BKNG', 'CMG']},
            'XLP': {'name': 'Consumer Staples', 'stocks': ['PG', 'KO', 'PEP', 'COST', 'WMT', 'PM', 'MDLZ', 'MO', 'CL', 'EL']},
            'XLU': {'name': 'Utilities', 'stocks': ['NEE', 'DUK', 'SO', 'D', 'AEP', 'SRE', 'XEL', 'EXC', 'WEC', 'ED']},
            'XLB': {'name': 'Materials', 'stocks': ['LIN', 'APD', 'SHW', 'FCX', 'ECL', 'NEM', 'NUE', 'DOW', 'DD', 'CTVA']},
            'XLRE': {'name': 'Real Estate', 'stocks': ['PLD', 'AMT', 'EQIX', 'CCI', 'PSA', 'O', 'SPG', 'WELL', 'DLR', 'AVB']},
            'XLC': {'name': 'Communication Services', 'stocks': ['GOOGL', 'META', 'NFLX', 'DIS', 'CMCSA', 'T', 'VZ', 'CHTR', 'TMUS', 'EA']},
        }

        # VIX thresholds for regime classification
        self.vix_thresholds = {
            'low': 12,      # Low volatility regime
            'normal': 20,   # Normal volatility
            'elevated': 25, # Elevated volatility
            'high': 30,     # High volatility
            'extreme': 40   # Extreme volatility / panic
        }

        # Cache for data
        self._cache = {}
        self._cache_timestamp = None
        self._cache_duration = timedelta(minutes=15)

    def get_full_market_context(self):
        """
        Get comprehensive market context including all components.
        Results are cached for _cache_duration (15 min) to avoid redundant API calls.

        Returns:
            dict: Complete market context with all analysis components
        """
        try:
            now = datetime.now()
            if (self._cache_timestamp is not None
                    and (now - self._cache_timestamp) < self._cache_duration
                    and self._cache):
                logger.debug("Market context served from cache")
                return self._cache

            context = {
                'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
                'market_regime': self.get_market_regime(),
                'sector_strength': self.get_sector_strength(),
                'vix_analysis': self.get_vix_analysis(),
                'market_breadth': self.get_market_breadth(),
                'index_trends': self.get_index_trends(),
                'trading_recommendation': {}
            }

            # Generate overall trading recommendation
            context['trading_recommendation'] = self._generate_trading_recommendation(context)

            self._cache = context
            self._cache_timestamp = now
            logger.info("Successfully generated full market context")
            return context

        except Exception as e:
            logger.error(f"Error generating market context: {str(e)}")
            return self._get_default_context()

    def get_market_regime(self):
        """
        Determine the current market regime (Bull/Bear/Sideways).
        Based on SPY trend analysis using SMA20/50/200.

        Returns:
            dict: Market regime classification and details
        """
        try:
            # Fetch 1 year (~252 trading days) so SMA200 uses a genuine 200-day window.
            # 6 months only gives ~126 days, making SMA200 actually SMA126.
            hist = get_daily('SPY', '1y')

            if hist.empty:
                return self._get_default_regime()

            # Calculate SMAs
            hist['SMA20'] = hist['Close'].rolling(window=20).mean()
            hist['SMA50'] = hist['Close'].rolling(window=50).mean()
            hist['SMA200'] = hist['Close'].rolling(window=200).mean()

            latest = hist.iloc[-1]
            current_price = latest['Close']
            sma20 = latest['SMA20']
            sma50 = latest['SMA50']
            sma200 = latest.get('SMA200', sma50)

            # Calculate trend scores
            price_vs_sma20 = (current_price - sma20) / sma20 if sma20 > 0 else 0
            price_vs_sma50 = (current_price - sma50) / sma50 if sma50 > 0 else 0
            price_vs_sma200 = (current_price - sma200) / sma200 if sma200 > 0 else 0
            sma20_vs_sma50 = (sma20 - sma50) / sma50 if sma50 > 0 else 0

            # Calculate 20-day momentum
            if len(hist) >= 20:
                momentum_20d = (current_price - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20]
            else:
                momentum_20d = 0

            # Determine regime
            bullish_signals = sum([
                price_vs_sma20 > 0,
                price_vs_sma50 > 0,
                price_vs_sma200 > 0,
                sma20_vs_sma50 > 0,
                momentum_20d > 0.02
            ])

            if bullish_signals >= 4:
                regime = 'BULL'
                regime_strength = min((bullish_signals / 5) * 100, 100)
            elif bullish_signals <= 1:
                regime = 'BEAR'
                regime_strength = min(((5 - bullish_signals) / 5) * 100, 100)
            else:
                regime = 'SIDEWAYS'
                regime_strength = 50

            return {
                'regime': regime,
                'strength': round(regime_strength, 1),
                'spy_price': round(current_price, 2),
                'price_vs_sma20': round(price_vs_sma20 * 100, 2),
                'price_vs_sma50': round(price_vs_sma50 * 100, 2),
                'price_vs_sma200': round(price_vs_sma200 * 100, 2),
                'sma20_vs_sma50': round(sma20_vs_sma50 * 100, 2),
                'momentum_20d': round(momentum_20d * 100, 2),
                'bullish_signals': bullish_signals,
                'total_signals': 5
            }

        except Exception as e:
            logger.error(f"Error determining market regime: {str(e)}")
            return self._get_default_regime()

    def get_index_trends(self):
        """
        Get trend analysis for major market indices.

        Returns:
            dict: Trend data for SPY, QQQ, DIA, IWM
        """
        try:
            index_trends = {}

            for symbol, name in self.market_indices.items():
                try:
                    hist = get_daily(symbol, '3mo')

                    if hist.empty:
                        continue

                    # Calculate indicators
                    hist['SMA20'] = hist['Close'].rolling(window=20).mean()
                    hist['SMA50'] = hist['Close'].rolling(window=50).mean()

                    latest = hist.iloc[-1]
                    prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else latest['Close']

                    current_price = latest['Close']
                    day_change = current_price - prev_close
                    day_change_pct = (day_change / prev_close) * 100 if prev_close > 0 else 0

                    # 5-day and 20-day returns
                    return_5d = ((current_price - hist['Close'].iloc[-5]) / hist['Close'].iloc[-5] * 100) if len(hist) >= 5 else 0
                    return_20d = ((current_price - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20] * 100) if len(hist) >= 20 else 0

                    # Trend determination
                    sma20 = latest['SMA20']
                    sma50 = latest['SMA50']

                    if current_price > sma20 > sma50:
                        trend = 'UPTREND'
                    elif current_price < sma20 < sma50:
                        trend = 'DOWNTREND'
                    else:
                        trend = 'SIDEWAYS'

                    index_trends[symbol] = {
                        'name': name,
                        'price': round(current_price, 2),
                        'day_change': round(day_change, 2),
                        'day_change_pct': round(day_change_pct, 2),
                        'return_5d': round(return_5d, 2),
                        'return_20d': round(return_20d, 2),
                        'trend': trend,
                        'above_sma20': current_price > sma20,
                        'above_sma50': current_price > sma50
                    }

                except Exception as e:
                    logger.warning(f"Error getting trend for {symbol}: {str(e)}")
                    continue

            return index_trends

        except Exception as e:
            logger.error(f"Error getting index trends: {str(e)}")
            return {}

    def get_sector_strength(self):
        """
        Calculate relative sector strength using sector ETFs.

        Returns:
            dict: Sector strength rankings and performance
        """
        try:
            sector_data = []

            for etf, info in self.sector_etfs.items():
                try:
                    hist = get_daily(etf, '1mo')

                    # Drop rows with no Close — yfinance sometimes appends an
                    # all-NaN placeholder bar for the in-progress day, which
                    # otherwise poisons every return calculation with NaN.
                    if not hist.empty:
                        hist = hist.dropna(subset=['Close'])

                    if hist.empty or len(hist) < 5:
                        continue

                    latest = hist.iloc[-1]
                    prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else latest['Close']

                    # Calculate returns
                    day_return = ((latest['Close'] - prev_close) / prev_close * 100) if prev_close > 0 else 0
                    week_return = ((latest['Close'] - hist['Close'].iloc[-5]) / hist['Close'].iloc[-5] * 100) if len(hist) >= 5 else 0
                    month_return = ((latest['Close'] - hist['Close'].iloc[0]) / hist['Close'].iloc[0] * 100) if len(hist) > 0 else 0

                    # Defensive: never let a NaN escape into the report
                    day_return = 0.0 if pd.isna(day_return) else day_return
                    week_return = 0.0 if pd.isna(week_return) else week_return
                    month_return = 0.0 if pd.isna(month_return) else month_return

                    # Calculate volume trend
                    avg_volume = hist['Volume'].rolling(window=20).mean().iloc[-1] if len(hist) >= 20 else hist['Volume'].mean()
                    volume_ratio = latest['Volume'] / avg_volume if avg_volume > 0 else 1

                    # Calculate RSI — replace 0 loss with NaN to avoid divide-by-zero
                    delta = hist['Close'].diff()
                    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                    rs = gain / loss.replace(0, float('nan'))
                    rsi = 100 - (100 / (1 + rs))
                    current_rsi = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50

                    # Calculate composite strength score
                    strength_score = (
                        (day_return * 0.3) +
                        (week_return * 0.3) +
                        (month_return * 0.4)
                    )

                    sector_data.append({
                        'etf': etf,
                        'name': info['name'],
                        'price': round(latest['Close'], 2),
                        'day_return': round(day_return, 2),
                        'week_return': round(week_return, 2),
                        'month_return': round(month_return, 2),
                        'strength_score': round(strength_score, 2),
                        'volume_ratio': round(volume_ratio, 2),
                        'rsi': round(current_rsi, 1),
                        'mapped_stocks': info['stocks']
                    })

                except Exception as e:
                    logger.warning(f"Error getting sector data for {etf}: {str(e)}")
                    continue

            if not sector_data:
                return {'sectors': [], 'top_sectors': [], 'bottom_sectors': [], 'rotation_signal': 'NEUTRAL'}

            # Sort by strength score
            sector_data.sort(key=lambda x: x['strength_score'], reverse=True)

            # Determine sector rotation signal
            top_3_avg = np.mean([s['strength_score'] for s in sector_data[:3]]) if len(sector_data) >= 3 else 0
            bottom_3_avg = np.mean([s['strength_score'] for s in sector_data[-3:]]) if len(sector_data) >= 3 else 0

            if top_3_avg > 2 and bottom_3_avg > 0:
                rotation_signal = 'RISK_ON'
            elif top_3_avg < -2 and bottom_3_avg < 0:
                rotation_signal = 'RISK_OFF'
            else:
                rotation_signal = 'NEUTRAL'

            return {
                'sectors': sector_data,
                'top_sectors': [s['name'] for s in sector_data[:3]],
                'bottom_sectors': [s['name'] for s in sector_data[-3:]],
                'rotation_signal': rotation_signal,
                'dispersion': round(sector_data[0]['strength_score'] - sector_data[-1]['strength_score'], 2) if sector_data else 0
            }

        except Exception as e:
            logger.error(f"Error calculating sector strength: {str(e)}")
            return {'sectors': [], 'top_sectors': [], 'bottom_sectors': [], 'rotation_signal': 'NEUTRAL'}

    def get_vix_analysis(self):
        """
        Analyze VIX for volatility regime classification.

        Returns:
            dict: VIX analysis with regime and trading implications
        """
        try:
            hist = get_daily('^VIX', '1mo')

            if hist.empty:
                return self._get_default_vix()

            latest = hist.iloc[-1]
            current_vix = latest['Close']

            # Calculate VIX SMAs
            hist['SMA3'] = hist['Close'].rolling(window=3).mean()
            hist['SMA10'] = hist['Close'].rolling(window=10).mean()
            hist['SMA20'] = hist['Close'].rolling(window=20).mean()

            vix_sma3 = hist['SMA3'].iloc[-1] if not pd.isna(hist['SMA3'].iloc[-1]) else current_vix
            vix_sma10 = hist['SMA10'].iloc[-1] if not pd.isna(hist['SMA10'].iloc[-1]) else current_vix
            vix_sma20 = hist['SMA20'].iloc[-1] if not pd.isna(hist['SMA20'].iloc[-1]) else current_vix

            # VIX change
            prev_vix = hist.iloc[-2]['Close'] if len(hist) > 1 else current_vix
            vix_change = current_vix - prev_vix
            vix_change_pct = (vix_change / prev_vix * 100) if prev_vix > 0 else 0

            # Determine regime
            if current_vix < self.vix_thresholds['low']:
                regime = 'LOW_VOLATILITY'
                regime_description = 'Complacent market - consider hedging'
            elif current_vix < self.vix_thresholds['normal']:
                regime = 'NORMAL'
                regime_description = 'Normal volatility conditions'
            elif current_vix < self.vix_thresholds['elevated']:
                regime = 'ELEVATED'
                regime_description = 'Above average volatility - reduce position sizes'
            elif current_vix < self.vix_thresholds['high']:
                regime = 'HIGH'
                regime_description = 'High volatility - caution advised'
            else:
                regime = 'EXTREME'
                regime_description = 'Extreme volatility - consider staying out'

            # VIX trend
            if current_vix > vix_sma3 > vix_sma10:
                vix_trend = 'RISING'
            elif current_vix < vix_sma3 < vix_sma10:
                vix_trend = 'FALLING'
            else:
                vix_trend = 'NEUTRAL'

            # Calculate signal adjustment factor
            # Higher VIX = lower confidence in BUY signals
            if current_vix < 15:
                signal_adjustment = 1.0  # Normal confidence
            elif current_vix < 20:
                signal_adjustment = 0.9  # Slight reduction
            elif current_vix < 25:
                signal_adjustment = 0.75  # Moderate reduction
            elif current_vix < 30:
                signal_adjustment = 0.6  # Significant reduction
            else:
                signal_adjustment = 0.4  # Major reduction

            # Widen stop loss recommendation
            stop_loss_multiplier = 1.0 + max(0, (current_vix - 15) / 30)

            return {
                'current': round(current_vix, 2),
                'change': round(vix_change, 2),
                'change_pct': round(vix_change_pct, 2),
                'sma3': round(vix_sma3, 2),
                'sma10': round(vix_sma10, 2),
                'sma20': round(vix_sma20, 2),
                'regime': regime,
                'regime_description': regime_description,
                'trend': vix_trend,
                'signal_adjustment': round(signal_adjustment, 2),
                'stop_loss_multiplier': round(stop_loss_multiplier, 2),
                'thresholds': self.vix_thresholds
            }

        except Exception as e:
            logger.error(f"Error analyzing VIX: {str(e)}")
            return self._get_default_vix()

    def get_market_breadth(self):
        """
        Calculate market breadth indicators.
        Uses sector ETFs as proxy for broader market breadth.

        Returns:
            dict: Market breadth analysis
        """
        try:
            # Get data for all sector ETFs
            above_sma20 = 0
            above_sma50 = 0
            advancing = 0
            declining = 0
            total = 0

            all_etfs = list(self.sector_etfs.keys()) + list(self.market_indices.keys())

            for symbol in all_etfs:
                try:
                    hist = get_daily(symbol, '3mo')

                    if hist.empty or len(hist) < 50:
                        continue

                    total += 1
                    latest = hist.iloc[-1]
                    prev = hist.iloc[-2]

                    # Calculate SMAs
                    sma20 = hist['Close'].rolling(window=20).mean().iloc[-1]
                    sma50 = hist['Close'].rolling(window=50).mean().iloc[-1]

                    if latest['Close'] > sma20:
                        above_sma20 += 1
                    if latest['Close'] > sma50:
                        above_sma50 += 1
                    if latest['Close'] > prev['Close']:
                        advancing += 1
                    else:
                        declining += 1

                except Exception as e:
                    continue

            if total == 0:
                return self._get_default_breadth()

            # Calculate breadth percentages
            pct_above_sma20 = (above_sma20 / total) * 100
            pct_above_sma50 = (above_sma50 / total) * 100

            # A/D ratio
            ad_ratio = advancing / declining if declining > 0 else advancing

            # Breadth thrust
            if pct_above_sma50 > 80:
                breadth_signal = 'STRONG_BULLISH'
            elif pct_above_sma50 > 60:
                breadth_signal = 'BULLISH'
            elif pct_above_sma50 > 40:
                breadth_signal = 'NEUTRAL'
            elif pct_above_sma50 > 20:
                breadth_signal = 'BEARISH'
            else:
                breadth_signal = 'STRONG_BEARISH'

            return {
                'pct_above_sma20': round(pct_above_sma20, 1),
                'pct_above_sma50': round(pct_above_sma50, 1),
                'advancing': advancing,
                'declining': declining,
                'ad_ratio': round(ad_ratio, 2),
                'breadth_signal': breadth_signal,
                'total_analyzed': total
            }

        except Exception as e:
            logger.error(f"Error calculating market breadth: {str(e)}")
            return self._get_default_breadth()

    def get_stock_sector(self, symbol):
        """
        Get the sector for a given stock symbol.

        Args:
            symbol (str): Stock symbol

        Returns:
            dict: Sector information and current sector strength
        """
        try:
            # Check our mapped sectors first
            for etf, info in self.sector_etfs.items():
                if symbol in info['stocks']:
                    return {
                        'sector': info['name'],
                        'sector_etf': etf,
                        'found_in_mapping': True
                    }

            # Fallback to yfinance sector info
            ticker = yf.Ticker(symbol)
            info = ticker.info
            sector = info.get('sector', 'Unknown')

            # Map yfinance sector to our ETF
            sector_etf_map = {
                'Technology': 'XLK',
                'Financial Services': 'XLF',
                'Financials': 'XLF',
                'Energy': 'XLE',
                'Industrials': 'XLI',
                'Healthcare': 'XLV',
                'Consumer Cyclical': 'XLY',
                'Consumer Discretionary': 'XLY',
                'Consumer Defensive': 'XLP',
                'Consumer Staples': 'XLP',
                'Utilities': 'XLU',
                'Basic Materials': 'XLB',
                'Materials': 'XLB',
                'Real Estate': 'XLRE',
                'Communication Services': 'XLK'  # Map to tech as proxy
            }

            etf = sector_etf_map.get(sector, 'SPY')

            return {
                'sector': sector,
                'sector_etf': etf,
                'found_in_mapping': False
            }

        except Exception as e:
            logger.warning(f"Error getting sector for {symbol}: {str(e)}")
            return {'sector': 'Unknown', 'sector_etf': 'SPY', 'found_in_mapping': False}

    def get_sector_weight_for_stock(self, symbol):
        """
        Get the sector strength weight to apply to a stock's signal.

        Args:
            symbol (str): Stock symbol

        Returns:
            float: Sector weight adjustment (0.8 to 1.2)
        """
        try:
            sector_info = self.get_stock_sector(symbol)
            sector_strength = self.get_sector_strength()

            if not sector_strength.get('sectors'):
                return 1.0

            # Find the stock's sector in the strength rankings
            sector_etf = sector_info.get('sector_etf', 'SPY')

            for sector in sector_strength['sectors']:
                if sector['etf'] == sector_etf:
                    strength_score = sector['strength_score']

                    # Convert strength score to weight adjustment
                    # Strong sectors get boost, weak sectors get penalty
                    if strength_score > 3:
                        return 1.15
                    elif strength_score > 1:
                        return 1.08
                    elif strength_score > -1:
                        return 1.0
                    elif strength_score > -3:
                        return 0.92
                    else:
                        return 0.85

            return 1.0

        except Exception as e:
            logger.warning(f"Error getting sector weight for {symbol}: {str(e)}")
            return 1.0

    def _generate_trading_recommendation(self, context):
        """
        Generate overall trading recommendation based on market context.

        Args:
            context (dict): Full market context

        Returns:
            dict: Trading recommendations
        """
        try:
            regime = context.get('market_regime', {})
            vix = context.get('vix_analysis', {})
            breadth = context.get('market_breadth', {})

            # Calculate overall market score
            market_score = 0

            # Regime contribution
            if regime.get('regime') == 'BULL':
                market_score += 30
            elif regime.get('regime') == 'BEAR':
                market_score -= 30

            # VIX contribution
            vix_regime = vix.get('regime', 'NORMAL')
            if vix_regime == 'LOW_VOLATILITY':
                market_score += 10
            elif vix_regime == 'ELEVATED':
                market_score -= 15
            elif vix_regime == 'HIGH':
                market_score -= 25
            elif vix_regime == 'EXTREME':
                market_score -= 40

            # Breadth contribution
            pct_above_50 = breadth.get('pct_above_sma50', 50)
            market_score += (pct_above_50 - 50) * 0.4

            # Generate recommendation
            if market_score > 30:
                recommendation = 'AGGRESSIVE_LONG'
                description = 'Strong bullish conditions - favor long positions'
                buy_confidence_mult = 1.2
                sell_confidence_mult = 0.8
            elif market_score > 10:
                recommendation = 'MODERATE_LONG'
                description = 'Favorable conditions for selective buying'
                buy_confidence_mult = 1.1
                sell_confidence_mult = 0.9
            elif market_score > -10:
                recommendation = 'NEUTRAL'
                description = 'Mixed signals - trade selectively with tight stops'
                buy_confidence_mult = 1.0
                sell_confidence_mult = 1.0
            elif market_score > -30:
                recommendation = 'CAUTIOUS'
                description = 'Unfavorable conditions - reduce exposure'
                buy_confidence_mult = 0.8
                sell_confidence_mult = 1.1
            else:
                recommendation = 'DEFENSIVE'
                description = 'High risk environment - avoid new positions'
                buy_confidence_mult = 0.6
                sell_confidence_mult = 1.2

            return {
                'recommendation': recommendation,
                'description': description,
                'market_score': round(market_score, 1),
                'buy_confidence_multiplier': buy_confidence_mult,
                'sell_confidence_multiplier': sell_confidence_mult,
                'position_size_factor': max(0.5, min(1.5, 1 + market_score / 100))
            }

        except Exception as e:
            logger.error(f"Error generating trading recommendation: {str(e)}")
            return {
                'recommendation': 'NEUTRAL',
                'description': 'Unable to determine market conditions',
                'market_score': 0,
                'buy_confidence_multiplier': 1.0,
                'sell_confidence_multiplier': 1.0,
                'position_size_factor': 1.0
            }

    def _get_default_context(self):
        """Return default context when data is unavailable"""
        return {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'market_regime': self._get_default_regime(),
            'sector_strength': {'sectors': [], 'top_sectors': [], 'bottom_sectors': [], 'rotation_signal': 'NEUTRAL'},
            'vix_analysis': self._get_default_vix(),
            'market_breadth': self._get_default_breadth(),
            'index_trends': {},
            'trading_recommendation': {
                'recommendation': 'NEUTRAL',
                'description': 'Data unavailable - using default settings',
                'market_score': 0,
                'buy_confidence_multiplier': 1.0,
                'sell_confidence_multiplier': 1.0,
                'position_size_factor': 1.0
            }
        }

    def _get_default_regime(self):
        """Return default market regime"""
        return {
            'regime': 'SIDEWAYS',
            'strength': 50,
            'spy_price': 0,
            'price_vs_sma20': 0,
            'price_vs_sma50': 0,
            'price_vs_sma200': 0,
            'sma20_vs_sma50': 0,
            'momentum_20d': 0,
            'bullish_signals': 0,
            'total_signals': 5
        }

    def _get_default_vix(self):
        """Return default VIX analysis"""
        return {
            'current': 20,
            'change': 0,
            'change_pct': 0,
            'sma3': 20,
            'sma10': 20,
            'sma20': 20,
            'regime': 'NORMAL',
            'regime_description': 'Data unavailable',
            'trend': 'NEUTRAL',
            'signal_adjustment': 1.0,
            'stop_loss_multiplier': 1.0,
            'thresholds': self.vix_thresholds
        }

    def _get_default_breadth(self):
        """Return default market breadth"""
        return {
            'pct_above_sma20': 50,
            'pct_above_sma50': 50,
            'advancing': 0,
            'declining': 0,
            'ad_ratio': 1,
            'breadth_signal': 'NEUTRAL',
            'total_analyzed': 0
        }
