import os
import time
import threading
import requests
from datetime import datetime
from dotenv import load_dotenv
from coinbase.rest import RESTClient
import logging

load_dotenv()

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# CONFIG
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET = os.getenv("COINBASE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Validation des clés API
if not COINBASE_API_KEY or not COINBASE_SECRET:
    log.error("❌ Clés Coinbase manquantes dans .env")
    raise ValueError("COINBASE_API_KEY ou COINBASE_SECRET_KEY non configurés")

INTERVAL_CRYPTO = 180

CRYPTO_UNIVERSE_RAW = [
    "BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "LINK-EUR",
    "AVAX-EUR", "ADA-EUR", "DOT-EUR", "DOGE-EUR", "LTC-EUR",
    "UNI-EUR", "ATOM-EUR", "NEAR-EUR", "APT-EUR",
    "ARB-EUR", "OP-EUR", "INJ-EUR", "ROSE-EUR"
]

MAX_CRYPTO_POSITIONS = 3
CRYPTO_SL_PCT = 7.0
CRYPTO_TP_PCT = 12.0
COINBASE_FEE_PCT = 1.2

TRAILING_STOP_PCT = 4.0
PYRAMID_MAX = 2

MAX_LOSS_STREAK = 3

# Initialisation globale
try:
    coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
except Exception as e:
    log.error(f"❌ Erreur initialisation Coinbase: {e}")
    coinbase = None

# Variables globales thread-safe
active_crypto_trades = {}
trades_lock = threading.Lock()
loss_streak = 0

# VALIDATION PRODUITS
def get_valid_products():
    """Récupère la liste des produits valides"""
    if not coinbase:
        return set()
    try:
        response = coinbase.get_products()
        products = response.get("products", [])
        return {p["product_id"] for p in products if "product_id" in p}
    except Exception as e:
        log.error(f"Erreur récupération produits: {e}")
        return set()

VALID_PRODUCTS = get_valid_products()
CRYPTO_UNIVERSE = [s for s in CRYPTO_UNIVERSE_RAW if s in VALID_PRODUCTS]

log.info(f"🚀 {len(CRYPTO_UNIVERSE)} crypto actives trouvées")

# UTILS
def send_telegram(msg):
    """Envoie un message Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        log.error(f"Erreur Telegram: {e}")

# DATA
def get_crypto_price(symbol):
    """Récupère le prix actuel d'une crypto"""
    if not coinbase or symbol not in VALID_PRODUCTS:
        return None
    try:
        pb = coinbase.get_best_bid_ask(product_ids=[symbol])
        pricebooks = pb.get("pricebooks", [])
        if not pricebooks or not pricebooks[0].get("asks"):
            return None
        price = float(pricebooks[0]["asks"][0]["price"])
        return price
    except Exception as e:
        log.debug(f"Erreur prix {symbol}: {e}")
        return None

def get_crypto_balance(currency):
    """Récupère le solde d'une devise"""
    if not coinbase:
        return 0
    try:
        accounts = coinbase.get_accounts()
        for acc in accounts.get("accounts", []):
            if acc.get("currency") == currency:
                balance = acc.get("available_balance", {}).get("value")
                return float(balance) if balance else 0
        return 0
    except Exception as e:
        log.debug(f"Erreur balance {currency}: {e}")
        return 0

# ANALYSE TECHNIQUE
def calculate_rsi(prices, period=14):
    """Calcule le RSI (Relative Strength Index)"""
    if len(prices) < period + 1:
        return None
    
    gains = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def detect_breakout_setup(prices):
    """Détecte les configurations de breakout"""
    if len(prices) < 20:
        return None

    recent = prices[-20:]
    resistance = max(recent[:-1])
    current = prices[-1]
    prev = prices[-2]
    momentum = current - prices[-5]

    # Configuration EARLY: approche de la résistance
    if current < resistance and (resistance - current) / resistance < 0.01 and momentum > 0:
        return "EARLY"

    # Configuration BREAKOUT: cassure de la résistance
    if current > resistance and prev <= resistance:
        return "BREAKOUT"

    # Configuration PULLBACK: retour à la résistance
    if prev > resistance and current <= resistance * 1.01:
        return "PULLBACK"

    return None

def get_crypto_ta(symbol):
    """Récupère l'analyse technique d'une crypto"""
    if not coinbase:
        return None
    try:
        candles = coinbase.get_candles(
            product_id=symbol,
            granularity="ONE_HOUR"
        )
        prices = [float(c["close"]) for c in candles.get("candles", [])]

        if len(prices) < 20:
            return None

        rsi = calculate_rsi(prices)
        setup = detect_breakout_setup(prices)
        week_perf = (prices[-1] - prices[-7]) / prices[-7] * 100 if len(prices) >= 7 else 0

        return {
            "rsi": rsi,
            "setup": setup,
            "week_perf": week_perf
        }
    except Exception as e:
        log.debug(f"Erreur TA {symbol}: {e}")
        return None

def place_crypto_order(symbol, side, amount_eur):
    """Place un ordre d'achat ou vente"""
    if not coinbase or symbol not in VALID_PRODUCTS:
        return False

    try:
        if side == "buy":
            if amount_eur < 5:
                log.info(f"⚠️ Montant trop faible pour {symbol}: {amount_eur}€")
                return False

            coinbase.market_order_buy(
                client_order_id=str(time.time()),
                product_id=symbol,
                quote_size=str(round(amount_eur, 2))
            )
            
            with trades_lock:
                entry_price = get_crypto_price(symbol)
                active_crypto_trades[symbol] = {
                    "entry": entry_price,
                    "amount": amount_eur,
                    "pyramids": 0,
                    "sl": None
                }
            
            msg = f"✅ BUY {symbol} @ {entry_price}€ | Montant: {amount_eur}€"
            log.info(msg)
            send_telegram(msg)
            return True

        else:  # SELL
            balance = get_crypto_balance(symbol.replace("-EUR", ""))
            if balance <= 0.001:
                log.info(f"⚠️ Solde insuffisant pour {symbol}")
                return False

            price = get_crypto_price(symbol)
            coinbase.market_order_sell(
                client_order_id=str(time.time()),
                product_id=symbol,
                base_size=str(round(balance, 8))
            )
            
            with trades_lock:
                active_crypto_trades.pop(symbol, None)
            
            msg = f"💰 SELL {symbol} @ {price}€ | Solde: {balance}"
            log.info(msg)
            send_telegram(msg)
            return True

    except Exception as e:
        log.error(f"Erreur lors du placement d'ordre {symbol} {side}: {e}")
        send_telegram(f"❌ Erreur ordre {symbol}: {str(e)[:50]}")
        return False

def update_trailing_stop():
    """Met à jour les stops suiveurs"""
    with trades_lock:
        for symbol, trade in list(active_crypto_trades.items()):
            price = get_crypto_price(symbol)
            if not price or not trade.get("entry"):
                continue

            entry = trade["entry"]
            gain = (price - entry) / entry * 100

            # Active le trailing stop quand on est en profit
            if gain > 2:
                sl = price * (1 - TRAILING_STOP_PCT / 100)
                if trade.get("sl") is None or sl > trade["sl"]:
                    trade["sl"] = sl
                    log.info(f"📍 Trailing stop {symbol}: {sl:.2f}€ (gain: {gain:.2f}%)")

            # Déclenche le stop loss
            if trade.get("sl") and price <= trade["sl"]:
                log.warning(f"🛑 Stop loss déclenché {symbol}")
                place_crypto_order(symbol, "sell", 0)

def try_pyramiding(symbol):
    """Ajoute des positions (pyramiding)"""
    with trades_lock:
        trade = active_crypto_trades.get(symbol)
        if not trade or trade["pyramids"] >= PYRAMID_MAX:
            return

        price = get_crypto_price(symbol)
        if not price or not trade.get("entry"):
            return

        gain = (price - trade["entry"]) / trade["entry"] * 100
        pyramid_threshold = (trade["pyramids"] + 1) * 3

        if gain > pyramid_threshold:
            amount = trade["amount"] * 0.5
            log.info(f"🔺 Pyramiding {symbol}: +{amount}€ (gain: {gain:.2f}%)")
            place_crypto_order(symbol, "buy", amount)
            trade["pyramids"] += 1

def scan_crypto():
    """Scanne les cryptos pour détecter les opportunités"""
    global loss_streak

    if loss_streak >= MAX_LOSS_STREAK:
        log.warning("🛑 STOP - Série de pertes trop importante")
        return

    cash = get_crypto_balance("EUR")
    
    if cash < 10:
        log.info(f"⚠️ Cash insuffisant: {cash}€")
        return

    with trades_lock:
        active_count = len(active_crypto_trades)

    if active_count >= MAX_CRYPTO_POSITIONS:
        log.debug(f"Limite de positions atteinte: {active_count}/{MAX_CRYPTO_POSITIONS}")
        return

    for symbol in CRYPTO_UNIVERSE:
        with trades_lock:
            if symbol in active_crypto_trades:
                continue

        price = get_crypto_price(symbol)
        ta = get_crypto_ta(symbol)

        if not price or not ta:
            continue

        if abs(ta["week_perf"]) < 1:
            continue

        setup = ta.get("setup")
        if not setup:
            continue

        # Calcul du risque basé sur la configuration
        risk = 0.1
        if setup == "EARLY":
            risk *= 0.5
        elif setup == "PULLBACK":
            risk *= 1.2

        amount = min(cash * risk, cash * 0.3)

        if amount > 5:
            log.info(f"📊 Signal {setup} trouvé pour {symbol} (perf: {ta['week_perf']:.2f}%, RSI: {ta.get('rsi', 'N/A')})")
            place_crypto_order(symbol, "buy", amount)
            break

def thread_crypto():
    """Boucle principale du trading"""
    log.info("🤖 Thread de trading démarré")
    
    while True:
        try:
            update_trailing_stop()

            with trades_lock:
                symbols = list(active_crypto_trades.keys())

            for symbol in symbols:
                try_pyramiding(symbol)

            scan_crypto()

        except Exception as e:
            log.error(f"Erreur dans la boucle trading: {e}")

        time.sleep(INTERVAL_CRYPTO)

if __name__ == "__main__":
    log.info("🚀 BOT V6 LANCÉ - Trading autonome activé")
    
    if not VALID_PRODUCTS:
        log.error("❌ Aucun produit valide trouvé. Vérifiez votre connexion Coinbase.")
    else:
        try:
            # Démarrer le thread de trading
            trading_thread = threading.Thread(target=thread_crypto, daemon=False)
            trading_thread.start()
            
            # Garder le main thread actif
            while True:
                time.sleep(1)
                
        except KeyboardInterrupt:
            log.info("⏹️ Arrêt du bot demandé")
            send_telegram("🛑 Bot arrêté")
