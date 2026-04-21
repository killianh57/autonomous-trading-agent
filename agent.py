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
from sonar_sentiment import get_sonar_sentiment, sonar_signal_modifier
from reddit_sentiment import reddit_sentiment_filter, reddit_ticker_heatmap
from twitter_sentiment import twitter_sentiment_filter

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
# NOTION AUTO-LOG + LINEAR TICKET
# ---------------------------------------------------------------------------
NOTION_TOKEN   = os.getenv("NOTION_TOKEN", "")
NOTION_DB_ID   = os.getenv("NOTION_TRADES_DB_ID", "e9fdd6706eea4fb4894f3f41066bc1c3")
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY", "")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID", "")

def notion_log_trade(trade: dict) -> None:
    """Log un trade ferme dans la base Notion Trades."""
    if not NOTION_TOKEN:
        return
    try:
        symbol   = str(trade.get("symbol") or trade.get("product_id") or trade.get("mint", "?"))
        direction = str(trade.get("direction") or trade.get("side", "?")).upper()
        status   = str(trade.get("status") or trade.get("reason", "?"))
        pnl_raw  = trade.get("pnl") or trade.get("pnl_sol") or trade.get("pnl_pct", 0)
        try:
            pnl = float(pnl_raw)
        except Exception:
            pnl = 0.0
        entry    = float(trade.get("entry") or trade.get("spend_sol") or 0)
        exit_p   = float(trade.get("exit") or trade.get("out_sol") or 0)
        reasons  = trade.get("reasons") or trade.get("rug_reasons") or []
        ts       = trade.get("ts") or trade.get("date") or datetime.now(timezone.utc).isoformat()
        outcome  = "Win" if pnl >= 0 else "Loss"
        props = {
            "Name":      {"title": [{"text": {"content": "{} {} {}".format(symbol, direction, outcome)}}]},
            "Symbol":    {"rich_text": [{"text": {"content": symbol}}]},
            "Direction": {"select": {"name": direction}},
            "Status":    {"select": {"name": status[:100]}},
            "PnL":       {"number": round(pnl, 4)},
            "Entry":     {"number": round(entry, 6)},
            "Exit":      {"number": round(exit_p, 6)},
            "Outcome":   {"select": {"name": outcome}},
            "Reasons":   {"rich_text": [{"text": {"content": ", ".join(str(r) for r in reasons[:10])[:2000]}}]},
            "Date":      {"date": {"start": ts[:19] + "Z" if "T" in str(ts) else datetime.now(timezone.utc).isoformat()[:19] + "Z"}},
        }
        def _notion_req():
            return requests.post(
                "https://api.notion.com/v1/pages",
                headers={"Authorization": "Bearer " + NOTION_TOKEN, "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                json={"parent": {"database_id": NOTION_DB_ID}, "properties": props},
                timeout=10
            )
        _with_retry(_notion_req, retries=2, label="NotionLog")
        log.info("Notion trade logged: %s %s PnL=%.4f", symbol, outcome, pnl)
    except Exception as e:
        log.warning("Notion log error: %s", e)

def linear_create_ticket(title: str, body: str) -> None:
    """Cree un ticket Linear quand erreur non-resolvable detectee."""
    if not LINEAR_API_KEY or not LINEAR_TEAM_ID:
        return
    try:
        query = """
        mutation IssueCreate($title: String!, $teamId: String!, $description: String!) {
          issueCreate(input: {title: $title, teamId: $teamId, description: $description}) {
            issue { id url }
          }
        }"""
        def _linear_req():
            return requests.post(
                "https://api.linear.app/graphql",
                headers={"Authorization": LINEAR_API_KEY, "Content-Type": "application/json"},
                json={"query": query, "variables": {"title": title[:256], "teamId": LINEAR_TEAM_ID, "description": body[:5000]}},
                timeout=10
            )
        resp = _with_retry(_linear_req, retries=2, label="LinearTicket")
        data = resp.json()
        url = data.get("data", {}).get("issueCreate", {}).get("issue", {}).get("url", "")
        log.info("Linear ticket created: %s", url)
        if url:
            send_telegram("Linear ticket: " + url)
    except Exception as e:
        log.warning("Linear ticket error: %s", e)


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
TRADE_ASSETS = ["SPY", "QQQ", "IBIT"]
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
# RLock: re-entrant so save_state() can be called from already-locked sections.
# Protects _state dict + STATE_FILE writes against concurrent access from
# scheduler thread, Flask health server thread, and Telegram polling thread.
_state_lock = threading.RLock()

def load_state() -> None:
    with _state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    saved = json.load(f)
                _state.update(saved)
            except Exception as e:
                log.error("State load error: %s", e)

def save_state() -> None:
    with _state_lock:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(_state, f, indent=2, default=str)
        except Exception as e:
            log.error("State save error: %s", e)

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
# Telegram rate-limit + ban-aware backoff (shared pattern with Bot 3).
# Prevents the 2026-04-21 incident: retrying during a 429 ban RESETS the
# retry_after timer, making the ban effectively permanent.
_tg_lock = threading.Lock()
_tg_banned_until = 0.0  # unix ts; 0 = not banned
_tg_last_sent_ts = 0.0
_TG_MIN_INTERVAL = 1.1
_tg_dropped_during_ban = 0

def send_telegram(msg: str) -> None:
    """Rate-limited, ban-aware Telegram sender. Drops silently during ban."""
    global _tg_banned_until, _tg_last_sent_ts, _tg_dropped_during_ban
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configure (token=%s chat=%s)", bool(TELEGRAM_TOKEN), bool(TELEGRAM_CHAT_ID))
        return
    now = time.time()
    with _tg_lock:
        # Silent drop during ban window
        if now < _tg_banned_until:
            _tg_dropped_during_ban += 1
            return
        wait = _TG_MIN_INTERVAL - (now - _tg_last_sent_ts)
        if wait > 0:
            time.sleep(wait)
        _tg_last_sent_ts = time.time()
    # Truncate to 4000 chars (Telegram hard limit 4096)
    if len(msg) > 4000:
        msg = msg[:3990] + "... [TRUNC]"
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code == 429:
            try:
                data = resp.json()
                retry_after = int(data.get("parameters", {}).get("retry_after", 60))
            except Exception:
                retry_after = 60
            retry_after = min(retry_after, 3600)
            with _tg_lock:
                _tg_banned_until = time.time() + retry_after
                _tg_dropped_during_ban = 0
            log.warning("Telegram 429: banned for %ds, dropping subsequent messages silently", retry_after)
            return
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

# --- Rate-limit + retry helper (Alpaca basic plan: 200 req/min/account) ---
# GET is idempotent -> retry on 429, 5xx, and network errors.
# POST creates orders -> SAFER to only retry on 429 (which guarantees the
# request was NOT processed). 5xx and network errors on POST fail fast
# to avoid duplicate orders (no client_order_id dedup today).
def _alpaca_request(method: str, url: str, payload: Optional[dict] = None,
                    max_retries: int = 3) -> Optional[dict]:
    import random
    is_post = (method == "POST")
    backoff = 1.0
    for attempt in range(max_retries + 1):
        try:
            if method == "GET":
                resp = requests.get(url, headers=_headers(), timeout=15)
            elif method == "POST":
                resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
            elif method == "DELETE":
                resp = requests.delete(url, headers=_headers(), timeout=15)
            else:
                log.error("_alpaca_request: unsupported method %s", method)
                return None

            if resp.status_code in (200, 201, 204):
                try:
                    return resp.json()
                except ValueError:
                    return {}
            # 429: always retry (server says request not processed)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                log.warning("ALPACA 429 rate-limited on %s, sleeping %.1fs (attempt %d/%d)",
                            url[-40:], wait, attempt + 1, max_retries + 1)
                if attempt < max_retries:
                    time.sleep(wait + random.uniform(0, 0.5))
                    backoff *= 2
                    continue
                return None
            # 5xx: retry only for GET (POST could have been processed -> dup risk)
            if 500 <= resp.status_code < 600 and not is_post:
                log.warning("ALPACA %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
                            method, url[-40:], resp.status_code, backoff,
                            attempt + 1, max_retries + 1)
                if attempt < max_retries:
                    time.sleep(backoff + random.uniform(0, 0.3))
                    backoff *= 2
                    continue
                log.error("ALPACA %s %s -> %d %s", method, url[-60:],
                          resp.status_code, resp.text[:200])
                return None
            # 5xx on POST or any 4xx: fail fast
            log.error("ALPACA %s %s -> %d %s", method, url[-60:],
                      resp.status_code, resp.text[:200])
            return None
        except (requests.Timeout, requests.ConnectionError) as e:
            # Network error on POST: fail fast (order could have been placed)
            if is_post:
                log.error("ALPACA POST network error, NOT retrying to avoid duplicate order: %s", e)
                return None
            # GET: safe to retry
            log.warning("ALPACA GET network error: %s, retrying in %.1fs (attempt %d/%d)",
                        e, backoff, attempt + 1, max_retries + 1)
            if attempt < max_retries:
                time.sleep(backoff + random.uniform(0, 0.3))
                backoff *= 2
                continue
            log.error("ALPACA GET giving up after retries: %s", e)
            return None
        except Exception as e:
            log.error("ALPACA %s unexpected error: %s", method, e)
            return None
    return None

def alpaca_get(path: str, base: str = None) -> Optional[dict]:
    url = (base or TRADE_BASE_URL) + path
    return _alpaca_request("GET", url)

def alpaca_post(path: str, payload: dict) -> Optional[dict]:
    url = TRADE_BASE_URL + path
    return _alpaca_request("POST", url, payload=payload)

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
    if score > 0 and any("vol" in r for r in reasons):
        bull += 1
    bear = sum(1 for r in reasons if any(w in r for w in ["bearish", "overbought", "death", "LL"]))
    if score < 0 and any("vol" in r for r in reasons):
        bear += 1
    abs_score = abs(score)

    if bull >= 2 and score >= CONFIDENCE_MIN:
        result["direction"]  = "LONG"
        result["confidence"] = min(score, 100)
    elif bear >= 3 and abs_score >= CONFIDENCE_MIN:
        result["direction"]  = "SHORT"
        result["confidence"] = min(abs_score, 100)
    else:
        result["confidence"] = min(abs_score, 100)
        # Contrarian: Extreme Fear + RSI oversold = opportunite d achat
        fg_val_now = get_fear_greed().get("value", 50)
        if fg_val_now < 25 and rsi_val < 38 and score > 15:
            result["direction"] = "LONG"
            result["confidence"] = min(score + 20, 100)
            result["reasons"].append("contrarian_extreme_fear")

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

    # --- SENTIMENT CONSENSUS (Sonar + Reddit + Twitter) ---
    # Collect scores from all 3 sources, veto only if 2/3 agree
    if result["direction"] in ("LONG", "SHORT"):
        sentiment_scores = []  # list of (name, score_on_-10_+10_scale)

        # 1. Sonar (news) - still adjusts confidence via its own modifier
        try:
            sonar = get_sonar_sentiment(symbol, asset_type="stock")
            result["confidence"] = sonar_signal_modifier(result["confidence"], sonar)
            sonar_score = sonar.get("score", 0) / 10.0  # normalize -100/+100 -> -10/+10
            sonar_score = max(-10, min(10, sonar_score))
            sentiment_scores.append(("Sonar", sonar_score))
            result["reasons"].append("Sonar:{}/{:.0f}".format(
                sonar.get("score", 0), sonar.get("summary", "?")[:40]))
        except Exception as e:
            log.warning("Sonar sentiment error: %s", e)

        # 2. Reddit (crowd)
        try:
            _, r_reason, r_score = reddit_sentiment_filter(symbol, asset_type="stock")
            sentiment_scores.append(("Reddit", r_score))
            result["reasons"].append(r_reason)
        except Exception as e:
            log.warning("Reddit sentiment error: %s", e)

        # 3. Twitter/X (social)
        try:
            _, t_reason, t_score = twitter_sentiment_filter(symbol, asset_type="stock")
            sentiment_scores.append(("Twitter", t_score))
            result["reasons"].append(t_reason)
        except Exception as e:
            log.warning("Twitter sentiment error: %s", e)

        # --- CONSENSUS VOTE ---
        if sentiment_scores:
            negatives = sum(1 for _, s in sentiment_scores if s <= -6)
            fomo = sum(1 for _, s in sentiment_scores if s >= 8)
            avg_sentiment = sum(s for _, s in sentiment_scores) / len(sentiment_scores)

            if negatives >= 2:
                result["direction"] = "HOLD"
                result["reasons"].append("CONSENSUS_VETO_bearish({}/{})".format(negatives, len(sentiment_scores)))
            elif fomo >= 2:
                result["direction"] = "HOLD"
                result["reasons"].append("CONSENSUS_VETO_fomo({}/{})".format(fomo, len(sentiment_scores)))
            elif negatives == 1:
                result["confidence"] = max(result["confidence"] - 5, 0)
                result["reasons"].append("CONSENSUS_warn_1neg")
            elif fomo == 1:
                result["confidence"] = max(result["confidence"] - 3, 0)
                result["reasons"].append("CONSENSUS_warn_1fomo")
            else:
                # Aligned bullish = small boost
                if avg_sentiment > 3 and result["direction"] == "LONG":
                    result["confidence"] = min(result["confidence"] + 3, 100)
                elif avg_sentiment < -3 and result["direction"] == "SHORT":
                    result["confidence"] = min(result["confidence"] + 3, 100)

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
    with _state_lock:
        start = _state.get("daily_start_value", 0.0)
        if start <= 0:
            return False
        loss_pct = (portfolio_value - start) / start
        if loss_pct <= DAILY_LOSS_LIMIT and not _state.get("daily_loss_alerted"):
            _state["paused"] = True
            _state["daily_loss_alerted"] = True
            save_state()
            alert = True
        else:
            alert = False
        paused = _state.get("paused", False)
    if alert:
        msg = "Daily loss limit atteinte: {:.1f}% - Trading pause".format(loss_pct * 100)
        log.warning(msg)
        send_telegram(msg)
        return True
    return paused

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

    with _state_lock:
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
        order = place_market_sell_full(symbol)
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
    if updated.get("status") not in ("open", None):
        notion_log_trade(updated)

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

    # Reddit trending tickers
    try:
        heat_stocks = reddit_ticker_heatmap("stock", limit=15)
        heat_crypto = reddit_ticker_heatmap("crypto", limit=15)
        if heat_stocks:
            lines.append("\nReddit Trending Stocks:")
            for t in heat_stocks[:5]:
                lines.append("  {} | {} mentions | {:+.1f} | {}".format(
                    t["ticker"], t["mentions"], t["avg_sentiment"], t["buzz"]))
        if heat_crypto:
            lines.append("\nReddit Trending Crypto:")
            for t in heat_crypto[:5]:
                lines.append("  {} | {} mentions | {:+.1f} | {}".format(
                    t["ticker"], t["mentions"], t["avg_sentiment"], t["buzz"]))
    except Exception as e:
        log.warning("Reddit heatmap error in morning brief: %s", e)

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
                elif isinstance(_n, _ast.AnnAssign):
                    if isinstance(_n.target, _ast.Name): _defined.add(_n.target.id)
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
            "/aide            - Aide\n/reddit          - Reddit trending tickers\n/scan_sante      - Scan sante du code"
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
        with _state_lock:
            _state["paused"] = True
            save_state()
        reply = "Trading Alpaca mis en pause"
    elif text == "/alpaca_resume":
        with _state_lock:
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
    elif text == "/reddit":
        try:
            heat_stocks = reddit_ticker_heatmap("stock", limit=15)
            heat_crypto = reddit_ticker_heatmap("crypto", limit=15)
            lines = ["Reddit Heatmap:"]
            lines.append("\nStocks Trending:")
            for t in (heat_stocks or [])[:8]:
                lines.append("  {} | {} mentions | {:+.1f} | {}".format(
                    t["ticker"], t["mentions"], t["avg_sentiment"], t["buzz"]))
            lines.append("\nCrypto Trending:")
            for t in (heat_crypto or [])[:8]:
                lines.append("  {} | {} mentions | {:+.1f} | {}".format(
                    t["ticker"], t["mentions"], t["avg_sentiment"], t["buzz"]))
            if not heat_stocks and not heat_crypto:
                lines.append("Aucun ticker trending detecte")
            reply = "\n".join(lines)
        except Exception as e:
            reply = "Reddit heatmap erreur: {}".format(e)
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
        _msg = "CRASH autonomous-trading-agent:\n" + _err_text + "\n\n" + _trace
        try:
            send_telegram(_msg)
        except Exception:
            pass
        try:
            linear_create_ticket("CRASH " + __file__, str(_crash_err) + "\n\n" + _tb.format_exc()[-800:])
        except Exception:
            pass
        # Auto-fix workflow trigger REMOVED (was pushing hallucinated commits)
        raise

