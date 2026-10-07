"""
Cache Manager — diskcache-backed TTL cache for market data.

TTL rules (PRD §4.2):
  intraday 1m/5m  → 10 minutes
  daily OHLCV     → 15 min during market hours, 12 h after close
  FRED macro      → 24 hours
  news sentiment  → 30 minutes
"""

import logging
import os
from datetime import datetime, time as dtime

logger = logging.getLogger(__name__)

# Try to import diskcache; fall back gracefully if not installed.
try:
    import diskcache as dc
    _DC_AVAILABLE = True
except ImportError:
    _DC_AVAILABLE = False
    logger.warning("diskcache not installed — pip install diskcache. Cache disabled.")


def _market_open() -> bool:
    """Return True if current ET wall-clock time is within market hours (09:30–16:00)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    now_et = datetime.now(ZoneInfo("America/New_York")).time()
    return dtime(9, 30) <= now_et <= dtime(16, 0)


def _ttl_daily_ohlcv() -> int:
    """15-minute TTL during market hours, 12-hour TTL after close."""
    return 15 * 60 if _market_open() else 12 * 3600


TTL_INTRADAY = 10 * 60       # 10 minutes
TTL_DAILY_OHLCV = None       # resolved dynamically
TTL_FRED_MACRO = 24 * 3600   # 24 hours
TTL_NEWS_SENTIMENT = 30 * 60  # 30 minutes


class CacheManager:
    """
    Thin wrapper around diskcache.Cache with domain-specific TTL helpers.

    Usage:
        cm = CacheManager()
        cm.set("AAPL_1d_2026-04-01", data, ttl=cm.TTL_DAILY_OHLCV)
        data = cm.get("AAPL_1d_2026-04-01")
    """

    def __init__(self, cache_dir: str = "cache"):
        self._enabled = _DC_AVAILABLE
        self._cache = None
        if self._enabled:
            os.makedirs(cache_dir, exist_ok=True)
            try:
                self._cache = dc.Cache(cache_dir)
                logger.info(f"CacheManager initialised at '{cache_dir}'")
            except Exception as e:
                logger.error(f"Failed to open diskcache at '{cache_dir}': {e}")
                self._enabled = False

    # ------------------------------------------------------------------
    # Public TTL constants
    # ------------------------------------------------------------------
    @property
    def TTL_INTRADAY(self) -> int:
        return TTL_INTRADAY

    @property
    def TTL_DAILY_OHLCV(self) -> int:
        return _ttl_daily_ohlcv()

    @property
    def TTL_FRED_MACRO(self) -> int:
        return TTL_FRED_MACRO

    @property
    def TTL_NEWS_SENTIMENT(self) -> int:
        return TTL_NEWS_SENTIMENT

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------
    @staticmethod
    def make_key(ticker: str, interval: str, date: str) -> str:
        """Canonical cache key: {ticker}_{interval}_{date}"""
        return f"{ticker}_{interval}_{date}"

    def get(self, key: str):
        """Return cached value or None if missing / cache disabled."""
        if not self._enabled:
            return None
        try:
            return self._cache.get(key)
        except Exception as e:
            logger.warning(f"Cache get error for '{key}': {e}")
            return None

    def set(self, key: str, value, ttl: int) -> bool:
        """Store value with the given TTL (seconds). Returns True on success."""
        if not self._enabled:
            return False
        try:
            self._cache.set(key, value, expire=ttl)
            return True
        except Exception as e:
            logger.warning(f"Cache set error for '{key}': {e}")
            return False

    def delete(self, key: str) -> None:
        if not self._enabled:
            return
        try:
            self._cache.delete(key)
        except Exception:
            pass

    def clear(self) -> None:
        """Wipe the entire cache (used by --no-cache flag)."""
        if not self._enabled:
            return
        try:
            self._cache.clear()
            logger.info("Cache cleared")
        except Exception as e:
            logger.warning(f"Cache clear error: {e}")

    def close(self) -> None:
        if self._cache:
            try:
                self._cache.close()
            except Exception:
                pass


# Module-level singleton — import and reuse across the codebase.
_instance: CacheManager | None = None


def get_cache(cache_dir: str = "cache") -> CacheManager:
    global _instance
    if _instance is None:
        _instance = CacheManager(cache_dir)
    return _instance
