"""
binance_futures_monitor.py
Funding rates + open interest via Binance Futures API.
100% gratuit, aucune cle API requise.
Remplace coinglass_monitor.py
"""

import requests
from datetime import datetime, timezone


BASE_URL = "https://fapi.binance.com/fapi/v1"

SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "IBIT": "BTCUSDT",
}

FUNDING_THRESHOLDS = {
    "strong_short": -0.003,
    "mild_short": -0.001,
    "neutral_high": 0.001,
    "strong_long": 0.003,
}


def get_funding_rate(ticker: str) -> dict:
    """
    Funding rate actuel pour un ticker.
    Negatif = shorts dominent = squeeze LONG probable.
    """
    symbol = SYMBOL_MAP.get(ticker)
    if not symbol:
        return {"error": f"No Binance mapping for {ticker}", "ticker": ticker}

    try:
        r = requests.get(
            f"{BASE_URL}/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            if data:
                rate = float(data[-1].get("fundingRate", 0))
                return {
                    "ticker": ticker,
                    "symbol": symbol,
                    "funding_rate": rate,
                    "signal": _interpret_funding(rate),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        return {"error": f"HTTP {r.status_code}", "ticker": ticker}
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


def get_open_interest(ticker: str) -> dict:
    """
    Open interest actuel pour un ticker.
    OI croissant + prix croissant = tendance forte.
    OI croissant + prix baissant = short squeeze imminent.
    """
    symbol = SYMBOL_MAP.get(ticker)
    if not symbol:
        return {"error": f"No Binance mapping for {ticker}", "ticker": ticker}

    try:
        r = requests.get(
            f"{BASE_URL}/openInterest",
            params={"symbol": symbol},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            oi = float(data.get("openInterest", 0))
            return {
                "ticker": ticker,
                "symbol": symbol,
                "open_interest": oi,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        return {"error": f"HTTP {r.status_code}", "ticker": ticker}
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


def get_long_short_ratio(ticker: str, period: str = "5m") -> dict:
    """
    Ratio longs/shorts pour un ticker.
    Ratio < 1 = shorts dominent = setup LONG potentiel.
    period: 5m | 15m | 30m | 1h | 2h | 4h | 6h | 12h | 1d
    """
    symbol = SYMBOL_MAP.get(ticker)
    if not symbol:
        return {"error": f"No Binance mapping for {ticker}", "ticker": ticker}

    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": 1},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            if data:
                ratio = float(data[-1].get("longShortRatio", 1.0))
                long_pct = float(data[-1].get("longAccount", 0.5)) * 100
                short_pct = float(data[-1].get("shortAccount", 0.5)) * 100
                return {
                    "ticker": ticker,
                    "symbol": symbol,
                    "long_short_ratio": ratio,
                    "long_pct": round(long_pct, 1),
                    "short_pct": round(short_pct, 1),
                    "sentiment": "BULLISH" if ratio > 1.2 else ("BEARISH" if ratio < 0.8 else "NEUTRAL"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        return {"error": f"HTTP {r.status_code}", "ticker": ticker}
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


def _interpret_funding(rate: float) -> str:
    t = FUNDING_THRESHOLDS
    if rate < t["strong_short"]:
        return "STRONG_LONG_SETUP"
    elif rate < t["mild_short"]:
        return "MILD_LONG_SETUP"
    elif rate <= t["neutral_high"]:
        return "NEUTRAL"
    elif rate < t["strong_long"]:
        return "MILD_SHORT_SETUP"
    else:
        return "STRONG_SHORT_SETUP"


def get_squeeze_score(ticker: str) -> dict:
    """
    Score de squeeze combine : funding + OI + long/short ratio.
    Score positif = squeeze LONG probable.
    Score negatif = squeeze SHORT probable.
    """
    funding_data = get_funding_rate(ticker)
    oi_data = get_open_interest(ticker)
    ls_data = get_long_short_ratio(ticker)

    if "error" in funding_data:
        return {
            "ticker": ticker,
            "squeeze_score": 0,
            "direction": "NEUTRAL",
            "error": funding_data["error"],
        }

    rate = funding_data.get("funding_rate", 0.0)
    signal = funding_data.get("signal", "NEUTRAL")
    ls_ratio = ls_data.get("long_short_ratio", 1.0) if "error" not in ls_data else 1.0

    score_map = {
        "STRONG_LONG_SETUP": 80,
        "MILD_LONG_SETUP": 50,
        "NEUTRAL": 0,
        "MILD_SHORT_SETUP": -50,
        "STRONG_SHORT_SETUP": -80,
    }
    squeeze_score = score_map.get(signal, 0)

    if ls_ratio < 0.8 and squeeze_score >= 0:
        squeeze_score += 10
    elif ls_ratio > 1.2 and squeeze_score <= 0:
        squeeze_score -= 10

    direction = "LONG" if squeeze_score > 0 else ("SHORT" if squeeze_score < 0 else "NEUTRAL")

    return {
        "ticker": ticker,
        "funding_rate": rate,
        "funding_signal": signal,
        "long_short_ratio": ls_ratio,
        "open_interest": oi_data.get("open_interest", 0),
        "squeeze_score": squeeze_score,
        "direction": direction,
        "actionable": abs(squeeze_score) >= 50,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    print("=== Binance Futures Monitor Test ===")
    for asset in ["BTC", "ETH", "SOL"]:
        result = get_squeeze_score(asset)
        print(f"\n{asset}:")
        print(f"  Funding rate : {result.get('funding_rate')}")
        print(f"  Signal       : {result.get('funding_signal')}")
        print(f"  L/S ratio    : {result.get('long_short_ratio')}")
        print(f"  Squeeze score: {result.get('squeeze_score')}")
        print(f"  Direction    : {result.get('direction')}")
        if "error" in result:
            print(f"  Error        : {result['error']}")
