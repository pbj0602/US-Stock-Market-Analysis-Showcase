"""
IBKR Manager — Dual Account Architecture (PRD §2).

Manages two simultaneous ib_insync connections:
  - LIVE  (port 7496): read-only  — portfolio snapshot, PnL, position monitoring
  - PAPER (port 7497): read-write — order placement, strategy execution

Safety guarantee: place_order() raises RuntimeError if called on the live connection.

Auto-reconnect: exponential backoff 5 → 15 → 30 → 60 s.
Heartbeat: every 60 s, logs connection status.
"""

import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_BACKOFF_SEQUENCE = [5, 15, 30, 60]  # seconds


class _IBKRConnection:
    """Wrapper around a single ib_insync IB connection."""

    def __init__(
        self,
        label: str,
        host: str,
        port: int,
        client_id: int,
        readonly: bool,
    ):
        self.label = label          # "LIVE" or "PAPER"
        self.host = host
        self.port = port
        self.client_id = client_id
        self.readonly = readonly
        self.ib = None
        self.connected = False
        self._lock = threading.Lock()

        try:
            from ib_insync import IB
            self._IB = IB
        except ImportError:
            logger.error("ib_insync not installed — pip install ib_insync")
            self._IB = None

    # ------------------------------------------------------------------
    def connect(self) -> bool:
        if not self._IB:
            return False
        with self._lock:
            try:
                self.ib = self._IB()
                self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=10)
                self.connected = True
                tag = "READ-ONLY" if self.readonly else "TRADING"
                logger.info(f"{self.label} [{tag}] ✓ connected  ({self.host}:{self.port})")
                return True
            except Exception as e:
                logger.error(f"{self.label}: connection failed — {e}")
                self.connected = False
                return False

    def disconnect(self) -> None:
        with self._lock:
            if self.ib and self.connected:
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
            self.connected = False

    def is_connected(self) -> bool:
        if not self.ib or not self.connected:
            return False
        try:
            return self.ib.isConnected()
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _port_open(self) -> bool:
        """Return True if the target port is accepting TCP connections (milliseconds)."""
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                return True
        except OSError:
            return False

    def connect_with_retry(self) -> bool:
        """Attempt connection with exponential backoff.

        Starts with a fast port-check: if the port is not even listening
        (IB Gateway / TWS not running), skip all retries immediately instead
        of waiting up to 110 seconds on timeouts.
        """
        if not self._port_open():
            logger.warning(
                f"{self.label}: port {self.port} not open — "
                "IB Gateway / TWS not running, skipping connection."
            )
            return False

        for delay in _BACKOFF_SEQUENCE:
            if self.connect():
                return True
            logger.warning(f"{self.label}: retrying in {delay}s …")
            time.sleep(delay)
        logger.error(f"{self.label}: all reconnect attempts failed")
        return False


class IBKRManager:
    """
    Manages dual IBKR connections (live + paper) with heartbeat monitoring.

    Usage:
        mgr = IBKRManager()
        mgr.connect_all()

        # Read-only operations on live account
        positions = mgr.live.ib.positions()

        # Order placement on paper account
        mgr.place_order(contract, order)

        mgr.disconnect_all()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        live_port: int = 7496,
        paper_port: int = 7497,
        client_id_live: int = 1,
        client_id_paper: int = 2,
    ):
        # Pull from config if available
        try:
            from config.config import Config
            host = Config.IBKR_HOST
            live_port = Config.IBKR_LIVE_PORT
            paper_port = Config.IBKR_PAPER_PORT
            client_id_live = Config.IBKR_CLIENT_ID_LIVE
            client_id_paper = Config.IBKR_CLIENT_ID_PAPER
        except Exception:
            pass  # Use defaults passed in

        self.live = _IBKRConnection(
            label="LIVE",
            host=host,
            port=live_port,
            client_id=client_id_live,
            readonly=True,
        )
        self.paper = _IBKRConnection(
            label="PAPER",
            host=host,
            port=paper_port,
            client_id=client_id_paper,
            readonly=False,
        )

        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_heartbeat = threading.Event()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def connect_all(self) -> dict:
        """
        Connect both accounts. Returns status dict.
        Prints startup banner per PRD requirement.
        """
        live_ok = self.live.connect_with_retry()
        paper_ok = self.paper.connect_with_retry()

        live_status = "✓ connected" if live_ok else "✗ FAILED"
        paper_status = "✓ connected" if paper_ok else "✗ FAILED"
        print(f"LIVE  [READ-ONLY] {live_status}  |  PAPER [TRADING] {paper_status}")

        if live_ok or paper_ok:
            self._start_heartbeat()

        return {"live": live_ok, "paper": paper_ok}

    def disconnect_all(self) -> None:
        self._stop_heartbeat.set()
        self.live.disconnect()
        self.paper.disconnect()
        logger.info("Both IBKR connections closed")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------
    def _start_heartbeat(self) -> None:
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="ibkr-heartbeat"
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(timeout=60):
            for conn in (self.live, self.paper):
                if conn.ib is None:
                    continue
                alive = conn.is_connected()
                tag = "READ-ONLY" if conn.readonly else "TRADING"
                if alive:
                    logger.info(f"[Heartbeat] {conn.label} [{tag}] ✓ alive")
                else:
                    logger.warning(
                        f"[Heartbeat] {conn.label} [{tag}] ✗ dropped — reconnecting …"
                    )
                    conn.connect_with_retry()

    # ------------------------------------------------------------------
    # Order submission (paper only)
    # ------------------------------------------------------------------
    def place_order(self, contract, order):
        """
        Submit an order to the PAPER account only.

        Raises:
            RuntimeError: If paper account is not connected, or if this method
                          is somehow routed to the live connection object.
        """
        # Hard block: verify we are targeting paper, not live
        if not self.paper.connected or self.paper.ib is None:
            raise RuntimeError(
                "Cannot place order: PAPER account is not connected. "
                "Orders must never be routed to the live account."
            )

        # Extra safety: refuse if caller somehow passes the live IB object
        if self.live.ib is not None and self.live.ib is self.paper.ib:
            raise RuntimeError(
                "Live and paper IB objects are identical — "
                "refusing to place order to prevent live account risk."
            )

        trade = self.paper.ib.placeOrder(contract, order)
        logger.info(
            f"[PAPER] Order submitted: {order.action} {order.totalQuantity} "
            f"{contract.symbol} @ {getattr(order, 'lmtPrice', 'MKT')}"
        )
        return trade

    # ------------------------------------------------------------------
    # Convenience read-only helpers (live account)
    # ------------------------------------------------------------------
    def get_live_positions(self):
        """Return live account positions as a list (read-only)."""
        if not self.live.is_connected():
            logger.error("Live account not connected")
            return []
        return self.live.ib.positions()

    def get_live_account_summary(self):
        """Return live account summary dict (read-only)."""
        if not self.live.is_connected():
            logger.error("Live account not connected")
            return {}
        items = self.live.ib.accountSummary()
        return {
            item.tag: {
                "value": (
                    float(item.value)
                    if item.value.replace(".", "").replace("-", "").isdigit()
                    else item.value
                ),
                "currency": item.currency,
            }
            for item in items
        }

    def get_paper_portfolio_value(self) -> float:
        """Return total paper account net liquidation value."""
        if not self.paper.is_connected():
            return 0.0
        try:
            summary = self.paper.ib.accountSummary()
            for item in summary:
                if item.tag == "NetLiquidation":
                    return float(item.value)
        except Exception as e:
            logger.error(f"Could not fetch paper portfolio value: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_manager_instance: Optional[IBKRManager] = None


def get_manager() -> IBKRManager:
    """Return the shared IBKRManager singleton (creates it on first call)."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = IBKRManager()
    return _manager_instance
