"""
Perplexity Sonar Sentiment Module
Shared module for Bot 1 (Alpaca) and Bot 2 (Coinbase)
Queries Perplexity Sonar API for real-time news sentiment on assets.
"""

import os
import json
import time
import logging
import requests

logger = logging.getLogger(__name__)

PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"  # cheap + fast, use "sonar-pro" for deeper

# Cache to avoid redundant API calls (asset -> (score, timestamp))
_sentiment_cache = {}
CACHE_TTL_SECONDS = 900  # 15 min cache per asset


def query_sonar(asset, asset_type="stock"):
    """
    Query Perplexity Sonar for real-time sentiment on an asset.
    Returns raw text response from Sonar.
    """
    if not PERPLEXITY_API_KEY:
        logger.warning("PERPLEXITY_API_KEY not set, skipping sentiment")
        return None

    if asset_type == "crypto":
        prompt = (
            f"What is the current market sentiment for {asset} cryptocurrency? "
            f"Consider: recent news, regulatory developments, whale movements, "
            f"exchange flows, social media buzz, macro factors. "
            f"Rate sentiment from -10 (extremely bearish) to +10 (extremely bullish). "
            f"Reply ONLY with this JSON format, no other text: "
            f'{{"asset": "{asset}", "score": <number>, "summary": "<1 sentence>", '
            f'"key_factor": "<main driver>", "risk": "<main risk>"}}'
        )
    else:
        prompt = (
            f"What is the current market sentiment for {asset} stock/ETF? "
            f"Consider: recent earnings, analyst ratings, sector trends, "
            f"macro environment (Fed, inflation), institutional flows. "
            f"Rate sentiment from -10 (extremely bearish) to +10 (extremely bullish). "
            f"Reply ONLY with this JSON format, no other text: "
            f'{{"asset": "{asset}", "score": <number>, "summary": "<1 sentence>", '
            f'"key_factor": "<main driver>", "risk": "<main risk>"}}'
        )

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a financial sentiment analyst. "
                    "Always respond with valid JSON only. No markdown, no explanation."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }

    try:
        resp = requests.post(
            PERPLEXITY_URL, headers=headers, json=payload, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content
    except requests.exceptions.Timeout:
        logger.error("Perplexity API timeout for %s", asset)
        return None
    except requests.exceptions.RequestException as e:
        logger.error("Perplexity API error for %s: %s", asset, e)
        return None
    except (KeyError, IndexError) as e:
        logger.error("Perplexity response parse error: %s", e)
        return None


def parse_sentiment(raw_text):
    """
    Parse JSON sentiment from Sonar response.
    Returns dict with score, summary, key_factor, risk.
    Returns None on parse failure.
    """
    if not raw_text:
        return None
    try:
        # Strip markdown fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        result = json.loads(text)
        # Validate score is numeric and in range
        score = float(result.get("score", 0))
        score = max(-10.0, min(10.0, score))
        result["score"] = score
        return result
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse sentiment JSON: %s | raw: %s", e, raw_text[:200])
        return None


def get_sentiment(asset, asset_type="stock"):
    """
    Get sentiment score for an asset with caching.
    Returns dict: {"score": float, "summary": str, "key_factor": str, "risk": str}
    Returns None if unavailable.
    """
    now = time.time()
    cache_key = f"{asset}_{asset_type}"

    # Check cache
    if cache_key in _sentiment_cache:
        cached_result, cached_time = _sentiment_cache[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            logger.info("Sentiment cache hit for %s: score=%s", asset, cached_result.get("score"))
            return cached_result

    # Query Sonar
    raw = query_sonar(asset, asset_type)
    result = parse_sentiment(raw)

    if result:
        _sentiment_cache[cache_key] = (result, now)
        logger.info(
            "Sonar sentiment for %s: score=%s, summary=%s",
            asset,
            result.get("score"),
            result.get("summary", "")[:80],
        )
    return result


def sentiment_filter(asset, asset_type="stock", min_score=-3.0):
    """
    Pre-trade sentiment filter.
    Returns (should_trade: bool, sentiment_data: dict or None)

    Logic:
    - score >= 2.0  -> BULLISH confirmation, boost confidence
    - score -3 to 2 -> NEUTRAL, allow trade but no boost
    - score < -3.0  -> BEARISH, BLOCK the trade

    min_score is configurable per bot.
    """
    sentiment = get_sentiment(asset, asset_type)
    if sentiment is None:
        # API unavailable -> don't block, but flag
        logger.warning("Sentiment unavailable for %s, allowing trade with caution", asset)
        return True, None

    score = sentiment.get("score", 0)
    if score < min_score:
        logger.info(
            "SENTIMENT BLOCK: %s score=%.1f < min=%.1f | %s",
            asset,
            score,
            min_score,
            sentiment.get("summary", ""),
        )
        return False, sentiment

    return True, sentiment


def sentiment_confidence_boost(base_confidence, sentiment_data):
    """
    Adjust confidence score based on sentiment.
    base_confidence: int (0-100)
    Returns adjusted confidence (0-100).
    """
    if sentiment_data is None:
        return base_confidence

    score = sentiment_data.get("score", 0)

    if score >= 5.0:
        # Strong bullish -> +10 confidence
        return min(100, base_confidence + 10)
    elif score >= 2.0:
        # Mild bullish -> +5 confidence
        return min(100, base_confidence + 5)
    elif score <= -5.0:
        # Strong bearish -> -15 confidence
        return max(0, base_confidence - 15)
    elif score <= -2.0:
        # Mild bearish -> -5 confidence
        return max(0, base_confidence - 5)
    else:
        # Neutral -> no change
        return base_confidence


def format_sentiment_telegram(sentiment_data):
    """
    Format sentiment data for Telegram notification.
    """
    if not sentiment_data:
        return "Sentiment: N/A (API unavailable)"

    score = sentiment_data.get("score", 0)
    summary = sentiment_data.get("summary", "No summary")
    key_factor = sentiment_data.get("key_factor", "Unknown")
    risk = sentiment_data.get("risk", "Unknown")

    if score >= 3:
        emoji = "\U0001f7e2"
    elif score >= 0:
        emoji = "\U0001f7e1"
    else:
        emoji = "\U0001f534"

    return (
        f"{emoji} Sentiment: {score:+.1f}/10\n"
        f"   {summary}\n"
        f"   Driver: {key_factor}\n"
        f"   Risk: {risk}"
    )


# === Quick test ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Test with BTC
    result = get_sentiment("BTC", "crypto")
    if result:
        print(f"\nBTC Sentiment: {result['score']:+.1f}")
        print(f"Summary: {result.get('summary', 'N/A')}")
        print(f"Key factor: {result.get('key_factor', 'N/A')}")
        print(f"Risk: {result.get('risk', 'N/A')}")
        print(f"\nTelegram format:\n{format_sentiment_telegram(result)}")
    else:
        print("Failed to get sentiment (check PERPLEXITY_API_KEY)")
