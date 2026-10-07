"""
News Scraping Module
Scrapes financial news for specified stocks and ETFs with enhanced sentiment analysis
using VADER and financial keyword scoring for better market-specific sentiment detection.
"""

import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import logging
from textblob import TextBlob
from config.config import Config
import feedparser
import time
import pytz
import re
import concurrent.futures
import json
import os

logger = logging.getLogger(__name__)

# Try to import VADER, fall back to TextBlob if not available
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    VADER_AVAILABLE = True
except ImportError:
    VADER_AVAILABLE = False
    logger.warning("VADER not available, using TextBlob for sentiment analysis. Install with: pip install vaderSentiment")


class FinancialSentimentAnalyzer:
    """
    Enhanced sentiment analyzer for financial news with:
    - VADER sentiment (when available)
    - Financial keyword scoring
    - Context-aware adjustments
    """

    def __init__(self):
        if VADER_AVAILABLE:
            self.vader = SentimentIntensityAnalyzer()
        else:
            self.vader = None

        # Financial-specific positive keywords with weights
        self.positive_keywords = {
            # Strong positive
            'beat': 2.0, 'beats': 2.0, 'exceeded': 2.0, 'surpassed': 2.0,
            'record': 1.5, 'breakthrough': 2.0, 'upgrade': 1.8, 'upgraded': 1.8,
            'outperform': 1.5, 'bullish': 1.5, 'surge': 1.5, 'surged': 1.5,
            'soar': 1.8, 'soared': 1.8, 'rally': 1.3, 'rallied': 1.3,
            'profit': 1.0, 'profitable': 1.2, 'growth': 1.0, 'growing': 1.0,
            'dividend': 0.8, 'buyback': 1.0, 'acquisition': 0.8,

            # Moderate positive
            'gain': 0.8, 'gains': 0.8, 'positive': 0.8, 'strong': 0.8,
            'higher': 0.6, 'rise': 0.6, 'rises': 0.6, 'rising': 0.6,
            'increase': 0.6, 'increased': 0.6, 'expansion': 0.8,
            'momentum': 0.7, 'opportunity': 0.5, 'recovery': 0.8,
            'optimistic': 1.0, 'confidence': 0.7, 'innovation': 0.6,

            # Mild positive
            'stable': 0.3, 'steady': 0.3, 'maintained': 0.2, 'affirmed': 0.3
        }

        # Financial-specific negative keywords with weights
        self.negative_keywords = {
            # Strong negative
            'fraud': -3.0, 'scandal': -2.5, 'bankrupt': -3.0, 'bankruptcy': -3.0,
            'lawsuit': -2.0, 'sued': -2.0, 'investigation': -1.8, 'sec': -1.5,
            'downgrade': -1.8, 'downgraded': -1.8, 'crash': -2.0, 'crashed': -2.0,
            'plunge': -2.0, 'plunged': -2.0, 'collapse': -2.5, 'collapsed': -2.5,
            'miss': -1.5, 'missed': -1.5, 'shortfall': -1.5, 'guidance cut': -2.0,
            'warning': -1.5, 'warns': -1.5, 'layoffs': -1.5, 'layoff': -1.5,
            'recall': -1.5, 'recalled': -1.5, 'default': -2.5,

            # Moderate negative
            'decline': -1.0, 'declined': -1.0, 'drop': -0.8, 'dropped': -0.8,
            'fall': -0.8, 'fell': -0.8, 'falling': -0.8, 'loss': -1.0,
            'losses': -1.0, 'weak': -0.8, 'weakness': -0.8, 'concern': -0.8,
            'concerns': -0.8, 'risk': -0.6, 'risks': -0.6, 'volatility': -0.5,
            'underperform': -1.2, 'bearish': -1.2, 'selloff': -1.3,
            'sell-off': -1.3, 'pressure': -0.5, 'headwinds': -0.8,

            # Mild negative
            'uncertain': -0.4, 'uncertainty': -0.4, 'cautious': -0.3,
            'challenging': -0.4, 'challenges': -0.4, 'slowdown': -0.6
        }

        # Context modifiers that can flip sentiment
        self.negation_words = ['not', 'no', 'never', 'neither', 'nobody', 'nothing',
                               'nowhere', 'without', 'hardly', 'barely', 'despite']

        self.intensifiers = {
            'very': 1.5, 'extremely': 2.0, 'significantly': 1.5, 'sharply': 1.5,
            'dramatically': 1.8, 'substantially': 1.5, 'massive': 1.8, 'huge': 1.5
        }

    def analyze(self, text):
        """
        Analyze sentiment of financial text using multiple methods.

        Args:
            text (str): Text to analyze

        Returns:
            dict: Sentiment analysis results
        """
        if not text or not isinstance(text, str):
            return self._default_sentiment()

        text_lower = text.lower()

        # Get VADER sentiment if available
        if self.vader:
            vader_scores = self.vader.polarity_scores(text)
            vader_compound = vader_scores['compound']
        else:
            vader_compound = None

        # Get TextBlob sentiment
        blob = TextBlob(text)
        textblob_polarity = blob.sentiment.polarity
        textblob_subjectivity = blob.sentiment.subjectivity

        # Get financial keyword sentiment
        keyword_score = self._calculate_keyword_score(text_lower)

        # Combine scores with weights
        if vader_compound is not None:
            # VADER available: weighted combination
            # VADER: 40%, Keywords: 40%, TextBlob: 20%
            combined_score = (
                vader_compound * 0.4 +
                keyword_score * 0.4 +
                textblob_polarity * 0.2
            )
        else:
            # No VADER: Keywords: 60%, TextBlob: 40%
            combined_score = (
                keyword_score * 0.6 +
                textblob_polarity * 0.4
            )

        # Normalize to -1 to 1 range
        combined_score = max(-1, min(1, combined_score))

        # Determine label and confidence
        if combined_score > 0.15:
            label = 'Positive'
            confidence = min(abs(combined_score) * 100, 100)
        elif combined_score < -0.15:
            label = 'Negative'
            confidence = min(abs(combined_score) * 100, 100)
        else:
            label = 'Neutral'
            confidence = (1 - abs(combined_score)) * 50  # Lower confidence for neutral

        return {
            'score': round(combined_score, 4),
            'label': label,
            'confidence': round(confidence, 1),
            'vader_score': round(vader_compound, 4) if vader_compound is not None else None,
            'keyword_score': round(keyword_score, 4),
            'textblob_score': round(textblob_polarity, 4),
            'subjectivity': round(textblob_subjectivity, 4)
        }

    def _calculate_keyword_score(self, text):
        """
        Calculate sentiment score based on financial keywords.

        Args:
            text (str): Lowercase text to analyze

        Returns:
            float: Keyword-based sentiment score
        """
        score = 0
        keyword_count = 0

        # Tokenize text (simple word boundary split)
        words = re.findall(r'\b\w+\b', text)

        for i, word in enumerate(words):
            # Check for negation in previous 3 words
            negated = any(words[max(0, i-3):i].count(neg) > 0 for neg in self.negation_words)

            # Check for intensifier in previous word
            intensifier = 1.0
            if i > 0 and words[i-1] in self.intensifiers:
                intensifier = self.intensifiers[words[i-1]]

            # Check positive keywords
            if word in self.positive_keywords:
                word_score = self.positive_keywords[word] * intensifier
                if negated:
                    word_score *= -0.5  # Flip and reduce magnitude
                score += word_score
                keyword_count += 1

            # Check negative keywords
            if word in self.negative_keywords:
                word_score = self.negative_keywords[word] * intensifier
                if negated:
                    word_score *= -0.5  # Flip and reduce magnitude
                score += word_score
                keyword_count += 1

        # Normalize by keyword count and cap at [-1, 1]
        if keyword_count > 0:
            normalized_score = score / (keyword_count * 1.5)  # Soften the impact
            return max(-1, min(1, normalized_score))

        return 0

    def _default_sentiment(self):
        """Return default neutral sentiment"""
        return {
            'score': 0,
            'label': 'Neutral',
            'confidence': 0,
            'vader_score': None,
            'keyword_score': 0,
            'textblob_score': 0,
            'subjectivity': 0
        }


class NewsScraper:
    def __init__(self):
        self.news_api_key = Config.NEWS_API_KEY
        self.session = requests.Session()
        # Use US Eastern timezone for market-relevant news timing
        self.us_tz = pytz.timezone('US/Eastern')
        # Initialize enhanced sentiment analyzer
        self.sentiment_analyzer = FinancialSentimentAnalyzer()
        
    def get_news_for_symbols(self, symbols, days_back=1, newsapi_symbols=None):
        """
        Get news for specified stock symbols.

        Args:
            symbols (list): Full list of stock symbols (Yahoo Finance + RSS for all).
            days_back (int): Number of days to look back for news.
            newsapi_symbols (list | None): Subset of symbols that get NewsAPI calls.
                Pass your holdings here to stay within the daily quota (200 req/day).
                If None, NewsAPI is called for ALL symbols (legacy behaviour).

        Returns:
            pandas.DataFrame: DataFrame with news data and sentiment analysis.
        """
        # Determine which symbols get the paid NewsAPI call
        if newsapi_symbols is None:
            newsapi_set = set(symbols)          # legacy: everyone
        else:
            newsapi_set = set(newsapi_symbols)  # budget-aware: holdings only
            logger.info(
                f"NewsAPI restricted to {len(newsapi_set)} holdings "
                f"(Yahoo Finance + RSS used for all {len(symbols)} symbols)"
            )

        # Pre-fetch RSS feeds ONCE for all symbols (they are general financial feeds,
        # not symbol-specific — fetching them per symbol wastes 54x the network calls)
        logger.info("Pre-fetching RSS feeds (once for all symbols)...")
        rss_cache = self._prefetch_rss_feeds()
        logger.info(f"RSS cache ready: {sum(len(v) for v in rss_cache.values())} entries across {len(rss_cache)} feeds")

        # Process symbols in parallel — 3 workers balances speed vs. Yahoo Finance rate limits
        all_news = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(
                    self._get_news_for_symbol,
                    symbol,
                    days_back,
                    rss_cache,
                    symbol in newsapi_set,
                ): symbol
                for symbol in symbols
            }
            for future in concurrent.futures.as_completed(futures):
                symbol = futures[future]
                try:
                    news = future.result()
                    all_news.extend(news)
                except Exception as e:
                    logger.error(f"Error scraping news for {symbol}: {str(e)}")

        # Add macro/market-wide news under the synthetic symbol "MARKET".
        # These are the top RSS headlines with NO symbol filter — captures broad
        # market events like Fed decisions, Trump tariffs, geopolitical shocks,
        # etc. that wouldn't match any individual stock ticker.
        # Cost: 0 extra API calls (RSS cache already fetched above) +
        #       1 optional NewsAPI call for richer macro headlines.
        all_news.extend(self._get_macro_market_news(rss_cache, days_back))

        if all_news:
            result_df = pd.DataFrame(all_news)
            result_df = result_df.drop_duplicates(subset=['Title', 'Symbol'], keep='first')
            result_df = result_df.sort_values(['Symbol', 'Published_Date'], ascending=[True, False])
            result_df.reset_index(drop=True, inplace=True)

            logger.info(f"Successfully scraped {len(result_df)} news articles")
            return result_df
        else:
            logger.warning("No news articles were successfully scraped")
            return pd.DataFrame()

    def _prefetch_rss_feeds(self):
        """
        Fetch all RSS feeds once upfront and return a dict of feed_url -> list of raw entries.
        These are general financial news feeds, not symbol-specific.
        """
        rss_feeds = [
            'https://feeds.finance.yahoo.com/rss/2.0/headline',
            'https://www.cnbc.com/id/100003114/device/rss/rss.html',
            'https://www.marketwatch.com/rss/topstories',
            'https://www.reuters.com/business/finance/rss'
        ]
        cache = {}

        def _fetch_feed(url):
            return feedparser.parse(url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_fetch_feed, url): url for url in rss_feeds}
            for future in concurrent.futures.as_completed(futures):
                url = futures[future]
                try:
                    feed = future.result(timeout=12)
                    entries = []
                    for entry in feed.entries[:15]:
                        try:
                            rss_dt = datetime(*entry.published_parsed[:6])
                            utc_dt = pytz.utc.localize(rss_dt)
                            us_dt = utc_dt.astimezone(self.us_tz)
                            published_date = us_dt.replace(tzinfo=None)
                        except Exception:
                            published_date = datetime.now(self.us_tz).replace(tzinfo=None)
                        entries.append({
                            'title': entry.get('title', ''),
                            'description': entry.get('summary', entry.get('description', '')),
                            'url': entry.get('link', ''),
                            'source': feed.feed.get('title', 'RSS Feed'),
                            'published_date': published_date,
                            'content': ''
                        })
                    cache[url] = entries
                except Exception as e:
                    logger.warning(f"Failed to fetch RSS feed {url}: {str(e)}")
                    cache[url] = []

        return cache

    def _get_macro_market_news(self, rss_cache, days_back=1):
        """
        Collect broad market / macro news stored under the synthetic symbol "MARKET".

        Two sources, zero extra network calls beyond what is already fetched:
        1. Top RSS headlines (no symbol filter) — captures Fed, Trump, tariffs, CPI, etc.
        2. One NewsAPI broad-market query (cached same-day) — richer headline set.

        Returns a list of processed article dicts (same schema as _process_news_articles).
        """
        MACRO_SYMBOL = 'MARKET'
        raw_articles = []

        # --- Source 1: top N headlines from each RSS feed (no symbol filter) ---
        MACRO_PER_FEED = 5  # take the 5 most recent headlines from each feed
        for entries in rss_cache.values():
            for entry in entries[:MACRO_PER_FEED]:
                if entry.get('title'):
                    raw_articles.append(entry)

        # --- Source 2: single broad NewsAPI query (1 call, cached) ---
        if self.news_api_key:
            macro_articles = self._get_macro_newsapi_articles(days_back)
            raw_articles.extend(macro_articles)

        logger.info(f"Macro news bucket: {len(raw_articles)} raw articles for '{MACRO_SYMBOL}'")
        return self._process_news_articles(MACRO_SYMBOL, raw_articles)

    def _get_macro_newsapi_articles(self, days_back=1):
        """
        One broad NewsAPI query covering market-moving macro topics.
        Cached to  cache/newsapi/MARKET_<YYYYMMDD>.json  so it only fires once per day.
        """
        cache_path = self._newsapi_cache_path('MARKET')
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                logger.info(f"NewsAPI macro cache hit ({len(cached)} articles)")
                return cached
            except Exception as e:
                logger.warning(f"NewsAPI macro cache unreadable, fetching live: {e}")

        articles = []
        try:
            us_now = datetime.now(self.us_tz)
            end_date = us_now.replace(tzinfo=None)
            start_date = end_date - timedelta(days=days_back)

            # Broad query: major market-moving topics — 1 request covers all of them
            macro_query = (
                'Federal Reserve OR Fed rate OR interest rate '
                'OR Trump tariff OR trade war OR trade deal OR China trade '
                'OR inflation OR CPI OR GDP '
                'OR stock market crash OR market rally '
                'OR recession OR economic outlook '
                'OR geopolitical risk OR sanctions OR executive order '
                'OR IPO OR SpaceX OR AI chip ban OR chip export '
                'OR Trump China OR US China relations OR G7 OR G20 '
                'OR White House policy OR Congress bill OR regulation tech '
                'OR oil price OR OPEC OR energy crisis '
                'OR bank crisis OR financial stability OR debt ceiling'
            )
            params = {
                'q': macro_query,
                'apiKey': self.news_api_key,
                'language': 'en',
                'sortBy': 'publishedAt',
                'from': start_date.strftime('%Y-%m-%d'),
                'to': end_date.strftime('%Y-%m-%d'),
                'pageSize': 20,
            }
            response = requests.get('https://newsapi.org/v2/everything', params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for article in data.get('articles', []):
                    if article.get('title') and article.get('description'):
                        articles.append({
                            'title': article['title'],
                            'description': article['description'],
                            'url': article.get('url', ''),
                            'source': article.get('source', {}).get('name', 'NewsAPI'),
                            'published_date': article.get('publishedAt', ''),
                            'content': article.get('content', ''),
                        })
                try:
                    with open(cache_path, 'w', encoding='utf-8') as f:
                        json.dump(articles, f)
                except Exception as ce:
                    logger.warning(f"Could not write macro NewsAPI cache: {ce}")
            elif response.status_code == 429:
                logger.warning("NewsAPI rate limit hit on macro query — skipping")
            else:
                logger.warning(f"NewsAPI macro query returned {response.status_code}")
        except Exception as e:
            logger.warning(f"Error fetching macro NewsAPI articles: {e}")

        return articles

    def _get_news_for_symbol(self, symbol, days_back, rss_cache, use_newsapi=True):
        """
        Fetch and process all news for a single symbol.
        Called in parallel by get_news_for_symbols.

        Args:
            use_newsapi (bool): Whether to call NewsAPI for this symbol.
                                Set False for watchlist symbols that are not holdings.
        """
        logger.info(f"Scraping news for {symbol}" + ("" if use_newsapi else " (Yahoo+RSS only)"))

        # Fetch company info once with a hard timeout
        company_info = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(lambda s=symbol: yf.Ticker(s).info)
                try:
                    company_info = future.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    logger.warning(f"Timeout fetching ticker info for {symbol}, using symbol as name")
        except Exception as e:
            logger.warning(f"Failed to fetch company info for {symbol}: {e}")
        company_name = company_info.get('longName', symbol).lower()
        short_name = company_info.get('shortName', symbol).lower()

        news_sources = []

        # NewsAPI — only for holdings / high-priority symbols to stay within daily quota
        if use_newsapi and self.news_api_key:
            news_sources.extend(self._get_newsapi_articles(symbol, days_back, company_name=company_name))

        # Yahoo Finance news
        news_sources.extend(self._get_yahoo_finance_news(symbol))

        # RSS: filter pre-fetched entries for this symbol (no extra network calls)
        news_sources.extend(self._filter_rss_for_symbol(symbol, rss_cache, company_name, short_name))

        return self._process_news_articles(symbol, news_sources)
    
    def _newsapi_cache_path(self, symbol):
        """Return the path of today's NewsAPI cache file for a given symbol."""
        cache_dir = os.path.join('cache', 'newsapi')
        os.makedirs(cache_dir, exist_ok=True)
        date_str = datetime.now(self.us_tz).strftime('%Y%m%d')
        return os.path.join(cache_dir, f"{symbol}_{date_str}.json")

    def _get_newsapi_articles(self, symbol, days_back, company_name=None):
        """Get news from NewsAPI with a same-day file cache.

        The cache prevents re-fetching the same symbol if the system runs
        multiple times in a day (e.g. both main run and sentiment re-summary).
        Cache files are named  cache/newsapi/<SYMBOL>_<YYYYMMDD>.json  and are
        automatically ignored on subsequent days.
        """
        articles = []

        if not self.news_api_key:
            return articles

        # --- Same-day cache check ---
        cache_path = self._newsapi_cache_path(symbol)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    articles = json.load(f)
                logger.info(f"NewsAPI cache hit for {symbol} ({len(articles)} articles)")
                return articles
            except Exception as e:
                logger.warning(f"NewsAPI cache for {symbol} unreadable, fetching live: {e}")

        try:
            if company_name is None:
                company_name = symbol

            us_now = datetime.now(self.us_tz)
            end_date = us_now.replace(tzinfo=None)
            start_date = end_date - timedelta(days=days_back)

            # Single combined query: "SYMBOL OR company name" — 1 request per symbol
            query = f'{symbol} OR "{company_name}"'
            try:
                url = "https://newsapi.org/v2/everything"
                params = {
                    'q': query,
                    'apiKey': self.news_api_key,
                    'language': 'en',
                    'sortBy': 'publishedAt',
                    'from': start_date.strftime('%Y-%m-%d'),
                    'to': end_date.strftime('%Y-%m-%d'),
                    'pageSize': 20
                }

                response = requests.get(url, params=params, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    for article in data.get('articles', []):
                        if article.get('title') and article.get('description'):
                            articles.append({
                                'title': article['title'],
                                'description': article['description'],
                                'url': article.get('url', ''),
                                'source': article.get('source', {}).get('name', 'NewsAPI'),
                                'published_date': article.get('publishedAt', ''),
                                'content': article.get('content', '')
                            })
                    # Persist to cache
                    try:
                        with open(cache_path, 'w', encoding='utf-8') as f:
                            json.dump(articles, f)
                    except Exception as ce:
                        logger.warning(f"Could not write NewsAPI cache for {symbol}: {ce}")
                elif response.status_code == 429:
                    logger.warning(f"NewsAPI rate limit hit for {symbol} — daily quota likely exceeded")
                else:
                    logger.warning(f"NewsAPI returned {response.status_code} for {symbol}")

            except Exception as e:
                logger.warning(f"Error with NewsAPI query for {symbol}: {str(e)}")

        except Exception as e:
            logger.warning(f"Error getting NewsAPI articles for {symbol}: {str(e)}")

        return articles
    
    def _get_yahoo_finance_news(self, symbol):
        """Get news from Yahoo Finance"""
        articles = []

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(lambda s=symbol: yf.Ticker(s).news)
                try:
                    news = future.result(timeout=15)
                except concurrent.futures.TimeoutError:
                    logger.warning(f"Timeout fetching Yahoo Finance news for {symbol}, skipping")
                    return articles
            
            for article in news:
                try:
                    # Handle new Yahoo Finance API structure where data is nested in 'content'
                    content = article.get('content', article)  # Fallback to article itself if no content key
                    
                    # Extract publish time and convert to US Eastern time
                    pub_time = content.get('providerPublishTime') or article.get('providerPublishTime', 0)
                    if isinstance(pub_time, str):
                        try:
                            dt = datetime.fromisoformat(pub_time.replace('Z', '+00:00'))
                            us_dt = dt.astimezone(self.us_tz)
                            published_date = us_dt.replace(tzinfo=None)  # Convert to timezone-naive US Eastern
                        except (ValueError, TypeError):
                            published_date = datetime.now(self.us_tz).replace(tzinfo=None)
                    else:
                        # Timestamp assumed to be UTC
                        utc_dt = datetime.fromtimestamp(pub_time if pub_time else 0, tz=pytz.utc)
                        us_dt = utc_dt.astimezone(self.us_tz)
                        published_date = us_dt.replace(tzinfo=None)
                    
                    articles.append({
                        'title': content.get('title', article.get('title', '')),
                        'description': content.get('summary', content.get('description', article.get('summary', ''))),
                        'url': content.get('canonicalUrl', {}).get('url', article.get('link', '')),
                        'source': content.get('provider', {}).get('displayName', article.get('publisher', 'Yahoo Finance')),
                        'published_date': published_date,
                        'content': ''
                    })
                except Exception as e:
                    logger.warning(f"Error processing Yahoo Finance article: {str(e)}")
                    continue
                    
        except Exception as e:
            logger.warning(f"Error getting Yahoo Finance news for {symbol}: {str(e)}")
        
        return articles
    
    def _filter_rss_for_symbol(self, symbol, rss_cache, company_name='', short_name=''):
        """
        Filter pre-fetched RSS entries for relevance to a given symbol.
        No network calls — operates purely on the in-memory cache.
        """
        articles = []
        for entries in rss_cache.values():
            for entry in entries:
                title = entry['title']
                description = entry['description']
                if self._is_relevant_article(symbol, title, description, company_name=company_name, short_name=short_name):
                    articles.append(entry)
        return articles
    
    def _is_relevant_article(self, symbol, title, description, company_name='', short_name=''):
        """Check if article is relevant to the symbol"""
        try:
            # Use pre-fetched company info (already lowercased by caller)
            text_to_check = f"{title} {description}".lower()
            
            # Check for symbol mentions
            if symbol.lower() in text_to_check:
                return True
            
            # Check for company name mentions
            if company_name and len(company_name) > 3:
                # Split company name into words and check for significant matches
                company_words = company_name.split()
                significant_words = [word for word in company_words if len(word) > 3 and word not in ['inc', 'corp', 'ltd', 'company', 'group']]
                
                for word in significant_words:
                    if word in text_to_check:
                        return True
            
            if short_name and len(short_name) > 3:
                if short_name in text_to_check:
                    return True
            
            return False
            
        except Exception as e:
            logger.warning(f"Error checking article relevance for {symbol}: {str(e)}")
            return False
    
    def _process_news_articles(self, symbol, articles):
        """Process and analyze news articles with enhanced sentiment analysis"""
        processed_articles = []

        for article in articles:
            try:
                # Perform enhanced sentiment analysis
                sentiment_data = self._analyze_sentiment(
                    article.get('title', ''),
                    article.get('description', '')
                )

                # Parse published date
                published_date = self._parse_date(article.get('published_date', ''))

                processed_article = {
                    'Symbol': symbol,
                    'Title': article.get('title', ''),
                    'Summary': article.get('description', ''),
                    'URL': article.get('url', ''),
                    'Source': article.get('source', ''),
                    'Published_Date': published_date,
                    'Sentiment_Score': sentiment_data['score'],
                    'Sentiment_Label': sentiment_data['label'],
                    'Confidence': sentiment_data['confidence'],
                    # Enhanced sentiment fields
                    'Keyword_Score': sentiment_data.get('keyword_score', 0),
                    'VADER_Score': sentiment_data.get('vader_score'),
                    'Subjectivity': sentiment_data.get('subjectivity', 0)
                }

                processed_articles.append(processed_article)

            except Exception as e:
                logger.warning(f"Error processing article: {str(e)}")
                continue

        return processed_articles
    
    def _analyze_sentiment(self, title, description):
        """
        Analyze sentiment of news article using enhanced financial sentiment analyzer.
        Uses VADER (if available) + financial keyword scoring + TextBlob.
        """
        try:
            # Combine title (weighted more heavily) and description for analysis
            # Title is usually more important for quick sentiment read
            text = f"{title} {title} {description}"  # Double-weight title

            # Use enhanced financial sentiment analyzer
            result = self.sentiment_analyzer.analyze(text)

            return {
                'score': result['score'],
                'label': result['label'],
                'confidence': result['confidence'],
                'keyword_score': result.get('keyword_score', 0),
                'vader_score': result.get('vader_score'),
                'subjectivity': result.get('subjectivity', 0)
            }

        except Exception as e:
            logger.warning(f"Error analyzing sentiment: {str(e)}")
            return {
                'score': 0.0,
                'label': 'Neutral',
                'confidence': 0.0,
                'keyword_score': 0,
                'vader_score': None,
                'subjectivity': 0
            }
    
    def _parse_date(self, date_string):
        """Parse various date formats and convert to US Eastern time (timezone-naive)"""
        if not date_string:
            return datetime.now(self.us_tz).replace(tzinfo=None)
        
        try:
            # Try ISO format first
            dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
            # Convert to US Eastern time, then make timezone-naive
            us_dt = dt.astimezone(self.us_tz)
            return us_dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            try:
                # Try other common formats (assume UTC if no timezone info)
                dt = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%SZ')
                utc_dt = pytz.utc.localize(dt)
                us_dt = utc_dt.astimezone(self.us_tz)
                return us_dt.replace(tzinfo=None)
            except (ValueError, TypeError):
                try:
                    # Plain datetime format (assume UTC)
                    dt = datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')
                    utc_dt = pytz.utc.localize(dt)
                    us_dt = utc_dt.astimezone(self.us_tz)
                    return us_dt.replace(tzinfo=None)
                except (ValueError, TypeError):
                    return datetime.now(self.us_tz).replace(tzinfo=None)
    
    def get_market_sentiment_summary(self, symbols, news_df=None):
        """Get overall market sentiment summary for given symbols.

        Args:
            symbols (list): Symbols to summarise.
            news_df (pd.DataFrame | None): Already-fetched news DataFrame from
                get_news_for_symbols().  When provided the method skips the
                fetch entirely, eliminating the duplicate NewsAPI calls that
                previously doubled quota usage.  Pass None only when calling
                this method standalone (e.g. from tests or scripts).
        """
        try:
            if news_df is None:
                # Standalone call — fetch fresh data (legacy path, not used from main.py)
                logger.warning(
                    "get_market_sentiment_summary called without pre-fetched news_df; "
                    "fetching now — this costs extra NewsAPI quota."
                )
                news_df = self.get_news_for_symbols(symbols, days_back=3)

            if news_df.empty:
                return {}

            # Calculate sentiment metrics by symbol
            sentiment_summary = {}

            for symbol in symbols:
                symbol_news = news_df[news_df['Symbol'] == symbol]

                if not symbol_news.empty:
                    avg_sentiment = symbol_news['Sentiment_Score'].mean()
                    sentiment_counts = symbol_news['Sentiment_Label'].value_counts()

                    sentiment_summary[symbol] = {
                        'Average_Sentiment': round(avg_sentiment, 3),
                        'Total_Articles': len(symbol_news),
                        'Positive_Count': sentiment_counts.get('Positive', 0),
                        'Negative_Count': sentiment_counts.get('Negative', 0),
                        'Neutral_Count': sentiment_counts.get('Neutral', 0),
                        'Latest_Article_Date': symbol_news['Published_Date'].max(),
                        'Top_Headlines': symbol_news.nlargest(3, 'Confidence')['Title'].tolist()
                    }

            return sentiment_summary

        except Exception as e:
            logger.error(f"Error generating market sentiment summary: {str(e)}")
            return {}
    
    def save_data(self, df, filename):
        """Save DataFrame to CSV file"""
        try:
            filepath = f"data/{filename}_{datetime.now().strftime('%Y%m%d')}.csv"
            df.to_csv(filepath, index=False)
            logger.info(f"News data saved to {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving news data: {str(e)}")
            return None