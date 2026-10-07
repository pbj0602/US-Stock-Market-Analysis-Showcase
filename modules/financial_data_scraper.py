"""
Financial Reports and Macro Data Scraping Module
Scrapes financial reports and macroeconomic indicators
"""

import time
import yfinance as yf
from modules.price_history import get_daily
import pandas as pd
import numpy as np
from fredapi import Fred
import requests
import ssl
import certifi
from datetime import datetime, timedelta
import logging
import concurrent.futures
from config.config import Config

logger = logging.getLogger(__name__)

class FinancialDataScraper:
    def __init__(self):
        # Initialize FRED API for macro data
        self.fred = None
        if Config.FRED_API_KEY:
            try:
                # fredapi uses urllib.request internally; patch it to use certifi's
                # CA bundle so FRED API calls are verified without disabling SSL.
                import urllib.request
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                https_handler = urllib.request.HTTPSHandler(context=ssl_context)
                opener = urllib.request.build_opener(https_handler)
                urllib.request.install_opener(opener)

                self.fred = Fred(api_key=Config.FRED_API_KEY)
                logger.info("FRED API initialised with certifi SSL verification")
            except Exception as e:
                logger.warning(f"Could not initialise FRED API: {str(e)}")
                self.fred = None
    
    def get_financial_data(self, symbols):
        """
        Fetch financial data for all symbols in parallel, with per-symbol retry.

        Uses Config.DATA_FETCH_WORKERS threads (default 5) instead of the
        previous per-symbol single-worker pool, which provided no concurrency.
        """
        financial_data = []
        workers = min(Config.DATA_FETCH_WORKERS, max(1, len(symbols)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._fetch_symbol_data, sym): sym for sym in symbols}
            for future in concurrent.futures.as_completed(futures):
                sym = futures[future]
                try:
                    result = future.result(timeout=30)
                    if result:
                        financial_data.append(result)
                except concurrent.futures.TimeoutError:
                    logger.warning(f"Timeout fetching financial data for {sym}, skipping")
                except Exception as e:
                    logger.error(f"Error fetching financial data for {sym}: {e}")

        if financial_data:
            result_df = pd.DataFrame(financial_data)
            logger.info(f"Successfully scraped financial data for {len(result_df)} symbols")
            return result_df
        else:
            logger.warning("No financial data was successfully scraped")
            return pd.DataFrame()

    def _fetch_symbol_data(self, symbol):
        """
        Fetch and process financial data for one symbol, retrying up to 3 times
        with exponential backoff (1s, 2s, 4s) on transient errors.
        """
        last_exc = None
        for attempt in range(3):
            try:
                t = yf.Ticker(symbol)
                info = t.info
                try:
                    stmt = t.quarterly_income_stmt
                    earnings = pd.DataFrame({'Earnings': stmt.loc['Net Income']}) \
                        if not stmt.empty and 'Net Income' in stmt.index else None
                except Exception:
                    earnings = None
                return self._process_financial_data(
                    symbol, info, earnings, t.quarterly_financials, t.financials
                )
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    delay = 2 ** attempt
                    logger.warning(
                        f"Attempt {attempt + 1}/3 failed for {symbol} ({e}), "
                        f"retrying in {delay}s"
                    )
                    time.sleep(delay)
        raise last_exc
    
    def _process_financial_data(self, symbol, info, quarterly_earnings, quarterly_financials, annual_financials):
        """Process and extract key financial metrics"""
        try:
            financial_summary = {
                'Symbol': symbol,
                'Date': datetime.now().date(),
                
                # Basic info
                'Market_Cap': info.get('marketCap'),
                'Enterprise_Value': info.get('enterpriseValue'),
                'Shares_Outstanding': info.get('sharesOutstanding'),
                'Float_Shares': info.get('floatShares'),
                
                # Valuation ratios
                'PE_Ratio': info.get('trailingPE'),
                'Forward_PE': info.get('forwardPE'),
                'PEG_Ratio': info.get('pegRatio'),
                'Price_to_Book': info.get('priceToBook'),
                'Price_to_Sales': info.get('priceToSalesTrailing12Months'),
                'EV_to_Revenue': info.get('enterpriseToRevenue'),
                'EV_to_EBITDA': info.get('enterpriseToEbitda'),
                
                # Financial strength
                # yfinance returns debtToEquity as a percentage (e.g. 150.0 == 1.5x).
                # Normalize to a ratio so it matches Config.DEBT_EQUITY_MAX (5.0 == 5x)
                # and downstream displays read as multiples, not percents.
                'Debt_to_Equity': (
                    info.get('debtToEquity') / 100.0
                    if info.get('debtToEquity') is not None else None
                ),
                'Current_Ratio': info.get('currentRatio'),
                'Quick_Ratio': info.get('quickRatio'),
                'Book_Value_Per_Share': info.get('bookValue'),
                
                # Profitability
                'ROA': info.get('returnOnAssets'),
                'ROE': info.get('returnOnEquity'),
                'Profit_Margin': info.get('profitMargins'),
                'Operating_Margin': info.get('operatingMargins'),
                'Gross_Margin': info.get('grossMargins'),
                
                # Growth rates
                'Revenue_Growth': info.get('revenueGrowth'),
                'Earnings_Growth': info.get('earningsGrowth'),
                'Quarterly_Revenue_Growth': info.get('revenueQuarterlyGrowth'),
                'Quarterly_Earnings_Growth': info.get('earningsQuarterlyGrowth'),
                
                # Dividend info
                'Dividend_Rate': info.get('dividendRate'),
                'Dividend_Yield': info.get('dividendYield'),
                'Payout_Ratio': info.get('payoutRatio'),
                'Dividend_Date': info.get('dividendDate'),
                'Ex_Dividend_Date': info.get('exDividendDate'),
                
                # Cash flow
                'Operating_Cash_Flow': info.get('operatingCashflow'),
                'Free_Cash_Flow': info.get('freeCashflow'),
                
                # Latest earnings data
                'Trailing_EPS': info.get('trailingEps'),
                'Forward_EPS': info.get('forwardEps'),
                'Last_Quarter_EPS': None,
                'EPS_Growth_QoQ': None,
            }
            
            # Add latest quarterly earnings data
            if quarterly_earnings is not None and not quarterly_earnings.empty:
                latest_quarter = quarterly_earnings.index[0]
                financial_summary['Last_Quarter_EPS'] = quarterly_earnings.loc[latest_quarter, 'Earnings']
                
                if len(quarterly_earnings) > 1:
                    prev_quarter = quarterly_earnings.index[1]
                    current_eps = quarterly_earnings.loc[latest_quarter, 'Earnings']
                    prev_eps = quarterly_earnings.loc[prev_quarter, 'Earnings']
                    if prev_eps != 0:
                        financial_summary['EPS_Growth_QoQ'] = (current_eps - prev_eps) / prev_eps * 100
            
            # Add latest revenue data from financials
            if not quarterly_financials.empty and 'Total Revenue' in quarterly_financials.index:
                latest_quarter = quarterly_financials.columns[0]
                financial_summary['Last_Quarter_Revenue'] = quarterly_financials.loc['Total Revenue', latest_quarter]
                
                if len(quarterly_financials.columns) > 1:
                    prev_quarter = quarterly_financials.columns[1]
                    current_rev = quarterly_financials.loc['Total Revenue', latest_quarter]
                    prev_rev = quarterly_financials.loc['Total Revenue', prev_quarter]
                    if prev_rev != 0:
                        financial_summary['Revenue_Growth_QoQ'] = (current_rev - prev_rev) / prev_rev * 100
            
            return financial_summary
            
        except Exception as e:
            logger.error(f"Error processing financial data for {symbol}: {str(e)}")
            return None
    
    def get_macro_data(self):
        """
        Get macroeconomic indicators
        
        Returns:
            pandas.DataFrame: DataFrame with macro data
        """
        macro_indicators = []
        
        # FRED indicators mapping
        fred_indicators = {
            'CPI': 'CPIAUCSL',  # Consumer Price Index
            'Core_CPI': 'CPILFESL',  # Core CPI
            'Inflation_Rate': 'T10YIE',  # 10-Year Breakeven Inflation Rate
            'Federal_Funds_Rate': 'FEDFUNDS',  # Federal Funds Rate
            '10Y_Treasury': 'DGS10',  # 10-Year Treasury Rate
            '2Y_Treasury': 'DGS2',  # 2-Year Treasury Rate
            'Yield_Curve': None,  # Will calculate as 10Y - 2Y
            'Unemployment_Rate': 'UNRATE',  # Unemployment Rate
            'GDP_Growth': 'GDP',  # Gross Domestic Product
            'Consumer_Sentiment': 'UMCSENT',  # University of Michigan Consumer Sentiment
            # NAPM / NAPMSI were removed from FRED; use Chicago Fed Activity Index
            # and Services Employment as proxies for manufacturing / services health
            'Chicago_Fed_Activity': 'CFNAI',
            'Services_Employment': 'SRVPRD',
            'Initial_Claims': 'ICSA',  # Initial Jobless Claims
            'Nonfarm_Payrolls': 'PAYEMS',  # Nonfarm Payrolls
            'Personal_Income': 'PI',  # Personal Income
            'Personal_Spending': 'PCE',  # Personal Consumption Expenditures
            'Retail_Sales': 'RSAFS',  # Retail Sales
            'Industrial_Production': 'INDPRO',  # Industrial Production Index
            'Housing_Starts': 'HOUST',  # Housing Starts
            'VIX': 'VIXCLS'  # VIX Volatility Index
        }
        
        if not self.fred:
            logger.warning("FRED API not available, using alternative macro data sources")
            return self._get_alternative_macro_data()
        
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=90)  # Last 3 months
            
            for indicator_name, fred_code in fred_indicators.items():
                if fred_code is None:
                    continue
                    
                try:
                    # Get the data
                    data = self.fred.get_series(fred_code, start=start_date, end=end_date)
                    
                    if not data.empty:
                        # Get latest value
                        latest_value = data.iloc[-1]
                        latest_date = data.index[-1]
                        
                        # Calculate change from previous period
                        change_value = None
                        change_pct = None
                        if len(data) > 1:
                            prev_value = data.iloc[-2]
                            change_value = latest_value - prev_value
                            if prev_value != 0:
                                change_pct = (change_value / prev_value) * 100
                        
                        macro_indicators.append({
                            'Indicator': indicator_name,
                            'FRED_Code': fred_code,
                            'Date': latest_date,
                            'Value': latest_value,
                            'Change': change_value,
                            'Change_Pct': change_pct,
                            'Units': self._get_indicator_units(indicator_name)
                        })
                        
                except Exception as e:
                    logger.warning(f"Could not fetch {indicator_name} ({fred_code}): {str(e)}")
                    continue
            
            # Calculate Yield Curve (10Y - 2Y)
            try:
                treasury_10y = next((x for x in macro_indicators if x['Indicator'] == '10Y_Treasury'), None)
                treasury_2y = next((x for x in macro_indicators if x['Indicator'] == '2Y_Treasury'), None)
                
                if treasury_10y and treasury_2y:
                    yield_curve = treasury_10y['Value'] - treasury_2y['Value']
                    macro_indicators.append({
                        'Indicator': 'Yield_Curve',
                        'FRED_Code': 'Calculated',
                        'Date': treasury_10y['Date'],
                        'Value': yield_curve,
                        'Change': None,
                        'Change_Pct': None,
                        'Units': 'Percentage Points'
                    })
            except Exception as e:
                logger.warning(f"Could not calculate yield curve: {str(e)}")
            
            # Add market-derived indicators
            macro_indicators.extend(self._get_market_macro_indicators())
            
            if macro_indicators:
                result_df = pd.DataFrame(macro_indicators)
                logger.info(f"Successfully scraped {len(result_df)} macro indicators")
                return result_df
            else:
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"Error scraping macro data: {str(e)}")
            return pd.DataFrame()
    
    def _get_market_macro_indicators(self):
        """Get market-derived macro indicators"""
        market_indicators = []
        
        try:
            # Dollar Index
            dxy = yf.Ticker('DX-Y.NYB')
            dxy_data = get_daily('DX-Y.NYB', '1mo')
            if not dxy_data.empty:
                latest = dxy_data.iloc[-1]
                prev = dxy_data.iloc[-2] if len(dxy_data) > 1 else None
                change_pct = ((latest['Close'] - prev['Close']) / prev['Close'] * 100) if prev is not None else None
                
                market_indicators.append({
                    'Indicator': 'Dollar_Index',
                    'FRED_Code': 'DX-Y.NYB',
                    'Date': latest.name.date(),
                    'Value': latest['Close'],
                    'Change': latest['Close'] - prev['Close'] if prev is not None else None,
                    'Change_Pct': change_pct,
                    'Units': 'Index'
                })
            
            # Gold Price
            gold = yf.Ticker('GC=F')
            gold_data = get_daily('GC=F', '1mo')
            if not gold_data.empty:
                latest = gold_data.iloc[-1]
                prev = gold_data.iloc[-2] if len(gold_data) > 1 else None
                change_pct = ((latest['Close'] - prev['Close']) / prev['Close'] * 100) if prev is not None else None
                
                market_indicators.append({
                    'Indicator': 'Gold_Price',
                    'FRED_Code': 'GC=F',
                    'Date': latest.name.date(),
                    'Value': latest['Close'],
                    'Change': latest['Close'] - prev['Close'] if prev is not None else None,
                    'Change_Pct': change_pct,
                    'Units': 'USD per ounce'
                })
            
            # Oil Price (WTI)
            oil = yf.Ticker('CL=F')
            oil_data = get_daily('CL=F', '1mo')
            if not oil_data.empty:
                latest = oil_data.iloc[-1]
                prev = oil_data.iloc[-2] if len(oil_data) > 1 else None
                change_pct = ((latest['Close'] - prev['Close']) / prev['Close'] * 100) if prev is not None else None
                
                market_indicators.append({
                    'Indicator': 'Oil_Price_WTI',
                    'FRED_Code': 'CL=F',
                    'Date': latest.name.date(),
                    'Value': latest['Close'],
                    'Change': latest['Close'] - prev['Close'] if prev is not None else None,
                    'Change_Pct': change_pct,
                    'Units': 'USD per barrel'
                })
                
        except Exception as e:
            logger.warning(f"Error getting market macro indicators: {str(e)}")
        
        return market_indicators
    
    def _get_indicator_units(self, indicator_name):
        """Get units for macro indicators"""
        units_map = {
            'CPI': 'Index (1982-84=100)',
            'Core_CPI': 'Index (1982-84=100)',
            'Inflation_Rate': 'Percent',
            'Federal_Funds_Rate': 'Percent',
            '10Y_Treasury': 'Percent',
            '2Y_Treasury': 'Percent',
            'Unemployment_Rate': 'Percent',
            'GDP_Growth': 'Billions of Chained 2012 Dollars',
            'Consumer_Sentiment': 'Index (1966:Q1=100)',
            'Chicago_Fed_Activity': 'Index',
            'Services_Employment': 'Thousands of Persons',
            'Initial_Claims': 'Thousands',
            'Nonfarm_Payrolls': 'Thousands of Persons',
            'Personal_Income': 'Billions of Dollars',
            'Personal_Spending': 'Billions of Dollars',
            'Retail_Sales': 'Millions of Dollars',
            'Industrial_Production': 'Index (2012=100)',
            'Housing_Starts': 'Thousands of Units',
            'VIX': 'Index'
        }
        return units_map.get(indicator_name, 'Unknown')
    
    def _get_alternative_macro_data(self):
        """Get macro data from alternative sources when FRED is unavailable"""
        try:
            macro_indicators = []
            
            # Get VIX from Yahoo Finance
            try:
                vix = yf.Ticker('^VIX')
                vix_data = get_daily('^VIX', '1mo')
                if not vix_data.empty:
                    latest = vix_data.iloc[-1]
                    prev = vix_data.iloc[-2] if len(vix_data) > 1 else None
                    change_pct = ((latest['Close'] - prev['Close']) / prev['Close'] * 100) if prev is not None else None
                    
                    macro_indicators.append({
                        'Indicator': 'VIX',
                        'FRED_Code': '^VIX',
                        'Date': latest.name.date(),
                        'Value': latest['Close'],
                        'Change': latest['Close'] - prev['Close'] if prev is not None else None,
                        'Change_Pct': change_pct,
                        'Units': 'Index'
                    })
            except Exception as e:
                logger.warning(f"Could not fetch VIX from Yahoo: {str(e)}")
            
            # Get Treasury yields from Yahoo Finance
            try:
                # 10-Year Treasury
                tnx = yf.Ticker('^TNX')
                tnx_data = get_daily('^TNX', '1mo')
                if not tnx_data.empty:
                    latest = tnx_data.iloc[-1]
                    prev = tnx_data.iloc[-2] if len(tnx_data) > 1 else None
                    change_pct = ((latest['Close'] - prev['Close']) / prev['Close'] * 100) if prev is not None else None
                    
                    macro_indicators.append({
                        'Indicator': '10Y_Treasury',
                        'FRED_Code': '^TNX',
                        'Date': latest.name.date(),
                        'Value': latest['Close'],
                        'Change': latest['Close'] - prev['Close'] if prev is not None else None,
                        'Change_Pct': change_pct,
                        'Units': 'Percent'
                    })
            except Exception as e:
                logger.warning(f"Could not fetch 10Y Treasury from Yahoo: {str(e)}")
            
            # Get additional market-derived indicators
            market_indicators = self._get_market_macro_indicators()
            macro_indicators.extend(market_indicators)
            
            if macro_indicators:
                result_df = pd.DataFrame(macro_indicators)
                logger.info(f"Successfully scraped {len(result_df)} macro indicators from alternative sources")
                return result_df
            else:
                logger.warning("No alternative macro data available")
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"Error getting alternative macro data: {str(e)}")
            return pd.DataFrame()
    
    def get_earnings_calendar(self, symbols):
        """Get upcoming earnings dates for symbols"""
        earnings_calendar = []
        
        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                calendar = ticker.calendar
                
                if calendar is not None and not calendar.empty:
                    earnings_date = calendar.index[0] if len(calendar) > 0 else None
                    earnings_calendar.append({
                        'Symbol': symbol,
                        'Earnings_Date': earnings_date,
                        'EPS_Estimate': calendar.iloc[0]['Earnings Estimate'] if earnings_date else None
                    })
                    
            except Exception as e:
                logger.warning(f"Could not get earnings calendar for {symbol}: {str(e)}")
                continue
        
        return pd.DataFrame(earnings_calendar) if earnings_calendar else pd.DataFrame()
    
    def save_data(self, df, filename):
        """Save DataFrame to CSV file"""
        try:
            filepath = f"data/{filename}_{datetime.now().strftime('%Y%m%d')}.csv"
            df.to_csv(filepath, index=False)
            logger.info(f"Data saved to {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving data: {str(e)}")
            return None