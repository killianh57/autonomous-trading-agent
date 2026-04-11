"""
alpha_signals.py
Coordinateur alpha : combine CryptoPanic + Coinglass + NewsAPI.
Genere un signal unifie LONG/SHORT/HOLD avec score de conviction.

Usage dans alpaca_bot.py :
    from alpha_signals import get_alpha_signal
    signal = get_alpha_signal("ETH")
    if signal["conviction"] >= 70 and signal["action"] == "LONG":
        # executer l'ordre
"""

from cryptopanic_monitor import get_crypto_sentiment, should_pause_on_news
from binance_futures_monitor import get_squeeze_score
from typing import Optional

SUPPORTED_CRYPTO = ["BTC", "ETH", "SOL", "IBIT"]

SENTIMENT_WEIGHT = 0.4
SQUEEZE_WEIGHT = 0.6

CONVICTION_THRESHOLDS = {
    "strong": 75,
    "moderate": 50,
    "weak": 25,
}


def get_alpha_signal(ticker: str) -> dict:
    """
    Signal alpha unifie pour un ticker crypto.

    Retourne:
        action    : LONG | SHORT | HOLD
        conviction: 0-100
        reason    : explication lisible
        risk_flag : True si evenement a risque detecte
        raw       : donnees brutes
    """
    if ticker not in SUPPORTED_CRYPTO:
        return {
            "ticker": ticker,
            "action": "HOLD",
            "conviction": 0,
            "reason": f"{ticker} not supported for alpha signals",
            "risk_flag": False,
            "raw": {},
        }

    currency = "BTC" if ticker == "IBIT" else ticker

    pause, pause_reason = should_pause_on_news(ticker)
    if pause:
        return {
            "ticker": ticker,
            "action": "HOLD",
            "conviction": 0,
            "reason": f"RISK PAUSE: {pause_reason}",
            "risk_flag": True,
            "raw": {},
        }

    sentiment_data = get_crypto_sentiment(ticker, limit=10)
    squeeze_data = get_squeeze_score(currency)

    sentiment_map = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}
    direction_map = {"LONG": 1.0, "NEUTRAL": 0.0, "SHORT": -1.0}

    sentiment_val = sentiment_map.get(sentiment_data.get("sentiment", "NEUTRAL"), 0.0)
    squeeze_val = direction_map.get(squeeze_data.get("direction", "NEUTRAL"), 0.0)

    if "error" in squeeze_data:
        combined = sentiment_val
        weights_note = "sentiment only (coinglass unavailable)"
    else:
        combined = (sentiment_val * SENTIMENT_WEIGHT) + (squeeze_val * SQUEEZE_WEIGHT)
        weights_note = f"sentiment={sentiment_val:.1f}*{SENTIMENT_WEIGHT} + squeeze={squeeze_val:.1f}*{SQUEEZE_WEIGHT}"

    raw_conviction = abs(combined) * 100

    bull_total = sentiment_data.get("bull_total", 0)
    bear_total = sentiment_data.get("bear_total", 0)
    articles = sentiment_data.get("articles_count", 0)
    if articles < 3:
        raw_conviction *= 0.7

    conviction = round(min(raw_conviction, 100), 1)

    if combined > 0.1 and conviction >= CONVICTION_THRESHOLDS["moderate"]:
        action = "LONG"
    elif combined < -0.1 and conviction >= CONVICTION_THRESHOLDS["moderate"]:
        action = "SHORT"
    else:
        action = "HOLD"

    funding_rate = squeeze_data.get("avg_funding_rate", 0.0)
    squeeze_score = squeeze_data.get("squeeze_score", 0)
    funding_signal = squeeze_data.get("funding_signal", "N/A")

    reason_parts = [
        f"News: {sentiment_data.get('sentiment')} (bull={bull_total}, bear={bear_total}, n={articles})",
        f"Funding: {funding_rate:.4f}% ({funding_signal})",
        f"Squeeze score: {squeeze_score}",
        f"Combined ({weights_note}): {combined:.3f}",
    ]
    reason = " | ".join(reason_parts)

    return {
        "ticker": ticker,
        "action": action,
        "conviction": conviction,
        "reason": reason,
        "risk_flag": sentiment_data.get("risk_detected", False),
        "raw": {
            "sentiment": sentiment_data,
            "squeeze": squeeze_data,
        },
    }


def get_alpha_signals_batch(tickers: list) -> dict:
    """
    Signaux alpha pour plusieurs tickers.
    Retourne dict ticker -> signal.
    """
    results = {}
    for ticker in tickers:
        if ticker in SUPPORTED_CRYPTO:
            results[ticker] = get_alpha_signal(ticker)
    return results


def format_signal_for_telegram(signal: dict) -> str:
    """
    Formate un signal pour notification Telegram.
    """
    ticker = signal.get("ticker", "?")
    action = signal.get("action", "HOLD")
    conviction = signal.get("conviction", 0)
    reason = signal.get("reason", "")
    risk = signal.get("risk_flag", False)

    emoji_action = {"LONG": "LONG", "SHORT": "SHORT", "HOLD": "HOLD"}
    emoji_conviction = "HIGH" if conviction >= 75 else ("MED" if conviction >= 50 else "LOW")

    risk_note = " [RISK DETECTED]" if risk else ""

    lines = [
        f"ALPHA SIGNAL: {ticker}{risk_note}",
        f"Action: {emoji_action.get(action, action)}",
        f"Conviction: {conviction}/100 ({emoji_conviction})",
        f"Reason: {reason[:200]}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print("=== Alpha Signals Test ===")
    for asset in ["ETH", "BTC", "SOL"]:
        sig = get_alpha_signal(asset)
        print(f"\n--- {asset} ---")
        print(f"Action    : {sig['action']}")
        print(f"Conviction: {sig['conviction']}/100")
        print(f"Risk flag : {sig['risk_flag']}")
        print(f"Reason    : {sig['reason']}")
        print(format_signal_for_telegram(sig))
