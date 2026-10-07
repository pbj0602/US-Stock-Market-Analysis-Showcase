"""
Portfolio Management Module
Manages user holdings and calculates daily portfolio snapshots
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import os
from modules.data_scraper import DataScraper
from config.config import Config

logger = logging.getLogger(__name__)

class PortfolioManager:
    def __init__(self, portfolio_file=None):
        self.portfolio_file = portfolio_file or 'data/portfolio.csv'
        self.data_scraper = DataScraper()
        self.portfolio_data = self._load_portfolio()
    
    def _load_portfolio(self):
        """Load portfolio from file or create sample portfolio"""
        try:
            if os.path.exists(self.portfolio_file):
                df = pd.read_csv(self.portfolio_file)
                logger.info(f"Loaded portfolio with {len(df)} holdings from {self.portfolio_file}")
                return df
            else:
                # Create sample portfolio
                sample_portfolio = pd.DataFrame({
                    'Symbol': ['AAPL', 'MSFT', 'GOOGL', 'TSLA', 'SPY'],
                    'Shares': [100, 50, 25, 30, 200],
                    'Purchase_Price': [150.00, 300.00, 2500.00, 200.00, 400.00],
                    'Purchase_Date': ['2024-01-15', '2024-01-20', '2024-02-01', '2024-02-15', '2024-01-10']
                })
                
                # Ensure data directory exists
                os.makedirs('data', exist_ok=True)
                
                # Save sample portfolio
                sample_portfolio.to_csv(self.portfolio_file, index=False)
                logger.info(f"Created sample portfolio with {len(sample_portfolio)} holdings at {self.portfolio_file}")
                return sample_portfolio
                
        except Exception as e:
            logger.error(f"Error loading portfolio: {str(e)}")
            return pd.DataFrame()
    
    def get_portfolio_snapshot(self, market_data=None):
        """
        Calculate current portfolio snapshot with returns and technical indicators
        
        Args:
            market_data (DataFrame): Market data from DataScraper, optional
            
        Returns:
            pandas.DataFrame: Portfolio snapshot with current values and metrics
        """
        if self.portfolio_data.empty:
            # Not an error: an account that has sold everything is flat, and
            # a flat account is a legitimate daily state. Logging it at ERROR
            # put a red line in every run's log for a normal 100%-cash day.
            logger.info("No holdings in portfolio file — account is flat (100% cash)")
            return pd.DataFrame()
        
        try:
            # Get current market data if not provided
            if market_data is None or market_data.empty:
                symbols = self.portfolio_data['Symbol'].unique().tolist()
                market_data = self.data_scraper.get_market_data(symbols)
            
            if market_data.empty:
                logger.error("No market data available for portfolio analysis")
                return pd.DataFrame()
            
            # Merge portfolio with market data
            portfolio_snapshot = []
            
            for _, holding in self.portfolio_data.iterrows():
                symbol = holding['Symbol']
                shares = holding['Shares']
                purchase_price = holding['Purchase_Price']
                purchase_date = pd.to_datetime(holding['Purchase_Date'])
                
                # Find market data for this symbol
                symbol_data = market_data[market_data['Symbol'] == symbol]
                
                if symbol_data.empty:
                    logger.warning(f"No market data found for {symbol}")
                    continue
                
                symbol_data = symbol_data.iloc[0]  # Get first (latest) row
                
                # Calculate position metrics
                current_price = symbol_data.get('Current_Price', symbol_data.get('Close', 0))
                if pd.isna(current_price) or current_price == 0:
                    current_price = symbol_data.get('Close', 0)
                
                position_value = shares * current_price
                cost_basis = shares * purchase_price
                unrealized_pnl = position_value - cost_basis
                unrealized_pnl_pct = (unrealized_pnl / cost_basis * 100) if cost_basis != 0 else 0
                
                # Calculate holding period
                holding_days = (datetime.now() - purchase_date).days
                
                # Create snapshot entry
                snapshot_entry = {
                    'Symbol': symbol,
                    'Shares': shares,
                    'Purchase_Price': purchase_price,
                    'Purchase_Date': purchase_date.strftime('%Y-%m-%d'),
                    'Current_Price': current_price,
                    'Position_Value': position_value,
                    'Cost_Basis': cost_basis,
                    'Unrealized_PnL': unrealized_pnl,
                    'Unrealized_PnL_Pct': unrealized_pnl_pct,
                    'Holding_Days': holding_days,
                    
                    # Add market data
                    'Day_Change': symbol_data.get('Day_Change', 0),
                    'Day_Change_Pct': symbol_data.get('Day_Change_Pct', 0),
                    'Volume': symbol_data.get('Day_Volume', symbol_data.get('Volume', 0)),
                    'Market_Cap': symbol_data.get('Market_Cap'),
                    'PE_Ratio': symbol_data.get('PE_Ratio'),
                    'Beta': symbol_data.get('Beta'),
                    '52_Week_High': symbol_data.get('52_Week_High'),
                    '52_Week_Low': symbol_data.get('52_Week_Low'),
                    
                    # Technical indicators
                    'RSI': symbol_data.get('RSI'),
                    'SMA_10': symbol_data.get('SMA_10'),
                    'SMA_20': symbol_data.get('SMA_20'),
                    'SMA_50': symbol_data.get('SMA_50'),
                    'EMA_20': symbol_data.get('EMA_20'),
                    'MACD': symbol_data.get('MACD'),
                    'MACD_Signal': symbol_data.get('MACD_Signal'),
                    'BB_Position': symbol_data.get('BB_Position'),
                    'Momentum_10': symbol_data.get('Momentum_10'),
                    'Momentum_20': symbol_data.get('Momentum_20'),
                    'Volatility': symbol_data.get('Volatility'),
                    'Volume_Ratio': symbol_data.get('Volume_Ratio'),
                    
                    # Position-specific technical signals
                    'Price_vs_SMA50': symbol_data.get('Price_vs_SMA50'),
                    'SMA10_vs_SMA50': symbol_data.get('SMA10_vs_SMA50'),
                    
                    # Additional metrics
                    'Daily_PnL': shares * symbol_data.get('Day_Change', 0),
                    'Weight': 0,  # Will be calculated after all positions are processed
                    'Sector': self._get_sector_info(symbol),
                    'Last_Updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                
                portfolio_snapshot.append(snapshot_entry)
            
            if not portfolio_snapshot:
                logger.error("No valid portfolio positions found")
                return pd.DataFrame()
            
            # Convert to DataFrame
            snapshot_df = pd.DataFrame(portfolio_snapshot)
            
            # Portfolio weight is each position's share of the user's total
            # account value (Config.TOTAL_ACCOUNT_VALUE), not of invested
            # capital only. With one small seed holding and mostly cash,
            # weighting against sum(Position_Value) always showed 100% for
            # that holding — which made every concentration alert
            # (HOLDING_TRIM_WEIGHT_PCT, wechat's 集中度警告) fire on a
            # position that's actually a small slice of the real account.
            account_value = Config.TOTAL_ACCOUNT_VALUE
            snapshot_df['Weight'] = (snapshot_df['Position_Value'] / account_value * 100) if account_value > 0 else 0
            
            # Add portfolio summary metrics
            snapshot_df = self._add_portfolio_summary(snapshot_df)
            
            # Round numerical columns
            numeric_columns = snapshot_df.select_dtypes(include=[np.number]).columns
            snapshot_df[numeric_columns] = snapshot_df[numeric_columns].round(4)
            
            logger.info(f"Generated portfolio snapshot for {len(snapshot_df)} positions")
            return snapshot_df
            
        except Exception as e:
            logger.error(f"Error generating portfolio snapshot: {str(e)}")
            return pd.DataFrame()
    
    def _get_sector_info(self, symbol):
        """Get sector information for a symbol"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return info.get('sector', 'Unknown')
        except Exception:
            return 'Unknown'
    
    def _add_portfolio_summary(self, snapshot_df):
        """Add portfolio-level summary metrics"""
        try:
            if snapshot_df.empty:
                return snapshot_df
            
            # Calculate portfolio totals
            total_value = snapshot_df['Position_Value'].sum()
            total_cost = snapshot_df['Cost_Basis'].sum()
            total_pnl = snapshot_df['Unrealized_PnL'].sum()
            total_daily_pnl = snapshot_df['Daily_PnL'].sum()
            
            # Portfolio performance metrics
            portfolio_return_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            
            # Sector allocation
            sector_allocation = snapshot_df.groupby('Sector')['Weight'].sum().to_dict()
            
            # Risk metrics
            portfolio_beta = (snapshot_df['Weight'] * snapshot_df['Beta'].fillna(1)).sum() / 100
            
            # Add summary as attributes (not rows)
            snapshot_df.attrs['portfolio_summary'] = {
                'total_value': total_value,
                'total_cost': total_cost,
                'total_pnl': total_pnl,
                'total_daily_pnl': total_daily_pnl,
                'portfolio_return_pct': portfolio_return_pct,
                'sector_allocation': sector_allocation,
                'portfolio_beta': portfolio_beta,
                'num_positions': len(snapshot_df),
                'largest_position': snapshot_df.loc[snapshot_df['Weight'].idxmax(), 'Symbol'] if len(snapshot_df) > 0 else None,
                'largest_position_weight': snapshot_df['Weight'].max() if len(snapshot_df) > 0 else 0
            }
            
            return snapshot_df
            
        except Exception as e:
            logger.error(f"Error adding portfolio summary: {str(e)}")
            return snapshot_df
    
    def get_portfolio_summary(self):
        """Get high-level portfolio summary"""
        try:
            snapshot = self.get_portfolio_snapshot()
            if snapshot.empty or not hasattr(snapshot, 'attrs'):
                return {}
            
            return snapshot.attrs.get('portfolio_summary', {})
            
        except Exception as e:
            logger.error(f"Error getting portfolio summary: {str(e)}")
            return {}
    
    def add_position(self, symbol, shares, purchase_price, purchase_date=None):
        """Add a new position to the portfolio"""
        try:
            if purchase_date is None:
                purchase_date = datetime.now().strftime('%Y-%m-%d')
            
            new_position = pd.DataFrame({
                'Symbol': [symbol],
                'Shares': [shares],
                'Purchase_Price': [purchase_price],
                'Purchase_Date': [purchase_date]
            })
            
            self.portfolio_data = pd.concat([self.portfolio_data, new_position], ignore_index=True)
            self._save_portfolio()
            
            logger.info(f"Added position: {shares} shares of {symbol} at ${purchase_price}")
            
        except Exception as e:
            logger.error(f"Error adding position: {str(e)}")
    
    def remove_position(self, symbol):
        """Remove a position from the portfolio"""
        try:
            self.portfolio_data = self.portfolio_data[self.portfolio_data['Symbol'] != symbol]
            self._save_portfolio()
            
            logger.info(f"Removed position: {symbol}")
            
        except Exception as e:
            logger.error(f"Error removing position: {str(e)}")
    
    def update_position(self, symbol, shares=None, purchase_price=None):
        """Update an existing position"""
        try:
            mask = self.portfolio_data['Symbol'] == symbol
            
            if not mask.any():
                logger.error(f"Position {symbol} not found")
                return
            
            if shares is not None:
                self.portfolio_data.loc[mask, 'Shares'] = shares
            
            if purchase_price is not None:
                self.portfolio_data.loc[mask, 'Purchase_Price'] = purchase_price
            
            self._save_portfolio()
            
            logger.info(f"Updated position: {symbol}")
            
        except Exception as e:
            logger.error(f"Error updating position: {str(e)}")
    
    def _save_portfolio(self):
        """Save portfolio to file"""
        try:
            os.makedirs(os.path.dirname(self.portfolio_file), exist_ok=True)
            self.portfolio_data.to_csv(self.portfolio_file, index=False)
            logger.info(f"Portfolio saved to {self.portfolio_file}")
            
        except Exception as e:
            logger.error(f"Error saving portfolio: {str(e)}")
    
    def load_portfolio_from_excel(self, excel_file):
        """Load portfolio from Excel file"""
        try:
            df = pd.read_excel(excel_file)
            
            # Validate required columns
            required_columns = ['Symbol', 'Shares', 'Purchase_Price', 'Purchase_Date']
            
            if not all(col in df.columns for col in required_columns):
                logger.error(f"Excel file must contain columns: {required_columns}")
                return False
            
            self.portfolio_data = df[required_columns]
            self._save_portfolio()
            
            logger.info(f"Loaded portfolio from Excel: {len(self.portfolio_data)} positions")
            return True
            
        except Exception as e:
            logger.error(f"Error loading portfolio from Excel: {str(e)}")
            return False
    
    def export_portfolio_snapshot(self, filename=None):
        """Export portfolio snapshot to CSV"""
        try:
            snapshot = self.get_portfolio_snapshot()
            
            if snapshot.empty:
                logger.error("No portfolio snapshot to export")
                return None
            
            if filename is None:
                filename = f"portfolio_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            
            filepath = f"data/{filename}"
            snapshot.to_csv(filepath, index=False)
            
            logger.info(f"Portfolio snapshot exported to {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Error exporting portfolio snapshot: {str(e)}")
            return None
    
    def get_performance_metrics(self, days_back=30):
        """Calculate portfolio performance metrics over time"""
        try:
            # This would require historical portfolio snapshots
            # For now, return current metrics
            summary = self.get_portfolio_summary()
            
            performance_metrics = {
                'current_return_pct': summary.get('portfolio_return_pct', 0),
                'current_value': summary.get('total_value', 0),
                'total_cost': summary.get('total_cost', 0),
                'unrealized_pnl': summary.get('total_pnl', 0),
                'daily_pnl': summary.get('total_daily_pnl', 0),
                'num_positions': summary.get('num_positions', 0),
                'portfolio_beta': summary.get('portfolio_beta', 1.0)
            }
            
            return performance_metrics
            
        except Exception as e:
            logger.error(f"Error calculating performance metrics: {str(e)}")
            return {}