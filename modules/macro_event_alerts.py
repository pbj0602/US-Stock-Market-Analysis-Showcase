"""
Macro Event Calendar Alerts (PRD §6.3).

Extends the existing EventRiskEngine with:
  1. 3-trading-day advance alerts for FOMC, CPI, NFP in daily notifications
  2. Historical event-day performance context from stored Parquet data
     e.g. "On past CPI release days, NVDA averaged -1.2%"
  3. Pre-event position reduction suggestion if event is T-1 and VIX > 20

Data source for macro dates:
  - econdb.com public API (free tier, no key needed for basic calendar)
  - Falls back to hardcoded known-pattern estimation if API unavailable
"""

import logging
from datetime import datetime, timedelta, date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_HIGH_IMPACT_EVENTS = {"FOMC", "CPI", "NFP"}
_ECONDB_URL = "https://www.econdb.com/api/series/?ticker={ticker}&format=json"

# Econdb event tickers for US macro calendar
_ECONDB_TICKERS = {
    "CPI": "CPIUS",
    "NFP": "PAYEMS",
    "FOMC": None,   # FOMC dates sourced differently
}

_VIX_REDUCTION_THRESHOLD = 20


class MacroEventAlerts:
    """
    Fetch upcoming macro events and compute historical performance context.
    """

    # ------------------------------------------------------------------
    # Fetch upcoming events
    # ------------------------------------------------------------------
    def get_upcoming_events(self, lookforward_days: int = 10) -> list[dict]:
        """
        Return a list of upcoming macro events within lookforward_days.

        Each event dict:
          { "event": "CPI", "date": date(2026,4,10), "days_until": 3,
            "impact": "HIGH", "consensus": "..." }
        """
        today = datetime.utcnow().date()
        events = []

        # Try econdb calendar
        try:
            events = self._fetch_econdb_events(today, lookforward_days)
        except Exception as e:
            logger.warning(f"[MacroAlerts] econdb fetch failed: {e}")

        # Fallback: approximate next occurrences from typical release patterns
        if not events:
            events = self._estimate_next_events(today, lookforward_days)

        return sorted(events, key=lambda e: e["date"])

    def _fetch_econdb_events(self, today: date, days: int) -> list[dict]:
        """Fetch economic calendar from econdb.com (free, no key)."""
        import requests
        from io import StringIO

        url = "https://www.econdb.com/api/events/?format=json&country=US"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            return []

        events = []
        cutoff = today + timedelta(days=days)
        for entry in raw.get("results", []):
            try:
                ev_date = datetime.strptime(entry["date"][:10], "%Y-%m-%d").date()
                if not (today <= ev_date <= cutoff):
                    continue
                name = entry.get("event", "").upper()
                impact = "HIGH" if any(k in name for k in _HIGH_IMPACT_EVENTS) else "MEDIUM"
                events.append({
                    "event": name,
                    "date": ev_date,
                    "days_until": (ev_date - today).days,
                    "impact": impact,
                    "consensus": entry.get("consensus", ""),
                })
            except Exception:
                continue
        return events

    def _estimate_next_events(self, today: date, days: int) -> list[dict]:
        """
        Rough estimation: CPI ~12th of month, NFP ~1st Friday, FOMC ~6-weekly.
        Used only as fallback.
        """
        events = []
        cutoff = today + timedelta(days=days)

        # CPI: typically released around the 10th-15th of each month
        for month_offset in range(3):
            year = today.year
            month = today.month + month_offset
            if month > 12:
                month -= 12
                year += 1
            cpi_date = date(year, month, 12)
            if today <= cpi_date <= cutoff:
                events.append({
                    "event": "CPI (est)",
                    "date": cpi_date,
                    "days_until": (cpi_date - today).days,
                    "impact": "HIGH",
                    "consensus": "",
                })

        # NFP: first Friday of each month
        for month_offset in range(3):
            year = today.year
            month = today.month + month_offset
            if month > 12:
                month -= 12
                year += 1
            first_day = date(year, month, 1)
            days_to_friday = (4 - first_day.weekday()) % 7
            nfp_date = first_day + timedelta(days=days_to_friday)
            if today <= nfp_date <= cutoff:
                events.append({
                    "event": "NFP (est)",
                    "date": nfp_date,
                    "days_until": (nfp_date - today).days,
                    "impact": "HIGH",
                    "consensus": "",
                })

        return events

    # ------------------------------------------------------------------
    # 3-day advance alert filter
    # ------------------------------------------------------------------
    def get_3day_alerts(self) -> list[dict]:
        """
        Return HIGH-impact events within 3 trading days.
        Used for inclusion in daily notification (PRD §6.3.2).
        """
        upcoming = self.get_upcoming_events(lookforward_days=5)
        return [e for e in upcoming if e["days_until"] <= 3 and e["impact"] == "HIGH"]

    # ------------------------------------------------------------------
    # Historical event-day performance context
    # ------------------------------------------------------------------
    def historical_event_performance(
        self,
        event_type: str,
        tickers: list[str],
        lookback_years: int = 2,
    ) -> dict[str, dict]:
        """
        Compute average ticker return on past event-day +/- 1 using stored Parquet.

        Returns:
          { "NVDA": {"avg_return": -0.012, "n_events": 18, "label": "CPI"},
            "AAPL": {...}, ... }
        """
        from modules.parquet_store import get_price_series

        # Map event type → approximate historical release dates
        event_dates = self._get_historical_event_dates(event_type, lookback_years)
        if not event_dates:
            return {}

        results = {}
        for ticker in tickers:
            try:
                prices = get_price_series(ticker, column="Close")
                if prices.empty:
                    continue
                idx = pd.to_datetime(prices.index)
                # Normalise to midnight and strip timezone so comparisons with
                # plain date objects don't raise tz-naive vs tz-aware errors.
                if idx.tz is not None:
                    idx = idx.tz_localize(None)
                prices.index = idx.normalize()

                returns = []
                for ev_date in event_dates:
                    ev_ts = pd.Timestamp(ev_date)
                    # Find closest trading day on or after event date
                    after = prices[prices.index >= ev_ts]
                    before = prices[prices.index < ev_ts]
                    if after.empty or before.empty:
                        continue
                    price_before = float(before.iloc[-1])
                    price_after = float(after.iloc[0])
                    ret = (price_after - price_before) / price_before
                    returns.append(ret)

                if returns:
                    avg = sum(returns) / len(returns)
                    results[ticker] = {
                        "avg_return": round(avg, 4),
                        "n_events": len(returns),
                        "label": event_type,
                        "summary": (
                            f"On past {event_type} release days, "
                            f"{ticker} averaged {avg*100:+.1f}%"
                        ),
                    }
            except Exception as e:
                logger.warning(f"[MacroAlerts] {ticker} history error: {e}")

        return results

    def _get_historical_event_dates(self, event_type: str, years: int) -> list[date]:
        """
        Return approximate historical dates for the given event type.
        CPI → ~12th of each month; NFP → first Friday; FOMC → quarterly approx.
        """
        today = datetime.utcnow().date()
        start = date(today.year - years, today.month, 1)
        dates = []
        cursor = start

        if event_type.upper().startswith("CPI"):
            while cursor <= today:
                try:
                    dates.append(date(cursor.year, cursor.month, 12))
                except ValueError:
                    pass
                month = cursor.month + 1
                year = cursor.year + (1 if month > 12 else 0)
                month = month if month <= 12 else month - 12
                cursor = date(year, month, 1)

        elif event_type.upper().startswith("NFP"):
            while cursor <= today:
                first_day = date(cursor.year, cursor.month, 1)
                days_to_fri = (4 - first_day.weekday()) % 7
                dates.append(first_day + timedelta(days=days_to_fri))
                month = cursor.month + 1
                year = cursor.year + (1 if month > 12 else 0)
                cursor = date(year, month if month <= 12 else month - 12, 1)

        elif event_type.upper().startswith("FOMC"):
            # FOMC meets ~8 times/year, roughly every 6 weeks
            ev = start
            while ev <= today:
                dates.append(ev)
                ev += timedelta(weeks=6)

        return [d for d in dates if d <= today]

    # ------------------------------------------------------------------
    # Position reduction suggestion
    # ------------------------------------------------------------------
    def should_reduce_positions(self, vix: float) -> Optional[str]:
        """
        Returns a warning string if T-1 high-impact event + VIX > 20.
        Returns None if no action needed.
        """
        upcoming = self.get_upcoming_events(lookforward_days=3)
        t1_events = [e for e in upcoming if e["days_until"] == 1 and e["impact"] == "HIGH"]
        if t1_events and vix > _VIX_REDUCTION_THRESHOLD:
            names = ", ".join(e["event"] for e in t1_events)
            return (
                f"CAUTION: {names} tomorrow and VIX={vix:.1f} > {_VIX_REDUCTION_THRESHOLD}. "
                "Consider reducing position sizes before close."
            )
        return None

    # ------------------------------------------------------------------
    # Full alert summary (used in daily notification)
    # ------------------------------------------------------------------
    def get_full_alert_summary(self, tickers: list[str], vix: float = 0.0) -> dict:
        """
        Return complete alert package for inclusion in daily notification.
        """
        three_day = self.get_3day_alerts()
        reduction_warning = self.should_reduce_positions(vix)

        # Historical context for upcoming events
        history = {}
        for event in three_day:
            ev_type = event["event"].replace(" (est)", "")
            ctx = self.historical_event_performance(ev_type, tickers[:5])
            if ctx:
                history[ev_type] = ctx

        return {
            "upcoming_alerts": three_day,
            "reduction_warning": reduction_warning,
            "historical_context": history,
            "has_alerts": bool(three_day),
        }
