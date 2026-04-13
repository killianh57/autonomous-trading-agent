"""
sonar_sentiment.py - Perplexity Sonar API sentiment layer
Shared module for Bot 1 (Alpaca) and Bot 2 (Coinbase).
Queries real-time news sentiment before each trade decision.

Env var required: PERPLEXITY_API_KEY
Cost: ~$5/1000 requests (sonar) or ~$1/M tokens
"""

import os
import time
import json
import logging
import requests

log = logging.getLogger("sonar")

PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
SONAR_URL = "https://api.perplexity.ai/chat/completions"
SONAR_MODEL = "sonar"  # cheapest, $1/M tokens. Use "sonar-pro" for deeper analysis

# Cache: avoid burning queries on same asset within 15 min
_cache = {}  # {asset: {"score": int, "summary": str, "ts": float}}
CACHE_TTL = 900  # 15 minutes


def get_sonar_sentiment(asset: str, asset_type: str = "crypto") -> dict:
    """
    Query Perplexity Sonar for real-time news sentiment on an asset.

    Args:
        asset: ticker symbol (e.g. "BTC", "NVDA", "QQQ")
        asset_type: "crypto" or "stock" (adjusts prompt)

    Returns:
        {"score": -100 to +100, "summary": str, "cached": bool}
        score > 0 = bullish news, score < 0 = bearish news
        On error returns {"score": 0, "summary": "error: ...", "cached": False}
    """
    if not PERPLEXITY_API_KEY:
        return {"score": 0, "summary": "no PERPLEXITY_API_KEY", "cached": False}

    # Check cache
    now = time.time()
    cached = _cache.get(asset)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return {"score": cached["score"], "summary": cached["summary"], "cached": True}

    # Build prompt based on asset type
    if asset_type == "crypto":
        prompt = (
            "You are a crypto market analyst. "
            "What is the current short-term sentiment for {} in the last 24 hours? "
            "Consider: regulation news, whale movements, exchange issues, "
            "macro events (Fed, inflation), partnerships, hacks, listings. "
            "Respond ONLY with valid JSON, no markdown: "
            '{{\"score\": <integer -100 to 100>, \"summary\": \"<one sentence>\"}}'
            " where score > 0 = bullish, < 0 = bearish, 0 = neutral."
        ).format(asset)
    else:
        prompt = (
            "You are a stock market analyst. "
            "What is the current short-term sentiment for {} in the last 24 hours? "
            "Consider: earnings, analyst upgrades/downgrades, sector rotation, "
            "macro events (Fed, tariffs, geopolitics), insider trades, news catalysts. "
            "Respond ONLY with valid JSON, no markdown: "
            '{{\"score\": <integer -100 to 100>, \"summary\": \"<one sentence>\"}}'
            " where score > 0 = bullish, < 0 = bearish, 0 = neutral."
        ).format(asset)

    try:
        resp = requests.post(
            SONAR_URL,
            headers={
                "Authorization": "Bearer " + PERPLEXITY_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": SONAR_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # Parse JSON from response (handle markdown fences if present)
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = json.loads(clean)
        score = max(-100, min(100, int(parsed.get("score", 0))))
        summary = str(parsed.get("summary", ""))[:200]

        # Cache result
        _cache[asset] = {"score": score, "summary": summary, "ts": now}
        log.info("Sonar %s: score=%d summary=%s", asset, score, summary[:80])

        return {"score": score, "summary": summary, "cached": False}

    except requests.exceptions.Timeout:
        log.warning("Sonar timeout for %s", asset)
        return {"score": 0, "summary": "timeout", "cached": False}
    except json.JSONDecodeError as e:
        log.warning("Sonar JSON parse error for %s: %s / raw: %s", asset, e, content[:100])
        return {"score": 0, "summary": "parse_error", "cached": False}
    except Exception as e:
        log.warning("Sonar error for %s: %s", asset, e)
        return {"score": 0, "summary": "error: " + str(e)[:100], "cached": False}


def sonar_signal_modifier(score: int, sonar: dict) -> int:
    """
    Adjust confidence score based on Sonar news sentiment.
    Strong sentiment (|score| >= 50) = +/- 20 confidence
    Moderate sentiment (|score| >= 25) = +/- 10 confidence
    Weak/neutral = no change
    """
    s = sonar.get("score", 0)
    if abs(s) < 25:
        return score  # Neutral news = no adjustment

    if score > 0:  # LONG signal
        if s >= 50:
            log.info("Sonar strong bullish +20: %s", sonar.get("summary", "")[:60])
            return score + 20
        elif s >= 25:
            log.info("Sonar moderate bullish +10: %s", sonar.get("summary", "")[:60])
            return score + 10
        elif s <= -50:
            log.info("Sonar strong bearish -20 (blocks LONG): %s", sonar.get("summary", "")[:60])
            return score - 20
        elif s <= -25:
            log.info("Sonar moderate bearish -10: %s", sonar.get("summary", "")[:60])
            return score - 10
    elif score < 0:  # SHORT signal (bear)
        if s <= -50:
            log.info("Sonar confirms bearish +20: %s", sonar.get("summary", "")[:60])
            return score - 20  # more negative = stronger SHORT
        elif s <= -25:
            return score - 10
        elif s >= 50:
            log.info("Sonar bullish contradicts SHORT -20: %s", sonar.get("summary", "")[:60])
            return score + 20  # weakens SHORT
        elif s >= 25:
            return score + 10

    return score
