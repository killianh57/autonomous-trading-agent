import os
import json
import time
import threading
import requests
import anthropic
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from coinbase.rest import RESTClient

load_dotenv()

# CONFIG
COINBASE_API_KEY  = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET   = os.getenv("COINBASE_SECRET_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

INTERVAL_CRYPTO = 180

CRYPTO_UNIVERSE_RAW = [
    "BTC-EUR","ETH-EUR","SOL-EUR","XRP-EUR","LINK-EUR",
    "AVAX-EUR","ADA-EUR","DOT-EUR","DOGE-EUR","LTC-EUR",
    "UNI-EUR","ATOM-EUR","NEAR-EUR","APT-EUR",
    "ARB-EUR","OP-EUR","INJ-EUR","ROSE-EUR"
]

MAX_CRYPTO_POSITIONS = 3
CRYPTO_SL_PCT = 7.0
CRYPTO_TP_PCT = 12.0
COINBASE_FEE_PCT = 1.2

TRAILING_STOP_PCT = 4.0
PYRAMID_MAX = 2

loss_streak = 0
MAX_LOSS_STREAK = 3

coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)

# 🔒 VALIDATION PRODUITS
def get_valid_products():
    try:
        products = coinbase.get_products()
        return {p["product_id"] for p in products.get("products", [])}
    except:
        return set()

VALID_PRODUCTS = get_valid_products()
CRYPTO_UNIVERSE = [s for s in CRYPTO_UNIVERSE_RAW if s in VALID_PRODUCTS]

# UTILS
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# DATA
def get_crypto_price(symbol):
    try:
        if symbol not in VALID_PRODUCTS:
            return None
        pb = coinbase.get_best_bid_ask(product_ids=[symbol])
        return float(pb["pricebooks"][0]["asks"][0]["price"])
    except:
        return None

def get_crypto_balance(currency):
    try:
        for acc in coinbase.get_accounts()["accounts"]:
            if acc["currency"] == currency:
                return float(acc["available_balance"]["value"])
        return 0
    except:
        return 0

active_crypto_trades = {}

# ANALYSE TECHNIQUE
def calculate_rsi(prices, period=14):
    if len(prices) < period+1:
        return None
    gains = [max(prices[i]-prices[i-1],0) for i in range(1,len(prices))]
    losses = [max(prices[i-1]-prices[i],0) for i in range(1,len(prices))]
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al == 0:
        return 100
    return 100-(100/(1+ag/al))


def detect_breakout_setup(prices):
    if len(prices) < 20:
        return None

    recent = prices[-20:]
    resistance = max(recent[:-1])
    support = min(recent[:-1])
    current = prices[-1]
    prev = prices[-2]

    momentum = current - prices[-5]

    if current < resistance and (resistance-current)/resistance < 0.01 and momentum > 0:
        return "EARLY"

    if current > resistance and prev <= resistance:
        return "BREAKOUT"

    if prev > resistance and current <= resistance*1.01:
        return "PULLBACK"

    return None


def get_crypto_ta(symbol):
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

        return {
            "rsi": rsi,
            "setup": setup,
            "week_perf": (prices[-1]-prices[-7])/prices[-7]*100
        }

    except:
        return None
def place_crypto_order(symbol, side, amount_eur):
    try:
        if symbol not in VALID_PRODUCTS:
            return

        if side == "buy":
            coinbase.market_order_buy(
                client_order_id=str(time.time()),
                product_id=symbol,
                quote_size=str(amount_eur)
            )
            active_crypto_trades[symbol] = {
                "entry": get_crypto_price(symbol),
                "amount": amount_eur,
                "pyramids": 0
            }
            send_telegram(f"✅ BUY {symbol}")

        else:
            price = get_crypto_price(symbol)
            balance = get_crypto_balance(symbol.replace("-EUR",""))
            if balance > 0:
                coinbase.market_order_sell(
                    client_order_id=str(time.time()),
                    product_id=symbol,
                    base_size=str(balance)
                )
                active_crypto_trades.pop(symbol, None)
                send_telegram(f"💰 SELL {symbol}")

    except Exception as e:
        log(e)


def update_trailing_stop():
    for symbol, trade in list(active_crypto_trades.items()):
        price = get_crypto_price(symbol)
        if not price:
            continue

        entry = trade["entry"]
        gain = (price-entry)/entry*100

        if gain > 2:
            sl = price*(1-TRAILING_STOP_PCT/100)
            trade["sl"] = max(trade.get("sl",0), sl)

        if trade.get("sl") and price <= trade["sl"]:
            place_crypto_order(symbol, "sell", 0)


def try_pyramiding(symbol):
    trade = active_crypto_trades.get(symbol)
    if not trade:
        return

    if trade["pyramids"] >= PYRAMID_MAX:
        return

    price = get_crypto_price(symbol)
    gain = (price-trade["entry"])/trade["entry"]*100

    if gain > (trade["pyramids"]+1)*3:
        place_crypto_order(symbol, "buy", trade["amount"]*0.5)
        trade["pyramids"] += 1


def scan_crypto():
    global loss_streak

    if loss_streak >= MAX_LOSS_STREAK:
        log("STOP LOSS STREAK")
        return

    cash = get_crypto_balance("EUR")

    for symbol in CRYPTO_UNIVERSE:
        if symbol in active_crypto_trades:
            continue

        price = get_crypto_price(symbol)
        ta = get_crypto_ta(symbol)

        if not price or not ta:
            continue

        if abs(ta["week_perf"]) < 1:
            continue

        setup = ta["setup"]
        if not setup:
            continue

        risk = 0.1

        if setup == "EARLY":
            risk *= 0.5
        elif setup == "PULLBACK":
            risk *= 1.2

        amount = min(cash * risk, cash * 0.3)

        if amount > 5:
            place_crypto_order(symbol, "buy", amount)
            break


def thread_crypto():
    while True:
        try:
            update_trailing_stop()

            for s in list(active_crypto_trades.keys()):
                try_pyramiding(s)

            scan_crypto()

        except Exception as e:
            log(e)

        time.sleep(INTERVAL_CRYPTO)


if __name__ == "__main__":
    log("🚀 BOT V5 LANCÉ")
    threading.Thread(target=thread_crypto).start()

