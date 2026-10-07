"""
Order Executor — Paper Trading Execution Engine (PRD §5).

Accepts Signal objects from the existing signal engine, applies pre-trade risk
filters, submits limit orders (mid-price, 2-min timeout) to the IBKR paper
account via IBKRManager, and logs all trades to:
  ./data/paper_trades.csv
  ./data/paper_portfolio_history.csv

Position sizing formula (PRD §5.3.2):
  shares = floor( (portfolio_value * position_pct) / current_price )

Risk filters:
  - Single position max: 20% of portfolio
  - Max open positions: 8
  - Do-Not-Trade list (earnings flagged tickers)
  - VIX hard stop > 30: no new positions

Bracket order: stop-loss at -5%, take-profit at +10% from fill (OCA).
"""

import csv
import logging
import math
import os
import threading
import time
from datetime import datetime, date

logger = logging.getLogger(__name__)

_TRADES_CSV = os.path.join("data", "paper_trades.csv")
_HISTORY_CSV = os.path.join("data", "paper_portfolio_history.csv")

_TRADES_HEADER = [
    "timestamp", "ticker", "action", "shares", "fill_price",
    "signal_score", "stop_loss", "take_profit", "exit_price",
    "exit_date", "realized_pnl",
]
_HISTORY_HEADER = ["date", "portfolio_value", "daily_pnl", "open_positions"]


def _ensure_csv(path: str, header: list) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


class OrderExecutor:
    """
    Execute paper trades via IBKRManager.

    Usage:
        executor = OrderExecutor(ibkr_manager)
        executor.run(signals, market_data)
    """

    def __init__(self, ibkr_manager=None):
        """
        Args:
            ibkr_manager: IBKRManager instance. If None, executor runs in
                          dry-run (log-only) mode without placing real orders.
        """
        self._mgr = ibkr_manager
        self._dry_run = ibkr_manager is None
        _ensure_csv(_TRADES_CSV, _TRADES_HEADER)
        _ensure_csv(_HISTORY_CSV, _HISTORY_HEADER)

        try:
            from config.config import Config
            self._position_pct = Config.PAPER_POSITION_PCT
            self._max_positions = Config.PAPER_MAX_POSITIONS
            self._max_single_pct = Config.PAPER_MAX_SINGLE_POSITION_PCT
            self._stop_loss_pct = Config.PAPER_STOP_LOSS_PCT
            self._take_profit_pct = Config.PAPER_TAKE_PROFIT_PCT
        except Exception:
            self._position_pct = 0.10
            self._max_positions = 8
            self._max_single_pct = 0.20
            self._stop_loss_pct = 0.05
            self._take_profit_pct = 0.10

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self, signals: list, market_data: dict) -> list:
        """
        Process a list of signal dicts and execute qualifying paper trades.

        Args:
            signals:     List of dicts with keys: ticker, signal, score,
                         do_not_trade (bool), current_price (float).
            market_data: Dict with keys: vix (float), open_positions (list).

        Returns:
            List of trade result dicts (one per executed order).
        """
        vix = market_data.get("vix", 0.0)
        open_positions: list = market_data.get("open_positions", [])
        portfolio_value: float = market_data.get("portfolio_value", 100_000.0)

        if vix > 30:
            logger.warning(f"[OrderExecutor] VIX={vix:.1f} > 30 — no new positions opened (PRD §5.3.1)")
            return []

        if len(open_positions) >= self._max_positions:
            logger.warning(
                f"[OrderExecutor] Max open positions reached ({self._max_positions}) — skipping"
            )
            return []

        results = []
        for sig in signals:
            if sig.get("signal") != "BUY":
                continue
            if sig.get("do_not_trade"):
                logger.info(f"[OrderExecutor] {sig['ticker']}: Do-Not-Trade flag — skipped")
                continue

            ticker = sig["ticker"]
            price = float(sig.get("current_price", 0))
            score = float(sig.get("score", 0))

            if price <= 0:
                logger.warning(f"[OrderExecutor] {ticker}: invalid price {price} — skipped")
                continue

            # Position sizing
            shares = self._calc_shares(portfolio_value, price)
            position_value = shares * price

            if position_value / portfolio_value > self._max_single_pct:
                logger.warning(
                    f"[OrderExecutor] {ticker}: position {position_value/portfolio_value:.1%} "
                    f"> max single {self._max_single_pct:.0%} — skipped"
                )
                continue

            if shares <= 0:
                continue

            trade_result = self._submit_order(
                ticker, shares, price, score,
                stop_price=sig.get("stop_loss"),
                tp_price=sig.get("target_price"),
            )
            if trade_result:
                results.append(trade_result)
                open_positions.append(ticker)

            if len(open_positions) >= self._max_positions:
                logger.info("[OrderExecutor] Max positions reached — stopping further orders")
                break

        return results

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------
    def _submit_order(
        self,
        ticker: str,
        shares: int,
        ref_price: float,
        score: float,
        stop_price: float | None = None,
        tp_price: float | None = None,
    ) -> dict | None:
        """Submit a bracket limit order to the paper account.

        stop_price / tp_price: use signal generator values when provided.
        Falls back to fixed-percentage defaults if missing or invalid.
        """
        # Use signal-generator levels if valid; else percentage fallback
        min_stop = round(ref_price * (1 - self._stop_loss_pct), 2)
        max_tp   = round(ref_price * (1 + self._take_profit_pct), 2)

        if stop_price and 0 < stop_price < ref_price:
            stop_price = round(stop_price, 2)
        else:
            stop_price = min_stop  # fallback

        if tp_price and tp_price > ref_price:
            tp_price = round(tp_price, 2)
        else:
            tp_price = max_tp  # fallback

        if self._dry_run:
            logger.info(
                f"[OrderExecutor DRY-RUN] BUY {shares} {ticker} "
                f"@ ~{ref_price:.2f} | SL={stop_price} TP={tp_price}"
            )
            fill_price = ref_price
        else:
            fill_price = self._place_bracket_order(ticker, shares, ref_price, stop_price, tp_price)
            if fill_price is None:
                return None

        trade = {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "action": "BUY",
            "shares": shares,
            "fill_price": fill_price,
            "signal_score": score,
            "stop_loss": stop_price,
            "take_profit": tp_price,
            "exit_price": "",
            "exit_date": "",
            "realized_pnl": "",
        }
        self._log_trade(trade)
        return trade

    def _place_bracket_order(
        self,
        ticker: str,
        shares: int,
        mid_price: float,
        stop_price: float,
        tp_price: float,
    ) -> float | None:
        """
        Place OCA bracket limit order via IBKRManager.
        Waits up to 2 minutes for fill; cancels if not filled.

        Returns fill price if filled, None otherwise.
        """
        try:
            from ib_insync import Stock, LimitOrder, StopOrder, OCA
        except ImportError:
            logger.error("[OrderExecutor] ib_insync not installed")
            return None

        contract = Stock(ticker, "SMART", "USD")
        limit_order = LimitOrder("BUY", shares, round(mid_price, 2))
        stop_order = StopOrder("SELL", shares, stop_price)
        tp_order = LimitOrder("SELL", shares, tp_price)

        # OCA group: stop-loss and take-profit cancel each other
        oca_group = f"OCA_{ticker}_{int(time.time())}"
        stop_order.ocaGroup = oca_group
        tp_order.ocaGroup = oca_group
        stop_order.ocaType = 1
        tp_order.ocaType = 1

        try:
            entry_trade = self._mgr.place_order(contract, limit_order)
        except RuntimeError as e:
            logger.error(f"[OrderExecutor] Order blocked: {e}")
            return None

        # Poll for fill up to 2 minutes (PRD §5.2.1)
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(10)
            status = entry_trade.orderStatus.status
            if status == "Filled":
                fill_price = entry_trade.orderStatus.avgFillPrice
                logger.info(f"[OrderExecutor] {ticker} BUY filled @ {fill_price}")
                # Place bracket legs
                try:
                    self._mgr.place_order(contract, stop_order)
                    self._mgr.place_order(contract, tp_order)
                except Exception as e:
                    logger.warning(f"[OrderExecutor] Could not place bracket legs: {e}")
                return fill_price
            elif status in ("Cancelled", "Inactive"):
                logger.warning(f"[OrderExecutor] {ticker} order {status} before fill")
                return None

        # Timeout — cancel entry order
        logger.warning(f"[OrderExecutor] {ticker} limit order timed out after 2 min — cancelling")
        try:
            self._mgr.paper.ib.cancelOrder(entry_trade.order)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _calc_shares(self, portfolio_value: float, price: float) -> int:
        """PRD §5.3.2: shares = floor( portfolio_value * position_pct / price )"""
        return math.floor((portfolio_value * self._position_pct) / price)

    def _log_trade(self, trade: dict) -> None:
        """Append one trade row to paper_trades.csv."""
        try:
            with open(_TRADES_CSV, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_TRADES_HEADER)
                writer.writerow({k: trade.get(k, "") for k in _TRADES_HEADER})
            logger.info(
                f"[TradeJournal] Logged {trade['action']} {trade['shares']} "
                f"{trade['ticker']} @ {trade['fill_price']}"
            )
        except Exception as e:
            logger.error(f"[TradeJournal] Failed to write trade: {e}")

    def append_portfolio_snapshot(
        self, portfolio_value: float, daily_pnl: float, open_positions: list
    ) -> None:
        """Append daily snapshot row to paper_portfolio_history.csv."""
        try:
            row = {
                "date": date.today().isoformat(),
                "portfolio_value": round(portfolio_value, 2),
                "daily_pnl": round(daily_pnl, 2),
                "open_positions": len(open_positions),
            }
            with open(_HISTORY_CSV, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_HISTORY_HEADER)
                writer.writerow(row)
        except Exception as e:
            logger.error(f"[TradeJournal] Failed to write portfolio snapshot: {e}")
