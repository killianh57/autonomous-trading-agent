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
MAX_DAYTRADE_POSITIONS = 6
STOCK_SL_PCT           = 3.0
STOCK_TP_PCT           = 4.0
STOCK_MIN_CONFIDENCE   = 65

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

COINBASE_FEE_PCT              = 1.2
CRYPTO_SL_PCT                 = 2.5
CRYPTO_TP_PCT                 = 4.0
TRAILING_STOP_PCT             = 3.0
CRYPTO_RISK_PER_TRADE         = 0.15
MAX_CRYPTO_POSITIONS          = 8
CRYPTO_MIN_CONFIDENCE         = 65
CRYPTO_CANDLE_WINDOW_HOURS    = 24

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
INTERVAL_STOCKS    = 30
INTERVAL_RISK      = 30
INTERVAL_SCHEDULER = 60

# ==================================================
# CLIENTS
# ==================================================
try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
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
swing_trades         = {}   # {symbol: entry_time} trades gardes overnight
failed_tickers       = {}  # {symbol: timestamp} cooldown apres echec
FAILED_COOLDOWN_SEC  = 3600  # 1h de pause apres un ordre echoue
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
# HELPERS AFFICHAGE TELEGRAM (100% ASCII dans le code)
# ==================================================
def _s(val, prefix="$", dec=2):
    """Formate un nombre avec signe."""
    sign = "+" if val >= 0 else ""
    return sign + prefix + str(round(val, dec))

def _bar(current, goal, length=8):
    """Barre de progression ASCII."""
    if goal == 0:
        return "[" + "." * length + "] 0%"
    pct    = min(current / goal, 1.0)
    filled = int(pct * length)
    return "[" + "#" * filled + "." * (length - filled) + "] " + str(int(pct * 100)) + "%"

def _sep():
    """Separateur de section."""
    return "\n" + "-" * 18 + "\n"

# Emojis via Unicode escapes (fichier reste 100% ASCII)
ICO_PORT   = "\U0001f4bc"  # portfolio
ICO_CHART  = "\U0001f4ca"  # stats
ICO_HOLD   = "\U0001f512"  # hold
ICO_TRADE  = "\u26a1"      # trade
ICO_CRYPTO = "\U0001f48e"  # crypto
ICO_TARGET = "\U0001f3af"  # target
ICO_CAL    = "\U0001f4c5"  # calendar
ICO_MOIS   = "\U0001f4c6"  # month
ICO_YEAR   = "\U0001f5d3"  # year
ICO_UP     = "\U0001f4c8"  # up
ICO_DOWN   = "\U0001f4c9"  # down
ICO_WORLD  = "\U0001f30d"  # world
ICO_BOT    = "\U0001f916"  # robot
ICO_BELL   = "\U0001f514"  # bell
ICO_HIST   = "\U0001f4dc"  # scroll
ICO_BEST   = "\U0001f3c6"  # trophy
ICO_WORST  = "\U0001f494"  # broken heart
ICO_PAUSE  = "\u23f8"      # pause
ICO_PLAY   = "\u25b6"      # play
ICO_SOS    = "\U0001f6a8"  # sos
ICO_BEACH  = "\U0001f3d6"  # beach
ICO_MORN   = "\U0001f305"  # sunrise
ICO_BRAIN  = "\U0001f9e0"  # brain
ICO_WAVE   = "\U0001f44b"  # wave
ICO_CHECK  = "\u2705"      # check
ICO_WARN   = "\u26a0"      # warning
ICO_RED    = "\U0001f534"  # red circle
ICO_GREEN  = "\U0001f7e2"  # green circle
ICO_YELLOW = "\U0001f7e1"  # yellow circle
ICO_BTC    = "\u20bf"      # bitcoin sign
ICO_MONEY  = "\U0001f4b5"  # dollar
ICO_NOTE   = "\U0001f9fe"  # receipt
ICO_ROCKET = "\U0001f680"  # rocket
ICO_MUSCLE = "\U0001f4aa"  # muscle

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
            es[key]          = equity
            es[key + "_key"] = k
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
# ANALYSE TECHNIQUE - ACTIONS
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

def get_intraday_prices(ticker, minutes=390):
    """Recupere les N dernieres minutes de bougies 5min (390min = 1 journee)."""
    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute5,
            start=datetime.now() - timedelta(minutes=minutes)
        )
        bars = list(data_client.get_stock_bars(req)[ticker])
        return bars
    except Exception:
        return []

def get_ta_intraday(ticker):
    """Analyse technique sur bougies 5min - pour le day trading rapide."""
    bars = get_intraday_prices(ticker, minutes=780)  # 2 jours de bougies 5min
    if not bars or len(bars) < 20:
        return None
    closes  = [b.close  for b in bars]
    highs   = [b.high   for b in bars]
    lows    = [b.low    for b in bars]
    volumes = [b.volume for b in bars]
    cur     = closes[-1]
    rsi     = calculate_rsi(closes, period=14)
    ma20    = sum(closes[-20:]) / 20
    ma9     = sum(closes[-9:])  / 9  if len(closes) >= 9  else None
    avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    cur_vol = volumes[-1]
    # VWAP simplifie (moyenne ponderee par volume sur la journee)
    day_bars = [b for b in bars if b.timestamp.date() == datetime.now().date()]
    if day_bars:
        vwap = sum(b.close * b.volume for b in day_bars) / max(sum(b.volume for b in day_bars), 1)
    else:
        vwap = cur
    return {
        "rsi":          rsi,
        "ma9":          ma9,
        "ma20":         ma20,
        "vwap":         vwap,
        "current":      cur,
        "above_vwap":   cur > vwap,
        "above_ma9":    cur > ma9 if ma9 else None,
        "above_ma20":   cur > ma20,
        "trend":        "haussier" if (ma9 and ma20 and ma9 > ma20) else "baissier",
        "volume_spike": cur_vol > avg_vol * 1.5,
        "week_perf":    ((cur - closes[-6]) / closes[-6] * 100) if len(closes) >= 6 else None,
        "high_20":      max(highs[-20:]),
        "low_20":       min(lows[-20:]),
    }

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
        "rsi":        rsi,
        "ma20":       ma20,
        "ma50":       ma50,
        "current":    cur,
        "trend":      "haussier" if (ma50 and ma20 > ma50) else "baissier",
        "above_ma20": cur > ma20,
        "week_perf":  ((cur - prices[-6]) / prices[-6] * 100) if len(prices) >= 6 else None
    }

# ==================================================
# ANALYSE TECHNIQUE - CRYPTO (bougies 5min)
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
        prices = prices[::-1]
        if len(prices) < 55:
            return None
        rsi  = calculate_rsi(prices, period=14)
        ma20 = sum(prices[-20:]) / 20
        ma50 = sum(prices[-50:]) / 50
        cur  = prices[-1]
        return {
            "rsi":          rsi,
            "ma20":         ma20,
            "ma50":         ma50,
            "current":      cur,
            "trend":        "haussier" if (cur > ma20) else "baissier",
            "strong_trend": (ma20 > ma50),
            "above_ma20":   cur > ma20,
            "week_perf":    ((cur - prices[-7]) / prices[-7] * 100) if len(prices) >= 7 else None,
            "prices":       prices
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
    wp = ("Perf 7j : " + str(round(ta["week_perf"], 1)) + "%\n") if ta.get("week_perf") else ""
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
    "Tu es un day trader professionnel - actions US, bougies 5min.\n"
    "Long + Short selon le setup. RR minimum 1:2. Max 2% par trade.\n"
    "Criteres favorables : prix au-dessus du VWAP, RSI entre 40-65, volume spike, MA9 > MA20.\n"
    "Setup SHORT : prix sous VWAP, RSI > 65 ou < 35 en retournement, volume fort.\n"
    "Si tu as perdu recemment sur ce ticker, sois plus prudent.\n"
    "Sois reactif - on cherche des mouvements de 2-5% dans la journee.\n"
    "Reponds UNIQUEMENT en JSON :\n"
    "{\"action\":\"BUY\"|\"SHORT\"|\"HOLD\","
    "\"confidence\":0-100,\"reason\":\"francais court\","
    "\"risk_pct\":1-2,\"tp_pct\":2-8}"
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
                ICO_CHECK + " <b>LONG " + label + "</b>  <b>" + symbol + "</b>\n"
                "~$" + str(valeur) + "   "
                "SL -" + str(STOCK_SL_PCT) + "%  TP +" + str(tp) + "%"
            )
        else:
            active_stock_trades.pop(symbol, None)
            send_telegram(
                ICO_MONEY + " <b>Cloture " + label + "</b>  <b>" + symbol + "</b>  ~$" + str(valeur)
            )
    except Exception as e:
        record_error("Order " + symbol + ": " + str(e))
        failed_tickers[symbol] = time.time()  # cooldown 1h
        send_telegram(ICO_WARN + " Ordre echoue " + symbol + "\n" + str(e)[:80])

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
            ICO_DOWN + " <b>SHORT</b>  <b>" + symbol + "</b>\n"
            "~$" + str(valeur) + "   "
            "SL +" + str(STOCK_SL_PCT) + "%  TP -" + str(tp) + "%"
        )
    except Exception as e:
        record_error("Short " + symbol + ": " + str(e))
        failed_tickers[symbol] = time.time()  # cooldown 1h
        send_telegram(ICO_WARN + " Short echoue " + symbol + "\n" + str(e)[:80])

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
                    ICO_CHECK + " <b>LONG " + label + "</b>  " + ICO_CRYPTO + " <b>" + symbol + "</b>\n"
                    "~" + str(round(amount_eur, 2)) + " EUR   "
                    "SL -" + str(CRYPTO_SL_PCT) + "%  TP +" + str(tp) + "%"
                )
            else:
                active_crypto_trades.pop(symbol, None)
                send_telegram(
                    ICO_MONEY + " <b>Vente " + label + "</b>  " + ICO_CRYPTO + " <b>" + symbol + "</b>  ~"
                    + str(round(amount_eur, 2)) + " EUR"
                )
    except Exception as e:
        record_error("Crypto " + symbol + ": " + str(e))
        send_telegram(ICO_WARN + " Ordre crypto echoue " + symbol + "\n" + str(e)[:80])

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
        log("Position detectee : " + symbol + " = " + str(round(valeur, 2)) + "EUR")
        send_telegram(
            ICO_BELL + " <b>Position detectee</b>  <b>" + symbol + "</b>\n"
            + str(round(balance, 6)) + " = ~" + str(round(valeur, 2)) + " EUR\n"
            "Ajoutee au suivi TP/SL"
        )

# ==================================================
# POCHE HOLD - ALPACA
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
            ICO_HOLD + " <b>HOLD " + horizon.upper() + "</b>  <b>" + symbol + "</b>\n"
            "$" + str(round(price, 2)) + "   Conf " + str(conf) + "%\n"
            + reason
        )
        place_order(symbol, "buy", amount / price, label="Hold")

    elif signal["action"] == "SELL" and symbol in hold_pos:
        memory["hold_portfolio"].pop(symbol, None)
        save_memory(memory)
        send_telegram(ICO_HOLD + " <b>Sortie Hold</b>  <b>" + symbol + "</b>\n" + reason)
        place_order(symbol, "sell", hold_pos[symbol]["qty"], label="Hold")

# ==================================================
# DAY TRADING - ALPACA
# ==================================================
def get_pdt_remaining():
    """Retourne le nombre de day trades restants disponibles."""
    try:
        if not trading_client:
            return PDT_LIMIT
        a = trading_client.get_account()
        equity = float(a.equity)
        if equity >= PDT_CAPITAL_THRESHOLD:
            return 999  # Pas de limite
        used = int(a.daytrade_count)
        return max(0, PDT_LIMIT - used)
    except Exception:
        return 0

def is_swing_mode():
    """True si on doit trader en swing (pas day trade)."""
    return get_pdt_remaining() <= 1  # Garde 1 trade d urgence

def scan_stocks():
    if trading_paused or vacation_mode or not is_market_open():
        return
    account   = get_account_info()
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if len(trade_pos) >= MAX_DAYTRADE_POSITIONS:
        return

    pdt_left  = get_pdt_remaining()
    swing     = is_swing_mode()
    trade_capital = account["equity"] * DAYTRADE_PCT

    if swing:
        log("Mode SWING actif - PDT restant: " + str(pdt_left))

    now = time.time()
    for ticker in DAYTRADE_UNIVERSE:
        if ticker in positions:
            continue
        # Skip si en cooldown apres un echec recent
        if ticker in failed_tickers and (now - failed_tickers[ticker]) < FAILED_COOLDOWN_SEC:
            continue
        price = get_price(ticker)
        if not price:
            continue
        ta       = get_ta_intraday(ticker) or get_ta(ticker)
        articles = get_news(ticker, count=3)
        wr       = get_winrate(ticker)
        wr_txt   = ("Winrate: " + str(round(wr, 0)) + "%\n") if wr else ""

        # Contexte intraday enrichi
        vwap_txt = ""
        vol_txt  = ""
        if ta and ta.get("vwap"):
            vwap_rel = "au-dessus" if ta.get("above_vwap") else "en-dessous"
            vwap_txt = "VWAP: " + str(round(ta["vwap"], 2)) + "$ (" + vwap_rel + ")\n"
        if ta and ta.get("volume_spike"):
            vol_txt = "Volume: SPIKE (+50% au-dessus moyenne)\n"
        if ta and ta.get("ma9"):
            ma9_rel = "au-dessus" if ta.get("above_ma9") else "en-dessous"
            vol_txt += "MA9: " + str(round(ta["ma9"], 2)) + "$ (" + ma9_rel + ")\n"

        signal = ask_claude(
            PROMPT_STOCKS,
            "Ticker: " + ticker + " | Prix: $" + str(round(price, 2)) + "\n"
            "TA 5min:\n" + format_ta(ta) + "\n"
            + vwap_txt + vol_txt +
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
                ICO_UP + " <b>Signal LONG</b>  <b>" + ticker + "</b>  $" + str(round(price, 2)) + "\n"
                + reason + "\nConf " + str(conf) + "%  TP +" + str(tp_pct) + "%"
            )
            place_order(ticker, "buy", qty, tp_pct=tp_pct, label="Day Trade")
            break
        elif action == "SHORT":
            send_telegram(
                ICO_DOWN + " <b>Signal SHORT</b>  <b>" + ticker + "</b>  $" + str(round(price, 2)) + "\n"
                + reason + "\nConf " + str(conf) + "%  TP -" + str(tp_pct) + "%"
            )
            open_short(ticker, qty, tp_pct=tp_pct)
            break
        time.sleep(0.5)

# ==================================================
# DISJONCTEUR DYNAMIQUE
# ==================================================
def get_crypto_capital():
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
    if current_capital > peak:
        memory["crypto_peak"] = current_capital
        save_memory(memory)
        return False
    drawdown_pct = ((peak - current_capital) / peak) * 100
    if peak < 100:
        max_drop = 50.0
    elif peak < 1000:
        max_drop = 30.0
    elif peak < 5000:
        max_drop = 20.0
    else:
        max_drop = 15.0
    return drawdown_pct >= max_drop

# ==================================================
# DAY TRADING - CRYPTO SCALPING 24/7
# ==================================================
def scan_crypto():
    if trading_paused or vacation_mode or not coinbase:
        return
    if check_circuit_breaker():
        log("Circuit breaker: drawdown max atteint, pause.")
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

        dip_buy  = (rsi < 40 and strong_trend and price > ta["ma50"])
        breakout = (50 <= rsi <= 70 and strong_trend and has_setup)

        if dip_buy or breakout:
            amount     = cash_eur * CRYPTO_RISK_PER_TRADE
            if amount < 2:
                continue
            strat_name = "Rebond" if dip_buy else "Breakout"
            reason     = strat_name + " | RSI=" + str(round(rsi, 0)) + " | Trend OK"
            place_crypto_order(symbol, "buy", amount, tp_pct=CRYPTO_TP_PCT, label="Scalping", reason=reason)
        time.sleep(0.3)

# ==================================================
# GESTION DU RISQUE - ACTIONS
# ==================================================
def check_stock_risk():
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    now       = datetime.now()
    for symbol, data in positions.items():
        if symbol in hold_syms:
            continue
        pnl_pct = data["pnl_pct"]
        trade   = active_stock_trades.get(symbol, {})
        tp_pct  = trade.get("tp_pct", STOCK_TP_PCT)
        side    = trade.get("side", "long")

        # Sortie fin de journee : ferme avant 21h45 (15min avant cloture)
        if is_market_open():
            market_close = now.replace(hour=19, minute=45, second=0, microsecond=0)
            if now >= market_close and pnl_pct > 0:
                send_telegram(ICO_MONEY + " <b>Cloture fin journee</b>  " + symbol + "  " + str(round(pnl_pct, 1)) + "%")
                if side == "long":
                    place_order(symbol, "sell", data["qty"], label="EOD")
                else:
                    place_order(symbol, "buy", abs(data["qty"]), label="EOD")
                continue

        if side == "long":
            if pnl_pct <= -STOCK_SL_PCT:
                send_telegram(ICO_RED + " <b>Stop Loss</b>  " + symbol + "  -" + str(round(abs(pnl_pct), 1)) + "%")
                place_order(symbol, "sell", data["qty"], label="SL")
            elif pnl_pct >= tp_pct:
                send_telegram(ICO_GREEN + " <b>Take Profit</b>  " + symbol + "  +" + str(round(pnl_pct, 1)) + "%")
                place_order(symbol, "sell", data["qty"], label="TP")
            # Breakeven stop : si +2%, monte le SL a +0.5%
            elif pnl_pct >= 2.0 and pnl_pct <= 0.5:
                send_telegram(ICO_YELLOW + " <b>Stop Breakeven</b>  " + symbol + "  (protection gains)")
                place_order(symbol, "sell", data["qty"], label="BE")
        elif side == "short":
            if pnl_pct <= -STOCK_SL_PCT:
                send_telegram(ICO_RED + " <b>Stop Loss SHORT</b>  " + symbol + "  -" + str(round(abs(pnl_pct), 1)) + "%")
                place_order(symbol, "buy", abs(data["qty"]), label="SL")
            elif pnl_pct >= tp_pct:
                send_telegram(ICO_GREEN + " <b>Take Profit SHORT</b>  " + symbol + "  +" + str(round(pnl_pct, 1)) + "%")
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
        currency = symbol.replace("-EUR", "")
        balance  = get_crypto_balance(currency)
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
            send_telegram(ICO_RED + " <b>Stop Loss</b>  " + symbol + "  -" + str(round(abs(net_pnl_pct), 1)) + "%")
            place_crypto_order(symbol, "sell", balance * price, label="SL")
        elif trailing_drop >= TRAILING_STOP_PCT and net_pnl_pct > 0:
            send_telegram(ICO_YELLOW + " <b>Trailing Stop</b>  " + symbol + "  -" + str(round(trailing_drop, 1)) + "% depuis pic")
            place_crypto_order(symbol, "sell", balance * price, label="TS")
        elif net_pnl_pct >= tp_pct:
            send_telegram(ICO_GREEN + " <b>Take Profit</b>  " + symbol + "  +" + str(round(net_pnl_pct, 1)) + "%")
            place_crypto_order(symbol, "sell", balance * price, label="TP")

def check_market_health():
    global trading_paused
    spy = get_spy_perf()
    if spy <= -10:
        send_telegram(ICO_SOS + " <b>CRASH !</b>  SPY " + str(round(spy, 1)) + "%\nTape /urgence")
    elif spy <= -5:
        trading_paused = True
        send_telegram(ICO_WARN + " <b>Forte baisse</b>  SPY " + str(round(spy, 1)) + "%\nDay trading suspendu.")
    elif spy <= -3:
        send_telegram(ICO_WARN + " Marche sous tension  SPY " + str(round(spy, 1)) + "%")

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        if "-EUR" in symbol:
            price = get_crypto_price(symbol)
        else:
            price = get_price(symbol)
        if price and price >= target:
            send_telegram(ICO_BELL + " <b>ALERTE !</b>  <b>" + symbol + "</b>  " + str(round(price, 2)) + " atteint " + ICO_CHECK)
            del custom_alerts[symbol]

# ==================================================
# DCA MENSUEL
# ==================================================
def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("DCA annule - pause.")
        return
    send_telegram(ICO_MONEY + " <b>DCA mensuel</b> en cours...")
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
# RAPPORTS TELEGRAM - DESIGN CARDS EPUREES
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
    titre     = ICO_CHART + " <b>RAPPORT</b>" if immediate else ICO_CHART + " <b>RAPPORT DU SOIR</b>"
    status    = "Vacances" if vacation_mode else "Pause" if trading_paused else "Actif"
    market    = "Ouvert" if is_market_open() else "Ferme"

    r  = titre + _sep()
    r += ICO_PORT + "  <b>" + str(round(account["equity"], 2)) + "$</b>   " + _s(account["pnl"]) + " auj.\n"
    r += ICO_BTC  + "  ~" + str(round(btc_val + eth_val, 2)) + " EUR hold\n"
    r += ICO_MONEY + "  Cash  " + str(round(account["cash"], 2)) + "$  |  " + str(round(cash_eur, 2)) + " EUR\n"
    r += ICO_WORLD + "  SPY " + _s(spy, prefix="", dec=2) + "%" + _sep()

    r += ICO_HOLD + " <b>HOLD (" + str(int(HOLD_PCT * 100)) + "%)  " + str(len(hold_pos)) + " pos</b>\n"
    if hold_pos:
        for s, d in hold_pos.items():
            hp   = load_memory()["hold_portfolio"].get(s, {})
            sign = "+" if d["pnl_pct"] >= 0 else ""
            r   += "  <b>" + s + "</b>  " + str(round(d["value"], 0)) + "$  " + sign + str(round(d["pnl_pct"], 1)) + "%  [" + hp.get("horizon", "?") + "]\n"
    else:
        r += "  En recherche...\n"

    r += "\n" + ICO_TRADE + " <b>DAY TRADE (" + str(int(DAYTRADE_PCT * 100)) + "%)  " + str(len(trade_pos)) + " pos</b>\n"
    if trade_pos:
        for s, d in trade_pos.items():
            t    = active_stock_trades.get(s, {})
            sign = "+" if d["pnl_pct"] >= 0 else ""
            r   += "  <b>" + s + "</b>  " + str(round(d["value"], 0)) + "$  " + sign + str(round(d["pnl_pct"], 1)) + "%  TP+" + str(t.get("tp_pct", STOCK_TP_PCT)) + "%\n"
    else:
        r += "  Aucun trade actif\n"

    r += "\n" + ICO_CRYPTO + " <b>SCALPING  " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + " pos</b>\n"
    if active_crypto_trades:
        for s, t in active_crypto_trades.items():
            entry   = t.get("entry") or 0
            price   = get_crypto_price(s) or entry
            net_pnl = ((price - entry) / entry * 100 - COINBASE_FEE_PCT) if entry else 0
            sign    = "+" if net_pnl >= 0 else ""
            r      += "  <b>" + s.replace("-EUR", "") + "</b>  " + str(round(t["amount"], 0)) + " EUR  " + sign + str(round(net_pnl, 1)) + "%\n"
    else:
        r += "  Aucun trade actif\n"

    r += _sep()
    r += ICO_TARGET + " Semaine\n" + _bar(max(wk_pnl, 0), wk_goal) + "  " + _s(wk_pnl) + "\n\n"
    r += ICO_CHART + "  Win " + str(round(stats["winrate"], 0)) + "%   PnL " + _s(stats["total_pnl"]) + "\n"
    r += ICO_BOT + "  " + status + "   " + market
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

    r  = ICO_CAL + " <b>RESUME SEMAINE</b>" + _sep()
    r += ICO_PORT + "  <b>" + str(round(account["equity"], 2)) + "$</b>   " + _s(wk_pnl) + " cette semaine\n"
    r += ICO_WORLD + "  SPY " + _s(spy, prefix="", dec=2) + "%   "
    r += ("Je bats le marche " + ICO_CHECK if vs_spy > 0 else "Marche > moi " + ICO_DOWN) + _sep()
    r += ICO_TARGET + " <b>OBJECTIFS</b>\n"
    r += "Sem.   " + _bar(max(wk_pnl, 0), wk * WEEKLY_GOAL_PCT / 100) + "  " + _s(wk_pnl) + "\n"
    r += "Mois   " + _bar(max(mo_pnl, 0), MONTHLY_GOAL_EUR)           + "  " + _s(mo_pnl) + "\n"
    r += "Annee  " + _bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + "  " + _s(yr_pnl) + _sep()
    r += ICO_CHART + "  " + str(len(wk_trades)) + " trades   " + str(round(stats["winrate"], 0)) + "% win\n"
    if best and best.get("pnl"):
        r += ICO_BEST + "  " + best["symbol"] + "  +" + str(round(best["pnl"], 2)) + "$\n"
    if worst and worst.get("pnl"):
        r += ICO_WORST + "  " + worst["symbol"] + "  " + str(round(worst["pnl"], 2)) + "$\n"
    r += "\nBonne semaine ! " + ICO_MUSCLE
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

    r  = ICO_MOIS + " <b>BILAN " + datetime.now().strftime("%B %Y").upper() + "</b>" + _sep()
    r += ICO_PORT + "  <b>" + str(round(account["equity"], 2)) + "$</b>   " + _s(mo_pnl) + " ce mois" + _sep()
    r += ICO_TARGET + " <b>OBJECTIFS</b>\n"
    r += "Mois   " + _bar(max(mo_pnl, 0), MONTHLY_GOAL_EUR)           + "  " + _s(mo_pnl) + "\n"
    r += "Annee  " + _bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + "  " + _s(yr_pnl) + _sep()
    r += ICO_CHART + "  " + str(len(ms.get("trades", []))) + " trades"
    if total_m > 0:
        r += "   " + str(round(ms["wins"] / total_m * 100, 0)) + "% win"
    if mo_pnl > 0:
        r += "\n" + ICO_NOTE + "  Impot ~" + str(round(mo_pnl * 0.30, 2)) + "$ (30%)"
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

    r  = ICO_YEAR + " <b>BILAN ANNUEL " + year + "</b>" + _sep()
    r += ICO_PORT + "  <b>" + str(round(account["equity"], 2)) + "$</b>   " + _s(yr_pnl) + " cette annee" + _sep()
    r += ICO_TARGET + " Objectif +" + str(ANNUAL_GOAL_PCT) + "%\n"
    r += _bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + "  " + _s(yr_pnl) + _sep()
    r += ICO_CHART + "  " + str(total_y) + " trades"
    if total_y > 0:
        r += "   " + str(round(ys["wins"] / total_y * 100, 0)) + "% win"
    r += "\n" + ICO_UP + "  Proj. 5 ans  ~" + str(round(proj_5y, 0)) + "$\n"
    if yr_pnl > 0:
        r += ICO_NOTE + "  Impot ~" + str(round(yr_pnl * 0.30, 2)) + "$ (30%)\n"
    r += "\nBonne annee ! " + ICO_ROCKET
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

    intl = []
    for ticker, name in [("EWJ", "JP"), ("FXI", "CN"), ("EWG", "DE"), ("EWU", "UK")]:
        p = get_market_perf(ticker)
        intl.append(name + " " + _s(p, prefix="", dec=1) + "%")

    if spy > 0.5:
        sentiment = "Favorable " + ICO_GREEN
    elif spy < -0.5:
        sentiment = "Defavorable " + ICO_RED
    else:
        sentiment = "Neutre " + ICO_YELLOW

    r  = ICO_MORN + " <b>BRIEFING MATIN</b>" + _sep()
    r += ICO_PORT + "  " + str(round(account["equity"], 2)) + "$   Cash EUR " + str(round(cash_eur, 2)) + "\n"
    r += ICO_WORLD + "  SPY " + _s(spy, prefix="", dec=2) + "%   " + "  ".join(intl) + "\n"
    r += "Sentiment  " + sentiment + _sep()
    r += ICO_BTC + "  BTC  " + str(round(btc_price, 0)) + " EUR\n"
    r += "   ETH  " + str(round(eth_price, 0)) + " EUR\n"
    r += ICO_CRYPTO + "  Scalping  " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + " pos" + _sep()
    r += ICO_TARGET + " Semaine\n" + _bar(max(wk_pnl, 0), wk_goal) + "  " + _s(wk_pnl) + "\n"
    r += "\nC est parti ! " + ICO_ROCKET
    send_telegram(r)

# ==================================================
# COMMANDES TELEGRAM
# ==================================================
def cmd_aide():
    lines = [
        ICO_BOT + " <b>COMMANDES</b>" + _sep(),
        ICO_PORT  + "  /status",
        ICO_HOLD  + "  /hold",
        ICO_TRADE + "  /positions",
        ICO_CRYPTO + "  /crypto",
        ICO_CHART + "  /report",
        ICO_HIST  + "  /historique",
        ICO_WORLD + "  /marche",
        ICO_TARGET + "  /objectifs",
        "      /technique NVDA",
        ICO_BRAIN + "  /pourquoi BTC-EUR",
        _sep(),
        ICO_MORN + "  /briefing",
        ICO_CAL  + "  /semaine",
        ICO_MOIS + "  /mois",
        ICO_YEAR + "  /annee",
        _sep(),
        ICO_BELL + "  /alerte BTC-EUR 90000",
        "      /alertes",
        "      /scan_holdings",
        _sep(),
        ICO_PAUSE + "  /pause",
        ICO_PLAY  + "  /resume",
        ICO_BEACH + "  /vacances  |  /retour",
        _sep(),
        ICO_SOS + "  /urgence   Tout fermer",
        "      (hold conserve)",
    ]
    send_telegram("\n".join(lines))

def cmd_pourquoi(symbol):
    symbol = symbol.upper()
    trade  = active_crypto_trades.get(symbol)
    if trade:
        send_telegram(
            ICO_BRAIN + " <b>RAISONNEMENT  " + symbol + "</b>" + _sep() +
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
    status    = "Vacances " + ICO_BEACH if vacation_mode else "Pause " + ICO_PAUSE if trading_paused else "Actif " + ICO_CHECK
    market    = "Ouvert " + ICO_GREEN if is_market_open() else "Ferme " + ICO_RED

    r  = ICO_PORT + " <b>PORTEFEUILLE</b>" + _sep()
    r += "Actions    <b>" + str(round(account["equity"], 2)) + "$</b>   " + _s(account["pnl"]) + "\n"
    r += "Crypto     ~" + str(round(btc_val + eth_val, 2)) + " EUR\n"
    r += "Cash       " + str(round(account["cash"], 2)) + "$   " + str(round(cash_eur, 2)) + " EUR\n"
    r += "SPY        " + _s(spy, prefix="", dec=2) + "%" + _sep()
    r += ICO_HOLD   + " Hold      " + str(len(hold_pos)) + " pos\n"
    r += ICO_TRADE  + " Trades    " + str(len(trade_pos)) + " pos\n"
    r += ICO_CRYPTO + " Scalping  " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + " pos" + _sep()
    r += ICO_CAL + "  Mois   " + _s(ms["pnl"]) + "\n"
    r += ICO_YEAR + "  Annee  " + _s(ys["pnl"]) + "\n"
    r += ICO_CHART + "  Win    " + str(round(stats["winrate"], 0)) + "%   PnL " + _s(stats["total_pnl"]) + _sep()
    r += ICO_TARGET + " Semaine\n" + _bar(max(wk_pnl, 0), wk_goal) + "  " + _s(wk_pnl) + "\n\n"
    r += status + "   " + market
    send_telegram(r)

def cmd_hold():
    memory    = load_memory()
    hold_port = memory.get("hold_portfolio", {})
    positions = get_positions()
    if not hold_port:
        send_telegram(ICO_HOLD + " <b>HOLD</b>" + _sep() + "Aucune position\nClaude analyse...")
        return
    r = ICO_HOLD + " <b>POCHE HOLD</b>" + _sep()
    for s, info in hold_port.items():
        pos  = positions.get(s, {})
        pnl  = (" " + ("+" if pos.get("pnl_pct", 0) >= 0 else "") + str(round(pos["pnl_pct"], 1)) + "%") if pos else ""
        val  = ("  " + str(round(pos.get("value", 0), 0)) + "$") if pos else ""
        r   += "<b>" + s + "</b>" + val + pnl + "  [" + info.get("horizon", "?") + "]\n"
        r   += "  depuis " + info.get("date", "?") + "\n\n"
    send_telegram(r)

def cmd_positions():
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if not trade_pos:
        send_telegram(ICO_TRADE + " <b>DAY TRADES</b>" + _sep() + "Aucun trade actif")
        return
    r = ICO_TRADE + " <b>DAY TRADES</b>" + _sep()
    for s, d in trade_pos.items():
        t    = active_stock_trades.get(s, {})
        side = "L" if t.get("side", "long") == "long" else "S"
        sign = "+" if d["pnl_pct"] >= 0 else ""
        r   += "[" + side + "] <b>" + s + "</b>  " + str(round(d["value"], 0)) + "$  " + sign + str(round(d["pnl_pct"], 1)) + "%\n"
        r   += "  TP +" + str(t.get("tp_pct", STOCK_TP_PCT)) + "%   SL -" + str(STOCK_SL_PCT) + "%\n\n"
    send_telegram(r)

def cmd_crypto():
    if not coinbase:
        send_telegram("Coinbase non connecte.")
        return
    total    = 0
    cash_eur = get_crypto_balance("EUR")
    r = ICO_CRYPTO + " <b>CRYPTO</b>" + _sep()
    r += ICO_HOLD + " <b>HOLD</b>\n"
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        currency = symbol.replace("-EUR", "")
        price    = get_crypto_price(symbol) or 0
        balance  = get_crypto_balance(currency)
        val      = balance * price
        total   += val
        tag      = "strict" if symbol in CRYPTO_HOLD_STRICT else "souple"
        r       += "  " + currency + "   " + str(round(val, 2)) + " EUR  [" + tag + "]\n"
    r += "  Total  " + str(round(total, 2)) + " EUR\n"
    r += "  Cash   " + str(round(cash_eur, 2)) + " EUR" + _sep()
    r += ICO_TRADE + " <b>SCALPING  " + str(len(active_crypto_trades)) + "/" + str(MAX_CRYPTO_POSITIONS) + " pos</b>\n"
    if active_crypto_trades:
        for s, t in active_crypto_trades.items():
            entry   = t.get("entry") or 0
            price   = get_crypto_price(s) or entry
            net_pnl = ((price - entry) / entry * 100 - COINBASE_FEE_PCT) if entry else 0
            sign    = "+" if net_pnl >= 0 else ""
            r      += "  <b>" + s.replace("-EUR", "") + "</b>  " + str(round(t["amount"], 0)) + " EUR  " + sign + str(round(net_pnl, 1)) + "%\n"
    else:
        r += "  Aucun trade actif\n"
    send_telegram(r)

def cmd_marche():
    r = ICO_WORLD + " <b>MARCHES</b>" + _sep()
    for ticker, name in [
        ("SPY", "USA S&P500"),
        ("QQQ", "Nasdaq"),
        ("EWJ", "Japon"),
        ("FXI", "Chine"),
        ("EWG", "Allemagne"),
        ("EWU", "UK"),
    ]:
        p     = get_market_perf(ticker)
        arrow = ICO_UP if p >= 0 else ICO_DOWN
        r    += arrow + "  " + name + "   " + _s(p, prefix="", dec=2) + "%\n"
    r += _sep()
    r += "Bourse : " + ("Ouverte " + ICO_GREEN if is_market_open() else "Fermee " + ICO_RED)
    send_telegram(r)

def cmd_technique(ticker):
    ta    = get_ta(ticker)
    price = get_price(ticker)
    if not ta or not price:
        send_telegram("Impossible d analyser " + ticker + ".")
        return
    wr  = get_winrate(ticker)
    rsi = ta.get("rsi") or 0
    if rsi < 30:
        rsi_label = "Survendu " + ICO_GREEN
    elif rsi > 70:
        rsi_label = "Surachete " + ICO_RED
    else:
        rsi_label = "Neutre " + ICO_YELLOW
    trend = "Haussier " + ICO_UP if ta.get("trend") == "haussier" else "Baissier " + ICO_DOWN
    ma20  = "Au-dessus " + ICO_CHECK if ta.get("above_ma20") else "En-dessous " + ICO_WARN

    r  = ICO_CHART + " <b>" + ticker + "</b>" + _sep()
    r += "Prix   <b>" + str(round(price, 2)) + "$</b>\n"
    if ta.get("week_perf"):
        r += "7j     " + _s(ta["week_perf"], prefix="", dec=1) + "%\n"
    r += _sep()
    r += "RSI    " + str(rsi) + "   " + rsi_label + "\n"
    r += "Trend  " + trend + "\n"
    r += "MA20   " + ma20 + "\n"
    if wr:
        r += _sep() + "Win rate  " + str(round(wr, 0)) + "%"
    send_telegram(r)

def cmd_objectifs():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    wk = checkpts.get("week",  account["equity"])
    mo = checkpts.get("month", account["equity"])
    yr = checkpts.get("year",  account["equity"])
    wk_pnl = account["equity"] - wk
    mo_pnl = account["equity"] - mo
    yr_pnl = account["equity"] - yr

    r  = ICO_TARGET + " <b>OBJECTIFS</b>" + _sep()
    r += ICO_CAL + " Semaine  +" + str(WEEKLY_GOAL_PCT) + "%\n"
    r += _bar(max(wk_pnl, 0), wk * WEEKLY_GOAL_PCT / 100) + "  " + _s(wk_pnl) + "\n\n"
    r += ICO_MOIS + " Mois  +" + str(MONTHLY_GOAL_EUR) + " EUR\n"
    r += _bar(max(mo_pnl, 0), MONTHLY_GOAL_EUR) + "  " + _s(mo_pnl) + "\n\n"
    r += ICO_YEAR + " Annee  +" + str(ANNUAL_GOAL_PCT) + "%\n"
    r += _bar(max(yr_pnl, 0), yr * ANNUAL_GOAL_PCT / 100) + "  " + _s(yr_pnl)
    send_telegram(r)

def cmd_historique():
    stats = get_stats()
    if not stats["recent"]:
        send_telegram(ICO_HIST + " <b>HISTORIQUE</b>" + _sep() + "Aucun trade enregistre")
        return
    r = ICO_HIST + " <b>HISTORIQUE</b>" + _sep()
    for t in reversed(stats["recent"]):
        side = "LONG" if t["side"] == "buy" else ("SHORT" if t["side"] == "short" else "VENTE")
        pnl  = ("   " + _s(t["pnl"])) if t.get("pnl") else ""
        r   += "<b>" + t["symbol"] + "</b>  " + side + "  @" + str(round(t["price"], 2)) + "$" + pnl + "\n"
        r   += "  " + t["date"] + "\n\n"
    r += _sep()
    r += "Win " + str(round(stats["winrate"], 0)) + "%   PnL " + _s(stats["total_pnl"])
    send_telegram(r)

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram(
        ICO_PAUSE + " <b>PAUSE</b>" + _sep() +
        "Nouveaux trades suspendus\n"
        "Stop loss toujours actif " + ICO_CHECK + "\n\n"
        "Tape /resume pour reprendre"
    )

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False
    vacation_mode  = False
    memory = load_memory()
    memory["crypto_peak"] = get_crypto_capital()
    save_memory(memory)
    send_telegram(
        ICO_PLAY + " <b>TRADING REPRIS</b>" + _sep() +
        "Bot actif " + ICO_CHECK + "\n"
        "Disjoncteur reinitialise " + ICO_CHECK
    )

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions    = get_positions()
    hold_syms    = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos    = {s: d for s, d in positions.items() if s not in hold_syms}
    crypto_count = len([s for s in active_crypto_trades if s not in CRYPTO_HOLD_STRICT])
    if not trade_pos and not crypto_count:
        send_telegram(
            ICO_SOS + " <b>URGENCE</b>" + _sep() +
            "Aucun trade a fermer\n"
            "Poche hold conservee " + ICO_CHECK
        )
        return
    send_telegram(
        ICO_SOS + " <b>URGENCE  FERMETURE EN COURS</b>" + _sep() +
        "Actions  " + str(len(trade_pos)) + "\n"
        "Crypto   " + str(crypto_count) + "\n"
        "Hold conserve " + ICO_CHECK
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
    send_telegram(ICO_CHECK + " Tout ferme   Tape /resume")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode  = True
    trading_paused = True
    send_telegram(
        ICO_BEACH + " <b>MODE VACANCES</b>" + _sep() +
        "Hold conserve         " + ICO_CHECK + "\n"
        "Stop loss actif       " + ICO_CHECK + "\n"
        "Nouveaux trades OFF   " + ICO_CHECK + "\n\n"
        "Tape /retour a ton retour !"
    )

def cmd_retour():
    global vacation_mode, trading_paused
    vacation_mode  = False
    trading_paused = False
    send_telegram(
        ICO_WAVE + " <b>BON RETOUR !</b>" + _sep() +
        "Trading repris " + ICO_CHECK
    )
    send_daily_report(immediate=True)

def cmd_alerte(args):
    try:
        symbol = args[0].upper()
        target = float(args[1])
        custom_alerts[symbol] = target
        send_telegram(
            ICO_BELL + " <b>ALERTE CREEE</b>" + _sep() +
            "<b>" + symbol + "</b>   cible  " + str(target)
        )
    except Exception:
        send_telegram("Format : /alerte BTC-EUR 90000")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram(ICO_BELL + " <b>ALERTES</b>" + _sep() + "Aucune alerte active")
        return
    r = ICO_BELL + " <b>ALERTES ACTIVES</b>" + _sep()
    for s, t in custom_alerts.items():
        if "-EUR" in s:
            p = get_crypto_price(s)
        else:
            p = get_price(s)
        diff = ("   " + str(round(abs((p - t) / t * 100), 1)) + "% restant") if p else ""
        r   += "<b>" + s + "</b>   " + str(t) + diff + "\n"
    send_telegram(r)

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

                if cmd in ["/aide", "/start"]:           cmd_aide()
                elif cmd in ["/status", "/statut"]:      cmd_status()
                elif cmd == "/positions":                cmd_positions()
                elif cmd == "/hold":                     cmd_hold()
                elif cmd == "/crypto":                   cmd_crypto()
                elif cmd == "/report":                   send_daily_report(immediate=True)
                elif cmd == "/historique":               cmd_historique()
                elif cmd == "/marche":                   cmd_marche()
                elif cmd == "/objectifs":                cmd_objectifs()
                elif cmd == "/briefing":                 send_morning_briefing()
                elif cmd == "/semaine":                  send_weekly_report()
                elif cmd == "/mois":                     send_monthly_report()
                elif cmd == "/annee":                    send_annual_report()
                elif cmd == "/pause":                    cmd_pause()
                elif cmd == "/resume":                   cmd_resume()
                elif cmd == "/urgence":                  cmd_urgence()
                elif cmd == "/vacances":                 cmd_vacances()
                elif cmd == "/retour":                   cmd_retour()
                elif cmd == "/alertes":                  cmd_voir_alertes()
                elif cmd == "/scan_holdings":            check_existing_holdings(); send_telegram("Scan termine " + ICO_CHECK)
                elif cmd == "/technique" and args:       cmd_technique(args[0].upper())
                elif cmd == "/pourquoi" and args:        cmd_pourquoi(args[0].upper())
                elif cmd == "/alerte" and len(args) >= 2: cmd_alerte(args)
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
                    ICO_WARN + " <b>BREAKING NEWS MACRO</b>\n\n" +
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
        ICO_BOT + " <b>Trading Agent V8  ONLINE</b>\n\n"
        "ALPACA\n"
        "Hold " + str(int(HOLD_PCT * 100)) + "%  Claude dynamique\n"
        "Day Trade " + str(int(DAYTRADE_PCT * 100)) + "%  Long/Short\n"
        "Max " + str(MAX_DAYTRADE_POSITIONS) + " pos  |  illimite/jour\n\n"
        "COINBASE\n"
        "Hold strict  BTC 25%  ETH 15%\n"
        "Hold souple  SOL / XRP / LINK\n"
        "Scalping 24/7  " + str(len(CRYPTO_UNIVERSE)) + " cryptos\n"
        "Max " + str(MAX_CRYPTO_POSITIONS) + " pos\n\n"
        "SL actions  -" + str(STOCK_SL_PCT) + "%\n"
        "SL crypto   -" + str(CRYPTO_SL_PCT) + "% (net frais)\n"
        "Trailing    -" + str(TRAILING_STOP_PCT) + "% depuis pic\n"
        "Disjoncteur  drawdown dynamique\n\n"
        "Tape /aide " + ICO_WAVE
    )

    check_existing_holdings()
    account = get_account_info()
    update_equity_checkpoints(account["equity"])
    log("Capital : $" + str(round(account["equity"], 2)) + " | Cash : $" + str(round(account["cash"], 2)))

    threading.Thread(target=start_health_server,  daemon=True).start()
    threading.Thread(target=handle_telegram,      daemon=True).start()
    threading.Thread(target=thread_crypto,        daemon=True).start()
    threading.Thread(target=thread_stocks,        daemon=True).start()
    threading.Thread(target=thread_risk,          daemon=True).start()
    threading.Thread(target=thread_news_watcher,  daemon=True).start()
    threading.Thread(target=thread_scheduler,     daemon=True).start()

    log("Agent V8  7 threads actifs")

    while True:
        time.sleep(60)
        log(
            "Alive | " + ("PAUSE" if trading_paused else "ACTIF") +
            " | " + ("OUVERT" if is_market_open() else "ferme") +
            " | Actions " + str(len(active_stock_trades)) +
            " | Crypto " + str(len(active_crypto_trades))
        )


if __name__ == "__main__":
    main()
