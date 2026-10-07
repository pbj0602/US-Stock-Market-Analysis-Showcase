"""
Data Scraping Module
Scrapes daily market data, volume, and technical indicators for US stocks and ETFs.

Phase 1 enhancements (PRD §4):
  - diskcache-backed TTL cache via CacheManager
  - Concurrent fetching with ThreadPoolExecutor (default 5 workers)
  - no_cache flag for forced refresh (maps to --no-cache CLI flag)
"""

import yfinance as yf
from modules.price_history import get_daily
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from config.config import Config

logger = logging.getLogger(__name__)

# Optional SQLite cache — imported lazily to avoid circular deps
try:
    from modules.data_store import DataStore as _DataStore
    _STORE = _DataStore()
except Exception:
    _STORE = None

# diskcache-backed cache manager
try:
    from modules.cache_manager import get_cache as _get_cache
    _CACHE = _get_cache()
except Exception:
    _CACHE = None


class DataScraper:
    def __init__(self, no_cache: bool = False, max_workers: int = 5):
        """
        Args:
            no_cache: If True, skip all cache reads (force fresh fetches).
            max_workers: Thread pool size for concurrent fetching.
        """
        self.data_cache = {}
        self._store = _STORE
        self._cache = _CACHE
        self.no_cache = no_cache
        self.max_workers = max_workers

    # ------------------------------------------------------------------
    # Internal: fetch a single symbol (called by ThreadPoolExecutor)
    # ------------------------------------------------------------------
    def _fetch_single(self, symbol: str, period: str):
        """Fetch and process one symbol. Returns a pd.Series or None."""
        # 1. Check diskcache
        cache_key = None
        if self._cache and not self.no_cache:
            date_str = datetime.now().strftime("%Y-%m-%d")
            cache_key = self._cache.make_key(symbol, f"daily_{period}", date_str)
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit: {symbol}")
                return cached

        try:
            logger.info(f"Fetching data for {symbol}")

            hist = get_daily(symbol, period)

            if hist.empty:
                logger.warning(f"No data found for {symbol}")
                return None

            hist = hist.dropna()
            hist['Symbol'] = symbol
            hist['Date'] = hist.index
            hist.reset_index(drop=True, inplace=True)

            hist['Price_Change'] = hist['Close'] - hist['Close'].shift(1)
            hist['Price_Change_Pct'] = (
                (hist['Close'] - hist['Close'].shift(1)) / hist['Close'].shift(1) * 100
            )
            hist['Volatility'] = (
                hist['Close'].pct_change().rolling(window=20).std() * np.sqrt(252)
            )

            hist = self._calculate_technical_indicators(hist)

            latest_data = hist.iloc[-1].copy()

            if 'Price_Change_Pct' in latest_data:
                latest_data['Day_Change_Pct'] = latest_data['Price_Change_Pct']
            if 'Price_Change' in latest_data:
                latest_data['Day_Change'] = latest_data['Price_Change']

            # Day change stays on the last *completed* session, the same basis
            # as the sector ETFs and every indicator. The live quote's change
            # is intraday once the run slips past 09:30 ET (it usually does),
            # which put stocks and their sector ETFs a session apart.
            current_data = self._get_current_session_data(symbol)
            if current_data:
                for key, value in current_data.items():
                    if key not in ['Day_Change', 'Day_Change_Pct'] and value is not None:
                        latest_data[key] = value

            # Store in diskcache
            if self._cache and cache_key:
                self._cache.set(cache_key, latest_data, ttl=self._cache.TTL_DAILY_OHLCV)

            return latest_data

        except Exception as e:
            logger.warning(f"Error fetching data for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    # 1y, not 6mo: SMA200 and the 126-day relative-strength lookback both need
    # more than six months of closes. At "6mo" SMA_200 was entirely NaN, which
    # would have silently failed open on the satellite uptrend gate.
    def get_market_data(self, symbols, period="1y"):
        """
        Fetch market data and technical indicators for all symbols concurrently.

        Args:
            symbols (list): Stock/ETF symbols.
            period (str): yfinance period string (e.g. '3mo', '1y').

        Returns:
            pd.DataFrame: One row per symbol with latest data + indicators.
        """
        all_data = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._fetch_single, sym, period): sym
                for sym in symbols
            }
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        all_data.append(result)
                except Exception as e:
                    logger.warning(f"Unhandled error for {sym}: {e}")

        if not all_data:
            logger.error("No data was successfully scraped")
            return pd.DataFrame()

        result_df = pd.DataFrame(all_data)
        result_df = result_df.round(4)
        logger.info(f"Successfully scraped data for {len(result_df)} symbols")
        return result_df

    def _calculate_technical_indicators(self, df):
        """Calculate technical indicators for the given DataFrame."""
        try:
            df['SMA_10'] = df['Close'].rolling(window=10).mean()
            df['SMA_20'] = df['Close'].rolling(window=20).mean()
            df['SMA_50'] = df['Close'].rolling(window=50).mean()
            # SMA200 is the long-trend filter for the satellite universe gate
            # (price > SMA50 > SMA200). min_periods lets it produce a usable
            # value before a full 200 sessions have accumulated.
            df['SMA_200'] = df['Close'].rolling(window=200, min_periods=150).mean()
            df['EMA_20'] = df['Close'].ewm(span=20).mean()

            delta = df['Close'].diff()
            # Wilder's Smoothed Moving Average (EWM alpha=1/14) — matches TradingView/Bloomberg
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss.replace(0, np.nan)  # guard against all-up days (loss=0 → RSI=100)
            df['RSI'] = 100 - (100 / (1 + rs))

            exp1 = df['Close'].ewm(span=12).mean()
            exp2 = df['Close'].ewm(span=26).mean()
            df['MACD'] = exp1 - exp2
            df['MACD_Signal'] = df['MACD'].ewm(span=9).mean()
            df['MACD_Histogram'] = df['MACD'] - df['MACD_Signal']

            df['BB_Middle'] = df['Close'].rolling(window=20).mean()
            bb_std = df['Close'].rolling(window=20).std()
            df['BB_Upper'] = df['BB_Middle'] + (bb_std * 2)
            df['BB_Lower'] = df['BB_Middle'] - (bb_std * 2)
            df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle']
            df['BB_Position'] = (
                (df['Close'] - df['BB_Lower']) / (df['BB_Upper'] - df['BB_Lower'])
            )

            df['Momentum_10'] = df['Close'] / df['Close'].shift(10) - 1
            df['Momentum_20'] = df['Close'] / df['Close'].shift(20) - 1
            # ~6-month total return, the relative-strength measure the
            # satellite universe gate ranks against the benchmark's own.
            _rs_lb = Config.SATELLITE_RS_LOOKBACK_DAYS
            if len(df) > _rs_lb:
                df['Momentum_126'] = df['Close'] / df['Close'].shift(_rs_lb) - 1
            else:
                # Not enough history yet — fall back to the full available span
                # so a newly-listed name isn't silently treated as strong.
                df['Momentum_126'] = df['Close'] / df['Close'].iloc[0] - 1

            df['Volume_SMA_20'] = df['Volume'].rolling(window=20).mean()
            df['Volume_Ratio'] = df['Volume'] / df['Volume_SMA_20']

            df['Price_vs_SMA50'] = (df['Close'] - df['SMA_50']) / df['SMA_50']
            df['SMA10_vs_SMA50'] = (df['SMA_10'] - df['SMA_50']) / df['SMA_50']
            df['Price_vs_SMA200'] = (df['Close'] - df['SMA_200']) / df['SMA_200']
            df['SMA50_vs_SMA200'] = (df['SMA_50'] - df['SMA_200']) / df['SMA_200']

            # Genuine SMA50 breakout detection: a "cross up" is price above SMA50
            # today but at/below it yesterday. Days_Since_SMA50_Cross lets the
            # signal layer distinguish a fresh breakout (≤3 sessions) from a stock
            # that has merely sat above its SMA50 for weeks. 999 = no recent cross.
            above = df['Close'] > df['SMA_50']
            cross_up = above & ~above.shift(1, fill_value=False)
            groups = cross_up.cumsum()
            df['Days_Since_SMA50_Cross'] = (~cross_up).groupby(groups).cumcount()
            df.loc[groups == 0, 'Days_Since_SMA50_Cross'] = 999

            # Structural levels for stop/target placement (#5): anchor exits to
            # real price structure rather than arbitrary fixed percentages.
            #   Recent_Swing_Low  — 20-session low → natural stop reference
            #   Recent_Swing_High — 60-session high → overhead resistance / recovery target
            if 'Low' in df.columns:
                df['Recent_Swing_Low'] = df['Low'].rolling(window=20, min_periods=5).min()
            if 'High' in df.columns:
                df['Recent_Swing_High'] = df['High'].rolling(window=60, min_periods=10).max()

            return df

        except Exception as e:
            logger.error(f"Error calculating technical indicators: {e}")
            return df

    def _get_current_session_data(self, symbol):
        """Get current session data including pre-market and after-hours."""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            current_data = {
                'Current_Price': info.get('currentPrice', info.get('regularMarketPrice')),
                'Previous_Close': info.get('previousClose', info.get('regularMarketPreviousClose')),
                'Day_High': info.get('dayHigh', info.get('regularMarketDayHigh')),
                'Day_Low': info.get('dayLow', info.get('regularMarketDayLow')),
                'Day_Volume': info.get('volume', info.get('regularMarketVolume')),
                'Market_Cap': info.get('marketCap'),
                'PE_Ratio': info.get('trailingPE'),
                'Forward_PE': info.get('forwardPE'),
                'PEG_Ratio': info.get('pegRatio'),
                'Dividend_Yield': info.get('dividendYield'),
                'Beta': info.get('beta'),
                '52_Week_High': info.get('fiftyTwoWeekHigh'),
                '52_Week_Low': info.get('fiftyTwoWeekLow'),
            }

            for key, value in current_data.items():
                if value is None:
                    current_data[key] = (
                        0 if key in [
                            'Market_Cap', 'PE_Ratio', 'Forward_PE', 'PEG_Ratio',
                            'Dividend_Yield', 'Beta', '52_Week_High', '52_Week_Low',
                        ] else None
                    )

            if current_data['Current_Price'] and current_data['Previous_Close']:
                current_data['Day_Change'] = (
                    current_data['Current_Price'] - current_data['Previous_Close']
                )
                current_data['Day_Change_Pct'] = (
                    current_data['Day_Change'] / current_data['Previous_Close'] * 100
                )

            return current_data

        except Exception as e:
            logger.error(f"Error getting current session data for {symbol}: {e}")
            return {}

    def get_historical_data(self, symbols, start_date, end_date):
        """Get historical data for specific date range (uses SQLite cache when available)."""
        all_data = []

        for symbol in symbols:
            try:
                if self._store and self._store.is_data_cached(symbol, start_date, end_date):
                    cached = self._store.get_price_data([symbol], start_date, end_date)
                    if not cached.empty:
                        cached = self._calculate_technical_indicators(cached)
                        all_data.append(cached)
                        logger.debug(f"Cache hit for {symbol}")
                        continue

                ticker = yf.Ticker(symbol)
                hist = ticker.history(start=start_date, end=end_date)

                if not hist.empty:
                    hist['Symbol'] = symbol
                    hist['Date'] = hist.index
                    hist.reset_index(drop=True, inplace=True)
                    hist['Date'] = pd.to_datetime(hist['Date']).dt.tz_localize(None)
                    hist = self._calculate_technical_indicators(hist)
                    if self._store:
                        self._store.save_price_data(hist)
                    all_data.append(hist)

            except Exception as e:
                logger.error(f"Error getting historical data for {symbol}: {e}")
                continue

        if all_data:
            return pd.concat(all_data, ignore_index=True)
        return pd.DataFrame()

    def get_sector_etf_data(self):
        """Get data for major sector ETFs for market overview."""
        sector_etfs = [
            'XLK', 'XLF', 'XLV', 'XLI', 'XLE',
            'XLB', 'XLP', 'XLY', 'XLU', 'XLRE',
        ]
        return self.get_market_data(sector_etfs)

    def save_data(self, df, filename):
        """Save DataFrame to CSV file."""
        try:
            filepath = f"data/{filename}_{datetime.now().strftime('%Y%m%d')}.csv"
            df.to_csv(filepath, index=False)
            logger.info(f"Data saved to {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving data: {e}")
            return None
