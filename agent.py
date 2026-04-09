"""
coinbase_bot.py - Crypto Trading Bot
Exchange  : Coinbase Advanced Trade
HOLD      : SOL-EUR, AVAX-EUR, LINK-EUR  (DCA mensuel, jamais vendre)
DAYTRADE  : BTC-EUR, ETH-EUR             (signaux EMA/RSI, RR >= 2.0)
Capital   : reel, petit (debut 40-100 EUR)
"""

import os
import json
import time
import uuid
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import requests
import schedule
from dotenv import load_dotenv
from flask import Flask

load_dotenv("/etc/secrets/.env")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coinbase_bot")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
CB_API_KEY    = os.getenv("COINBASE_API_KEY", "")
CB_API_SECRET = os.getenv("COINBASE_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "10000"))

HOLD_ASSETS  = ["SOL-EUR", "AVAX-EUR", "LINK-EUR"]
TRADE_ASSETS = ["BTC-EUR", "ETH-EUR"]
ALL_ASSETS   = HOLD_ASSETS + TRADE_ASSETS

# Allocations cibles en % du capital total crypto
HOLD_ALLOCATION = {
    "SOL-EUR":  0.30,
    "AVAX-EUR": 0.20,
    "LINK-EUR": 0.15,
}

# Limites de risque
MIN_CAPITAL_EUR    = 5.0    # Sous ce seuil : lecture seule, zero ordre
MAX_TRADE_PCT      = 0.10   # Max 10% du capital par trade
CONFIDENCE_MIN     = 80     # Score minimum pour entrer
RR_MIN             = 2.0    # Risk/Reward minimum
DAILY_LOSS_LIMIT   = -0.05  # -5% sur la journee : pause auto
SPREAD_MAX_PCT     = 0.005  # Spread > 0.5% : skip (marche illiquide)
HOLD_STOP_LOSS_PCT = -0.30  # Stop catastrophique hold assets : -30%

TRADE_LOG_FILE = "crypto_trades.json"
STATE_FILE     = "crypto_state.json"

# ---------------------------------------------------------------------------
# STATE GLOBAL (pause, perte journaliere)
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
        return
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.error("Telegram error: %s", e)

# ---------------------------------------------------------------------------
# COINBASE ADVANCED TRADE API (HMAC-SHA256)
# ---------------------------------------------------------------------------
CB_BASE_URL = "https://api.coinbase.com"

def _cb_headers(method, path, body=""):
    key_name = CB_API_KEY
    key_secret = CB_API_SECRET.replace("\\n", "\n")
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": int(time.time()),
        "exp": int(time.time()) + 120,
        "uri": method.upper() + " api.coinbase.com" + path,
    }
    private_key = load_pem_private_key(key_secret.encode(), password=None)
    token = jwt.encode(payload, private_key, algorithm="ES256",
                       headers={"kid": key_name, "nonce": uuid.uuid4().hex})
    return {"Authorization": "Bearer " + token,
            "Content-Type": "application/json"}

def cb_get(path: str) -> Optional[dict]:
    try:
        headers = _cb_headers("GET", path)
        resp = requests.get(CB_BASE_URL + path, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        log.error("CB GET %s -> %s %s", path, resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.error("CB GET error %s: %s", path, e)
        return None

def cb_post(path: str, payload: dict) -> Optional[dict]:
    try:
        body = json.dumps(payload)
        headers = _cb_headers("POST", path, body)
        resp = requests.post(CB_BASE_URL + path, headers=headers, data=body, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        log.error("CB POST %s -> %s %s", path, resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.error("CB POST error %s: %s", path, e)
        return None

# ---------------------------------------------------------------------------
# ACCOUNT & BALANCES
# ---------------------------------------------------------------------------
def get_accounts() -> dict:
    """Retourne {currency: available_balance} pour tous les comptes."""
    data = cb_get("/api/v3/brokerage/accounts")
    if not data:
        return {}
    balances = {}
    for acc in data.get("accounts", []):
        currency = acc.get("currency", "")
        available = float(acc.get("available_balance", {}).get("value", 0))
        balances[currency] = available
    return balances

def get_eur_balance() -> float:
    balances = get_accounts()
    return balances.get("EUR", 0.0)

def get_portfolio_value_eur() -> float:
    """Valeur totale du portefeuille en EUR (cash + positions)."""
    balances = get_accounts()
    total = balances.get("EUR", 0.0)
    prices = get_prices(ALL_ASSETS)
    for asset in ALL_ASSETS:
        coin = asset.split("-")[0]
        qty = balances.get(coin, 0.0)
        price = prices.get(asset, 0.0)
        total += qty * price
    return total

# ---------------------------------------------------------------------------
# PRICES & SPREAD CHECK
# ---------------------------------------------------------------------------
def get_prices(product_ids: list) -> dict:
    """Retourne {product_id: mid_price} pour une liste de paires."""
    prices = {}
    for pid in product_ids:
        data = cb_get("/api/v3/brokerage/best_bid_ask?product_ids={}".format(pid))
        if not data:
            continue
        for entry in data.get("pricebooks", []):
            if entry.get("product_id") != pid:
                continue
            bids = entry.get("bids", [])
            asks = entry.get("asks", [])
            if bids and asks:
                bid = float(bids[0]["price"])
                ask = float(asks[0]["price"])
                prices[pid] = (bid + ask) / 2.0
    return prices

def spread_ok(product_id: str) -> bool:
    """Verifie que le spread bid/ask est acceptable (<= SPREAD_MAX_PCT)."""
    data = cb_get("/api/v3/brokerage/best_bid_ask?product_ids={}".format(product_id))
    if not data:
        return False
    for entry in data.get("pricebooks", []):
        if entry.get("product_id") != product_id:
            continue
        bids = entry.get("bids", [])
        asks = entry.get("asks", [])
        if not bids or not asks:
            return False
        bid = float(bids[0]["price"])
        ask = float(asks[0]["price"])
        if bid <= 0:
            return False
        spread_pct = (ask - bid) / bid
        if spread_pct > SPREAD_MAX_PCT:
            log.info("Spread %s trop large: %.3f%%", product_id, spread_pct * 100)
            return False
        return True
    return False

# ---------------------------------------------------------------------------
# CANDLES & INDICATEURS
# ---------------------------------------------------------------------------
def get_candles(product_id: str, granularity: str = "ONE_HOUR", limit: int = 60) -> list:
    """
    Retourne liste de candles [{open, high, low, close, volume}].
    granularity: ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, ONE_HOUR, SIX_HOUR, ONE_DAY
    """
    path = "/api/v3/brokerage/products/{}/candles?granularity={}&limit={}".format(
        product_id, granularity, limit
    )
    data = cb_get(path)
    if not data:
        return []
    candles = []
    for c in data.get("candles", []):
        try:
            candles.append({
                "open":   float(c["open"]),
                "high":   float(c["high"]),
                "low":    float(c["low"]),
                "close":  float(c["close"]),
                "volume": float(c["volume"]),
            })
        except (KeyError, ValueError):
            continue
    # Coinbase retourne les candles du plus recent au plus ancien -> inverser
    return list(reversed(candles))

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
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

# ---------------------------------------------------------------------------
# ANALYSE & SIGNAL
# ---------------------------------------------------------------------------
def analyze(product_id: str) -> dict:
    """
    Retourne signal complet pour un asset.
    {direction: LONG|SHORT|HOLD, confidence: 0-100, entry, sl, tp, reasons}
    """
    result = {
        "product_id": product_id,
        "direction":  "HOLD",
        "confidence": 0,
        "entry":      0.0,
        "sl":         0.0,
        "tp":         0.0,
        "reasons":    [],
    }

    candles = get_candles(product_id, granularity="ONE_HOUR", limit=60)
    if len(candles) < 22:
        result["reasons"].append("not enough candles: {}".format(len(candles)))
        return result

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    current = closes[-1]
    result["entry"] = current

    score = 0
    reasons = []

    # EMA 9 / 21
    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    if len(ema9) >= 2 and len(ema21) >= 2:
        e9  = ema9[-1];  e9p  = ema9[-2]
        e21 = ema21[-1]; e21p = ema21[-2]
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
    avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1.0
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

    # SL/TP via ATR (2x ATR stop, 4x ATR target = RR 2.0)
    atr = calc_atr(candles, 14)
    if atr > 0:
        result["sl"] = round(current - 2.0 * atr, 6)
        result["tp"] = round(current + 4.0 * atr, 6)

    result["reasons"] = reasons

    bull_count = sum(1 for r in reasons if any(w in r for w in ["bullish", "oversold", "golden", "HH"]))
    bear_count = sum(1 for r in reasons if any(w in r for w in ["bearish", "overbought", "death", "LL"]))

    abs_score = abs(score)
    if bull_count >= 3 and score >= CONFIDENCE_MIN:
        result["direction"]  = "LONG"
        result["confidence"] = min(score, 100)
    elif bear_count >= 3 and abs_score >= CONFIDENCE_MIN:
        result["direction"]  = "SHORT"
        result["confidence"] = min(abs_score, 100)
    else:
        result["confidence"] = min(abs_score, 100)

    return result

# ---------------------------------------------------------------------------
# GARDE-FOUS : TOUTES LES CONDITIONS AVANT D'AGIR
# ---------------------------------------------------------------------------
def has_enough_capital() -> bool:
    eur = get_eur_balance()
    if eur < MIN_CAPITAL_EUR:
        log.info("Capital insuffisant: EUR %.2f < %.2f", eur, MIN_CAPITAL_EUR)
        return False
    return True

def is_hold_asset(product_id: str) -> bool:
    return product_id in HOLD_ASSETS

def has_position(product_id: str) -> bool:
    coin = product_id.split("-")[0]
    balances = get_accounts()
    qty = balances.get(coin, 0.0)
    return qty > 0.0001

def get_position_qty(product_id: str) -> float:
    coin = product_id.split("-")[0]
    balances = get_accounts()
    return balances.get(coin, 0.0)

def check_daily_loss(portfolio_value: float) -> bool:
    """Retourne True si limite de perte journaliere atteinte."""
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
def _gen_client_order_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return "bot_{}".format(ts)

def place_market_buy(product_id: str, quote_size_eur: float) -> Optional[dict]:
    """
    Achat market en EUR (quote currency).
    Verifie: capital suffisant, spread ok, pas deja en position.
    """
    if _state.get("paused"):
        log.info("Bot en pause - BUY bloque: %s", product_id)
        return None
    if not has_enough_capital():
        return None
    if not spread_ok(product_id):
        log.info("Spread trop large, BUY skip: %s", product_id)
        return None
    eur_balance = get_eur_balance()
    if quote_size_eur > eur_balance:
        quote_size_eur = eur_balance * 0.95  # securite 5%
        if quote_size_eur < 1.0:
            log.info("Solde insuffisant pour BUY %s: %.2f EUR", product_id, eur_balance)
            return None

    payload = {
        "client_order_id": _gen_client_order_id(),
        "product_id":      product_id,
        "side":            "BUY",
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": "{:.2f}".format(quote_size_eur)
            }
        }
    }
    result = cb_post("/api/v3/brokerage/orders", payload)
    if result:
        log.info("BUY %s EUR %.2f - success", product_id, quote_size_eur)
    return result

def place_market_sell_full(product_id: str) -> Optional[dict]:
    """
    Vente totale de la position (base_size = qty detenue).
    Verifie: position > 0, pas un hold asset (sauf stop catastrophique).
    """
    if not has_position(product_id):
        log.info("Pas de position a vendre: %s", product_id)
        return None
    if not spread_ok(product_id):
        log.info("Spread trop large, SELL skip: %s", product_id)
        return None

    qty = get_position_qty(product_id)
    if qty <= 0:
        return None

    payload = {
        "client_order_id": _gen_client_order_id(),
        "product_id":      product_id,
        "side":            "SELL",
        "order_configuration": {
            "market_market_ioc": {
                "base_size": "{:.8f}".format(qty)
            }
        }
    }
    result = cb_post("/api/v3/brokerage/orders", payload)
    if result:
        log.info("SELL %s qty %.6f - success", product_id, qty)
    return result

def place_market_sell_partial(product_id: str, qty: float) -> Optional[dict]:
    """Vente partielle (pour take profit progressif)."""
    if not has_position(product_id):
        return None
    current_qty = get_position_qty(product_id)
    qty = min(qty, current_qty)
    if qty <= 0:
        return None

    payload = {
        "client_order_id": _gen_client_order_id(),
        "product_id":      product_id,
        "side":            "SELL",
        "order_configuration": {
            "market_market_ioc": {
                "base_size": "{:.8f}".format(qty)
            }
        }
    }
    result = cb_post("/api/v3/brokerage/orders", payload)
    if result:
        log.info("SELL PARTIAL %s qty %.6f", product_id, qty)
    return result

# ---------------------------------------------------------------------------
# STARTUP AUDIT (au lancement du bot)
# ---------------------------------------------------------------------------
def startup_audit() -> None:
    """
    Verifie l'etat du portefeuille au demarrage.
    - Detecte les positions existantes
    - Verifie si SL des hold assets est atteint
    - Initialise daily_start_value
    """
    log.info("Startup audit...")
    balances = get_accounts()
    prices   = get_prices(ALL_ASSETS)
    total_eur = balances.get("EUR", 0.0)

    lines = ["Startup Audit:"]
    lines.append("EUR disponible: {:.2f}".format(total_eur))

    for asset in ALL_ASSETS:
        coin = asset.split("-")[0]
        qty  = balances.get(coin, 0.0)
        price = prices.get(asset, 0.0)
        value = qty * price
        if qty > 0:
            lines.append("{}: qty={:.6f} val={:.2f} EUR".format(asset, qty, value))
            total_eur += value

            # Verifier stop catastrophique sur hold assets
            if is_hold_asset(asset) and price > 0:
                # Estimer prix moyen (pas disponible sans historique)
                # On verifie juste que la position est vivante
                lines.append("  -> HOLD asset, monitored")

    _state["daily_start_value"] = total_eur
    _state["daily_loss_alerted"] = False
    save_state()

    lines.append("Portfolio total: {:.2f} EUR".format(total_eur))
    if total_eur < MIN_CAPITAL_EUR:
        lines.append("ATTENTION: capital < {:.0f} EUR - mode lecture seule".format(MIN_CAPITAL_EUR))

    msg = "\n".join(lines)
    log.info(msg)
    send_telegram(msg)

# ---------------------------------------------------------------------------
# SCAN DAYTRADE (BTC-EUR, ETH-EUR)
# ---------------------------------------------------------------------------
def run_trade_scan() -> None:
    """Scan des assets de day trade - appele toutes les 15 min."""
    if _state.get("paused"):
        return

    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    # Crypto = 24/7 mais eviter 2h-6h UTC (faible liquidite)
    if 2 <= hour < 6:
        return

    portfolio_value = get_portfolio_value_eur()
    if check_daily_loss(portfolio_value):
        return
    if portfolio_value < MIN_CAPITAL_EUR:
        return

    eur_balance = get_eur_balance()

    for pid in TRADE_ASSETS:
        # Deja en position ? Verifier SL/TP
        if has_position(pid):
            _check_exit(pid)
            continue

        # Analyser le signal
        signal = analyze(pid)
        log.info(
            "Signal %s: %s conf=%d reasons=%s",
            pid, signal["direction"], signal["confidence"], signal["reasons"]
        )

        if signal["direction"] != "LONG":
            continue
        if signal["confidence"] < CONFIDENCE_MIN:
            continue

        entry = signal["entry"]
        sl    = signal["sl"]
        tp    = signal["tp"]

        if sl <= 0 or tp <= 0 or sl >= entry:
            log.info("SL/TP invalide pour %s, skip", pid)
            continue

        rr = (tp - entry) / (entry - sl)
        if rr < RR_MIN:
            log.info("RR %.2f < %.1f pour %s, skip", rr, RR_MIN, pid)
            continue

        # Taille de position
        trade_eur = min(portfolio_value * MAX_TRADE_PCT, eur_balance * 0.90)
        if trade_eur < 1.0:
            log.info("Pas assez de capital EUR pour trader %s", pid)
            continue

        log.info("TRADE: BUY %s EUR=%.2f conf=%d RR=%.1f", pid, trade_eur, signal["confidence"], rr)
        order = place_market_buy(pid, trade_eur)

        if order:
            trade = {
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "product_id": pid,
                "direction":  "LONG",
                "trade_eur":  trade_eur,
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
                "BUY EUR {:.2f} @ ~{:.4f}\n"
                "SL: {:.4f} | TP: {:.4f}\n"
                "RR: {:.1f}:1 | Conf: {}%\n"
                "Raisons: {}"
            ).format(
                pid, trade_eur, entry,
                sl, tp, rr,
                signal["confidence"],
                ", ".join(signal["reasons"])
            )
            send_telegram(msg)

def _check_exit(product_id: str) -> None:
    """
    Verifie si SL ou TP est atteint sur une position ouverte.
    Utilise le dernier trade logue pour connaitre entry/sl/tp.
    """
    trade = _get_open_trade(product_id)
    if not trade:
        return

    prices = get_prices([product_id])
    current = prices.get(product_id, 0.0)
    if current <= 0:
        return

    sl = trade.get("sl", 0.0)
    tp = trade.get("tp", 0.0)

    if sl > 0 and current <= sl:
        log.info("SL atteint %s: %.4f <= %.4f", product_id, current, sl)
        order = place_market_sell_full(product_id)
        if order:
            trade["status"]   = "closed_sl"
            trade["exit"]     = current
            trade["exit_ts"]  = datetime.now(timezone.utc).isoformat()
            _update_trade(trade)
            pnl_pct = (current - trade["entry"]) / trade["entry"] * 100
            send_telegram(
                "SL touche: {}\nExit: {:.4f} | PnL: {:.1f}%".format(product_id, current, pnl_pct)
            )

    elif tp > 0 and current >= tp:
        log.info("TP atteint %s: %.4f >= %.4f", product_id, current, tp)
        # TP partiel: vendre 50%, laisser courir le reste
        qty = get_position_qty(product_id)
        partial_qty = qty * 0.5
        order = place_market_sell_partial(product_id, partial_qty)
        if order:
            trade["status"]  = "partial_tp"
            trade["exit_ts"] = datetime.now(timezone.utc).isoformat()
            _update_trade(trade)
            pnl_pct = (current - trade["entry"]) / trade["entry"] * 100
            send_telegram(
                "TP partiel (50%): {}\nExit: {:.4f} | PnL: {:.1f}%".format(
                    product_id, current, pnl_pct
                )
            )

# ---------------------------------------------------------------------------
# HOLD ASSET MONITORING (verifier stop catastrophique -30%)
# ---------------------------------------------------------------------------
def monitor_hold_assets() -> None:
    """
    Surveille les assets HOLD.
    Vend uniquement si -30% (protection catastrophe).
    On ne connait pas le prix d'entree -> on utilise le prix d'il y a 30 jours.
    """
    prices_now = get_prices(HOLD_ASSETS)

    for asset in HOLD_ASSETS:
        if not has_position(asset):
            continue
        candles_d = get_candles(asset, granularity="ONE_DAY", limit=31)
        if len(candles_d) < 2:
            continue
        price_30d = candles_d[0]["close"]
        price_now = prices_now.get(asset, 0.0)
        if price_30d <= 0 or price_now <= 0:
            continue
        change_pct = (price_now - price_30d) / price_30d
        if change_pct <= HOLD_STOP_LOSS_PCT:
            msg = "STOP CATASTROPHIQUE {}: {:.1f}% en 30j - Vente d'urgence".format(
                asset, change_pct * 100
            )
            log.warning(msg)
            send_telegram(msg)
            place_market_sell_full(asset)

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

def _get_open_trade(product_id: str) -> Optional[dict]:
    trades = _load_trades()
    for t in reversed(trades):
        if t.get("product_id") == product_id and t.get("status") == "open":
            return t
    return None

def _update_trade(updated: dict) -> None:
    trades = _load_trades()
    for i, t in enumerate(trades):
        if (t.get("product_id") == updated.get("product_id")
                and t.get("timestamp") == updated.get("timestamp")):
            trades[i] = updated
            break
    _save_trades(trades)

# ---------------------------------------------------------------------------
# MORNING BRIEF
# ---------------------------------------------------------------------------
def morning_brief() -> None:
    balances = get_accounts()
    prices   = get_prices(ALL_ASSETS)
    total    = balances.get("EUR", 0.0)

    lines = ["Morning Brief - Crypto\n"]
    lines.append("EUR cash: {:.2f}".format(balances.get("EUR", 0.0)))

    for asset in ALL_ASSETS:
        coin  = asset.split("-")[0]
        qty   = balances.get(coin, 0.0)
        price = prices.get(asset, 0.0)
        value = qty * price
        total += value
        tag = "HOLD" if is_hold_asset(asset) else "TRADE"
        if qty > 0:
            lines.append("{} [{}]: {:.6f} = {:.2f} EUR".format(asset, tag, qty, value))

    lines.append("\nPortfolio: {:.2f} EUR".format(total))
    start = _state.get("daily_start_value", total)
    if start > 0:
        pnl_pct = (total - start) / start * 100
        lines.append("PnL journee: {:.1f}%".format(pnl_pct))

    _state["daily_start_value"] = total
    _state["daily_loss_alerted"] = False
    save_state()

    send_telegram("\n".join(lines))

# ---------------------------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------------------------
_last_update_id = 0

def poll_telegram_commands() -> None:
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return
    try:
        url = "https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_TOKEN)
        resp = requests.get(
            url,
            params={"offset": _last_update_id + 1, "timeout": 3},
            timeout=10,
        )
        if not resp.ok:
            return
        for update in resp.json().get("result", []):
            _last_update_id = update["update_id"]
            msg_data = update.get("message", {})
            text     = msg_data.get("text", "").strip()
            chat_id  = msg_data.get("chat", {}).get("id")
            if not text or not chat_id:
                continue
            _handle_command(text.lower(), chat_id)
    except Exception as e:
        log.error("Telegram poll error: %s", e)

def _handle_command(text: str, chat_id) -> None:
    if text == "/aide":
        reply = (
            "Commandes crypto:\n"
            "/crypto_status   - Portfolio complet\n"
            "/crypto_prix     - Prix live\n"
            "/crypto_trades   - 5 derniers trades\n"
            "/crypto_signal   - Analyse BTC+ETH\n"
            "/crypto_pause    - Pause trading\n"
            "/crypto_resume   - Reprendre\n"
            "/crypto_urgence  - Fermer toutes positions trade\n"
            "/aide            - Aide"
        )
    elif text == "/crypto_status":
        balances = get_accounts()
        prices   = get_prices(ALL_ASSETS)
        total    = balances.get("EUR", 0.0)
        lines    = ["Portfolio Crypto:"]
        lines.append("EUR: {:.2f}".format(balances.get("EUR", 0.0)))
        for asset in ALL_ASSETS:
            coin  = asset.split("-")[0]
            qty   = balances.get(coin, 0.0)
            price = prices.get(asset, 0.0)
            if qty > 0:
                value = qty * price
                total += value
                lines.append("{}: {:.6f} = {:.2f} EUR".format(asset, qty, value))
        lines.append("Total: {:.2f} EUR".format(total))
        lines.append("Statut: {}".format("PAUSE" if _state.get("paused") else "ACTIF"))
        reply = "\n".join(lines)
    elif text == "/crypto_prix":
        prices = get_prices(ALL_ASSETS)
        lines  = ["Prix live:"]
        for pid, price in prices.items():
            lines.append("{}: {:.4f} EUR".format(pid, price))
        reply = "\n".join(lines)
    elif text == "/crypto_trades":
        trades = _load_trades()[-5:]
        if trades:
            lines = ["5 derniers trades:"]
            for t in reversed(trades):
                status  = t.get("status", "?")
                entry   = t.get("entry", 0)
                exit_p  = t.get("exit", 0)
                icon    = "green" if "tp" in status else ("red" if "sl" in status else "blue")
                pnl_str = ""
                if exit_p and entry:
                    pnl_pct = (exit_p - entry) / entry * 100
                    pnl_str = " | {:.1f}%".format(pnl_pct)
                lines.append("{} {} [{}]{}".format(
                    t.get("product_id"), t.get("direction"), status, pnl_str
                ))
            reply = "\n".join(lines)
        else:
            reply = "Aucun trade logue"
    elif text == "/crypto_signal":
        lines = ["Analyse signals:"]
        for pid in TRADE_ASSETS:
            sig = analyze(pid)
            lines.append("{}: {} conf={}%\n  {}".format(
                pid, sig["direction"], sig["confidence"],
                ", ".join(sig["reasons"][:3])
            ))
        reply = "\n".join(lines)
    elif text == "/crypto_pause":
        _state["paused"] = True
        save_state()
        reply = "Trading crypto mis en pause"
    elif text == "/crypto_resume":
        _state["paused"] = False
        _state["daily_loss_alerted"] = False
        save_state()
        reply = "Trading crypto repris"
    elif text == "/crypto_urgence":
        closed = 0
        for pid in TRADE_ASSETS:
            if has_position(pid):
                if not is_hold_asset(pid):
                    order = place_market_sell_full(pid)
                    if order:
                        closed += 1
        reply = "Urgence: {} positions TRADE fermees".format(closed)
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
# HEALTH SERVER (Render)
# ---------------------------------------------------------------------------
health_app = Flask(__name__)

@health_app.route("/health")
def health():
    return {"status": "ok", "bot": "coinbase_v1"}, 200

@health_app.route("/")
def index():
    return {"status": "running", "paused": _state.get("paused", False)}, 200

def run_health_server() -> None:
    health_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ---------------------------------------------------------------------------
# SCHEDULER
# ---------------------------------------------------------------------------
def setup_scheduler() -> None:
    schedule.every().day.at("08:00").do(morning_brief)
    schedule.every(15).minutes.do(run_trade_scan)
    schedule.every(60).minutes.do(monitor_hold_assets)
    schedule.every(5).seconds.do(poll_telegram_commands)
    log.info("Scheduler configure")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Coinbase Bot V1 demarrage...")
    load_state()

    if not CB_API_KEY or not CB_API_SECRET:
        log.error("COINBASE_API_KEY ou COINBASE_API_SECRET manquant")
        return

    # Health server
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    log.info("Health server port %d", PORT)

    # Audit initial
    startup_audit()

    # Scheduler
    setup_scheduler()

    # Loop principale
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error("Scheduler error: %s", e)
            send_telegram("Coinbase Bot erreur: {}".format(e))
        time.sleep(1)


if __name__ == "__main__":
    main()
