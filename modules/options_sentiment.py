"""
Options Market Sentiment (PRD §6.1).

Two signals:
  1. CBOE Put/Call ratio — fetched from CBOE public CSV (no API key needed)
     P/C > 1.2  → elevated fear  → contrarian bullish signal
     P/C < 0.7  → complacency   → contrarian bearish signal

  2. VIX term structure (VIX vs VIX3M via yfinance ^VIX and ^VIX3M)
     VIX3M / VIX < 1.0 → inverted term structure → institutional hedging / caution

These are returned as additional feature inputs for the existing market
state classifier (appended to market_context_data).
"""

import logging
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

# CBOE CDN URL — kept as first attempt; falls back to SPY options if 403/unavailable
_CBOE_PC_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/PCALL_History.csv"
)

_PC_FEAR_THRESHOLD = 1.2     # above → elevated fear (contrarian bullish)
_PC_COMPLACENCY_THRESHOLD = 0.7  # below → complacency (contrarian bearish)


class OptionsSentiment:
    """
    Fetch and interpret options market sentiment indicators.
    """

    # ------------------------------------------------------------------
    # Put/Call Ratio
    # ------------------------------------------------------------------
    def get_put_call_ratio(self) -> dict:
        """
        Fetch latest CBOE total P/C ratio from public CSV.

        Returns:
            {
              "pc_ratio": 0.94,
              "date": "2026-04-01",
              "signal": "NEUTRAL",          # "BULLISH" | "BEARISH" | "NEUTRAL"
              "signal_detail": "...",
              "source": "CBOE"
            }
        """
        # 1st attempt: CBOE public CSV
        pc, date_str, source = self._fetch_cboe_pc()

        # 2nd attempt: derive from SPY options via yfinance (free, no auth)
        if pc is None:
            pc, date_str, source = self._fetch_spy_pc()

        if pc is None:
            logger.warning("[OptionsSentiment] All P/C sources unavailable")
            return self._pc_fallback()

        if pc > _PC_FEAR_THRESHOLD:
            signal = "BULLISH"
            detail = f"P/C={pc:.2f} > {_PC_FEAR_THRESHOLD} — elevated fear, contrarian bullish signal"
        elif pc < _PC_COMPLACENCY_THRESHOLD:
            signal = "BEARISH"
            detail = f"P/C={pc:.2f} < {_PC_COMPLACENCY_THRESHOLD} — complacency, contrarian bearish signal"
        else:
            signal = "NEUTRAL"
            detail = f"P/C={pc:.2f} — within normal range"

        logger.info(f"[OptionsSentiment] P/C ratio: {pc:.2f} ({source}) → {signal}")
        return {
            "pc_ratio": round(pc, 3),
            "date": date_str,
            "signal": signal,
            "signal_detail": detail,
            "source": source,
        }

    def _fetch_cboe_pc(self):
        """Try CBOE public CSV. Returns (pc_ratio, date_str, source) or (None, None, None)."""
        try:
            from io import StringIO
            resp = requests.get(_CBOE_PC_URL, timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(StringIO(resp.text))
            ratio_col = next(
                (c for c in df.columns
                 if any(k in c.upper() for k in ("CALL", "RATIO", "P/C"))),
                None,
            )
            if ratio_col is None or df.empty:
                return None, None, None
            latest = df.iloc[-1]
            return float(latest[ratio_col]), str(latest[df.columns[0]]), "CBOE"
        except Exception as e:
            logger.debug(f"[OptionsSentiment] CBOE P/C fetch failed: {e}")
            return None, None, None

    def _fetch_spy_pc(self):
        """Calculate P/C from SPY near-term options. Returns (pc_ratio, date_str, source)."""
        try:
            spy = yf.Ticker("SPY")
            expirations = spy.options
            if not expirations:
                return None, None, None
            # Use the nearest expiry (most liquid)
            chain = spy.option_chain(expirations[0])
            put_vol  = chain.puts["volume"].fillna(0).sum()
            call_vol = chain.calls["volume"].fillna(0).sum()
            if call_vol == 0:
                return None, None, None
            pc = put_vol / call_vol
            date_str = datetime.now().strftime("%Y-%m-%d")
            logger.debug(f"[OptionsSentiment] SPY P/C proxy: puts={put_vol:.0f} calls={call_vol:.0f} ratio={pc:.3f}")
            return pc, date_str, "SPY options (proxy)"
        except Exception as e:
            logger.debug(f"[OptionsSentiment] SPY P/C fetch failed: {e}")
            return None, None, None

    def _pc_fallback(self) -> dict:
        return {
            "pc_ratio": None,
            "date": None,
            "signal": "UNKNOWN",
            "signal_detail": "P/C data unavailable",
            "source": "N/A",
        }

    # ------------------------------------------------------------------
    # VIX Term Structure
    # ------------------------------------------------------------------
    def get_vix_term_structure(self) -> dict:
        """
        Fetch VIX (spot) and VIX3M from yfinance and compute term structure.

        Returns:
            {
              "vix": 18.5,
              "vix3m": 19.2,
              "ratio": 1.038,        # VIX3M / VIX
              "inverted": False,
              "signal": "NORMAL",    # "CAUTION" if inverted
              "signal_detail": "..."
            }
        """
        try:
            vix_data = yf.download(
                ["^VIX", "^VIX3M"],
                period="2d",
                interval="1d",
                auto_adjust=True,
                progress=False,
            )

            closes = vix_data["Close"]
            vix = float(closes["^VIX"].dropna().iloc[-1])
            vix3m = float(closes["^VIX3M"].dropna().iloc[-1])

            ratio = vix3m / vix if vix > 0 else None
            inverted = ratio is not None and ratio < 1.0

            ratio_str = f"{ratio:.3f}" if ratio is not None else "N/A"
            if inverted:
                signal = "CAUTION"
                detail = (
                    f"VIX3M/VIX={ratio_str} < 1.0 — inverted term structure, "
                    "institutional hedging detected"
                )
            else:
                signal = "NORMAL"
                detail = (
                    f"VIX3M/VIX={ratio_str} — normal (contango) term structure"
                )

            logger.info(
                f"[OptionsSentiment] VIX={vix:.2f} VIX3M={vix3m:.2f} "
                f"ratio={ratio_str} → {signal}"
            )
            return {
                "vix": round(vix, 2),
                "vix3m": round(vix3m, 2),
                "ratio": round(ratio, 4) if ratio else None,
                "inverted": inverted,
                "signal": signal,
                "signal_detail": detail,
            }

        except Exception as e:
            logger.warning(f"[OptionsSentiment] VIX term structure fetch failed: {e}")
            return {
                "vix": None,
                "vix3m": None,
                "ratio": None,
                "inverted": False,
                "signal": "UNKNOWN",
                "signal_detail": "VIX term structure data unavailable",
            }

    # ------------------------------------------------------------------
    # Combined snapshot
    # ------------------------------------------------------------------
    def get_options_sentiment(self) -> dict:
        """
        Return combined P/C + VIX term structure snapshot, ready to be
        merged into market_context_data as additional input features.
        """
        pc = self.get_put_call_ratio()
        vts = self.get_vix_term_structure()

        # Compute composite options sentiment score (-1 bearish … +1 bullish)
        score = 0.0
        if pc["signal"] == "BULLISH":
            score += 0.5
        elif pc["signal"] == "BEARISH":
            score -= 0.5
        if vts["signal"] == "CAUTION":
            score -= 0.3

        return {
            "put_call": pc,
            "vix_term_structure": vts,
            "options_sentiment_score": round(score, 2),
            "timestamp": datetime.now().isoformat(),
        }
