"""
Interactive Brokers API Integration Module
Connects to IBKR account to get real portfolio data and place trades
"""

import pandas as pd
import logging
from datetime import datetime
import time

logger = logging.getLogger(__name__)

class IBKRConnector:
    def __init__(self):
        self.ib = None
        self.connected = False
        
        # Try to import ib_insync (requires: pip install ib_insync)
        try:
            from ib_insync import IB, Stock, util
            self.IB = IB
            self.Stock = Stock
            self.util = util
            logger.info("ib_insync library available")
        except ImportError:
            logger.warning("ib_insync not installed. Run: pip install ib_insync")
            self.IB = None
    
    def connect(self, host='127.0.0.1', port=4002, client_id=1):
        """
        Connect to IBKR TWS or IB Gateway
        
        Args:
            host: IP address (default localhost)
            port: TWS=7497, IB Gateway=4002 (paper trading), TWS=7496, IB Gateway=4001 (live)
            client_id: Unique client ID
            
        Returns:
            bool: True if connected successfully
        """
        if not self.IB:
            logger.error("ib_insync not available. Install with: pip install ib_insync")
            return False
        
        try:
            self.ib = self.IB()
            self.ib.connect(host, port, clientId=client_id, timeout=10)
            self.connected = True
            logger.info(f"Connected to IBKR at {host}:{port}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to IBKR: {str(e)}")
            logger.info("Make sure TWS or IB Gateway is running and API is enabled")
            return False
    
    def disconnect(self):
        """Disconnect from IBKR"""
        if self.ib and self.connected:
            self.ib.disconnect()
            self.connected = False
            logger.info("Disconnected from IBKR")
    
    def get_portfolio_positions(self):
        """
        Get current portfolio positions from IBKR account
        
        Returns:
            pandas.DataFrame: Portfolio positions with current values
        """
        if not self.connected:
            logger.error("Not connected to IBKR")
            return pd.DataFrame()
        
        try:
            # Get portfolio positions
            positions = self.ib.positions()
            
            if not positions:
                logger.warning("No positions found in IBKR account")
                return pd.DataFrame()
            
            portfolio_data = []
            
            for pos in positions:
                contract = pos.contract
                
                # Get current market price
                ticker = self.ib.reqMktData(contract, '', False, False)
                self.ib.sleep(1)  # Wait for price data
                
                current_price = ticker.marketPrice() or ticker.close
                
                position_data = {
                    'Symbol': contract.symbol,
                    'Shares': pos.position,
                    'Average_Cost': getattr(pos, 'avgCost', 0),
                    'Current_Price': current_price,
                    'Position_Value': pos.position * current_price,
                    'Unrealized_PnL': getattr(pos, 'unrealizedPNL', 0),
                    'Realized_PnL': getattr(pos, 'realizedPNL', 0),
                    'Currency': contract.currency,
                    'Exchange': contract.exchange,
                    'Contract_Type': contract.secType
                }
                
                portfolio_data.append(position_data)
            
            df = pd.DataFrame(portfolio_data)
            logger.info(f"Retrieved {len(df)} positions from IBKR")
            
            return df
            
        except Exception as e:
            logger.error(f"Error getting portfolio positions: {str(e)}")
            return pd.DataFrame()
    
    def get_account_summary(self):
        """
        Get account summary information
        
        Returns:
            dict: Account summary with key metrics
        """
        if not self.connected:
            logger.error("Not connected to IBKR")
            return {}
        
        try:
            # Request account summary
            account_summary = self.ib.accountSummary()
            
            summary_dict = {}
            for item in account_summary:
                summary_dict[item.tag] = {
                    'value': float(item.value) if item.value.replace('.', '').replace('-', '').isdigit() else item.value,
                    'currency': item.currency
                }
            
            return summary_dict
            
        except Exception as e:
            logger.error(f"Error getting account summary: {str(e)}")
            return {}
    
    def place_market_order(self, symbol, quantity, action='BUY'):
        """
        Place a market order (for demonstration - be careful with real money!)
        
        Args:
            symbol: Stock symbol
            quantity: Number of shares
            action: 'BUY' or 'SELL'
            
        Returns:
            dict: Order status information
        """
        if not self.connected:
            logger.error("Not connected to IBKR")
            return None
        
        try:
            # Create contract
            contract = self.Stock(symbol, 'SMART', 'USD')
            
            # Create market order
            from ib_insync import MarketOrder
            order = MarketOrder(action, quantity)
            
            # Place order
            trade = self.ib.placeOrder(contract, order)
            
            logger.info(f"Placed {action} order for {quantity} shares of {symbol}")
            
            return {
                'orderId': trade.order.orderId,
                'status': trade.orderStatus.status,
                'symbol': symbol,
                'quantity': quantity,
                'action': action,
                'orderType': 'MKT'
            }
            
        except Exception as e:
            logger.error(f"Error placing order: {str(e)}")
            return None
    
    def convert_to_portfolio_format(self, ibkr_positions):
        """
        Convert IBKR positions to our portfolio format
        
        Args:
            ibkr_positions: DataFrame from get_portfolio_positions()
            
        Returns:
            pandas.DataFrame: Portfolio in our standard format
        """
        if ibkr_positions.empty:
            return pd.DataFrame()
        
        try:
            portfolio_data = []
            
            for _, pos in ibkr_positions.iterrows():
                # Estimate purchase date (IBKR doesn't provide this directly)
                purchase_date = datetime.now().strftime('%Y-%m-%d')
                
                portfolio_row = {
                    'Symbol': pos['Symbol'],
                    'Shares': pos['Shares'],
                    'Purchase_Price': pos['Average_Cost'],
                    'Purchase_Date': purchase_date
                }
                
                portfolio_data.append(portfolio_row)
            
            df = pd.DataFrame(portfolio_data)
            
            # Save to CSV to replace the sample portfolio
            csv_path = 'data/portfolio_ibkr.csv'
            df.to_csv(csv_path, index=False)
            logger.info(f"IBKR portfolio saved to {csv_path}")
            
            return df
            
        except Exception as e:
            logger.error(f"Error converting portfolio format: {str(e)}")
            return pd.DataFrame()

# Usage example and setup instructions
def setup_ibkr_connection():
    """
    Instructions for setting up IBKR connection
    """
    instructions = """
    📊 IBKR Integration Setup Instructions:
    
    1. Install Required Package:
       pip install ib_insync
    
    2. Setup TWS or IB Gateway:
       - Download TWS or IB Gateway from Interactive Brokers
       - Login with your account credentials
       - Enable API access: Configure -> API -> Enable ActiveX and Socket Clients
       - Set Socket port: 7497 (TWS Live), 7496 (TWS Paper), 4001 (Gateway Live), 4002 (Gateway Paper)
    
    3. Usage:
       connector = IBKRConnector()
       if connector.connect(port=7497):  # Use 7496 for paper trading
           positions = connector.get_portfolio_positions()
           account = connector.get_account_summary()
           portfolio_csv = connector.convert_to_portfolio_format(positions)
           connector.disconnect()
    
    4. Security Notes:
       - Use paper trading port (7496) for testing
       - IBKR API allows real trading - be extremely careful
       - Always test with paper trading first
    
    5. Replace Portfolio Manager:
       # In main.py, modify portfolio initialization:
       connector = IBKRConnector()
       if connector.connect():
           ibkr_portfolio = connector.get_portfolio_positions()
           portfolio_csv = connector.convert_to_portfolio_format(ibkr_portfolio)
           portfolio_manager = PortfolioManager('data/portfolio_ibkr.csv')
           connector.disconnect()
    """
    
    print(instructions)
    return instructions

if __name__ == "__main__":
    setup_ibkr_connection()