"""
agent.py - Alpaca Trading Bot
Exchange  : Alpaca Markets
HOLD      : AAPL, MSFT, GOOGL  (DCA mensuel, jamais vendre)
DAYTRADE  : SPY, QQQ            (signaux EMA/RSI, RR >= 2.0)
Capital   : paper ou live USD
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import schedule
from dotenv import load_dotenv
from flask import Flask
from alpha_signals import get_alpha_signal

# ---------------------------------------------------------------------------
# RETRY LOGIC - exponential backoff sur erreurs API transitoires
# ---------------------------------------------------------------------------
import time as _time_module

def _with_retry(fn, retries=4, base_delay=1.0, label=""):
    """Retry avec exponential backoff. 429/5xx = retente. 4xx autre = fail direct."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as _e:
            _msg = str(_e)
            _is_transient = any(c in _msg for c in ["429","500","502","503","504","timeout","Timeout","ConnectionError","RemoteDisconnected"])
            if not _is_transient or attempt == retries - 1:
                raise
            _delay = base_delay * (2 ** attempt)
            log.warning("Retry %s (%d/%d) apres %.1fs - %s", label, attempt+1, retries-1, _delay, _msg[:80])
            _time_module.sleep(_delay)
    raise RuntimeError("Max retries reached: " + label)



load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = "claude-haiku-4-5-20251001"



# ---------------------------------------------------------------------------
# FEAR & GREED INDEX - sentiment macro crypto (alternative.me)
# ---------------------------------------------------------------------------
_fg_cache = {"value": None, "label": None, "ts": 0}

def get_fear_greed() -> dict:
    """Retourne Fear & Greed Index. Cache 1h. 0=extreme fear, 100=extreme greed."""
    global _fg_cache
    now = _time_module.time()
    if _fg_cache["value"] is not None and now - _fg_cache["ts"] < 3600:
        return _fg_cache
    try:
        def _fetch():
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
            return r.json()["data"][0]
        data = _with_retry(_fetch, label="FearGreed")
        _fg_cache = {
            "value": int(data["value"]),
            "label": data["value_classification"],
            "ts": now
        }
        log.info("Fear&Greed: %d (%s)", _fg_cache["value"], _fg_cache["label"])
    except Exception as e:
        log.warning("Fear&Greed fetch error: %s", e)
        if _fg_cache["value"] is None:
            _fg_cache = {"value": 50, "label": "Neutral", "ts": now}
    return _fg_cache

def fg_signal_modifier(score: int, fg: dict) -> int:
    """Ajuste le score de confiance selon le sentiment macro.
    Extreme Fear (<20) : +15 (opportunite d achat contrariante)
    Extreme Greed (>80): -15 (risque de retournement)
    """
    val = fg.get("value", 50)
    if val < 20:
        log.info("Extreme Fear %d : bonus +15 signal", val)
        return score + 15
    if val > 80:
        log.info("Extreme Greed %d : malus -15 signal", val)
        return score - 15
    return score


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "10001"))

TRADE_BASE_URL = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"
DATA_BASE_URL  = "https://data.alpaca.markets"

HOLD_ASSETS  = ["VT", "SCHD", "VNQ"]
TRADE_ASSETS = ["QQQ", "IBIT"]
ALL_ASSETS   = HOLD_ASSETS + TRADE_ASSETS

CORE_SYMBOLS = {"VT", "SCHD", "VNQ"}  # Jamais vendre ces positions

HOLD_ALLOCATION = {
    "VT":   0.40,
    "SCHD": 0.15,
    "VNQ":  0.05,
}

HOLD_STOP_LOSS_PCT = -0.30
MIN_CAPITAL_USD    = 10.0
MAX_TRADE_PCT      = 0.10
CONFIDENCE_MIN     = 60
RR_MIN             = 2.0
DAILY_LOSS_LIMIT   = -0.05

TRADE_LOG_FILE = "alpaca_trades.json"
STATE_FILE     = "alpaca_state.json"

# ---------------------------------------------------------------------------
# STATE GLOBAL
# ---------------------------------------------------------------------------
_state = {
    "paused": False,
    "daily_start_value": 0.0,
    "daily_loss_alerted": False,
}

def load_state() -> None:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            _state.update(saved)
        except Exception as e:
            log.error("State load error: %s", e)

def save_state() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f, indent=2, default=str)
    except Exception as e:
        log.error("State save error: %s", e)

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configure (token=%s chat=%s)", bool(TELEGRAM_TOKEN), bool(TELEGRAM_CHAT_ID))
        return
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            log.error("Telegram erreur %d: %s", resp.status_code, resp.text[:300])
    except Exception as e:
        log.error("Telegram error: %s", e)

# ---------------------------------------------------------------------------
# ALPACA API
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }

def alpaca_get(path: str, base: str = None) -> Optional[dict]:
    url = (base or TRADE_BASE_URL) + path
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        log.error("ALPACA GET %s -> %d %s", path, resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.error("ALPACA GET error %s: %s", path, e)
        return None

def alpaca_post(path: str, payload: dict) -> Optional[dict]:
    url = TRADE_BASE_URL + path
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        log.error("ALPACA POST %s -> %d %s", path, resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.error("ALPACA POST error %s: %s", path, e)
        return None

# ---------------------------------------------------------------------------
# ACCOUNT & BALANCES
# ---------------------------------------------------------------------------
def get_account() -> dict:
    return alpaca_get("/v2/account") or {}

def get_cash_balance() -> float:
    return float(get_account().get("cash", 0.0))

def get_portfolio_value() -> float:
    return float(get_account().get("portfolio_value", 0.0))

def get_positions() -> dict:
    """Retourne {symbol: {qty, market_value, avg_cost, unrealized_pnl_pct}}."""
    data = alpaca_get("/v2/positions")
    if not isinstance(data, list):
        return {}
    result = {}
    for pos in data:
        symbol = pos.get("symbol", "")
        result[symbol] = {
            "qty":               float(pos.get("qty", 0)),
            "market_value":      float(pos.get("market_value", 0)),
            "avg_cost":          float(pos.get("avg_entry_price", 0)),
            "unrealized_pnl_pct": float(pos.get("unrealized_plpc", 0)) * 100,
        }
    return result

def has_position(symbol: str) -> bool:
    return get_positions().get(symbol, {}).get("qty", 0) > 0

def get_position_qty(symbol: str) -> float:
    return get_positions().get(symbol, {}).get("qty", 0.0)

def is_hold_asset(symbol: str) -> bool:
    return symbol in HOLD_ASSETS

def is_market_open() -> bool:
    data = alpaca_get("/v2/clock")
    return bool(data and data.get("is_open"))

# ---------------------------------------------------------------------------
# MARKET DATA & PRICES
# ---------------------------------------------------------------------------
def get_prices(symbols: list) -> dict:
    prices = {}
    for sym in symbols:
        data = alpaca_get("/v2/stocks/{}/quotes/latest".format(sym), base=DATA_BASE_URL)
        if not data:
            continue
        quote = data.get("quote", {})
        bid = float(quote.get("bp", 0))
        ask = float(quote.get("ap", 0))
        if bid > 0 and ask > 0:
            prices[sym] = (bid + ask) / 2.0
        elif ask > 0:
            prices[sym] = ask
    return prices

def spread_ok(symbol: str) -> bool:
    data = alpaca_get("/v2/stocks/{}/quotes/latest".format(symbol), base=DATA_BASE_URL)
    if not data:
        return False
    quote = data.get("quote", {})
    bid = float(quote.get("bp", 0))
    ask = float(quote.get("ap", 0))
    if bid <= 0 or ask <= 0:
        return False
    spread_pct = (ask - bid) / bid
    if spread_pct > 0.005:
        log.info("Spread %s trop large: %.3f%%", symbol, spread_pct * 100)
        return False
    return True

# ---------------------------------------------------------------------------
# CANDLES & INDICATEURS
# ---------------------------------------------------------------------------
def get_candles(symbol: str, timeframe: str = "1Hour", limit: int = 60) -> list:
    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=limit * 3 if "Hour" in timeframe else limit * 60)
    path  = "/v2/stocks/{}/bars?timeframe={}&start={}&limit={}&sort=asc".format(
        symbol, timeframe,
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        limit,
    )
    data = alpaca_get(path, base=DATA_BASE_URL)
    if not data:
        return []
    candles = []
    for bar in data.get("bars", []):
        try:
            candles.append({
                "open":   float(bar["o"]),
                "high":   float(bar["h"]),
                "low":    float(bar["l"]),
                "close":  float(bar["c"]),
                "volume": float(bar["v"]),
            })
        except (KeyError, ValueError):
            continue
    return candles

def calc_ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1.0 - k))
    return result

def calc_rsi(values: list, period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calc_atr(candles: list, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

# ---------------------------------------------------------------------------
# ANALYSE & SIGNAL
# ---------------------------------------------------------------------------
def analyze(symbol: str) -> dict:
    result = {
        "symbol":     symbol,
        "direction":  "HOLD",
        "confidence": 0,
        "entry":      0.0,
        "sl":         0.0,
        "tp":         0.0,
        "reasons":    [],
    }

    candles = get_candles(symbol, timeframe="1Hour", limit=60)
    if len(candles) < 22:
        result["reasons"].append("not enough candles: {}".format(len(candles)))
        return result

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    current = closes[-1]
    result["entry"] = current

    score   = 0
    reasons = []

    # EMA 9 / 21
    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    if len(ema9) >= 2 and len(ema21) >= 2:
        e9, e9p   = ema9[-1],  ema9[-2]
        e21, e21p = ema21[-1], ema21[-2]
        if e9 > e21 and current > e9:
            reasons.append("EMA bullish"); score += 25
        elif e9 < e21 and current < e9:
            reasons.append("EMA bearish"); score -= 25
        if e9p < e21p and e9 > e21:
            reasons.append("golden cross"); score += 20
        elif e9p > e21p and e9 < e21:
            reasons.append("death cross"); score -= 20

    # RSI 1H
    rsi_val = calc_rsi(closes, 14)
    if rsi_val < 35:
        reasons.append("RSI oversold {:.0f}".format(rsi_val)); score += 15
    elif rsi_val > 65:
        reasons.append("RSI overbought {:.0f}".format(rsi_val)); score -= 15

    # Volume spike
    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1.0
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    if vol_ratio > 1.5:
        bonus = 15 if score >= 0 else -15
        reasons.append("vol x{:.1f}".format(vol_ratio)); score += bonus

    # Structure prix (10 bougies)
    highs10 = [c["high"] for c in candles[-10:]]
    if highs10[-1] > highs10[0] and closes[-1] > closes[-5]:
        reasons.append("HH structure"); score += 15
    elif highs10[-1] < highs10[0] and closes[-1] < closes[-5]:
        reasons.append("LL structure"); score -= 15

    # SL/TP via ATR (SL = 2x ATR, TP = 4x ATR -> RR 2.0)
    atr = calc_atr(candles, 14)
    if atr > 0:
        result["sl"] = round(current - 2.0 * atr, 4)
        result["tp"] = round(current + 4.0 * atr, 4)

    result["reasons"] = reasons

    bull = sum(1 for r in reasons if any(w in r for w in ["bullish", "oversold", "golden", "HH"]))
    bear = sum(1 for r in reasons if any(w in r for w in ["bearish", "overbought", "death", "LL"]))
    abs_score = abs(score)

    if bull >= 3 and score >= CONFIDENCE_MIN:
        result["direction"]  = "LONG"
        result["confidence"] = min(score, 100)
    elif bear >= 3 and abs_score >= CONFIDENCE_MIN:
        result["direction"]  = "SHORT"
        result["confidence"] = min(abs_score, 100)
    else:
        result["confidence"] = min(abs_score, 100)

    # Alpha signal boost for crypto-backed assets (IBIT)
    if symbol in ("IBIT",):
        try:
            alpha = get_alpha_signal(symbol)
            if alpha.get("risk_flag"):
                result["direction"]  = "HOLD"
                result["confidence"] = 0
                result["reasons"].append("alpha_risk: " + alpha.get("reason", ""))
            elif alpha.get("action") == result["direction"] and alpha.get("conviction", 0) >= 60:
                boost = int(alpha["conviction"] * 0.2)
                result["confidence"] = min(result["confidence"] + boost, 100)
                result["reasons"].append("alpha_boost+" + str(boost))
        except Exception as e:
            log.warning("alpha_signal error for %s: %s", symbol, e)


    # Fear & Greed modifier
    fg = get_fear_greed()
    if result["direction"] in ("LONG", "SHORT"):
        result["confidence"] = fg_signal_modifier(result["confidence"], fg)
        result["reasons"].append("F&G:{}/{}".format(fg.get("value","?"), fg.get("label","?")))

    # Claude pre-trade validation
    if result["direction"] in ("LONG", "SHORT") and result["confidence"] >= CONFIDENCE_MIN:
        try:
            def _ask_claude():
                prompt = (
                    "Tu es un trader pro. Valide ce signal Alpaca:\n"
                    "Symbol: {}  Direction: {}  Confidence: {}\n"
                    "Raisons: {}\n"
                    "Prix: {:.2f}  SL: {:.2f}  TP: {:.2f}\n"
                    "Fear&Greed: {} ({})\n\n"
                    "Reponds JSON uniquement: {{\"valid\": true/false, \"confidence_adj\": +/-N, \"reason\": \"...\"}}"
                ).format(
                    symbol, result["direction"], result["confidence"],
                    ", ".join(result["reasons"][-5:]),
                    result["entry"], result["sl"], result["tp"],
                    fg.get("value","?"), fg.get("label","?")
                )
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": ANTHROPIC_MODEL, "max_tokens": 150, "messages": [{"role": "user", "content": prompt}]},
                    timeout=12
                )
                return r.json()["content"][0]["text"]
            import json as _json
            raw = _with_retry(_ask_claude, retries=2, label="Claude/Alpaca")
            cleaned = raw.strip().strip("```json").strip("```").strip()
            verdict = _json.loads(cleaned)
            if not verdict.get("valid", True):
                log.info("Claude invalide signal %s: %s", symbol, verdict.get("reason",""))
                result["direction"]  = "HOLD"
                result["confidence"] = 0
                result["reasons"].append("claude_veto: " + verdict.get("reason","")[:60])
            else:
                adj = int(verdict.get("confidence_adj", 0))
                result["confidence"] = max(0, min(100, result["confidence"] + adj))
                if adj != 0:
                    result["reasons"].append("claude_adj:{:+d}".format(adj))
        except Exception as _ce:
            log.warning("Claude analysis error %s: %s", symbol, _ce)

    return result

# ---------------------------------------------------------------------------
# GARDE-FOUS
# ---------------------------------------------------------------------------
def has_enough_capital() -> bool:
    cash = get_cash_balance()
    if cash < MIN_CAPITAL_USD:
        log.info("Capital insuffisant: USD %.2f < %.2f", cash, MIN_CAPITAL_USD)
        return False
    return True

def check_daily_loss(portfolio_value: float) -> bool:
    start = _state.get("daily_start_value", 0.0)
    if start <= 0:
        return False
    loss_pct = (portfolio_value - start) / start
    if loss_pct <= DAILY_LOSS_LIMIT and not _state.get("daily_loss_alerted"):
        _state["paused"] = True
        _state["daily_loss_alerted"] = True
        save_state()
        msg = "Daily loss limit atteinte: {:.1f}% - Trading pause".format(loss_pct * 100)
        log.warning(msg)
        send_telegram(msg)
        return True
    return _state.get("paused", False)

# ---------------------------------------------------------------------------
# ORDRES
# ---------------------------------------------------------------------------
def place_market_buy(symbol: str, notional_usd: float) -> Optional[dict]:
    if _state.get("paused"):
        log.info("Bot en pause - BUY bloque: %s", symbol)
        return None
    if not has_enough_capital():
        return None
    cash = get_cash_balance()
    if notional_usd > cash:
        notional_usd = cash * 0.95
        if notional_usd < 1.0:
            return None
    payload = {
        "symbol":        symbol,
        "notional":      round(notional_usd, 2),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    if DRY_RUN:
        log.info("DRY_RUN: skip BUY %s USD %.2f", symbol, notional_usd)
        return {"dry_run": True}
    result = alpaca_post("/v2/orders", payload)
    if result:
        log.info("BUY %s USD %.2f - success (id=%s)", symbol, notional_usd, result.get("id", ""))
    return result

def place_market_sell_full(symbol: str) -> Optional[dict]:
    if symbol in CORE_SYMBOLS:
        log.warning("SELL bloque: %s est un asset CORE protege", symbol)
        return None
    qty = get_position_qty(symbol)
    if qty <= 0:
        log.info("Pas de position a vendre: %s", symbol)
        return None
    payload = {
        "symbol":        symbol,
        "qty":           "{:.4f}".format(qty),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    if DRY_RUN:
        log.info("DRY_RUN: skip SELL %s qty %.4f", symbol, qty)
        return {"dry_run": True}
    result = alpaca_post("/v2/orders", payload)
    if result:
        log.info("SELL %s qty %.4f - success", symbol, qty)
    return result

def place_market_sell_partial(symbol: str, qty: float) -> Optional[dict]:
    if symbol in CORE_SYMBOLS:
        log.warning("SELL PARTIAL bloque: %s est un asset CORE protege", symbol)
        return None
    current_qty = get_position_qty(symbol)
    qty = min(qty, current_qty)
    if qty <= 0:
        return None
    payload = {
        "symbol":        symbol,
        "qty":           "{:.4f}".format(qty),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    if DRY_RUN:
        log.info("DRY_RUN: skip SELL PARTIAL %s qty %.4f", symbol, qty)
        return {"dry_run": True}
    result = alpaca_post("/v2/orders", payload)
    if result:
        log.info("SELL PARTIAL %s qty %.4f", symbol, qty)
    return result

# ---------------------------------------------------------------------------
# STARTUP AUDIT
# ---------------------------------------------------------------------------
def startup_audit() -> None:
    log.info("Startup audit...")
    acc       = get_account()
    positions = get_positions()
    prices    = get_prices(ALL_ASSETS)

    cash  = float(acc.get("cash", 0.0))
    total = float(acc.get("portfolio_value", cash))

    lines = ["Alpaca Bot - Startup Audit:"]
    lines.append("Mode: {}".format("PAPER" if ALPACA_PAPER else "LIVE"))
    lines.append("USD cash: {:.2f}".format(cash))
    lines.append("Portfolio total: {:.2f} USD".format(total))

    for sym in ALL_ASSETS:
        pos = positions.get(sym)
        if pos and pos["qty"] > 0:
            lines.append("{}: qty={:.4f} val={:.2f} USD pnl={:.1f}%".format(
                sym, pos["qty"], pos["market_value"], pos["unrealized_pnl_pct"]
            ))
            if is_hold_asset(sym):
                lines.append("  -> HOLD asset, monitored")

    if total < MIN_CAPITAL_USD:
        lines.append("ATTENTION: capital < {:.0f} USD - mode lecture seule".format(MIN_CAPITAL_USD))

    _state["daily_start_value"] = total
    _state["daily_loss_alerted"] = False
    save_state()

    msg = "\n".join(lines)
    log.info(msg)
    send_telegram(msg)

# ---------------------------------------------------------------------------
# SCAN DAYTRADE
# ---------------------------------------------------------------------------
def run_trade_scan() -> None:
    if _state.get("paused"):
        return
    if not is_market_open():
        log.info("Marche ferme - scan skip")
        return

    portfolio_value = get_portfolio_value()
    if check_daily_loss(portfolio_value):
        return
    if portfolio_value < MIN_CAPITAL_USD:
        return

    cash = get_cash_balance()

    for sym in TRADE_ASSETS:
        if has_position(sym):
            _check_exit(sym)
            continue

        signal = analyze(sym)
        log.info("Signal %s: %s conf=%d reasons=%s",
                 sym, signal["direction"], signal["confidence"], signal["reasons"])

        if signal["direction"] != "LONG":
            continue
        if signal["confidence"] < CONFIDENCE_MIN:
            continue

        entry = signal["entry"]
        sl    = signal["sl"]
        tp    = signal["tp"]

        if sl <= 0 or tp <= 0 or sl >= entry:
            log.info("SL/TP invalide pour %s, skip", sym)
            continue

        rr = (tp - entry) / (entry - sl)
        if rr < RR_MIN:
            log.info("RR %.2f < %.1f pour %s, skip", rr, RR_MIN, sym)
            continue

        trade_usd = min(portfolio_value * MAX_TRADE_PCT, cash * 0.90)
        if trade_usd < 1.0:
            log.info("Pas assez de capital USD pour trader %s", sym)
            continue

        log.info("TRADE: BUY %s USD=%.2f conf=%d RR=%.1f", sym, trade_usd, signal["confidence"], rr)
        order = place_market_buy(sym, trade_usd)

        if order:
            trade = {
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "symbol":     sym,
                "direction":  "LONG",
                "trade_usd":  trade_usd,
                "entry":      entry,
                "sl":         sl,
                "tp":         tp,
                "confidence": signal["confidence"],
                "rr":         round(rr, 2),
                "reasons":    signal["reasons"],
                "status":     "open",
            }
            _log_trade(trade)
            msg = (
                "Trade ouvert: {}\n"
                "BUY USD {:.2f} @ ~{:.2f}\n"
                "SL: {:.2f} | TP: {:.2f}\n"
                "RR: {:.1f}:1 | Conf: {}%\n"
                "Raisons: {}"
            ).format(
                sym, trade_usd, entry,
                sl, tp, rr,
                signal["confidence"],
                ", ".join(signal["reasons"])
            )
            send_telegram(msg)

def _check_exit(symbol: str) -> None:
    trade = _get_open_trade(symbol)
    if not trade:
        return
    prices  = get_prices([symbol])
    current = prices.get(symbol, 0.0)
    if current <= 0:
        return

    sl = trade.get("sl", 0.0)
    tp = trade.get("tp", 0.0)

    if sl > 0 and current <= sl:
        log.info("SL atteint %s: %.4f <= %.4f", symbol, current, sl)
        order = place_market_sell_full(symbol), print(MON_VAR_INEXISTANTE)
        if order:
            trade["status"]  = "closed_sl"
            trade["exit"]    = current
            trade["exit_ts"] = datetime.now(timezone.utc).isoformat()
            _update_trade(trade)
            pnl_pct = (current - trade["entry"]) / trade["entry"] * 100
            send_telegram("SL touche: {}\nExit: {:.2f} | PnL: {:.1f}%".format(symbol, current, pnl_pct))

    elif tp > 0 and current >= tp:
        log.info("TP atteint %s: %.4f >= %.4f", symbol, current, tp)
        qty   = get_position_qty(symbol)
        order = place_market_sell_partial(symbol, qty * 0.5)
        if order:
            trade["status"]  = "partial_tp"
            trade["exit_ts"] = datetime.now(timezone.utc).isoformat()
            _update_trade(trade)
            pnl_pct = (current - trade["entry"]) / trade["entry"] * 100
            send_telegram("TP partiel (50%): {}\nExit: {:.2f} | PnL: {:.1f}%".format(symbol, current, pnl_pct))

# ---------------------------------------------------------------------------
# HOLD ASSET MONITORING
# ---------------------------------------------------------------------------
def monitor_hold_assets() -> None:
    positions = get_positions()
    for sym in HOLD_ASSETS:
        pos = positions.get(sym)
        if not pos or pos["qty"] <= 0:
            continue
        pnl_pct = pos["unrealized_pnl_pct"]
        if pnl_pct <= HOLD_STOP_LOSS_PCT * 100:
            msg = "STOP CATASTROPHIQUE {}: {:.1f}% - Vente d'urgence".format(sym, pnl_pct)
            log.warning(msg)
            send_telegram(msg)
            place_market_sell_full(sym)

# ---------------------------------------------------------------------------
# TRADE LOGGER
# ---------------------------------------------------------------------------
def _load_trades() -> list:
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    try:
        with open(TRADE_LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def _save_trades(trades: list) -> None:
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2, default=str)
    except Exception as e:
        log.error("Trade log save error: %s", e)

def _log_trade(trade: dict) -> None:
    trades = _load_trades()
    trades.append(trade)
    _save_trades(trades)

def _get_open_trade(symbol: str) -> Optional[dict]:
    trades = _load_trades()
    for t in reversed(trades):
        if t.get("symbol") == symbol and t.get("status") == "open":
            return t
    return None

def _update_trade(updated: dict) -> None:
    trades = _load_trades()
    for i, t in enumerate(trades):
        if (t.get("symbol") == updated.get("symbol")
                and t.get("timestamp") == updated.get("timestamp")):
            trades[i] = updated
            break
    _save_trades(trades)

# ---------------------------------------------------------------------------
# MORNING BRIEF
# ---------------------------------------------------------------------------
def morning_brief() -> None:
    acc       = get_account()
    positions = get_positions()
    prices    = get_prices(ALL_ASSETS)

    cash  = float(acc.get("cash", 0.0))
    total = float(acc.get("portfolio_value", cash))

    lines = ["Morning Brief - Alpaca\n"]
    lines.append("USD cash: {:.2f}".format(cash))
    lines.append("Portfolio: {:.2f} USD".format(total))

    for sym in ALL_ASSETS:
        price = prices.get(sym, 0.0)
        pos   = positions.get(sym)
        if pos and pos["qty"] > 0:
            lines.append("{}: {:.2f} USD | qty={:.4f} | pnl={:.1f}%".format(
                sym, price, pos["qty"], pos["unrealized_pnl_pct"]
            ))
        else:
            lines.append("{}: {:.2f} USD".format(sym, price))

    start = _state.get("daily_start_value", 0.0)
    if start > 0:
        lines.append("PnL journalier: {:.2f}%".format((total - start) / start * 100))

    _state["daily_start_value"] = total
    _state["daily_loss_alerted"] = False
    save_state()

    send_telegram("\n".join(lines))

# ---------------------------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------------------------
_last_update_id = 0

_telegram_lock = threading.Lock()


def _scan_sante():
    import ast as _ast, re as _re, glob as _glob
    py_files = [f for f in _glob.glob("*.py") if not f.startswith(".")]
    if not py_files:
        return "Aucun fichier .py trouve"
    errors = []
    for fpath in py_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as _f:
                _code = _f.read()
            try:
                _tree = _ast.parse(_code)
            except SyntaxError as _e:
                errors.append("{}: SyntaxError L{}".format(fpath, _e.lineno))
                continue
            bad = sum(1 for c in _code if ord(c) > 127)
            if bad:
                errors.append("{}: {} non-ASCII".format(fpath, bad))
            _defined = set()
            for _n in _ast.walk(_tree):
                if isinstance(_n, _ast.Assign):
                    for _t in _n.targets:
                        if isinstance(_t, _ast.Name): _defined.add(_t.id)
                elif isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    _defined.add(_n.name)
                elif isinstance(_n, _ast.Import):
                    for _a in _n.names: _defined.add(_a.asname or _a.name.split(".")[0])
                elif isinstance(_n, _ast.ImportFrom):
                    for _a in _n.names: _defined.add(_a.asname or _a.name)
            _skip = {"True","False","None","NONE","GET","POST","PUT","DELETE","OK","EOF"}
            _undef = sorted(set(
                _n.id for _n in _ast.walk(_tree)
                if isinstance(_n, _ast.Name) and isinstance(_n.ctx, _ast.Load)
                and _re.match(r"^[A-Z][A-Z_0-9]{2,}$", _n.id)
                and _n.id not in _defined and _n.id not in _skip
            ))
            if _undef:
                errors.append("{}: UNDEF {}".format(fpath, ", ".join(_undef[:3])))
        except Exception as _e:
            errors.append("{}: {}".format(fpath, str(_e)[:60]))
    if errors:
        return "Scan sante - ERREURS:\n" + "\n".join("  - " + e for e in errors)
    return "Scan sante: {} fichier(s) OK".format(len(py_files))


def poll_telegram_commands() -> None:
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return
    # Lock : empeche 2 threads de poller simultanement (bug doublon)
    if not _telegram_lock.acquire(blocking=False):
        log.debug("Telegram poll already running, skipping")
        return
    try:
        url  = "https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_TOKEN)
        resp = requests.get(url, params={"offset": _last_update_id + 1, "timeout": 0}, timeout=15)
        if not resp.ok:
            return
        for update in resp.json().get("result", []):
            uid = update["update_id"]
            if uid <= _last_update_id:
                continue  # doublon detecte, skip
            _last_update_id = uid
            msg     = update.get("message", {})
            text    = msg.get("text", "").strip()
            chat_id = msg.get("chat", {}).get("id")
            if text and chat_id:
                _handle_command(text, chat_id)
    except Exception as e:
        log.error("Telegram poll error: %s", e)
    finally:
        _telegram_lock.release()

def _handle_command(text: str, chat_id) -> None:
    if text == "/aide":
        reply = (
            "Commandes Alpaca:\n"
            "/alpaca_status   - Portfolio complet\n"
            "/alpaca_prix     - Prix live\n"
            "/alpaca_trades   - 5 derniers trades\n"
            "/alpaca_signal   - Analyse SPY+QQQ\n"
            "/alpaca_pause    - Pause trading\n"
            "/alpaca_resume   - Reprendre\n"
            "/alpaca_urgence  - Fermer toutes positions trade\n"
            "/alpaca_test     - Test achat+vente 2 USD SPY\n"
            "/aide            - Aide\n/scan_sante      - Scan sante du code"
        )
    elif text == "/alpaca_status":
        acc       = get_account()
        positions = get_positions()
        prices    = get_prices(ALL_ASSETS)
        cash  = float(acc.get("cash", 0.0))
        total = float(acc.get("portfolio_value", cash))
        lines = ["Portfolio Alpaca:"]
        lines.append("Cash: {:.2f} USD".format(cash))
        lines.append("Total: {:.2f} USD".format(total))
        for sym in ALL_ASSETS:
            pos   = positions.get(sym)
            price = prices.get(sym, 0.0)
            if pos and pos["qty"] > 0:
                lines.append("{}: {:.2f} | qty={:.4f} | pnl={:.1f}%".format(
                    sym, price, pos["qty"], pos["unrealized_pnl_pct"]
                ))
            else:
                lines.append("{}: {:.2f} (pas de position)".format(sym, price))
        reply = "\n".join(lines)
    elif text == "/alpaca_prix":
        prices = get_prices(ALL_ASSETS)
        lines  = ["Prix live:"]
        for sym in ALL_ASSETS:
            lines.append("{}: {:.2f} USD".format(sym, prices.get(sym, 0.0)))
        reply = "\n".join(lines)
    elif text == "/alpaca_trades":
        trades = _load_trades()
        if not trades:
            reply = "Aucun trade enregistre"
        else:
            lines = ["5 derniers trades:"]
            for t in trades[-5:]:
                lines.append("{} {} @ {:.2f} | {} | exit={}".format(
                    t.get("symbol"), t.get("direction"),
                    t.get("entry", 0), t.get("status"), t.get("exit", "-")
                ))
            reply = "\n".join(lines)
    elif text == "/alpaca_signal":
        lines = ["Signaux:"]
        for sym in TRADE_ASSETS:
            sig = analyze(sym)
            lines.append("{}: {} conf={}%\n  {}".format(
                sym, sig["direction"], sig["confidence"],
                ", ".join(sig["reasons"][:3])
            ))
        reply = "\n".join(lines)
    elif text == "/alpaca_pause":
        _state["paused"] = True
        save_state()
        reply = "Trading Alpaca mis en pause"
    elif text == "/alpaca_resume":
        _state["paused"] = False
        _state["daily_loss_alerted"] = False
        save_state()
        reply = "Trading Alpaca repris"
    elif text == "/alpaca_urgence":
        closed = 0
        positions = get_positions()
        for sym in list(positions.keys()):
            if sym not in CORE_SYMBOLS and has_position(sym):
                order = place_market_sell_full(sym)
                if order:
                    closed += 1
        reply = "Urgence: {} positions satellite fermees (CORE proteges)".format(closed)
    elif text == "/alpaca_test":
        reply = "Test trade: achat 2 USD SPY..."
        requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN),
            json={"chat_id": chat_id, "text": reply},
            timeout=10,
        )
        buy = place_market_buy("SPY", 2.0)
        if not buy:
            reply = "Test ECHOUE: achat SPY impossible (voir logs)"
        else:
            time.sleep(3)
            sell = place_market_sell_full("SPY")
            reply = "Test OK: achat + vente SPY executes" if sell else "Test PARTIEL: achat OK, vente echouee"
    elif text == "/scan_sante":
        reply = _scan_sante()

    else:
        return

    try:
        requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN),
            json={"chat_id": chat_id, "text": reply},
            timeout=10,
        )
    except Exception as e:
        log.error("Telegram reply error: %s", e)

# ---------------------------------------------------------------------------
# HEALTH SERVER
# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def health():
    return {"status": "ok", "bot": "alpaca", "paper": ALPACA_PAPER}, 200

def run_health_server() -> None:
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Alpaca Bot V1 demarrage...")
    load_state()

    log.info("ENV CHECK: ALPACA_API_KEY=%s ALPACA_SECRET_KEY=%s",
             bool(ALPACA_API_KEY), bool(ALPACA_SECRET_KEY))
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.error("ALPACA_API_KEY ou ALPACA_SECRET_KEY manquant")
        log.error("Vars dispo: %s", [k for k in os.environ if "ALPACA" in k])
        return

    log.info("Config: KEY=%s... PAPER=%s TELEGRAM=%s CHAT=%s",
             ALPACA_API_KEY[:10], ALPACA_PAPER, bool(TELEGRAM_TOKEN), bool(TELEGRAM_CHAT_ID))

    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    log.info("Health server port %d", PORT)

    startup_audit()

    schedule.every().day.at("09:00").do(morning_brief)
    schedule.every(15).minutes.do(run_trade_scan)
    schedule.every(60).minutes.do(monitor_hold_assets)
    schedule.every(5).seconds.do(poll_telegram_commands)
    log.info("Scheduler configure")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error("Scheduler error: %s", e)
            send_telegram("Alpaca Bot erreur: {}".format(e))
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as _crash_err:
        import traceback as _tb, requests as _req, os as _os
        _err_text = type(_crash_err).__name__ + ": " + str(_crash_err)[:300]
        _trace = _tb.format_exc()[-600:]
        _msg = "CRASH autonomous-trading-agent:\n" + _err_text + "\n\n" + _trace + "\nAuto-fix en cours..."
        try:
            send_telegram(_msg)
        except Exception:
            pass
        try:
            _gh_token = _os.getenv("GITHUB_TOKEN", "")
            if _gh_token:
                _req.post(
                    "https://api.github.com/repos/killianh57/autonomous-trading-agent/actions/workflows/auto-fix.yml/dispatches",
                    headers={"Authorization": "Bearer " + _gh_token, "Accept": "application/vnd.github+json"},
                    json={"ref": "main"},
                    timeout=10
                )
        except Exception:
            pass
        raise

