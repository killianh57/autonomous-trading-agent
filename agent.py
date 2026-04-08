# -*- coding: utf-8 -*-
import os
import json
import time
import threading
import requests
import anthropic
from http.server import HTTPServer, BaseHTTPRequestHandler
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from coinbase.rest import RESTClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ==================================================
# CONFIGURATION
# ==================================================
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY  = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET   = os.getenv("COINBASE_SECRET_KEY")

# ==================================================
# PORTEFEUILLE ALPACA
# HOLD 60%  : Claude choisit dynamiquement
# TRADE 40% : Long/Short, tout Alpaca, illimite/jour
# ==================================================
HOLD_PCT               = 0.60
DAYTRADE_PCT           = 0.40
MAX_HOLD_POSITIONS     = 8
MAX_DAYTRADE_POSITIONS = 4
STOCK_SL_PCT           = 3.0
STOCK_TP_PCT           = 6.0
STOCK_MIN_CONFIDENCE   = 72

DAYTRADE_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "INTC", "QCOM", "AVGO", "MU",
    "JPM", "BAC", "GS", "V", "MA", "PYPL",
    "LLY", "UNH", "PFE", "MRNA", "ABBV",
    "XOM", "CVX",
    "SHOP", "UBER", "ABNB", "COIN", "PLTR", "RBLX",
    "SNAP", "ROKU", "DKNG", "HOOD", "MELI",
    "QQQ", "SPY", "TQQQ", "SQQQ", "SPXL", "UVXY", "ARKK",
    "XLK", "XLF", "XLE", "XLV", "XLI",
]

# ==================================================
# PORTEFEUILLE COINBASE (EUR)
# HOLD STRICT 40% : BTC 25% | ETH 15% (jamais vendus)
# HOLD SOUPLE 10% : SOL 5% | XRP 3% | LINK 2%
# TRADE 50%       : Scalping actif 24/7
# ==================================================
CRYPTO_HOLD_STRICT = ["BTC-EUR", "ETH-EUR"]
CRYPTO_HOLD_SOUPLE = ["SOL-EUR", "XRP-EUR", "LINK-EUR"]
CRYPTO_HOLD_ALL    = CRYPTO_HOLD_STRICT + CRYPTO_HOLD_SOUPLE
CRYPTO_HOLD_ALLOC  = {
    "BTC-EUR":  0.25,
    "ETH-EUR":  0.15,
    "SOL-EUR":  0.05,
    "XRP-EUR":  0.03,
    "LINK-EUR": 0.02,
}

COINBASE_FEE_PCT              = 1.2   # frais aller-retour
CRYPTO_SL_PCT                 = 2.5   # stop loss net
CRYPTO_TP_PCT                 = 4.0   # take profit net rentable apres frais
TRAILING_STOP_PCT             = 3.0   # trailing stop elargi (evite les micro-ventes)
CRYPTO_RISK_PER_TRADE         = 0.15  # 15% du cash dispo par trade
MAX_CRYPTO_POSITIONS          = 8
CRYPTO_MIN_CONFIDENCE         = 65
CRYPTO_CANDLE_WINDOW_HOURS    = 24    # fenetre bougies 5min

CRYPTO_UNIVERSE_RAW = [
    "BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR",
    "ADA-EUR", "DOGE-EUR", "LTC-EUR", "DOT-EUR",
    "LINK-EUR", "AVAX-EUR", "UNI-EUR", "ATOM-EUR",
]

# ==================================================
# OBJECTIFS
# ==================================================
WEEKLY_GOAL_PCT  = 1.0
MONTHLY_GOAL_EUR = 100
ANNUAL_GOAL_PCT  = 20.0
DCA_MONTHLY_EUR  = 100
MEMORY_FILE      = "trade_memory.json"

INTERVAL_CRYPTO    = 20
INTERVAL_STOCKS    = 120
INTERVAL_RISK      = 30
INTERVAL_SCHEDULER = 60

# ==================================================
# CLIENTS
# ==================================================
try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    print("Alpaca TradingClient OK")
except Exception as e:
    print("Alpaca TradingClient error: " + str(e))
    trading_client = None

try:
    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    print("Alpaca DataClient OK")
except Exception as e:
    print("Alpaca DataClient error: " + str(e))
    data_client = None

try:
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print("Anthropic client OK")
except Exception as e:
    print("Anthropic client error: " + str(e))
    claude_client = None

try:
    coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
    print("Coinbase client OK")
except Exception as e:
    coinbase = None
    print("Coinbase init error: " + str(e))

# ==================================================
# ETAT GLOBAL
# ==================================================
trading_paused       = False
vacation_mode        = False
custom_alerts        = {}
active_stock_trades  = {}
active_crypto_trades = {}
_lock                = threading.RLock()

# ==================================================
# VALIDATION PRODUITS COINBASE
# ==================================================
def get_valid_products(retries=3, delay=5):
    if not coinbase:
        return set()
    for attempt in range(retries):
        try:
            response = coinbase.get_products()
            products = response.get("products", [])
            result   = {p["product_id"] for p in products if "product_id" in p}
            if result:
                return result
        except Exception as e:
            print("Erreur produits tentative " + str(attempt + 1) + ": " + str(e))
        if attempt < retries - 1:
            time.sleep(delay)
    return set()

VALID_PRODUCTS  = get_valid_products()
CRYPTO_UNIVERSE = [s for s in CRYPTO_UNIVERSE_RAW if s in VALID_PRODUCTS]
print(str(len(CRYPTO_UNIVERSE)) + " crypto actives: " + str(CRYPTO_UNIVERSE))

# ==================================================
# UTILITAIRES
# ==================================================
def progress_bar(current, goal, length=10):
    if goal == 0:
        return "." * length
    pct    = min(current / goal, 1.0)
    filled = int(pct * length)
    return ("#" * filled) + ("." * (length - filled)) + " " + str(int(pct * 100)) + "%"

def log(msg):
    print("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + str(msg))

def send_telegram(msg):
    try:
        requests.post(
            "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log("Telegram error: " + str(e))

# ==================================================
# MEMOIRE
# ==================================================
def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "trades": [],
        "hold_portfolio": {},
        "stats": {"wins": 0, "losses": 0, "total_pnl": 0},
        "monthly_stats": {},
        "annual_stats": {},
        "patterns": {},
        "errors": [],
        "equity_start": {}
    }

def save_memory(memory):
    with _lock:
        tmp = MEMORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(memory, f, indent=2)
        os.replace(tmp, MEMORY_FILE)

def record_trade(symbol, side, qty, price, pnl=None):
    with _lock:
        memory = load_memory()
        now    = datetime.now()
        month  = now.strftime("%Y-%m")
        year   = now.strftime("%Y")
        trade  = {
            "date":   now.strftime("%Y-%m-%d %H:%M"),
            "symbol": symbol,
            "side":   side,
            "qty":    qty,
            "price":  price,
            "pnl":    pnl
        }
        memory["trades"].append(trade)
        if pnl is not None:
            memory["stats"]["total_pnl"] += pnl
            if pnl > 0:
                memory["stats"]["wins"] += 1
            else:
                memory["stats"]["losses"] += 1
            ms = memory["monthly_stats"].setdefault(month, {"wins": 0, "losses": 0, "pnl": 0, "trades": []})
            ms["pnl"] += pnl
            if pnl > 0:
                ms["wins"] += 1
            else:
                ms["losses"] += 1
            ms["trades"].append(trade)
            ys = memory["annual_stats"].setdefault(year, {"wins": 0, "losses": 0, "pnl": 0})
            ys["pnl"] += pnl
            if pnl > 0:
                ys["wins"] += 1
            else:
                ys["losses"] += 1
            p = memory["patterns"].setdefault(symbol, {"wins": 0, "losses": 0, "total_pnl": 0})
            p["total_pnl"] += pnl
            if pnl > 0:
                p["wins"] += 1
            else:
                p["losses"] += 1
        memory["trades"] = memory["trades"][-200:]
        save_memory(memory)

def record_error(msg):
    memory = load_memory()
    memory["errors"].append({
        "date":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "error": str(msg)
    })
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def update_equity_checkpoints(equity):
    memory = load_memory()
    now    = datetime.now()
    es     = memory["equity_start"]
    for key, fmt in [("week", "%Y-%W"), ("month", "%Y-%m"), ("year", "%Y")]:
        k = now.strftime(fmt)
        if es.get(key + "_key") != k:
            es[key]           = equity
            es[key + "_key"]  = k
    memory["equity_start"] = es
    save_memory(memory)

def get_equity_checkpoints():
    return load_memory().get("equity_start", {})

def get_stats():
    m     = load_memory()
    s     = m["stats"]
    total = s["wins"] + s["losses"]
    result = dict(s)
    result["winrate"] = (s["wins"] / total * 100) if total > 0 else 0
    result["recent"]  = m["trades"][-5:]
    return result

def get_monthly_stats(month=None):
    if not month:
        month = datetime.now().strftime("%Y-%m")
    return load_memory()["monthly_stats"].get(month, {"wins": 0, "losses": 0, "pnl": 0, "trades": []})

def get_annual_stats(year=None):
    if not year:
        year = datetime.now().strftime("%Y")
    return load_memory()["annual_stats"].get(year, {"wins": 0, "losses": 0, "pnl": 0})

def get_winrate(symbol):
    p = load_memory()["patterns"].get(symbol)
    if not p:
        return None
    t = p["wins"] + p["losses"]
    return (p["wins"] / t * 100) if t > 0 else None

def get_best_worst(trades):
    w = [t for t in trades if t.get("pnl") is not None]
    if not w:
        return None, None
    return max(w, key=lambda x: x["pnl"]), min(w, key=lambda x: x["pnl"])

# ==================================================
# DONNEES MARCHE - ALPACA
# ==================================================
def is_market_open():
    now = datetime.utcnow()
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=20, minute=0,  second=0, microsecond=0)
    return open_time <= now <= close_time

def get_account_info():
    if not trading_client:
        return {"equity": 0, "cash": 0, "buying_power": 0, "pnl": 0}
    try:
        a = trading_client.get_account()
        return {
            "equity":       float(a.equity),
            "cash":         float(a.cash),
            "buying_power": float(a.buying_power),
            "pnl":          float(a.equity) - float(a.last_equity)
        }
    except Exception as e:
        log("get_account_info error: " + str(e))
        return {"equity": 0, "cash": 0, "buying_power": 0, "pnl": 0}

def get_positions():
    if not trading_client:
        return {}
    result = {}
    try:
        for p in trading_client.get_all_positions():
            result[p.symbol] = {
                "qty":       float(p.qty),
                "value":     float(p.market_value),
                "avg_price": float(p.avg_entry_price),
                "pnl":       float(p.unrealized_pl),
                "pnl_pct":   float(p.unrealized_plpc) * 100,
                "side":      "long" if float(p.qty) > 0 else "short"
            }
    except Exception as e:
        log("get_positions error: " + str(e))
    return result

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=ticker)
        )[ticker].close
    except Exception:
        return None

def get_market_perf(ticker):
    try:
        cur  = data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=ticker)
        )[ticker].close
        bars = list(data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=3)
        ))[ticker])
        if len(bars) >= 2:
            return ((cur - bars[-2].close) / bars[-2].close) * 100
        return 0
    except Exception:
        return 0

def get_spy_perf():
    return get_market_perf("SPY")

# ==================================================
# DONNEES MARCHE - COINBASE
# ==================================================
def get_crypto_price(symbol):
    try:
        if not coinbase:
            return None
        pb = coinbase.get_best_bid_ask(product_ids=[symbol])
        return float(pb["pricebooks"][0]["asks"][0]["price"])
    except Exception:
        return None

def get_crypto_balance(currency):
    try:
        if not coinbase:
            return 0
        for acc in coinbase.get_accounts()["accounts"]:
            if acc["currency"] == currency:
                return float(acc["available_balance"]["value"])
        return 0
    except Exception:
        return 0

# ==================================================
# ANALYSE TECHNIQUE - ACTIONS (daily bars)
# ==================================================
def get_historical_prices(ticker, days=60):
    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=days)
        )
        return [bar.close for bar in data_client.get_stock_bars(req)[ticker]]
    except Exception:
        return []

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains  = [max(prices[i] - prices[i - 1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i - 1] - prices[i], 0) for i in range(1, len(prices))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    return round(100 - (100 / (1 + ag / al)), 1)

def get_ta(ticker):
    prices = get_historical_prices(ticker)
    if not prices or len(prices) < 20:
        return None
    rsi  = calculate_rsi(prices)
    ma20 = sum(prices[-20:]) / 20
    ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else None
    cur  = prices[-1]
    return {
        "rsi":       rsi,
        "ma20":      ma20,
        "ma50":      ma50,
        "current":   cur,
        "trend":     "haussier" if (ma50 and ma20 > ma50) else "baissier",
        "above_ma20": cur > ma20,
        "week_perf": ((cur - prices[-6]) / prices[-6] * 100) if len(prices) >= 6 else None
    }

# ==================================================
# ANALYSE TECHNIQUE - CRYPTO (bougies 5min Coinbase)
# ==================================================
def get_crypto_ta(symbol):
    try:
        if not coinbase:
            return None
        end_ts   = int(time.time())
        start_ts = end_ts - CRYPTO_CANDLE_WINDOW_HOURS * 3600
        candles  = coinbase.get_candles(
            product_id=symbol,
            start=str(start_ts),
            end=str(end_ts),
            granularity="FIVE_MINUTE"
        )
        prices = [float(c["close"]) for c in candles.get("candles", [])]
        prices = prices[::-1]  # ordre chronologique
        if len(prices) < 55:  # On a besoin de plus de bougies pour la MA50
            return None
            
        rsi  = calculate_rsi(prices, period=14) # RSI lissé sur 14 périodes
        ma20 = sum(prices[-20:]) / 20
        ma50 = sum(prices[-50:]) / 50
        cur  = prices[-1]
        
        return {
            "rsi":       rsi,
            "ma20":      ma20,
            "ma50":      ma50,
            "current":   cur,
            "trend":     "haussier" if (cur > ma20) else "baissier",
            "strong_trend": (ma20 > ma50), # Tendance lourde validée !
            "above_ma20": cur > ma20,
            "week_perf": ((cur - prices[-7]) / prices[-7] * 100) if len(prices) >= 7 else None,
            "prices":    prices
        }
    except Exception:
        return None

def detect_breakout_setup(prices, threshold=0.03):
    if not prices or len(prices) < 20:
        return False
    recent_high = max(prices[-20:])
    return (recent_high - prices[-1]) / recent_high <= threshold

def format_ta(ta):
    if not ta:
        return "Donnees indisponibles"
    rsi_txt = ""
    if ta.get("rsi"):
        if ta["rsi"] < 30:
            label = "Survendu"
        elif ta["rsi"] > 70:
            label = "Surachete"
        else:
            label = "Neutre"
        rsi_txt = "RSI " + str(ta["rsi"]) + " " + label + "\n"
    wp = ("Perf : " + str(round(ta["week_perf"], 1)) + "%\n") if ta.get("week_perf") else ""
    ma_status = "OK" if ta.get("above_ma20") else "Attention"
    return rsi_txt + "Tendance : " + ta["trend"] + "\nMA20 : " + ma_status + "\n" + wp

# ==================================================
# NEWS
# ==================================================
def get_news(ticker, count=5):
    try:
        q = ticker.replace("-EUR", "").replace("-USD", "").replace("USDT", "")
        resp = requests.get(
            "https://newsapi.org/v2/everything?q=" + q +
            "&language=en&sortBy=publishedAt&pageSize=" + str(count) +
            "&apiKey=" + NEWS_API_KEY,
            timeout=10
        )
        return resp.json().get("articles", [])
    except Exception:
        return []

def format_news(articles, count=3):
    lines = ["- " + a["title"] for a in articles[:count]]
    return "\n".join(lines) if lines else "Aucune news recente"

# ==================================================
# CLAUDE IA
# ==================================================
PROMPT_HOLD = (
    "Tu es un gestionnaire de portefeuille long terme.\n"
    "Tu choisis dynamiquement les meilleurs actifs selon l'actu, les fondamentaux et le momentum.\n"
    "Horizons : court (semaines), moyen (mois), long (annees) selon l'opportunite.\n"
    "Univers : toutes les actions US, ETF sectoriels, ETF internationaux tradables sur Alpaca.\n"
    "Reponds UNIQUEMENT en JSON :\n"
    "{\"action\":\"BUY\"|\"SELL\"|\"HOLD\",\"symbol\":\"TICKER\","
    "\"horizon\":\"court\"|\"moyen\"|\"long\","
    "\"confidence\":0-100,\"reason\":\"francais court\",\"allocation_pct\":1-10}\n"
    "Si aucune opportunite : {\"action\":\"HOLD\",\"symbol\":\"\","
    "\"horizon\":\"\",\"confidence\":0,\"reason\":\"pas d opportunite\",\"allocation_pct\":0}"
)

PROMPT_STOCKS = (
    "Tu es un day trader professionnel - actions US.\n"
    "Long + Short selon le setup. RR minimum 1:2. Max 2% du capital par trade.\n"
    "Si tu as perdu recemment sur ce ticker, sois plus prudent.\n"
    "Reponds UNIQUEMENT en JSON :\n"
    "{\"action\":\"BUY\"|\"SHORT\"|\"HOLD\","
    "\"confidence\":0-100,\"reason\":\"francais court\","
    "\"risk_pct\":1-2,\"tp_pct\":3-15}"
)

def ask_claude(prompt, user_msg):
    try:
        res = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=prompt,
            messages=[{"role": "user", "content": user_msg}]
        )
        raw = res.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        record_error("Claude error: " + str(e))
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur Claude"}

# ==================================================
# ORDRES - ALPACA
# ==================================================
def place_order(symbol, side, qty, tp_pct=None, label=""):
    try:
        if side == "buy":
            order_side = OrderSide.BUY
        else:
            order_side = OrderSide.SELL
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=round(abs(qty), 4),
            side=order_side,
            time_in_force=TimeInForce.DAY
        ))
        price  = get_price(symbol)
        valeur = round(abs(qty) * price, 2) if price else "?"
        record_trade(symbol, side, round(abs(qty), 4), price or 0)
        tp = tp_pct or STOCK_TP_PCT
        if side == "buy":
            active_stock_trades[symbol] = {
                "side":   "long",
                "qty":    qty,
                "entry":  price,
                "tp_pct": tp
            }
            send_telegram(
                "<b>LONG " + label + "</b> <b>" + symbol + "</b>\n"
                "~$" + str(valeur) + "\n"
                "SL: -" + str(STOCK_SL_PCT) + "% | TP: +" + str(tp) + "%"
            )
        else:
            active_stock_trades.pop(symbol, None)
            send_telegram(
                "<b>Cloture " + label + "</b> <b>" + symbol + "</b> ~$" + str(valeur)
            )
    except Exception as e:
        record_error("Order " + symbol + ": " + str(e))
        send_telegram("<b>Ordre echoue</b> " + symbol + "\n" + str(e)[:100])

def open_short(symbol, qty, tp_pct=None):
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=round(abs(qty), 4),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        ))
        price  = get_price(symbol)
        valeur = round(abs(qty) * price, 2) if price else "?"
        tp     = tp_pct or STOCK_TP_PCT
        record_trade(symbol, "short", round(abs(qty), 4), price or 0)
        active_stock_trades[symbol] = {
            "side":   "short",
            "qty":    qty,
            "entry":  price,
            "tp_pct": tp
        }
        send_telegram(
            "<b>SHORT Day Trade</b> <b>" + symbol + "</b>\n"
            "~$" + str(valeur) + "\n"
            "SL: +" + str(STOCK_SL_PCT) + "% | TP: -" + str(tp) + "%"
        )
    except Exception as e:
        record_error("Short " + symbol + ": " + str(e))
        send_telegram("<b>Short echoue</b> " + symbol + "\n" + str(e)[:100])

# ==================================================
# ORDRES - COINBASE (EUR)
# ==================================================
def place_crypto_order(symbol, side, amount_eur, tp_pct=None, label="", reason=""):
    try:
        if not coinbase:
            return
        if side == "buy":
            coinbase.market_order_buy(
                client_order_id="bot_" + str(int(time.time())),
                product_id=symbol,
                quote_size=str(round(amount_eur, 2))
            )
        else:
            price = get_crypto_price(symbol)
            if not price:
                return
            coinbase.market_order_sell(
                client_order_id="bot_" + str(int(time.time())),
                product_id=symbol,
                base_size=str(round(amount_eur / price, 8))
            )
        price = get_crypto_price(symbol)
        record_trade(symbol, side, round(amount_eur / (price or 1), 8), price or 0)
        tp = tp_pct or CRYPTO_TP_PCT
        with _lock:
            if side == "buy":
                active_crypto_trades[symbol] = {
                    "side":       "long",
                    "amount":     amount_eur,
                    "entry":      price,
                    "peak":       price,
                    "tp_pct":     tp,
                    "reason":     reason,
                    "entry_time": datetime.utcnow().isoformat()
                }
                send_telegram(
                    "<b>LONG " + label + "</b> <b>" + symbol + "</b>\n"
                    "~" + str(round(amount_eur, 2)) + "EUR\n"
                    "SL: -" + str(CRYPTO_SL_PCT) + "% | TP: +" + str(tp) + "% (nets frais)"
                )
            else:
                active_crypto_trades.pop(symbol, None)
                send_telegram(
                    "<b>Vente " + label + "</b> <b>" + symbol + "</b> ~"
                    + str(round(amount_eur, 2)) + "EUR (frais deduits)"
                )
    except Exception as e:
        record_error("Crypto " + symbol + ": " + str(e))
        send_telegram("<b>Ordre crypto echoue</b> " + symbol + "\n" + str(e)[:100])

# ==================================================
# DETECTION POSITIONS EXISTANTES
# ==================================================
def check_existing_holdings():
    if not coinbase:
        return
    for symbol in CRYPTO_UNIVERSE:
        if symbol in CRYPTO_HOLD_STRICT:
            continue
        if symbol in active_crypto_trades:
            continue
        currency = symbol.replace("-EUR", "")
        balance  = get_crypto_balance(currency)
        if balance <= 0:
            continue
        price  = get_crypto_price(symbol)
        if not price:
            continue
        valeur = balance * price
        if valeur < 1.0:
            continue
        with _lock:
            active_crypto_trades[symbol] = {
                "side":       "long",
                "amount":     valeur,
                "entry":      price,
                "peak":       price,
                "tp_pct":     CRYPTO_TP_PCT,
                "reason":     "Position existante detectee",
                "entry_time": datetime.utcnow().isoformat()
            }
        log("Position detectee : " + symbol + " " + str(round(balance, 6)) + " = " + str(round(valeur, 2)) + "EUR")
        send_telegram(
            "<b>Position detectee</b> <b>" + symbol + "</b>\n"
            + str(round(balance, 6)) + " = ~" + str(round(valeur, 2)) + "EUR\n"
            "Ajoutee au suivi TP/SL"
        )

# ==================================================
# POCHE HOLD - ALPACA (Claude decide dynamiquement)
# ==================================================
def manage_hold_portfolio():
    if trading_paused or vacation_mode or not is_market_open():
        return
    account   = get_account_info()
    positions = get_positions()
    memory    = load_memory()
    hold_port = memory.get("hold_portfolio", {})
    hold_pos  = {s: d for s, d in positions.items() if s in hold_port}
    if len(hold_pos) >= MAX_HOLD_POSITIONS:
        return
    hold_capital  = account["equity"] * HOLD_PCT
    hold_invested = sum(d["value"] for d in hold_pos.values())
    available     = hold_capital - hold_invested
    if available < account["equity"] * 0.01:
        return

    signal = ask_claude(
        PROMPT_HOLD,
        "Capital hold disponible: $" + str(round(available, 2)) + "\n"
        "SPY: " + str(round(get_spy_perf(), 2)) + "%\n"
        "News: " + format_news(get_news("stocks market economy", count=3)) + "\n"
        "Positions hold: " + str(list(hold_port.keys()) or "aucune") + "\n"
        "Quel actif ajouter ou renforcer ?"
    )
    if signal.get("action") == "HOLD" or not signal.get("symbol"):
        return
    symbol  = signal["symbol"].upper()
    conf    = signal.get("confidence", 0)
    reason  = signal.get("reason", "")
    horizon = signal.get("horizon", "moyen")
    alloc   = signal.get("allocation_pct", 3) / 100
    if conf < STOCK_MIN_CONFIDENCE:
        return

    if signal["action"] == "BUY":
        amount = available * alloc
        price  = get_price(symbol)
        if not price or amount < 1:
            return
        memory["hold_portfolio"][symbol] = {
            "horizon": horizon,
            "entry":   price,
            "date":    datetime.now().strftime("%Y-%m-%d")
        }
        save_memory(memory)
        send_telegram(
            "<b>HOLD " + horizon.upper() + "</b>\n"
            "<b>" + symbol + "</b> $" + str(round(price, 2)) + "\n"
            + reason + "\nConfiance : " + str(conf) + "%"
        )
        place_order(symbol, "buy", amount / price, label="Hold")

    elif signal["action"] == "SELL" and symbol in hold_pos:
        memory["hold_portfolio"].pop(symbol, None)
        save_memory(memory)
        send_telegram("<b>Sortie Hold</b> <b>" + symbol + "</b>\n" + reason)
        place_order(symbol, "sell", hold_pos[symbol]["qty"], label="Hold")

# ==================================================
# DAY TRADING - ALPACA (Claude + TA)
# ==================================================
def scan_stocks():
    if trading_paused or vacation_mode or not is_market_open():
        return
    account   = get_account_info()
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if len(trade_pos) >= MAX_DAYTRADE_POSITIONS:
        return
    trade_capital = account["equity"] * DAYTRADE_PCT

    for ticker in DAYTRADE_UNIVERSE:
        if ticker in positions:
            continue
        price = get_price(ticker)
        if not price:
            continue
        ta       = get_ta(ticker)
        articles = get_news(ticker, count=3)
        wr       = get_winrate(ticker)
        wr_txt   = ("Winrate: " + str(round(wr, 0)) + "%\n") if wr else ""

        signal = ask_claude(
            PROMPT_STOCKS,
            "Ticker: " + ticker + " | Prix: $" + str(round(price, 2)) + "\n"
            "TA:\n" + format_ta(ta) + "\n"
            "News:\n" + format_news(articles) + "\n"
            + wr_txt
        )
        action = signal.get("action", "HOLD")
        conf   = signal.get("confidence", 0)
        reason = signal.get("reason", "")
        tp_pct = signal.get("tp_pct", STOCK_TP_PCT)
        risk   = signal.get("risk_pct", 1)
        if conf < STOCK_MIN_CONFIDENCE:
            continue
        qty = (trade_capital * risk / 100) / price

        if action == "BUY" and account["cash"] >= qty * price:
            send_telegram(
                "<b>Signal LONG</b>\n<b>" + ticker + "</b> $" + str(round(price, 2)) + "\n"
                + reason + "\nConfiance : " + str(conf) + "% | TP: +" + str(tp_pct) + "%"
            )
            place_order(ticker, "buy", qty, tp_pct=tp_pct, label="Day Trade")
            break
        elif action == "SHORT":
            send_telegram(
                "<b>Signal SHORT</b>\n<b>" + ticker + "</b> $" + str(round(price, 2)) + "\n"
                + reason + "\nConfiance : " + str(conf) + "% | TP: -" + str(tp_pct) + "%"
            )
            open_short(ticker, qty, tp_pct=tp_pct)
            break
        time.sleep(0.5)

# ==================================================
# GESTION DU DRAWDOWN DYNAMIQUE (DISJONCTEUR)
# ==================================================
def get_crypto_capital():
    # Calcule tout l'argent sur Coinbase (Cash + Cryptos)
    total = get_crypto_balance("EUR")
    for symbol in set(CRYPTO_UNIVERSE + CRYPTO_HOLD_ALL):
        currency = symbol.replace("-EUR", "")
        bal = get_crypto_balance(currency)
        if bal > 0:
            price = get_crypto_price(symbol)
            if price:
                total += bal * price
    return total

def check_circuit_breaker():
    current_capital = get_crypto_capital()
    if current_capital <= 0:
        return False
        
    memory = load_memory()
    peak = memory.get("crypto_peak", current_capital)
    
    # Si on est plus riche qu'avant, on enregistre le nouveau record !
    if current_capital > peak:
        memory["crypto_peak"] = current_capital
        save_memory(memory)
        return False
        
    # Calcul de la chute en pourcentage
    drawdown_pct = ((peak - current_capital) / peak) * 100
    
    # Paliers de chute acceptables selon le capital
    if peak < 100:
        max_drop = 50.0   # -50% autorisé si < 100€
    elif peak < 1000:
        max_drop = 30.0   # -30% autorisé si < 1000€
    elif peak < 5000:
        max_drop = 20.0   # -20% autorisé si < 5000€
    else:
        max_drop = 15.0   # -15% autorisé au-delà
        
    if drawdown_pct >= max_drop:
        return True
    return False

# ==================================================
# DAY TRADING - CRYPTO SCALPING 24/7
# ==================================================
def scan_crypto():
    if trading_paused or vacation_mode or not coinbase:
        return
        
    # NOUVEAU DISJONCTEUR INTELLIGENT
    if check_circuit_breaker():
        log("Circuit breaker: Chute du capital max atteinte, pause.")
        return

    if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS:
        return
    
    cash_eur = get_crypto_balance("EUR")
    if cash_eur < 5:
        return

    for symbol in CRYPTO_UNIVERSE:
        if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS:
            break
        if symbol in active_crypto_trades:
            continue
        price = get_crypto_price(symbol)
        ta    = get_crypto_ta(symbol)
        if not price or not ta or not ta.get("rsi"):
            continue
        if ta.get("week_perf") is None or abs(ta["week_perf"]) < 0.1:
            continue

        rsi          = ta["rsi"]
        strong_trend = ta.get("strong_trend", False)
        prices       = ta.get("prices", [])
        has_setup    = detect_breakout_setup(prices)

        # STRATÉGIE 1 : Dip Buy sécurisé (On achète un creux UNIQUEMENT si la tendance de fond est haussière)
        dip_buy = (rsi < 40 and strong_trend and price > ta["ma50"])
        
        # STRATÉGIE 2 : Breakout confirmé (Le prix casse une résistance AVEC une tendance de fond forte et du momentum)
        breakout = (50 <= rsi <= 70 and strong_trend and has_setup)

        if dip_buy or breakout:
            amount = cash_eur * CRYPTO_RISK_PER_TRADE
            if amount < 2:
                continue
                
            strat_name = "Rebond" if dip_buy else "Breakout"
            reason = strat_name + " confirmé | RSI=" + str(round(rsi, 0)) + " | Trend de fond OK"
            place_crypto_order(symbol, "buy", amount, tp_pct=CRYPTO_TP_PCT, label="Scalping", reason=reason)
            
        time.sleep(0.3)

# ==================================================
# GESTION DU RISQUE - ACTIONS
# ==================================================
def check_stock_risk():
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    for symbol, data in positions.items():
        if symbol in hold_syms:
            continue
        pnl_pct = data["pnl_pct"]
        trade   = active_stock_trades.get(symbol, {})
        tp_pct  = trade.get("tp_pct", STOCK_TP_PCT)
        side    = trade.get("side", "long")
        if side == "long":
            if pnl_pct <= -STOCK_SL_PCT:
                send_telegram("<b>Stop Loss</b> <b>" + symbol + "</b> -" + str(round(abs(pnl_pct), 1)) + "%")
                place_order(symbol, "sell", data["qty"], label="SL")
            elif pnl_pct >= tp_pct:
                send_telegram("<b>Take Profit</b> <b>" + symbol + "</b> +" + str(round(pnl_pct, 1)) + "%")
                place_order(symbol, "sell", data["qty"], label="TP")
        elif side == "short":
            if pnl_pct <= -STOCK_SL_PCT:
                send_telegram("<b>Stop Loss SHORT</b> <b>" + symbol + "</b> -" + str(round(abs(pnl_pct), 1)) + "%")
                place_order(symbol, "buy", abs(data["qty"]), label="SL")
            elif pnl_pct >= tp_pct:
                send_telegram("<b>Take Profit SHORT</b> <b>" + symbol + "</b> +" + str(round(pnl_pct, 1)) + "%")
                place_order(symbol, "buy", abs(data["qty"]), label="TP")

# ==================================================
# GESTION DU RISQUE - CRYPTO
# ==================================================
def check_crypto_risk():
    if not coinbase:
        return
    for symbol, trade in list(active_crypto_trades.items()):
        if symbol in CRYPTO_HOLD_STRICT:
            continue
            
        currency      = symbol.replace("-EUR", "")
        balance       = get_crypto_balance(currency)
        
        # Auto-nettoyage de la mémoire si la crypto a été vendue manuellement ou est vide
        if balance <= 0:
            with _lock:
                active_crypto_trades.pop(symbol, None)
            continue
            
        price = get_crypto_price(symbol)
        if not price:
            continue
            
        entry         = trade.get("entry", price)
        tp_pct        = trade.get("tp_pct", CRYPTO_TP_PCT)
        gross_pnl_pct = ((price - entry) / entry * 100) if entry else 0
        net_pnl_pct   = gross_pnl_pct - COINBASE_FEE_PCT

        with _lock:
            current_peak = trade.get("peak", entry)
            if price > current_peak:
                active_crypto_trades[symbol]["peak"] = price
                current_peak = price

        trailing_drop = ((current_peak - price) / current_peak * 100) if current_peak else 0

        if net_pnl_pct <= -CRYPTO_SL_PCT:
            send_telegram(
                "<b>Stop Loss crypto</b> " + symbol +
                " (Net: -" + str(round(abs(net_pnl_pct), 1)) + "%)"
            )
            place_crypto_order(symbol, "sell", balance * price, label="SL")
        elif trailing_drop >= TRAILING_STOP_PCT and net_pnl_pct > 0:
            send_telegram(
                "<b>Trailing Stop</b> " + symbol +
                " (-" + str(round(trailing_drop, 1)) + "% depuis pic)"
            )
            place_crypto_order(symbol, "sell", balance * price, label="TS")
        elif net_pnl_pct >= tp_pct:
            send_telegram(
                "<b>Take Profit crypto</b> " + symbol +
                " (Net: +" + str(round(net_pnl_pct, 1)) + "%)"
            )
            place_crypto_order(symbol, "sell", balance * price, label="TP")

def check_market_health():
    global trading_paused
    spy = get_spy_perf()
    if spy <= -10:
        send_telegram("<b>CRASH !</b> SPY " + str(round(spy, 1)) + "%\nTape /urgence")
    elif spy <= -5:
        trading_paused = True
        send_telegram("<b>Forte baisse SPY " + str(round(spy, 1)) + "%</b>\nDay trading suspendu.")
    elif spy <= -3:
        send_telegram("Marche sous tension SPY " + str(round(spy, 1)) + "%")

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        if "-EUR" in symbol:
            price = get_crypto_price(symbol)
        else:
            price = get_price(symbol)
        if price and price >= target:
            send_telegram("<b>ALERTE !</b> <b>" + symbol + "</b> atteint " + str(round(price, 2)))
            del custom_alerts[symbol]

# ==================================================
# DCA MENSUEL
# ==================================================
def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("DCA annule - pause.")
        return
    send_telegram("<b>DCA mensuel</b> en cours...")
    dca_eur = DCA_MONTHLY_EUR
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        amount = dca_eur * 0.70 * alloc
        if amount >= 1:
            place_crypto_order(symbol, "buy", amount, label="DCA")
    signal = ask_claude(
        PROMPT_HOLD,
        "DCA mensuel de $" + str(round(dca_eur * 0.30, 2)) + " USD disponible.\n"
        "Positions hold: " + str(list(load_memory().get("hold_portfolio", {}).keys())) + "\n"
        "Quel actif renforcer ce mois-ci ?"
    )
    if signal.get("action") == "BUY" and signal.get("symbol"):
        price = get_price(signal["symbol"].upper())
        if price:
            place_order(signal["symbol"].upper(), "buy", (dca_eur * 0.30) / price, label="DCA")

# ==================================================
# RAPPORTS
# ==================================================
def send_daily_report(immediate=False):
    account   = get_account_info()
    positions = get_positions()
    stats     = get_stats()
    spy       = get_spy_perf()
    checkpts  = get_equity_checkpoints()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    hold_pos  = {s: d for s, d in positions.items() if s in hold_syms}
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    wk        = checkpts.get("week", account["equity"])
    wk_pnl    = account["equity"] - wk
    wk_goal   = wk * WEEKLY_GOAL_PCT / 100
    btc_val   = get_crypto_balance("BTC") * (get_crypto_price("BTC-EUR") or 0)
    eth_val   = get_crypto_balance("ETH") * (get_crypto_price("ETH-EUR") or 0)
    cash_eur  = get_crypto_balance("EUR")
    titre     = "<b>Rapport immediat</b>" if immediate else "<b>Rapport du soir</b>"

    r = titre + "\n" + "=" * 22 + "\n\n"
    r += "Actions : <b>$" + str(round(account["equity"], 2)) + "</b>\n"
    r += "Crypto hold : ~" + str(round(btc_val + eth_val, 2)) + "EUR\n"
    r += "Cash USD : $" + str(round(account["cash"], 2)) + " | EUR : " + str(round(cash_eur, 2)) + "EUR\n"
    pnl_sign = "+" if account["pnl"] >= 0 else ""
    r += "Aujourd hui : $" + pnl_sign + str(round(account["pnl"], 2)) + " | SPY : " + str(round(spy, 2)) + "%\n\n"

    r += "<b>Hold (" + str(int(HOLD_PCT * 100)) + "%) - " + str(len(hold_pos)) + " pos</b>\n"
    for s, d in hold_pos.items():
        hp   = load_memory()["hold_portfolio"].get(s, {})
        sign = "+" if d["pnl_pct"] >= 0 else ""
        r   += "  <b>" + s + "</b> $" + str(round(d["value"], 2)) + " (" + sign + str(round(d["pnl_pct"], 2)) + "%) [" + hp.get("horizon", "?") + "]\n"
    if not hold_pos:
        r += "  (Claude cherche)\n"

    r += "\n<b>Day Trade (" + str(int(DAYTRADE_PCT * 100)) + "%) - " + str(len(trade_pos)) + " pos</b>\n"
    for s, d in trade_pos.items():
        t    = active_stock_trades.get(s, {})
        sign = "+" if d["pnl_pct"] >= 0 else ""
        r   += "  <b>" + s + "</b> $" + str(round(d["value"], 2)) + " (" + sign + str(round(d["pnl_pct"], 2)) + "%) TP:+" + str(t.get("tp_pct", STOCK_TP_PCT)) + "%\n"
    if not trade_pos:
        r += "  (aucune)\n"

    r += "\n<b>Crypto scalping - " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + " pos</b>\n"
    for s, t in active_crypto_trades.items():
        entry   = t.get("entry") or 0
        price   = get_crypto_price(s) or entry
        net_pnl = ((price - entry) / entry * 100 - COINBASE_FEE_PCT) if entry else 0
        sign    = "+" if net_pnl >= 0 else ""
        r      += "  <b>" + s + "</b> " + str(round(t["amount"], 2)) + "EUR (" + sign + str(round(net_pnl, 1)) + "% net)\n"

    r += "\nSemaine :\n" + progress_bar(max(wk_pnl, 0), wk_goal) + " $" + ("+" if wk_pnl >= 0 else "") + str(round(wk_pnl, 2)) + "\n"
    r += "\nReussite : " + str(round(stats["winrate"], 0)) + "% | PnL : $" + str(round(stats["total_pnl"], 2)) + "\n"
    status = "vacances" if vacation_mode else "pause" if trading_paused else "actif"
    market = "ouvert" if is_market_open() else "ferme"
    r += "Bot: " + status + " | Marche: " + market
    send_telegram(r)

def send_weekly_report():
    account   = get_account_info()
    stats     = get_stats()
    spy       = get_spy_perf()
    checkpts  = get_equity_checkpoints()
    memory    = load_memory()
    wk_ago    = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    wk_trades = [t for t in memory["trades"] if t["date"] >= wk_ago]
    best, worst = get_best_worst(wk_trades)
    wk = checkpts.get("week",  account["equity"])
    mo = checkpts.get("month", account["equity"])
    yr = checkpts.get("year",  account["equity"])
    wk_pnl = account["equity"] - wk
    mo_pnl = account["equity"] - mo
    yr_pnl = account["equity"] - yr
    vs_spy = account["pnl"] - (account["equity"] * spy / 100)

    r  = "<b>RESUME SEMAINE</b>\n" + "=" * 22 + "\n\n"
    r += "<b>$" + str(round(account["equity"], 2)) + "</b> | "
    r += ("+" if wk_pnl >= 0 else "") + "$" + str(round(wk_pnl, 2)) + "\n"
    r += "SPY : " + str(round(spy, 2)) + "% | "
    r += ("Je bats le marche !" if vs_spy > 0 else "Marche > moi") + "\n\n"
    r += "Semaine : " + progress_bar(max(wk_pnl, 0), wk * WEEKLY_GOAL_PCT / 100) + " $" + ("+" if wk_pnl >= 0 else "") + str(round(wk_pnl, 2)) + "\n"
    r += "Mois    : " + progress_bar(max(mo_pnl, 0), MONTHLY_GOAL_EUR) + " $" + ("+" if mo_pnl >= 0 else "") + str(round(mo_pnl, 2)) + "\n"
    r += "Annee   : " + progress_bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + " $" + ("+" if yr_pnl >= 0 else "") + str(round(yr_pnl, 2)) + "\n\n"
    r += str(len(wk_trades)) + " trades | " + str(round(stats["winrate"], 0)) + "% reussite\n"
    if best and best.get("pnl"):
        r += "Meilleur: " + best["symbol"] + " +$" + str(round(best["pnl"], 2)) + "\n"
    if worst and worst.get("pnl"):
        r += "Pire: " + worst["symbol"] + " $" + str(round(worst["pnl"], 2)) + "\n"
    r += "\nBonne semaine !"
    send_telegram(r)

def send_monthly_report():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    month    = datetime.now().strftime("%Y-%m")
    ms       = get_monthly_stats(month)
    mo       = checkpts.get("month", account["equity"])
    yr       = checkpts.get("year",  account["equity"])
    mo_pnl   = account["equity"] - mo
    yr_pnl   = account["equity"] - yr
    total_m  = ms["wins"] + ms["losses"]

    r  = "<b>BILAN " + datetime.now().strftime("%B %Y").upper() + "</b>\n" + "=" * 22 + "\n\n"
    r += "<b>$" + str(round(account["equity"], 2)) + "</b> | Ce mois : $" + ("+" if mo_pnl >= 0 else "") + str(round(mo_pnl, 2)) + "\n\n"
    r += "Mois  : " + progress_bar(max(mo_pnl, 0), MONTHLY_GOAL_EUR) + " $" + ("+" if mo_pnl >= 0 else "") + str(round(mo_pnl, 2)) + "\n"
    r += "Annee : " + progress_bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + " $" + ("+" if yr_pnl >= 0 else "") + str(round(yr_pnl, 2)) + "\n\n"
    r += str(len(ms.get("trades", []))) + " trades"
    if total_m > 0:
        r += " | " + str(round(ms["wins"] / total_m * 100, 0)) + "% reussite"
    if mo_pnl > 0:
        r += "\nImpot estime (30%) : ~$" + str(round(mo_pnl * 0.30, 2))
    send_telegram(r)

def send_annual_report():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    year     = str(datetime.now().year)
    ys       = get_annual_stats(year)
    yr       = checkpts.get("year", account["equity"])
    yr_pnl   = account["equity"] - yr
    total_y  = ys["wins"] + ys["losses"]
    proj_5y  = account["equity"] * ((1 + yr_pnl / max(yr, 1)) ** 5)

    r  = "<b>BILAN ANNUEL " + year + "</b>\n" + "=" * 22 + "\n\n"
    r += "<b>$" + str(round(account["equity"], 2)) + "</b> | PnL : $" + ("+" if yr_pnl >= 0 else "") + str(round(yr_pnl, 2)) + "\n\n"
    r += "Objectif +" + str(ANNUAL_GOAL_PCT) + "% :\n"
    r += progress_bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + " $" + ("+" if yr_pnl >= 0 else "") + str(round(yr_pnl, 2)) + "\n\n"
    r += str(total_y) + " trades"
    if total_y > 0:
        r += " | " + str(round(ys["wins"] / total_y * 100, 0)) + "% reussite"
    r += "\nProjection 5 ans : ~$" + str(round(proj_5y, 2)) + "\n"
    if yr_pnl > 0:
        r += "Impot estime (30%) : ~$" + str(round(yr_pnl * 0.30, 2)) + "\n"
    r += "\nBonne annee !"
    send_telegram(r)

def send_morning_briefing():
    account   = get_account_info()
    btc_price = get_crypto_price("BTC-EUR") or 0
    eth_price = get_crypto_price("ETH-EUR") or 0
    cash_eur  = get_crypto_balance("EUR")
    spy       = get_spy_perf()
    checkpts  = get_equity_checkpoints()
    wk        = checkpts.get("week", account["equity"])
    wk_pnl    = account["equity"] - wk
    wk_goal   = wk * WEEKLY_GOAL_PCT / 100

    r  = "<b>BRIEFING MATIN</b>\n" + "=" * 22 + "\n\n"
    r += "$" + str(round(account["equity"], 2)) + " | Cash EUR : " + str(round(cash_eur, 2)) + "EUR\n"
    r += "SPY : " + str(round(spy, 2)) + "%\n\n"
    r += "BTC : " + str(round(btc_price, 2)) + "EUR | ETH : " + str(round(eth_price, 2)) + "EUR\n"
    r += "Scalping actif : " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + "\n\n"
    r += "Semaine : " + progress_bar(max(wk_pnl, 0), wk_goal) + " $" + ("+" if wk_pnl >= 0 else "") + str(round(wk_pnl, 2)) + "\n"
    if spy > 0.5:
        sentiment = "Favorable"
    elif spy < -0.5:
        sentiment = "Defavorable"
    else:
        sentiment = "Neutre"
    r += "\nSentiment : " + sentiment + "\nC est parti !"
    send_telegram(r)

# ==================================================
# COMMANDES TELEGRAM
# ==================================================
def cmd_aide():
    send_telegram(
        "<b>Commandes</b>\n\n"
        "/status | /positions | /hold\n"
        "/crypto | /report | /historique\n"
        "/marche | /objectifs\n"
        "/technique NVDA\n"
        "/pourquoi BTC-EUR\n\n"
        "/briefing | /semaine\n"
        "/mois | /annee\n\n"
        "/pause | /resume\n"
        "/vacances | /retour\n\n"
        "/alerte BTC-EUR 90000\n"
        "/alertes\n"
        "/scan_holdings\n\n"
        "/urgence - Ferme les trades\n"
        "(poche hold conservee)"
    )

def cmd_pourquoi(symbol):
    symbol = symbol.upper()
    trade  = active_crypto_trades.get(symbol)
    if trade:
        send_telegram(
            "<b>Raisonnement " + symbol + " :</b>\n\n" +
            trade.get("reason", "Non sauvegarde.")
        )
    else:
        send_telegram("Aucun trade actif pour " + symbol + ".")

def cmd_status():
    account   = get_account_info()
    stats     = get_stats()
    spy       = get_spy_perf()
    ms        = get_monthly_stats()
    ys        = get_annual_stats()
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    hold_pos  = {s: d for s, d in positions.items() if s in hold_syms}
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    checkpts  = get_equity_checkpoints()
    wk        = checkpts.get("week", account["equity"])
    wk_pnl    = account["equity"] - wk
    wk_goal   = wk * WEEKLY_GOAL_PCT / 100
    btc_val   = get_crypto_balance("BTC") * (get_crypto_price("BTC-EUR") or 0)
    eth_val   = get_crypto_balance("ETH") * (get_crypto_price("ETH-EUR") or 0)
    cash_eur  = get_crypto_balance("EUR")

    pnl_sign = "+" if account["pnl"] >= 0 else ""
    send_telegram(
        "<b>Portefeuille</b>\n\n"
        "Actions : <b>$" + str(round(account["equity"], 2)) + "</b>\n"
        "Crypto hold : ~" + str(round(btc_val + eth_val, 2)) + "EUR\n"
        "Cash USD : $" + str(round(account["cash"], 2)) + " | EUR : " + str(round(cash_eur, 2)) + "EUR\n"
        "Aujourd hui : $" + pnl_sign + str(round(account["pnl"], 2)) + "\n\n"
        "Hold : " + str(len(hold_pos)) + " pos | Trade : " + str(len(trade_pos)) + " pos | Scalping : " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + "\n\n"
        "Mois : $" + ("+" if ms["pnl"] >= 0 else "") + str(round(ms["pnl"], 2)) + " | Annee : $" + ("+" if ys["pnl"] >= 0 else "") + str(round(ys["pnl"], 2)) + "\n\n"
        "Semaine :\n" + progress_bar(max(wk_pnl, 0), wk_goal) + " $" + ("+" if wk_pnl >= 0 else "") + str(round(wk_pnl, 2)) + "\n\n"
        "Reussite : " + str(round(stats["winrate"], 0)) + "% | SPY : " + str(round(spy, 2)) + "%\n"
        "Bot: " + ("vacances" if vacation_mode else "pause" if trading_paused else "actif") +
        " | Marche: " + ("ouvert" if is_market_open() else "ferme")
    )

def cmd_hold():
    memory    = load_memory()
    hold_port = memory.get("hold_portfolio", {})
    positions = get_positions()
    if not hold_port:
        send_telegram("Poche hold vide - Claude cherche.")
        return
    msg = "<b>Poche HOLD</b>\n\n"
    for s, info in hold_port.items():
        pos  = positions.get(s, {})
        pnl  = (" (" + ("+" if pos.get("pnl_pct", 0) >= 0 else "") + str(round(pos["pnl_pct"], 2)) + "%)") if pos else ""
        msg += "<b>" + s + "</b> [" + info.get("horizon", "?") + "] depuis " + info.get("date", "?") + pnl + "\n"
    send_telegram(msg)

def cmd_positions():
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if not trade_pos:
        send_telegram("Aucun day trade actif.")
        return
    msg = "<b>Day Trades actifs</b>\n\n"
    for s, d in trade_pos.items():
        t    = active_stock_trades.get(s, {})
        side = t.get("side", "long")
        sign = "+" if d["pnl_pct"] >= 0 else ""
        msg += "[" + side + "] <b>" + s + "</b> $" + str(round(d["value"], 2)) + " (" + sign + str(round(d["pnl_pct"], 2)) + "%) TP:+" + str(t.get("tp_pct", STOCK_TP_PCT)) + "%\n"
    send_telegram(msg)

def cmd_crypto():
    if not coinbase:
        send_telegram("Coinbase non connecte.")
        return
    lines = []
    total = 0
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        currency = symbol.replace("-EUR", "")
        price    = get_crypto_price(symbol) or 0
        balance  = get_crypto_balance(currency)
        val      = balance * price
        total   += val
        strict   = "HOLD strict" if symbol in CRYPTO_HOLD_STRICT else "HOLD souple"
        lines.append("<b>" + currency + "</b> " + str(round(balance, 6)) + " = " + str(round(val, 2)) + "EUR [" + strict + "]")
    cash_eur = get_crypto_balance("EUR")
    msg  = "<b>Poche Hold Crypto</b>\n\n" + "\n".join(lines)
    msg += "\n\nTotal hold : ~" + str(round(total, 2)) + "EUR\n"
    msg += "Cash EUR : " + str(round(cash_eur, 2)) + "EUR\n"
    msg += "\nScalping actif : " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + " pos"
    if active_crypto_trades:
        msg += "\n"
        for s, t in active_crypto_trades.items():
            entry   = t.get("entry") or 0
            price   = get_crypto_price(s) or entry
            net_pnl = ((price - entry) / entry * 100 - COINBASE_FEE_PCT) if entry else 0
            sign    = "+" if net_pnl >= 0 else ""
            msg    += "  <b>" + s + "</b> " + str(round(t["amount"], 2)) + "EUR (" + sign + str(round(net_pnl, 1)) + "% net)\n"
    send_telegram(msg)

def cmd_marche():
    spy  = get_spy_perf()
    intl = []
    for ticker, name in [("EWJ", "Japon"), ("FXI", "Chine"), ("EWG", "Allemagne"), ("EWU", "UK")]:
        p = get_market_perf(ticker)
        intl.append(name + " : " + ("+" if p >= 0 else "") + str(round(p, 2)) + "%")
    msg  = "<b>Marches</b>\n\nSPY : " + ("+" if spy >= 0 else "") + str(round(spy, 2)) + "%\n\n"
    msg += "\n".join(intl)
    msg += "\n\nMarche : " + ("ouvert" if is_market_open() else "ferme")
    send_telegram(msg)

def cmd_technique(ticker):
    ta    = get_ta(ticker)
    price = get_price(ticker)
    if not ta or not price:
        send_telegram("Impossible d analyser " + ticker + ".")
        return
    wr  = get_winrate(ticker)
    msg = "<b>" + ticker + "</b> $" + str(round(price, 2)) + "\n\n" + format_ta(ta)
    if wr:
        msg += "Reussite : " + str(round(wr, 0)) + "%"
    send_telegram(msg)

def cmd_objectifs():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    wk = checkpts.get("week",  account["equity"])
    mo = checkpts.get("month", account["equity"])
    yr = checkpts.get("year",  account["equity"])
    wk_pnl = account["equity"] - wk
    mo_pnl = account["equity"] - mo
    yr_pnl = account["equity"] - yr
    send_telegram(
        "<b>Objectifs</b>\n\n"
        "Semaine (+" + str(WEEKLY_GOAL_PCT) + "%) :\n" +
        progress_bar(max(wk_pnl, 0), wk * WEEKLY_GOAL_PCT / 100) +
        "\n$" + ("+" if wk_pnl >= 0 else "") + str(round(wk_pnl, 2)) + "\n\n"
        "Mois (+" + str(MONTHLY_GOAL_EUR) + "EUR) :\n" +
        progress_bar(max(mo_pnl, 0), MONTHLY_GOAL_EUR) +
        "\n$" + ("+" if mo_pnl >= 0 else "") + str(round(mo_pnl, 2)) + "\n\n"
        "Annee (+" + str(ANNUAL_GOAL_PCT) + "%) :\n" +
        progress_bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) +
        "\n$" + ("+" if yr_pnl >= 0 else "") + str(round(yr_pnl, 2))
    )

def cmd_historique():
    stats = get_stats()
    if not stats["recent"]:
        send_telegram("Aucun trade.")
        return
    msg = "<b>5 derniers trades</b>\n\n"
    for t in reversed(stats["recent"]):
        pnl  = (" | $" + ("+" if (t.get("pnl") or 0) >= 0 else "") + str(round(t["pnl"], 2))) if t.get("pnl") else ""
        msg += t["date"] + " - " + t["side"].upper() + " <b>" + t["symbol"] + "</b> @ $" + str(round(t["price"], 2)) + pnl + "\n"
    msg += "\n" + str(round(stats["winrate"], 0)) + "% | PnL : $" + str(round(stats["total_pnl"], 2))
    send_telegram(msg)

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram("<b>Pause</b>\nStop loss actif. Tape /resume.")

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False
    vacation_mode  = False
    
    # On réinitialise le record de capital pour débloquer le disjoncteur
    memory = load_memory()
    memory["crypto_peak"] = get_crypto_capital()
    save_memory(memory)
    
    send_telegram("<b>Trading repris !</b>\nDisjoncteur réinitialisé.")

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions    = get_positions()
    hold_syms    = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos    = {s: d for s, d in positions.items() if s not in hold_syms}
    crypto_count = len([s for s in active_crypto_trades if s not in CRYPTO_HOLD_STRICT])
    if not trade_pos and not crypto_count:
        send_telegram("Aucun trade actif.\nPoche hold conservee.")
        return
    send_telegram(
        "<b>URGENCE</b>\nFermeture " + str(len(trade_pos)) + " action(s) + " +
        str(crypto_count) + " crypto(s)...\nHold conserve."
    )
    for s, d in trade_pos.items():
        place_order(s, "sell", abs(d["qty"]), label="URGENCE")
    for symbol, trade in list(active_crypto_trades.items()):
        if symbol in CRYPTO_HOLD_STRICT:
            continue
        currency = symbol.replace("-EUR", "")
        balance  = get_crypto_balance(currency)
        price    = get_crypto_price(symbol) or 0
        if balance > 0 and price > 0:
            place_crypto_order(symbol, "sell", balance * price, label="URGENCE")
    send_telegram("Trades fermes.\nTape /resume.")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode  = True
    trading_paused = True
    send_telegram("<b>Mode vacances</b>\nHold conserve\nStop loss actif\nAucun nouveau trade\nTape /retour !")

def cmd_retour():
    global vacation_mode, trading_paused
    vacation_mode  = False
    trading_paused = False
    send_telegram("<b>Bon retour !</b>")
    send_daily_report(immediate=True)

def cmd_alerte(args):
    try:
        symbol = args[0].upper()
        target = float(args[1])
        custom_alerts[symbol] = target
        send_telegram("Alerte : <b>" + symbol + "</b> -> " + str(target))
    except Exception:
        send_telegram("Format : /alerte BTC-EUR 90000")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("Aucune alerte.")
        return
    msg = "<b>Alertes actives</b>\n\n"
    for s, t in custom_alerts.items():
        if "-EUR" in s:
            p = get_crypto_price(s)
        else:
            p = get_price(s)
        diff = (" (" + str(round(abs((p - t) / t * 100), 1)) + "% restant)") if p else ""
        msg += "<b>" + s + "</b> -> " + str(t) + diff + "\n"
    send_telegram(msg)

# ==================================================
# HANDLER TELEGRAM
# ==================================================
def handle_telegram():
    last_update_id = None
    while True:
        try:
            res = requests.get(
                "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/getUpdates",
                params={"timeout": 30, "offset": last_update_id},
                timeout=35
            )
            for update in res.json().get("result", []):
                last_update_id = update["update_id"] + 1
                text = update.get("message", {}).get("text", "").strip()
                if not text:
                    continue
                parts = text.split()
                cmd   = parts[0].lower()
                args  = parts[1:] if len(parts) > 1 else []

                if cmd in ["/aide", "/start"]:
                    cmd_aide()
                elif cmd in ["/status", "/statut"]:
                    cmd_status()
                elif cmd == "/positions":
                    cmd_positions()
                elif cmd == "/hold":
                    cmd_hold()
                elif cmd == "/crypto":
                    cmd_crypto()
                elif cmd == "/report":
                    send_daily_report(immediate=True)
                elif cmd == "/historique":
                    cmd_historique()
                elif cmd == "/marche":
                    cmd_marche()
                elif cmd == "/objectifs":
                    cmd_objectifs()
                elif cmd == "/briefing":
                    send_morning_briefing()
                elif cmd == "/semaine":
                    send_weekly_report()
                elif cmd == "/mois":
                    send_monthly_report()
                elif cmd == "/annee":
                    send_annual_report()
                elif cmd == "/pause":
                    cmd_pause()
                elif cmd == "/resume":
                    cmd_resume()
                elif cmd == "/urgence":
                    cmd_urgence()
                elif cmd == "/vacances":
                    cmd_vacances()
                elif cmd == "/retour":
                    cmd_retour()
                elif cmd == "/alertes":
                    cmd_voir_alertes()
                elif cmd == "/scan_holdings":
                    check_existing_holdings()
                    send_telegram("Scan termine.")
                elif cmd == "/technique" and args:
                    cmd_technique(args[0].upper())
                elif cmd == "/pourquoi" and args:
                    cmd_pourquoi(args[0].upper())
                elif cmd == "/alerte" and len(args) >= 2:
                    cmd_alerte(args)
        except Exception:
            pass
        time.sleep(2)

# ==================================================
# THREADS
# ==================================================
def thread_crypto():
    while True:
        try:
            check_existing_holdings()
            check_crypto_risk()
            if not trading_paused and not vacation_mode:
                scan_crypto()
        except Exception as e:
            record_error("thread_crypto: " + str(e))
        time.sleep(INTERVAL_CRYPTO)

def thread_stocks():
    while True:
        try:
            if not trading_paused and not vacation_mode and is_market_open():
                manage_hold_portfolio()
                scan_stocks()
        except Exception as e:
            record_error("thread_stocks: " + str(e))
        time.sleep(INTERVAL_STOCKS)

def thread_risk():
    while True:
        try:
            check_stock_risk()
            check_custom_alerts()
        except Exception as e:
            record_error("thread_risk: " + str(e))
        time.sleep(INTERVAL_RISK)

def thread_news_watcher():
    last_title = ""
    while True:
        try:
            news = get_news("FED inflation interest rates market crash", count=1)
            if news and news[0]["title"] != last_title:
                last_title = news[0]["title"]
                send_telegram(
                    "<b>BREAKING NEWS MACRO</b>\n\n" +
                    news[0]["title"] + "\n" +
                    news[0].get("url", "")
                )
        except Exception:
            pass
        time.sleep(1200)

def thread_scheduler():
    briefing_sent = None
    daily_sent    = None
    weekly_sent   = None
    monthly_sent  = None
    annual_sent   = None

    while True:
        try:
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")
            account = get_account_info()
            update_equity_checkpoints(account["equity"])
            check_market_health()

            if now.hour == 8 and now.minute < 5 and briefing_sent != today:
                send_morning_briefing()
                briefing_sent = today

            if now.hour == 15 and now.minute == 25 and briefing_sent != today + "_pre":
                send_morning_briefing()
                briefing_sent = today + "_pre"

            if now.hour == 21 and now.minute < 5 and daily_sent != today:
                send_daily_report()
                daily_sent = today

            wk = now.strftime("%Y-%W")
            if now.weekday() == 0 and now.hour == 8 and now.minute < 5 and weekly_sent != wk:
                send_weekly_report()
                weekly_sent = wk

            mo = now.strftime("%Y-%m")
            if now.day == 1 and now.hour == 9 and now.minute < 5 and monthly_sent != mo:
                run_dca()
                send_monthly_report()
                monthly_sent = mo

            yr = now.strftime("%Y")
            if now.month == 1 and now.day == 1 and now.hour == 10 and now.minute < 5 and annual_sent != yr:
                send_annual_report()
                annual_sent = yr

        except Exception as e:
            record_error("thread_scheduler: " + str(e))
        time.sleep(INTERVAL_SCHEDULER)

# ==================================================
# HEALTH SERVER (Render)
# ==================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ==================================================
# MAIN
# ==================================================
def main():
    send_telegram(
        "<b>Trading Agent V8 - FUSION</b>\n\n"
        "ALPACA (USD)\n"
        "Hold " + str(int(HOLD_PCT * 100)) + "% - Claude choisit dynamiquement\n"
        "Day Trade " + str(int(DAYTRADE_PCT * 100)) + "% - Long/Short toute la bourse\n"
        "Max " + str(MAX_DAYTRADE_POSITIONS) + " pos simultanees | illimite/jour\n\n"
        "COINBASE (EUR)\n"
        "Hold strict - BTC 25% | ETH 15%\n"
        "Hold souple - SOL/XRP/LINK\n"
        "Scalping 24/7 - " + str(len(CRYPTO_UNIVERSE)) + " cryptos\n"
        "Max " + str(MAX_CRYPTO_POSITIONS) + " pos simultanees\n\n"
        "SL : -" + str(CRYPTO_SL_PCT) + "% crypto (net frais) | -" + str(STOCK_SL_PCT) + "% actions\n"
        "Trailing stop : -" + str(TRAILING_STOP_PCT) + "% depuis pic\n"
        "Disjoncteur actif : Drawdown dynamique par capital\n\n"
        "Tape /aide"
    )

    check_existing_holdings()
    account = get_account_info()
    update_equity_checkpoints(account["equity"])
    log("Capital Alpaca : $" + str(round(account["equity"], 2)) + " | Cash : $" + str(round(account["cash"], 2)))

    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram,     daemon=True).start()
    threading.Thread(target=thread_crypto,       daemon=True).start()
    threading.Thread(target=thread_stocks,       daemon=True).start()
    threading.Thread(target=thread_risk,         daemon=True).start()
    threading.Thread(target=thread_news_watcher, daemon=True).start()
    threading.Thread(target=thread_scheduler,    daemon=True).start()

    log("Agent V8 - 7 threads actifs")

    while True:
        time.sleep(60)
        log(
            "Alive | " + ("PAUSE" if trading_paused else "ACTIF") +
            " | Marche " + ("OUVERT" if is_market_open() else "ferme") +
            " | Actions: " + str(len(active_stock_trades)) +
            " | Crypto: " + str(len(active_crypto_trades))
        )


if __name__ == "__main__":
    main()
