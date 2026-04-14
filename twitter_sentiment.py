"""
Twitter Sentiment Module for Trading Bots
Scans Twitter/X for ticker mentions + sentiment signals.
Uses Twitter API v2 Recent Search (Bearer Token auth).
Env var: TWITTER_BEARER_TOKEN
"""

import os
import re
import time
import json
import logging
import requests
from collections import defaultdict

logger = logging.getLogger(__name__)

# --- CONFIG ---
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
TWITTER_API_URL = "https://api.twitter.com/2/tweets/search/recent"

# Cache: {key: {"data": dict, "ts": float}}
_twitter_cache = {}
CACHE_TTL = 900  # 15 min

# Sentiment keywords (same scale as reddit_sentiment for consistency)
BULLISH_WORDS = [
    "moon", "rocket", "bull", "buy", "long", "breakout", "pump",
    "undervalued", "gem", "dip", "accumulate", "bullish", "calls",
    "yolo", "diamond hands", "hodl", "send it", "ath",
    "all time high", "squeeze", "gamma", "liftoff", "parabolic",
    "reversal", "support held", "bottomed"
]
BEARISH_WORDS = [
    "bear", "short", "sell", "crash", "dump", "puts", "overvalued",
    "bubble", "scam", "rug", "dead", "rip", "bagholding", "loss",
    "panic", "bearish", "correction", "recession", "capitulation",
    "breakdown", "resistance rejected", "topped", "exit"
]

# Cashtag mappings
STOCK_CASHTAGS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META",
    "AMD", "PLTR", "SOFI", "RIVN", "NIO", "BABA", "SPY", "QQQ",
    "VT", "SCHD", "VNQ", "IBIT", "MSTR", "COIN", "GME", "AMC",
    "ARM", "SMCI", "AVGO", "NFLX", "DIS", "BA", "JPM", "GS"
]
CRYPTO_CASHTAGS = [
    "BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "XRP", "ADA",
    "DOT", "MATIC", "SHIB", "PEPE", "WIF", "BONK", "JUP",
    "RENDER", "FET", "NEAR", "SUI", "APT", "INJ", "TIA"
]


def _twitter_headers():
    """Build auth headers."""
    return {
        "Authorization": "Bearer {}".format(TWITTER_BEARER_TOKEN),
        "User-Agent": "TradingBot/1.0",
    }


def _search_tweets(query, max_results=50):
    """
    Search recent tweets (last 7 days) via Twitter API v2.
    Returns list of tweet dicts with text + public_metrics.
    """
    if not TWITTER_BEARER_TOKEN:
        logger.warning("TWITTER_BEARER_TOKEN not set, skipping")
        return []

    params = {
        "query": query,
        "max_results": min(max_results, 100),
        "tweet.fields": "created_at,public_metrics,lang",
        "sort_order": "relevancy",
    }

    try:
        resp = requests.get(
            TWITTER_API_URL,
            headers=_twitter_headers(),
            params=params,
            timeout=15,
        )
        if resp.status_code == 429:
            logger.warning("Twitter rate limited, backing off")
            return []
        if resp.status_code == 401:
            logger.error("Twitter auth failed - check TWITTER_BEARER_TOKEN")
            return []
        if resp.status_code != 200:
            logger.warning("Twitter API returned %d: %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        tweets = data.get("data", [])
        meta = data.get("meta", {})
        logger.info("Twitter search '%s': %d results (total: %s)",
                     query[:40], len(tweets), meta.get("result_count", "?"))
        return tweets

    except Exception as e:
        logger.error("Twitter search error: %s", e)
        return []


def _score_tweet(tweet):
    """Score a single tweet for sentiment. Returns float -1.0 to +1.0."""
    text = tweet.get("text", "").lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text)
    bear = sum(1 for w in BEARISH_WORDS if w in text)
    total = bull + bear
    if total == 0:
        return 0.0
    raw = (bull - bear) / total
    # Weight by engagement
    metrics = tweet.get("public_metrics", {})
    engagement = (
        metrics.get("like_count", 0)
        + metrics.get("retweet_count", 0) * 2
        + metrics.get("reply_count", 0)
    )
    weight = min(max(engagement / 50, 0.1), 3.0)
    return round(raw * weight, 3)


def scan_twitter(asset=None, asset_type="stock", max_results=50):
    """
    Scan Twitter for sentiment on a specific asset or general market.

    Args:
        asset: Ticker symbol (e.g. "NVDA", "BTC"). None = general scan.
        asset_type: "stock" or "crypto"
        max_results: Max tweets to fetch.

    Returns:
        dict: {
            "score": float (-10 to +10),
            "mentions": int,
            "sentiment": "bullish" | "bearish" | "neutral",
            "top_tweets": list[dict],
            "buzz_level": "low" | "medium" | "high" | "extreme",
            "source": "twitter"
        }
    """
    cache_key = "twitter:{}:{}".format(asset_type, asset or "general")
    cached = _twitter_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    if not TWITTER_BEARER_TOKEN:
        result = {
            "score": 0, "mentions": 0, "sentiment": "neutral",
            "top_tweets": [], "buzz_level": "low", "source": "twitter"
        }
        return result

    # Build query
    if asset:
        # Cashtag search is most precise on Twitter
        query = "${} -is:retweet lang:en".format(asset.upper())
    elif asset_type == "crypto":
        tags = " OR ".join("${}".format(t) for t in CRYPTO_CASHTAGS[:8])
        query = "({}) -is:retweet lang:en".format(tags)
    else:
        tags = " OR ".join("${}".format(t) for t in STOCK_CASHTAGS[:8])
        query = "({}) -is:retweet lang:en".format(tags)

    tweets = _search_tweets(query, max_results=max_results)

    if not tweets:
        result = {
            "score": 0, "mentions": 0, "sentiment": "neutral",
            "top_tweets": [], "buzz_level": "low", "source": "twitter"
        }
        _twitter_cache[cache_key] = {"data": result, "ts": time.time()}
        return result

    # Score tweets
    scores = [_score_tweet(t) for t in tweets]
    avg_score = sum(scores) / len(scores) if scores else 0
    final_score = round(max(-10, min(10, avg_score * 10)), 1)

    mentions = len(tweets)
    if mentions >= 80:
        buzz = "extreme"
    elif mentions >= 40:
        buzz = "high"
    elif mentions >= 15:
        buzz = "medium"
    else:
        buzz = "low"

    if final_score >= 3:
        sentiment = "bullish"
    elif final_score <= -3:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Top tweets by engagement
    def _engagement(t):
        m = t.get("public_metrics", {})
        return m.get("like_count", 0) + m.get("retweet_count", 0) * 2 + m.get("reply_count", 0)

    top = sorted(tweets, key=_engagement, reverse=True)[:5]
    top_tweets = [
        {
            "text": t.get("text", "")[:120],
            "likes": t.get("public_metrics", {}).get("like_count", 0),
            "retweets": t.get("public_metrics", {}).get("retweet_count", 0),
            "sentiment_score": _score_tweet(t),
        }
        for t in top
    ]

    result = {
        "score": final_score,
        "mentions": mentions,
        "sentiment": sentiment,
        "top_tweets": top_tweets,
        "buzz_level": buzz,
        "source": "twitter",
    }

    _twitter_cache[cache_key] = {"data": result, "ts": time.time()}
    logger.info("Twitter scan %s: score=%s, mentions=%d, buzz=%s",
                cache_key, final_score, mentions, buzz)
    return result


def twitter_sentiment_filter(asset, asset_type="stock"):
    """
    Quick filter for trade decisions.
    Returns tuple: (should_proceed: bool, reason: str, score: float)

    Rules:
    - score <= -6 -> block (extreme bearish = possible further dump)
    - score >= 8 + buzz extreme -> caution (FOMO = possible top)
    - No token -> pass through (no blocking without data)
    """
    if not TWITTER_BEARER_TOKEN:
        return True, "Twitter: no token configured, skipping", 0

    data = scan_twitter(asset=asset, asset_type=asset_type)
    score = data["score"]
    buzz = data["buzz_level"]
    mentions = data["mentions"]

    if mentions < 5:
        return True, "Twitter: low coverage ({} tweets), no signal".format(mentions), score

    # Extreme bearish
    if score <= -6:
        return False, "Twitter: extreme bearish ({}/10, {} tweets) - crowd panic".format(score, mentions), score

    # Extreme bullish + extreme buzz = FOMO
    if score >= 8 and buzz == "extreme":
        return False, "Twitter: FOMO territory ({}/10, {} buzz) - possible top".format(score, buzz), score

    sentiment = data["sentiment"]
    return True, "Twitter: {} ({}/10, {} tweets, {} buzz)".format(sentiment, score, mentions, buzz), score


# --- STANDALONE TEST ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if not TWITTER_BEARER_TOKEN:
        print("Set TWITTER_BEARER_TOKEN env var to test")
    else:
        print("=== Twitter Scan: NVDA ===")
        r = scan_twitter("NVDA", "stock")
        print("  Score: {}/10 | Mentions: {} | Buzz: {} | Sentiment: {}".format(
            r["score"], r["mentions"], r["buzz_level"], r["sentiment"]))

        print("\n=== Twitter Scan: BTC ===")
        r = scan_twitter("BTC", "crypto")
        print("  Score: {}/10 | Mentions: {} | Buzz: {} | Sentiment: {}".format(
            r["score"], r["mentions"], r["buzz_level"], r["sentiment"]))

        print("\n=== Twitter Filter: TSLA ===")
        ok, reason, sc = twitter_sentiment_filter("TSLA", "stock")
        print("  Proceed: {} | {}".format(ok, reason))
