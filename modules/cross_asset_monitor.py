"""
Cross-Asset Monitor (PRD §6.2).

Tracks six macro indicators daily and emits a RISK-OFF warning when
DXY 5-day change > +1.5% AND HYG 5-day change < -1%.

Indicators (all via yfinance):
  DXY   DX-Y.NYB   US Dollar Index
  HG=F  Copper     Dr. Copper — global growth proxy
  CL=F  Crude Oil  Inflation / energy proxy
  JPY=X JPY/USD    Yen strength → carry trade unwind risk
  HYG   HYG        High-yield ETF — credit market health
  T10Y2Y (FRED)    2Y-10Y Treasury spread — recession probability

Output: dict of current values + 5-day change % for each indicator,
plus 'risk_off_warning' bool and 'risk_off_reason' str.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
from modules.price_history import get_daily

logger = logging.getLogger(__name__)

_INDICATORS = {
    "DXY": {"symbol": "DX-Y.NYB", "label": "US Dollar Index"},
    "Copper": {"symbol": "HG=F", "label": "Copper (Dr. Copper)"},
    "Crude_Oil": {"symbol": "CL=F", "label": "Crude Oil"},
    "JPY_USD": {"symbol": "JPY=X", "label": "JPY/USD"},
    "HYG": {"symbol": "HYG", "label": "High-Yield ETF"},
}

_RISK_OFF_DXY_THRESHOLD = 1.5   # DXY +1.5% in 5 days
_RISK_OFF_HYG_THRESHOLD = -1.0  # HYG -1.0% in 5 days


def _fetch_5day_change(yf_symbol: str) -> tuple[float | None, float | None]:
    """
    Return (current_price, 5_day_change_pct) for yf_symbol.
    Both are None on failure.
    """
    try:
        # yf.download shares global state and mixes tickers when called from
        # the thread pool below ("cannot convert the series to float");
        # get_daily goes through Ticker.history, which is safe per-thread.
        hist = get_daily(yf_symbol, '1mo')
        if hist.empty or len(hist) < 2:
            return None, None
        closes = hist['Close']

        current = float(closes.iloc[-1])
        # Use actual 5-trading-day look-back (or as many as available)
        lookback = min(5, len(closes) - 1)
        prior = float(closes.iloc[-(lookback + 1)])
        change_pct = (current - prior) / prior * 100
        return round(current, 4), round(change_pct, 2)

    except Exception as e:
        logger.warning(f"[CrossAsset] Could not fetch {yf_symbol}: {e}")
        return None, None


def _fetch_t10y2y() -> tuple[float | None, float | None]:
    """
    Fetch 2Y-10Y Treasury spread from FRED (T10Y2Y series).
    Returns (current_value, 5_day_change) in percentage points.
    """
    try:
        from config.config import Config
        fred_key = getattr(Config, "FRED_API_KEY", None)
        if fred_key:
            # fredapi uses urllib; this step runs before FinancialDataScraper
            # installs its certifi opener, so without this the FRED call fails
            # SSL verification on python.org builds of macOS Python.
            import ssl
            import urllib.request

            import certifi
            from fredapi import Fred
            urllib.request.install_opener(urllib.request.build_opener(
                urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=certifi.where()))))
            fred = Fred(api_key=fred_key)
            series = fred.get_series("T10Y2Y", observation_start=str(
                datetime.utcnow().date() - timedelta(days=30)
            ))
            series = series.dropna()
            if len(series) >= 2:
                current = float(series.iloc[-1])
                lookback = min(5, len(series) - 1)
                prior = float(series.iloc[-(lookback + 1)])
                change = round(current - prior, 3)
                return round(current, 3), change
    except Exception as e:
        logger.warning(f"[CrossAsset] FRED T10Y2Y fetch failed: {e}")

    # Fallback: 10Y minus 13-week bill, both quoted in percent by Yahoo.
    # Yahoo has no 2Y index, so this is the 10Y-3M spread — same sign
    # convention (negative = inverted), usually a bit wider.
    try:
        t10, _ = _fetch_5day_change("^TNX")
        t3m, _ = _fetch_5day_change("^IRX")
        if t10 is not None and t3m is not None:
            return round(t10 - t3m, 3), None
    except Exception:
        pass

    return None, None


class CrossAssetMonitor:
    """
    Fetch cross-asset snapshot and evaluate RISK-OFF conditions.
    """

    def cross_asset_snapshot(self) -> dict:
        """
        Return dict with current values and 5-day change % for all indicators,
        plus RISK-OFF warning flag.

        Schema:
          {
            "DXY":      {"current": 104.5, "change_5d_pct": 1.8, "label": "..."},
            "Copper":   {...},
            "Crude_Oil":{...},
            "JPY_USD":  {...},
            "HYG":      {...},
            "T10Y2Y":   {"current": -0.3, "change_5d": 0.05, "label": "2Y-10Y Spread"},
            "risk_off_warning": True,
            "risk_off_reason": "DXY +1.8% (5d) and HYG -1.3% (5d)",
            "timestamp": "2026-04-01T08:15:00"
          }
        """
        snapshot: dict = {}

        # Fetch equity/commodity/FX indicators concurrently
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch(key, sym):
            cur, chg = _fetch_5day_change(sym)
            return key, cur, chg

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(_fetch, key, info["symbol"]): key
                for key, info in _INDICATORS.items()
            }
            for fut in as_completed(futures):
                key, cur, chg = fut.result()
                snapshot[key] = {
                    "current": cur,
                    "change_5d_pct": chg,
                    "label": _INDICATORS[key]["label"],
                }

        # FRED spread
        t10y2y_cur, t10y2y_chg = _fetch_t10y2y()
        snapshot["T10Y2Y"] = {
            "current": t10y2y_cur,
            "change_5d": t10y2y_chg,
            "label": "2Y-10Y Treasury Spread",
        }

        # RISK-OFF evaluation (PRD §6.2.3)
        dxy_chg = snapshot.get("DXY", {}).get("change_5d_pct")
        hyg_chg = snapshot.get("HYG", {}).get("change_5d_pct")

        risk_off = False
        reason = ""
        if dxy_chg is not None and hyg_chg is not None:
            if dxy_chg > _RISK_OFF_DXY_THRESHOLD and hyg_chg < _RISK_OFF_HYG_THRESHOLD:
                risk_off = True
                reason = (
                    f"DXY {dxy_chg:+.1f}% (5d) and HYG {hyg_chg:+.1f}% (5d) "
                    f"— risk-off conditions detected"
                )
                logger.warning(f"[CrossAsset] RISK-OFF WARNING: {reason}")

        snapshot["risk_off_warning"] = risk_off
        snapshot["risk_off_reason"] = reason
        snapshot["timestamp"] = datetime.now().isoformat()

        return snapshot

    def format_summary_table(self, snapshot: dict) -> str:
        """Return a plain-text table suitable for report inclusion."""
        lines = [
            f"{'Indicator':<20} {'Current':>10} {'5d Change':>12}",
            "-" * 44,
        ]
        for key in ("DXY", "Copper", "Crude_Oil", "JPY_USD", "HYG"):
            d = snapshot.get(key, {})
            cur = f"{d['current']:.2f}" if d.get("current") is not None else "N/A"
            chg = f"{d['change_5d_pct']:+.2f}%" if d.get("change_5d_pct") is not None else "N/A"
            label = d.get("label", key)
            lines.append(f"{label:<20} {cur:>10} {chg:>12}")

        d = snapshot.get("T10Y2Y", {})
        cur = f"{d['current']:.3f}" if d.get("current") is not None else "N/A"
        lines.append(f"{'2Y-10Y Spread':<20} {cur:>10} {'':>12}")

        if snapshot.get("risk_off_warning"):
            lines += ["", f"⚠  RISK-OFF: {snapshot['risk_off_reason']}"]

        return "\n".join(lines)
