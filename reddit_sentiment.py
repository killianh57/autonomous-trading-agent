"""
Reddit Sentiment Module for Trading Bots
Scans key subreddits for ticker mentions + sentiment signals.
Uses Reddit public JSON API (no auth needed, rate-limited ~1req/sec).
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
REDDIT_SUBREDDITS_STOCKS = ["wallstreetbets", "stocks", "investing", "options"]
REDDIT_SUBREDDITS_CRYPTO = ["cryptocurrency", "CryptoMoonShots", "ethtrader", "solana", "altcoin"]
REDDIT_USER_AGENT = "TradingBot/1.0 (sentiment scanner)"
REDDIT_BASE_URL = "https://www.reddit.com"

# Cache: {asset: {"score": float, "mentions": int, "posts": list, "ts": float}}
_reddit_cache = {}
CACHE_TTL = 900  # 15 min

# Sentiment keywords
BULLISH_WORDS = [
    "moon", "rocket", "bull", "buy", "long", "breakout", "pump",
    "undervalued", "gem", "dip", "accumulate", "bullish", "calls",
    "yolo", "diamond hands", "hodl", "to the moon", "send it",
    "lambo", "ath", "all time high", "squeeze", "gamma"
]
BEARISH_WORDS = [
    "bear", "short", "sell", "crash", "dump", "puts", "overvalued",
    "bubble", "scam", "rug", "dead", "rip", "bagholding", "loss",
    "panic", "bearish", "correction", "recession", "capitulation"
]

# Common tickers to detect (expanded dynamically)
STOCK_TICKERS = {
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META",
    "AMD", "PLTR", "SOFI", "RIVN", "NIO", "BABA", "SPY", "QQQ",
    "VT", "SCHD", "VNQ", "IBIT", "MSTR", "COIN", "GME", "AMC",
    "ARM", "SMCI", "AVGO", "NFLX", "DIS", "BA", "JPM", "GS"
}
CRYPTO_TICKERS = {
    "BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "XRP", "ADA",
    "DOT", "MATIC", "SHIB", "PEPE", "WIF", "BONK", "JUP",
    "RENDER", "FET", "NEAR", "SUI", "APT", "INJ", "TIA"
}


def _fetch_subreddit(subreddit, sort="hot", limit=25):
    """Fetch posts from a subreddit via public JSON API."""
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/{sort}.json"
    params = {"limit": limit, "raw_json": 1}
    headers = {"User-Agent": REDDIT_USER_AGENT}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 429:
            logger.warning(f"Reddit rate limited on r/{subreddit}, backing off")
            time.sleep(2)
            return []
        if resp.status_code != 200:
            logger.warning(f"Reddit r/{subreddit} returned {resp.status_code}")
            return []
        data = resp.json()
        posts = []
        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            posts.append({
                "title": p.get("title", ""),
                "selftext": p.get("selftext", "")[:500],
                "score": p.get("score", 0),
                "num_comments": p.get("num_comments", 0),
                "created_utc": p.get("created_utc", 0),
                "subreddit": subreddit,
                "permalink": p.get("permalink", ""),
                "upvote_ratio": p.get("upvote_ratio", 0.5),
            })
        return posts
    except Exception as e:
        logger.error(f"Reddit fetch error r/{subreddit}: {e}")
        return []


def _extract_tickers(text, asset_type="stock"):
    """Extract ticker mentions from text."""
    tickers = STOCK_TICKERS if asset_type == "stock" else CRYPTO_TICKERS
    found = set()
    text_upper = text.upper()
    for ticker in tickers:
        # Match $TICKER or standalone TICKER (word boundary)
        pattern = r'(?:\$' + ticker + r'\b|(?<![A-Z])' + ticker + r'(?![A-Z]))'
        if re.search(pattern, text_upper):
            found.add(ticker)
    return found


def _score_post(post):
    """Score a single post for sentiment. Returns float -1.0 to +1.0."""
    text = (post["title"] + " " + post["selftext"]).lower()
    bull_count = sum(1 for w in BULLISH_WORDS if w in text)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text)
    total = bull_count + bear_count
    if total == 0:
        return 0.0
    raw = (bull_count - bear_count) / total
    # Weight by engagement (log scale)
    engagement = post["score"] + post["num_comments"] * 2
    weight = min(max(engagement / 100, 0.1), 3.0)
    return round(raw * weight, 3)


def scan_reddit(asset=None, asset_type="stock", max_age_hours=24):
    """
    Scan Reddit for sentiment on a specific asset or general market.
    
    Args:
        asset: Ticker symbol (e.g. "NVDA", "BTC"). None = general scan.
        asset_type: "stock" or "crypto"
        max_age_hours: Only consider posts within this window.
    
    Returns:
        dict: {
            "score": float (-10 to +10),
            "mentions": int,
            "sentiment": "bullish" | "bearish" | "neutral",
            "top_posts": list[dict],
            "buzz_level": "low" | "medium" | "high" | "extreme",
            "source": "reddit"
        }
    """
    cache_key = f"{asset_type}:{asset or 'general'}"
    cached = _reddit_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        logger.info(f"Reddit cache hit for {cache_key}")
        return cached["data"]

    subreddits = (
        REDDIT_SUBREDDITS_CRYPTO if asset_type == "crypto"
        else REDDIT_SUBREDDITS_STOCKS
    )

    all_posts = []
    cutoff = time.time() - (max_age_hours * 3600)

    for sub in subreddits:
        posts = _fetch_subreddit(sub, sort="hot", limit=25)
        # Rate limit courtesy
        time.sleep(0.5)
        for p in posts:
            if p["created_utc"] < cutoff:
                continue
            if asset:
                text = p["title"] + " " + p["selftext"]
                tickers = _extract_tickers(text, asset_type)
                if asset.upper() not in tickers:
                    continue
            all_posts.append(p)

    if not all_posts:
        result = {
            "score": 0,
            "mentions": 0,
            "sentiment": "neutral",
            "top_posts": [],
            "buzz_level": "low",
            "source": "reddit"
        }
        _reddit_cache[cache_key] = {"data": result, "ts": time.time()}
        return result

    # Score all relevant posts
    scores = [_score_post(p) for p in all_posts]
    avg_score = sum(scores) / len(scores) if scores else 0
    # Scale to -10 / +10
    final_score = round(max(-10, min(10, avg_score * 10)), 1)

    mentions = len(all_posts)
    if mentions >= 20:
        buzz = "extreme"
    elif mentions >= 10:
        buzz = "high"
    elif mentions >= 5:
        buzz = "medium"
    else:
        buzz = "low"

    if final_score >= 3:
        sentiment = "bullish"
    elif final_score <= -3:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Top posts by engagement
    top = sorted(all_posts, key=lambda x: x["score"] + x["num_comments"], reverse=True)[:5]
    top_posts = [
        {
            "title": p["title"][:100],
            "score": p["score"],
            "comments": p["num_comments"],
            "subreddit": p["subreddit"],
            "sentiment_score": _score_post(p),
        }
        for p in top
    ]

    result = {
        "score": final_score,
        "mentions": mentions,
        "sentiment": sentiment,
        "top_posts": top_posts,
        "buzz_level": buzz,
        "source": "reddit"
    }

    _reddit_cache[cache_key] = {"data": result, "ts": time.time()}
    logger.info(
        f"Reddit scan {cache_key}: score={final_score}, "
        f"mentions={mentions}, buzz={buzz}"
    )
    return result


def reddit_ticker_heatmap(asset_type="stock", limit=25):
    """
    Scan all subreddits and return most-mentioned tickers ranked.
    Useful for discovering trending assets before they move.
    
    Returns:
        list[dict]: Sorted by mentions desc.
        [{"ticker": "NVDA", "mentions": 12, "avg_sentiment": 0.7, "buzz": "high"}, ...]
    """
    subreddits = (
        REDDIT_SUBREDDITS_CRYPTO if asset_type == "crypto"
        else REDDIT_SUBREDDITS_STOCKS
    )

    ticker_data = defaultdict(lambda: {"mentions": 0, "scores": []})
    cutoff = time.time() - 86400  # 24h

    for sub in subreddits:
        posts = _fetch_subreddit(sub, sort="hot", limit=limit)
        time.sleep(0.5)
        for p in posts:
            if p["created_utc"] < cutoff:
                continue
            text = p["title"] + " " + p["selftext"]
            tickers = _extract_tickers(text, asset_type)
            post_score = _score_post(p)
            for t in tickers:
                ticker_data[t]["mentions"] += 1
                ticker_data[t]["scores"].append(post_score)

    results = []
    for ticker, info in ticker_data.items():
        avg = sum(info["scores"]) / len(info["scores"]) if info["scores"] else 0
        m = info["mentions"]
        results.append({
            "ticker": ticker,
            "mentions": m,
            "avg_sentiment": round(avg, 2),
            "buzz": "extreme" if m >= 15 else "high" if m >= 8 else "medium" if m >= 3 else "low",
        })

    results.sort(key=lambda x: x["mentions"], reverse=True)
    return results[:20]


def reddit_sentiment_filter(asset, asset_type="stock"):
    """
    Quick filter for trade decisions.
    Returns tuple: (should_proceed: bool, reason: str, score: float)
    
    Rules:
    - score <= -6 on a LONG -> block (extreme bearish crowd = possible further dump)
    - score >= 8 on a LONG -> caution (extreme FOMO = possible top)
    - buzz "extreme" + score > 6 -> contrarian warning
    """
    data = scan_reddit(asset=asset, asset_type=asset_type)
    score = data["score"]
    buzz = data["buzz_level"]
    mentions = data["mentions"]

    if mentions < 2:
        return True, f"Reddit: low coverage ({mentions} mentions), no signal", score

    # Extreme bearish crowd -> block longs
    if score <= -6:
        return False, f"Reddit: extreme bearish ({score}/10, {mentions} mentions) - crowd panic, wait", score

    # Extreme bullish + extreme buzz -> FOMO warning
    if score >= 8 and buzz == "extreme":
        return False, f"Reddit: FOMO territory ({score}/10, {buzz} buzz) - possible top, skip", score

    # Moderate signals -> proceed with info
    sentiment = data["sentiment"]
    return True, f"Reddit: {sentiment} ({score}/10, {mentions} mentions, {buzz} buzz)", score


# --- STANDALONE TEST ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Reddit Ticker Heatmap (Stocks) ===")
    heatmap = reddit_ticker_heatmap("stock")
    for t in heatmap[:10]:
        print(f"  {t['ticker']:>6} | {t['mentions']:>2} mentions | sentiment {t['avg_sentiment']:>+.2f} | {t['buzz']}")

    print("\n=== Reddit Ticker Heatmap (Crypto) ===")
    heatmap_c = reddit_ticker_heatmap("crypto")
    for t in heatmap_c[:10]:
        print(f"  {t['ticker']:>6} | {t['mentions']:>2} mentions | sentiment {t['avg_sentiment']:>+.2f} | {t['buzz']}")

    print("\n=== Sentiment Filter Test: NVDA ===")
    ok, reason, sc = reddit_sentiment_filter("NVDA", "stock")
    print(f"  Proceed: {ok} | {reason}")

    print("\n=== Sentiment Filter Test: BTC ===")
    ok, reason, sc = reddit_sentiment_filter("BTC", "crypto")
    print(f"  Proceed: {ok} | {reason}")
