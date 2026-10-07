"""
Quick test to verify IB Gateway paper trading connection.
Run IB Gateway first (paper account, port 4002), then:
  python3 test_ibkr_connection.py
"""
import sys
from modules.ibkr_manager import IBKRManager

def main():
    print("Testing IBKR paper trading connection...")
    print("Make sure IB Gateway is running with paper account on port 4002\n")

    mgr = IBKRManager(
        host="127.0.0.1",
        live_port=4001,
        paper_port=4002,
        client_id_live=1,
        client_id_paper=2,
    )

    # Try paper only first (skip live if you don't have a live account)
    print("Connecting to PAPER account (port 4002)...")
    paper_ok = mgr.paper.connect()

    if not paper_ok:
        print("\nFAILED: Could not connect to IB Gateway paper account.")
        print("Check:")
        print("  1. IB Gateway is running and logged in (paper account)")
        print("  2. API is enabled: Configure → Settings → API → Settings")
        print("  3. Socket port is set to 4002")
        print("  4. 'Enable ActiveX and Socket Clients' is checked")
        sys.exit(1)

    print("\nConnected! Fetching account info...")

    try:
        summary = mgr.paper.ib.accountSummary()
        for item in summary:
            if item.tag in ("NetLiquidation", "TotalCashValue", "BuyingPower"):
                print(f"  {item.tag}: {item.value} {item.currency}")
    except Exception as e:
        print(f"  Could not fetch account summary: {e}")

    print("\nFetching paper positions...")
    try:
        positions = mgr.paper.ib.positions()
        if positions:
            for p in positions:
                print(f"  {p.contract.symbol}: {p.position} shares @ avg {p.avgCost:.2f}")
        else:
            print("  No open positions (this is normal for a fresh paper account)")
    except Exception as e:
        print(f"  Could not fetch positions: {e}")

    portfolio_value = mgr.get_paper_portfolio_value()
    print(f"\nPaper Portfolio Net Liquidation: ${portfolio_value:,.2f}")

    mgr.paper.disconnect()
    print("\nConnection test PASSED. Paper trading is ready.")

if __name__ == "__main__":
    main()
