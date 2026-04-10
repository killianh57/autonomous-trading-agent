"""
cryptopanic_monitor.py
News crypto temps reel via CryptoPanic API.
Source alpha : articles + votes bullish/bearish + sentiment score.
"""

import os
import requests
from datetime import datetime, timezone
from typing import Optional


CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
BASE_URL = "https://cryptopanic.com/api/free/v1"

ASSET_CURRENCIES = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "IBIT": "BTC",
    "QQQ": None,
}

BULLISH_WORDS = [
    "surge", "rally", "pump", "breakout", "bullish", "beat", "record",
    "growth", "adoption", "upgrade", "launch", "partnership", "etf",
    "institutional", "accumulation", "buy", "long", "up", "gain",
]

BEARISH_WORDS = [
    "crash", "dump", "drop", "fall", "bearish", "hack", "exploit",
    "sec", "ban", "regulation", "investigation", "fraud", "bankruptcy",
    "delisted", "lawsuit", "sell", "short", "down", "loss", "warning",
]

HIGH_RISK_WORDS = [
    "hack", "exploit", "sec", "investigation", "fraud", "bankruptcy",
    "delisted", "lawsuit", "recall", "scandal", "ban", "shutdown",
]


def _get_news(currency: str, filter_type: str = "hot", limit: int = 10) -> list:
    """
    Recupere les news CryptoPanic pour une devise.
    filter_type: hot | new | rising | bullish | bearish | important | saved | lol
    """
    if not CRYPTOPANIC_TOKEN:
        return []

    params = {
        "auth_token": CRYPTOPANIC_TOKEN,
        "currencies": currency,
        "filter": filter_type,
        "public": "true",
        "kind": "news",
    }

    try:
        r = requests.get(f"{BASE_URL}/posts/", params=params, timeout=10)
        if r.ok:
            data = r.json()
            results = data.get("results", [])
            return results[:limit]
        return []
    except Exception:
        return []


def score_article(title: str, body: str = "") -> dict:
    """
    Score sentiment d'un article. Retourne bull_score, bear_score, sentiment.
    """
    text = (title + " " + body).lower()

    bull_score = sum(1 for w in BULLISH_WORDS if w in text)
    bear_score = sum(1 for w in BEARISH_WORDS if w in text)
    risk_flag = any(w in text for w in HIGH_RISK_WORDS)

    if bull_score > bear_score + 1:
        sentiment = "BULLISH"
    elif bear_score > bull_score + 1:
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    return {
        "bull_score": bull_score,
        "bear_score": bear_score,
        "sentiment": sentiment,
        "risk_flag": risk_flag,
    }


def get_crypto_sentiment(ticker: str, limit: int = 10) -> dict:
    """
    Sentiment agrege pour un ticker crypto.
    Retourne : sentiment, score, risk_detected, articles_count, headlines
    """
    currency = ASSET_CURRENCIES.get(ticker)
    if not currency:
        return {
            "ticker": ticker,
            "sentiment": "NEUTRAL",
            "bull_total": 0,
            "bear_total": 0,
            "risk_detected": False,
            "articles_count": 0,
            "headlines": [],
            "error": "No crypto mapping for this ticker",
        }

    articles = _get_news(currency, filter_type="hot", limit=limit)

    bull_total = 0
    bear_total = 0
    risk_detected = False
    headlines = []

    for article in articles:
        title = article.get("title", "")
        scores = score_article(title)
        bull_total += scores["bull_score"]
        bear_total += scores["bear_score"]
        if scores["risk_flag"]:
            risk_detected = True
        headlines.append({
            "title": title[:100],
            "sentiment": scores["sentiment"],
            "risk": scores["risk_flag"],
            "published_at": article.get("published_at", ""),
            "url": article.get("url", ""),
        })

    if bull_total > bear_total + 2:
        final_sentiment = "BULLISH"
    elif bear_total > bull_total + 2:
        final_sentiment = "BEARISH"
    else:
        final_sentiment = "NEUTRAL"

    return {
        "ticker": ticker,
        "currency": currency,
        "sentiment": final_sentiment,
        "bull_total": bull_total,
        "bear_total": bear_total,
        "risk_detected": risk_detected,
        "articles_count": len(articles),
        "headlines": headlines,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def should_pause_on_news(ticker: str) -> tuple:
    """
    Retourne (True, raison) si news a haut risque detectee.
    """
    currency = ASSET_CURRENCIES.get(ticker)
    if not currency:
        return False, "No crypto mapping"

    articles = _get_news(currency, filter_type="new", limit=5)
    for article in articles:
        title = article.get("title", "")
        for word in HIGH_RISK_WORDS:
            if word in title.lower():
                return True, f"High-risk news: {title[:80]}"

    return False, "No high-risk news detected"


def get_sentiment_modifier(ticker: str) -> float:
    """
    Modificateur de conviction base sur le sentiment news.
    BULLISH -> +0.5 | BEARISH -> -1.5 | NEUTRAL -> 0.0
    """
    result = get_crypto_sentiment(ticker, limit=8)
    sentiment = result.get("sentiment", "NEUTRAL")

    if sentiment == "BULLISH":
        return +0.5
    elif sentiment == "BEARISH":
        return -1.5
    return 0.0


def get_multi_asset_sentiment(tickers: list) -> dict:
    """
    Sentiment pour plusieurs assets en une fois.
    """
    results = {}
    for ticker in tickers:
        currency = ASSET_CURRENCIES.get(ticker)
        if currency:
            results[ticker] = get_crypto_sentiment(ticker)
    return results


if __name__ == "__main__":
    print("=== CryptoPanic Monitor Test ===")
    for asset in ["ETH", "BTC", "SOL"]:
        result = get_crypto_sentiment(asset, limit=5)
        print(f"\n{asset}: {result['sentiment']} "
              f"(bull={result['bull_total']}, bear={result['bear_total']}, "
              f"risk={result['risk_detected']}, articles={result['articles_count']})")
        for h in result["headlines"][:3]:
            print(f"  [{h['sentiment']}] {h['title'][:80]}")
