"""
Stock Suggester Module
Discovers and suggests new trading opportunities based on various criteria:
- Momentum leaders
- Volume breakouts
- Sector rotation plays
- Unusual activity detection
- Technical pattern recognition
"""

import yfinance as yf
from modules.price_history import get_daily
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from config.config import Config

logger = logging.getLogger(__name__)


class StockSuggester:
    """
    Discovers new stock opportunities by scanning multiple universes
    and applying various filters and scoring mechanisms.
    """

    def __init__(self):
        # Stock universes to scan
        self.universes = {
            'sp500_tech': ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'AVGO', 'ORCL', 'CRM',
                          'ADBE', 'AMD', 'INTC', 'QCOM', 'TXN', 'IBM', 'NOW', 'INTU', 'AMAT', 'MU'],
            'sp500_finance': ['JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'BLK', 'SCHW', 'AXP', 'USB',
                             'PNC', 'TFC', 'COF', 'STT', 'FITB', 'MTB', 'KEY', 'CFG', 'RF'],
            'sp500_healthcare': ['UNH', 'JNJ', 'LLY', 'PFE', 'ABBV', 'MRK', 'TMO', 'ABT', 'DHR', 'BMY',
                                'AMGN', 'GILD', 'CVS', 'ELV', 'CI', 'HUM', 'VRTX', 'REGN', 'ZTS', 'BIIB'],
            'sp500_consumer': ['AMZN', 'TSLA', 'HD', 'MCD', 'NKE', 'LOW', 'SBUX', 'TJX', 'BKNG', 'CMG',
                              'TGT', 'ORLY', 'AZO', 'ROST', 'DHI', 'LEN', 'MAR', 'HLT', 'YUM', 'DG'],
            'sp500_industrial': ['CAT', 'UNP', 'HON', 'UPS', 'BA', 'RTX', 'DE', 'LMT', 'GE', 'MMM',
                                'FDX', 'CSX', 'NSC', 'EMR', 'ITW', 'ETN', 'PH', 'ROK', 'PCAR', 'CMI'],
            'growth_stocks': ['PLTR', 'SNOW', 'CRWD', 'DDOG', 'NET', 'ZS', 'OKTA', 'MDB', 'TEAM', 'HUBS',
                             'TTD', 'ROKU', 'XYZ', 'SHOP', 'MELI', 'SE', 'PINS', 'SNAP', 'RBLX', 'U'],
            'dividend_aristocrats': ['JNJ', 'PG', 'KO', 'PEP', 'MCD', 'WMT', 'ABT', 'MMM', 'XOM', 'CVX',
                                    'CL', 'GD', 'ITW', 'SWK', 'EMR', 'ED', 'ADP', 'AFL', 'SYY', 'CLX'],
            'etfs': ['SPY', 'QQQ', 'IWM', 'DIA', 'VTI', 'VOO', 'ARKK', 'XLK', 'XLF', 'XLE',
                    'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLRE', 'XLB', 'GLD', 'SLV', 'TLT']
        }

        # Scoring weights for suggestions
        self.weights = {
            'momentum': 0.25,
            'volume': 0.20,
            'trend': 0.20,
            'rsi_position': 0.15,
            'relative_strength': 0.20
        }

        # Thresholds
        self.min_volume = 500000  # Minimum average volume
        self.min_price = 10       # Minimum stock price
        self.max_price = 1000     # Maximum stock price

    def get_suggestions(self, existing_symbols=None, max_suggestions=10, scan_type='all'):
        """
        Get stock suggestions based on various criteria.

        Args:
            existing_symbols (list): Symbols already in portfolio (to exclude)
            max_suggestions (int): Maximum number of suggestions to return
            scan_type (str): Type of scan - 'all', 'momentum', 'volume', 'oversold', 'breakout'

        Returns:
            dict: Categorized stock suggestions
        """
        try:
            existing_symbols = existing_symbols or []
            suggestions = {
                'momentum_leaders': [],
                'volume_breakouts': [],
                'oversold_bounces': [],
                'sector_rotation': [],
                'relative_strength': [],
                'scan_summary': {}
            }

            # Get all symbols to scan (exclude existing)
            all_symbols = self._get_scan_universe(scan_type)
            symbols_to_scan = [s for s in all_symbols if s not in existing_symbols]

            logger.info(f"Scanning {len(symbols_to_scan)} symbols for suggestions...")

            # Scan stocks in parallel for efficiency
            scanned_data = self._parallel_scan(symbols_to_scan)

            if not scanned_data:
                logger.warning("No stocks could be scanned")
                return suggestions

            # Convert to DataFrame for easier analysis
            df = pd.DataFrame(scanned_data)

            # Apply different filters and scoring
            suggestions['momentum_leaders'] = self._find_momentum_leaders(df, max_suggestions)
            suggestions['volume_breakouts'] = self._find_volume_breakouts(df, max_suggestions)
            suggestions['oversold_bounces'] = self._find_oversold_bounces(df, max_suggestions)
            suggestions['sector_rotation'] = self._find_sector_rotation_plays(df, max_suggestions)
            suggestions['relative_strength'] = self._find_relative_strength_leaders(df, max_suggestions)

            # Generate summary
            suggestions['scan_summary'] = {
                'total_scanned': len(scanned_data),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'scan_type': scan_type,
                'top_overall': self._get_top_overall(df, max_suggestions)
            }

            logger.info(f"Generated {sum(len(v) for k, v in suggestions.items() if isinstance(v, list))} total suggestions")
            return suggestions

        except Exception as e:
            logger.error(f"Error getting suggestions: {str(e)}")
            return {'error': str(e)}

    def _get_scan_universe(self, scan_type):
        """Get the list of symbols to scan based on scan type"""
        if scan_type == 'all':
            # Combine all universes, remove duplicates
            all_symbols = []
            for symbols in self.universes.values():
                all_symbols.extend(symbols)
            return list(set(all_symbols))
        elif scan_type in self.universes:
            return self.universes[scan_type]
        else:
            # Default to tech + growth
            return list(set(self.universes['sp500_tech'] + self.universes['growth_stocks']))

    def _parallel_scan(self, symbols, max_workers=10):
        """Scan stocks in parallel for efficiency"""
        results = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_symbol = {
                executor.submit(self._scan_single_stock, symbol): symbol
                for symbol in symbols
            }

            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.debug(f"Error scanning {symbol}: {str(e)}")

        return results

    def _scan_single_stock(self, symbol):
        """Scan a single stock and return its metrics"""
        try:
            ticker = yf.Ticker(symbol)
            hist = get_daily(symbol, '3mo')

            if hist.empty or len(hist) < 20:
                return None

            info = ticker.info

            # Basic filters
            avg_volume = hist['Volume'].mean()
            current_price = hist['Close'].iloc[-1]

            if avg_volume < self.min_volume:
                return None
            if current_price < self.min_price or current_price > self.max_price:
                return None

            # Calculate metrics
            # Momentum (20-day and 50-day returns)
            momentum_20d = (current_price - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20] if len(hist) >= 20 else 0
            momentum_50d = (current_price - hist['Close'].iloc[-50]) / hist['Close'].iloc[-50] if len(hist) >= 50 else momentum_20d

            # Volume ratio (today vs 20-day average)
            current_volume = hist['Volume'].iloc[-1]
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1

            # RSI calculation
            delta = hist['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = (100 - (100 / (1 + rs))).iloc[-1]

            # SMAs
            sma20 = hist['Close'].rolling(window=20).mean().iloc[-1]
            sma50 = hist['Close'].rolling(window=50).mean().iloc[-1] if len(hist) >= 50 else sma20

            # Trend strength
            price_vs_sma20 = (current_price - sma20) / sma20 if sma20 > 0 else 0
            price_vs_sma50 = (current_price - sma50) / sma50 if sma50 > 0 else 0

            # Volatility
            volatility = hist['Close'].pct_change().std() * np.sqrt(252)

            # 52-week position
            week_high = info.get('fiftyTwoWeekHigh', current_price)
            week_low = info.get('fiftyTwoWeekLow', current_price)
            week_range_position = (current_price - week_low) / (week_high - week_low) if week_high != week_low else 0.5

            # Relative strength vs SPY (simplified)
            relative_strength = momentum_20d  # Could be enhanced with actual SPY comparison

            # Get sector
            sector = info.get('sector', 'Unknown')

            return {
                'symbol': symbol,
                'name': info.get('shortName', symbol),
                'sector': sector,
                'price': round(current_price, 2),
                'momentum_20d': round(momentum_20d * 100, 2),
                'momentum_50d': round(momentum_50d * 100, 2),
                'volume_ratio': round(volume_ratio, 2),
                'avg_volume': int(avg_volume),
                'rsi': round(rsi, 1) if not pd.isna(rsi) else 50,
                'price_vs_sma20': round(price_vs_sma20 * 100, 2),
                'price_vs_sma50': round(price_vs_sma50 * 100, 2),
                'volatility': round(volatility * 100, 2),
                'week_range_position': round(week_range_position * 100, 2),
                'relative_strength': round(relative_strength * 100, 2),
                'market_cap': info.get('marketCap', 0)
            }

        except Exception as e:
            logger.debug(f"Error scanning {symbol}: {str(e)}")
            return None

    def _find_momentum_leaders(self, df, limit):
        """Find stocks with strong momentum"""
        try:
            # Filter for positive momentum and good volume
            momentum_df = df[
                (df['momentum_20d'] > 5) &  # At least 5% gain in 20 days
                (df['volume_ratio'] > 0.8) &  # Decent volume
                (df['rsi'] < 75)  # Not extremely overbought
            ].copy()

            # Score by combined momentum
            momentum_df['score'] = (
                momentum_df['momentum_20d'] * 0.5 +
                momentum_df['momentum_50d'] * 0.3 +
                momentum_df['volume_ratio'] * 10 * 0.2
            )

            # Sort and return top
            momentum_df = momentum_df.sort_values('score', ascending=False).head(limit)

            return [
                {
                    'symbol': row['symbol'],
                    'name': row['name'],
                    'sector': row['sector'],
                    'price': row['price'],
                    'momentum_20d': row['momentum_20d'],
                    'momentum_50d': row['momentum_50d'],
                    'rsi': row['rsi'],
                    'reason': f"Strong momentum: +{row['momentum_20d']:.1f}% (20d), +{row['momentum_50d']:.1f}% (50d)"
                }
                for _, row in momentum_df.iterrows()
            ]

        except Exception as e:
            logger.error(f"Error finding momentum leaders: {str(e)}")
            return []

    def _find_volume_breakouts(self, df, limit):
        """Find stocks with unusual volume activity"""
        try:
            # Filter for high volume with positive price action
            volume_df = df[
                (df['volume_ratio'] > 2.0) &  # At least 2x normal volume
                (df['momentum_20d'] > 0)  # Positive price direction
            ].copy()

            # Score by volume ratio and momentum
            volume_df['score'] = (
                volume_df['volume_ratio'] * 0.6 +
                volume_df['momentum_20d'] * 0.4
            )

            volume_df = volume_df.sort_values('score', ascending=False).head(limit)

            return [
                {
                    'symbol': row['symbol'],
                    'name': row['name'],
                    'sector': row['sector'],
                    'price': row['price'],
                    'volume_ratio': row['volume_ratio'],
                    'momentum_20d': row['momentum_20d'],
                    'reason': f"Volume breakout: {row['volume_ratio']:.1f}x normal volume"
                }
                for _, row in volume_df.iterrows()
            ]

        except Exception as e:
            logger.error(f"Error finding volume breakouts: {str(e)}")
            return []

    def _find_oversold_bounces(self, df, limit):
        """Find oversold stocks showing signs of bounce"""
        try:
            # Filter for oversold conditions with positive recent action
            oversold_df = df[
                (df['rsi'] < 35) &  # Oversold RSI
                (df['week_range_position'] < 30) &  # Near 52-week low
                (df['momentum_20d'] > -15)  # Not in free fall
            ].copy()

            # Score by oversold level and potential
            oversold_df['score'] = (
                (40 - oversold_df['rsi']) * 0.4 +  # More oversold = higher score
                (30 - oversold_df['week_range_position']) * 0.3 +
                (oversold_df['momentum_20d'] + 15) * 0.3
            )

            oversold_df = oversold_df.sort_values('score', ascending=False).head(limit)

            return [
                {
                    'symbol': row['symbol'],
                    'name': row['name'],
                    'sector': row['sector'],
                    'price': row['price'],
                    'rsi': row['rsi'],
                    'week_range_position': row['week_range_position'],
                    'reason': f"Oversold bounce candidate: RSI {row['rsi']:.0f}, {row['week_range_position']:.0f}% of 52w range"
                }
                for _, row in oversold_df.iterrows()
            ]

        except Exception as e:
            logger.error(f"Error finding oversold bounces: {str(e)}")
            return []

    def _find_sector_rotation_plays(self, df, limit):
        """Find stocks in strong sectors showing relative strength"""
        try:
            # Group by sector and find average momentum
            sector_momentum = df.groupby('sector')['momentum_20d'].mean().sort_values(ascending=False)
            top_sectors = sector_momentum.head(3).index.tolist()

            # Filter for stocks in top sectors
            rotation_df = df[df['sector'].isin(top_sectors)].copy()

            # Score by sector + individual strength
            rotation_df['sector_avg'] = rotation_df['sector'].map(sector_momentum)
            rotation_df['score'] = (
                rotation_df['momentum_20d'] * 0.5 +
                rotation_df['sector_avg'] * 0.3 +
                rotation_df['volume_ratio'] * 5 * 0.2
            )

            rotation_df = rotation_df.sort_values('score', ascending=False).head(limit)

            return [
                {
                    'symbol': row['symbol'],
                    'name': row['name'],
                    'sector': row['sector'],
                    'price': row['price'],
                    'momentum_20d': row['momentum_20d'],
                    'reason': f"Strong sector ({row['sector']}): Stock +{row['momentum_20d']:.1f}%, Sector avg +{row['sector_avg']:.1f}%"
                }
                for _, row in rotation_df.iterrows()
            ]

        except Exception as e:
            logger.error(f"Error finding sector rotation plays: {str(e)}")
            return []

    def _find_relative_strength_leaders(self, df, limit):
        """Find stocks with strong relative strength"""
        try:
            # Filter for stocks above both SMAs with strong momentum
            rs_df = df[
                (df['price_vs_sma20'] > 2) &  # Above SMA20
                (df['price_vs_sma50'] > 5) &  # Above SMA50
                (df['momentum_20d'] > 3)  # Positive momentum
            ].copy()

            # Score by relative strength metrics
            rs_df['score'] = (
                rs_df['price_vs_sma20'] * 0.3 +
                rs_df['price_vs_sma50'] * 0.3 +
                rs_df['momentum_20d'] * 0.4
            )

            rs_df = rs_df.sort_values('score', ascending=False).head(limit)

            return [
                {
                    'symbol': row['symbol'],
                    'name': row['name'],
                    'sector': row['sector'],
                    'price': row['price'],
                    'price_vs_sma20': row['price_vs_sma20'],
                    'price_vs_sma50': row['price_vs_sma50'],
                    'reason': f"Relative strength: {row['price_vs_sma20']:.1f}% above SMA20, {row['price_vs_sma50']:.1f}% above SMA50"
                }
                for _, row in rs_df.iterrows()
            ]

        except Exception as e:
            logger.error(f"Error finding relative strength leaders: {str(e)}")
            return []

    def _get_top_overall(self, df, limit):
        """Get top overall suggestions combining all metrics"""
        try:
            scored_df = df.copy()

            # Normalize scores to 0-100 scale
            for col in ['momentum_20d', 'volume_ratio', 'relative_strength']:
                if col in scored_df.columns:
                    min_val = scored_df[col].min()
                    max_val = scored_df[col].max()
                    if max_val > min_val:
                        scored_df[f'{col}_norm'] = (scored_df[col] - min_val) / (max_val - min_val) * 100
                    else:
                        scored_df[f'{col}_norm'] = 50

            # RSI score (optimal around 50-60)
            scored_df['rsi_score'] = 100 - abs(scored_df['rsi'] - 55) * 2

            # Calculate composite score
            scored_df['composite_score'] = (
                scored_df.get('momentum_20d_norm', 50) * self.weights['momentum'] +
                scored_df.get('volume_ratio_norm', 50) * self.weights['volume'] +
                scored_df['price_vs_sma20'] * self.weights['trend'] +
                scored_df['rsi_score'] * self.weights['rsi_position'] +
                scored_df.get('relative_strength_norm', 50) * self.weights['relative_strength']
            )

            top_df = scored_df.sort_values('composite_score', ascending=False).head(limit)

            return [
                {
                    'symbol': row['symbol'],
                    'name': row['name'],
                    'sector': row['sector'],
                    'price': row['price'],
                    'composite_score': round(row['composite_score'], 1),
                    'momentum_20d': row['momentum_20d'],
                    'rsi': row['rsi'],
                    'volume_ratio': row['volume_ratio']
                }
                for _, row in top_df.iterrows()
            ]

        except Exception as e:
            logger.error(f"Error getting top overall: {str(e)}")
            return []

    def quick_scan(self, symbols):
        """
        Quick scan of specific symbols for immediate opportunities.

        Args:
            symbols (list): List of symbols to scan

        Returns:
            list: Quick scan results
        """
        try:
            results = self._parallel_scan(symbols, max_workers=5)
            df = pd.DataFrame(results) if results else pd.DataFrame()

            if df.empty:
                return []

            # Add quick recommendation
            recommendations = []
            for _, row in df.iterrows():
                rec = {
                    'symbol': row['symbol'],
                    'price': row['price'],
                    'rsi': row['rsi'],
                    'momentum_20d': row['momentum_20d'],
                    'volume_ratio': row['volume_ratio']
                }

                # Quick signal
                if row['rsi'] < 30 and row['momentum_20d'] > -10:
                    rec['signal'] = 'POTENTIAL_BUY'
                    rec['reason'] = 'Oversold with stabilizing momentum'
                elif row['rsi'] > 70 and row['momentum_20d'] < 5:
                    rec['signal'] = 'POTENTIAL_SELL'
                    rec['reason'] = 'Overbought with weakening momentum'
                elif row['momentum_20d'] > 10 and row['volume_ratio'] > 1.5:
                    rec['signal'] = 'WATCH_BREAKOUT'
                    rec['reason'] = 'Strong momentum with volume confirmation'
                else:
                    rec['signal'] = 'NEUTRAL'
                    rec['reason'] = 'No clear setup'

                recommendations.append(rec)

            return recommendations

        except Exception as e:
            logger.error(f"Error in quick scan: {str(e)}")
            return []
