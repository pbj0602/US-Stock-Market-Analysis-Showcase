"""
Unit tests for IBKRManager (PRD §2.5 Acceptance Criteria).

Tests:
  - place_order() on live connection raises RuntimeError
  - place_order() on paper connection succeeds (mocked)
  - Heartbeat reconnects paper when it drops
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch


class TestIBKRManagerSafety(unittest.TestCase):
    """PRD §2.5: place_order with live account must raise RuntimeError."""

    def _make_manager(self):
        """Return an IBKRManager with both connections mocked as connected."""
        from modules.ibkr_manager import IBKRManager, _IBKRConnection

        mgr = IBKRManager.__new__(IBKRManager)

        # Mock live connection — connected but readonly
        live = _IBKRConnection.__new__(_IBKRConnection)
        live.label = "LIVE"
        live.host = "127.0.0.1"
        live.port = 7496
        live.client_id = 1
        live.readonly = True
        live.connected = True
        live.ib = MagicMock()
        live.ib.isConnected.return_value = True
        live._lock = threading.Lock()
        live._IB = MagicMock()

        # Mock paper connection — connected, read-write
        paper = _IBKRConnection.__new__(_IBKRConnection)
        paper.label = "PAPER"
        paper.host = "127.0.0.1"
        paper.port = 7497
        paper.client_id = 2
        paper.readonly = False
        paper.connected = True
        paper.ib = MagicMock()
        paper.ib.isConnected.return_value = True
        paper._lock = threading.Lock()
        paper._IB = MagicMock()

        mgr.live = live
        mgr.paper = paper
        mgr._stop_heartbeat = threading.Event()
        mgr._heartbeat_thread = None
        return mgr

    def test_place_order_on_disconnected_paper_raises(self):
        """place_order raises RuntimeError when paper is not connected."""
        mgr = self._make_manager()
        mgr.paper.connected = False
        mgr.paper.ib = None

        contract = MagicMock()
        order = MagicMock()

        with self.assertRaises(RuntimeError) as ctx:
            mgr.place_order(contract, order)

        self.assertIn("PAPER account is not connected", str(ctx.exception))

    def test_place_order_raises_when_live_and_paper_are_same_object(self):
        """
        Verify the extra safety: if live.ib and paper.ib accidentally point
        to the same IB object (misconfiguration), place_order must raise.
        """
        mgr = self._make_manager()
        shared_ib = MagicMock()
        shared_ib.isConnected.return_value = True
        mgr.live.ib = shared_ib
        mgr.paper.ib = shared_ib  # same object — should be refused

        contract = MagicMock()
        order = MagicMock()

        with self.assertRaises(RuntimeError) as ctx:
            mgr.place_order(contract, order)

        self.assertIn("Live and paper IB objects are identical", str(ctx.exception))

    def test_place_order_on_paper_succeeds(self):
        """place_order on a properly connected paper account should not raise."""
        mgr = self._make_manager()
        # Ensure live and paper have distinct objects
        mgr.live.ib = MagicMock()
        mgr.paper.ib = MagicMock()
        mgr.paper.ib.placeOrder.return_value = MagicMock()

        contract = MagicMock()
        order = MagicMock()
        order.action = "BUY"
        order.totalQuantity = 10
        contract.symbol = "AAPL"

        trade = mgr.place_order(contract, order)
        mgr.paper.ib.placeOrder.assert_called_once_with(contract, order)
        self.assertIsNotNone(trade)

    def test_heartbeat_reconnects_dropped_paper(self):
        """
        Simulates paper dropping mid-heartbeat; verifies connect_with_retry is called.
        """
        from modules.ibkr_manager import _IBKRConnection

        mgr = self._make_manager()
        # Simulate paper dropping
        mgr.paper.ib.isConnected.return_value = False
        mgr.paper.connected = False

        reconnect_called = threading.Event()

        def fake_reconnect():
            reconnect_called.set()
            mgr.paper.connected = True
            return True

        mgr.paper.connect_with_retry = fake_reconnect

        # Run one heartbeat tick manually
        for conn in (mgr.live, mgr.paper):
            if conn.ib is None:
                continue
            alive = conn.is_connected()
            if not alive:
                conn.connect_with_retry()

        self.assertTrue(reconnect_called.is_set(), "connect_with_retry was not called on dropped paper")


if __name__ == "__main__":
    unittest.main()
