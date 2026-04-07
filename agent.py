import os
import json
import time
import threading
import requests
import anthropic
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from coinbase.rest import RESTClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY  = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET   = os.getenv("COINBASE_SECRET_KEY")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PORTEFEUILLE ALPACA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOLD_PCT              = 0.60
DAYTRADE_PCT          = 0.40
MAX_HOLD_POSITIONS    = 8
MAX_DAYTRADE_POSITIONS = 4
STOCK_SL_PCT          = 3.0
STOCK_TP_PCT          = 6.0
MIN_CONFIDENCE        = 75

DAYTRADE_UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA",
    "AMD","INTC","QCOM","AVGO","MU","ASML",
    "JPM","BAC","GS","MS","V","MA","PYPL",
    "LLY","UNH","JNJ","PFE","MRNA","ABBV",
    "XOM","CVX","OXY",
    "SHOP","UBER","ABNB","COIN","PLTR","RBLX","U",
    "SNAP","ROKU","DKNG","HOOD","MELI","SE",
    "QQQ","SPY","TQQQ","SQQQ","SPXL","UVXY","ARKK",
    "XLK","XLF","XLE","XLV","XLI",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PORTEFEUILLE COINBASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRYPTO_HOLD_STRICT = ["BTC-USD", "ETH-USD"]
CRYPTO_HOLD_SOUPLE = ["SOL-USD", "XRP-USD", "LINK-USD"]
CRYPTO_HOLD_ALL    = CRYPTO_HOLD_STRICT + CRYPTO_HOLD_SOUPLE
CRYPTO_HOLD_ALLOC  = {
    "BTC-USD": 0.25, "ETH-USD": 0.15,
    "SOL-USD": 0.05, "XRP-USD": 0.03, "LINK-USD": 0.02,
}
CRYPTO_HOLD_PCT      = 0.50
CRYPTO_TRADE_PCT     = 0.50
MAX_CRYPTO_POSITIONS = 3
CRYPTO_SL_PCT        = 7.0
CRYPTO_TP_PCT        = 12.0

CRYPTO_UNIVERSE = [
    "BTC-USD","ETH-USD","SOL-USD","XRP-USD","LINK-USD",
    "AVAX-USD","POL-USD","ADA-USD","DOT-USD","DOGE-USD",
    "LTC-USD","UNI-USD","ATOM-USD","NEAR-USD","APT-USD",
    "ARB-USD","OP-USD","INJ-USD","ROSE-USD","FET-USD",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OBJECTIFS ET TIMERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WEEKLY_GOAL_PCT  = 1.0
MONTHLY_GOAL_EUR = 100
ANNUAL_GOAL_PCT  = 20.0
DCA_MONTHLY_EUR  = 100
MEMORY_FILE      = "trade_memory.json"

INTERVAL_CRYPTO    = 300 # 5 min pour soulager l'API
INTERVAL_STOCKS    = 300 # 5 min pour soulager l'API
INTERVAL_RISK      = 30
INTERVAL_SCHEDULER = 60

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLIENTS (PAPER TRADING ACTIVÉ POUR SÉCURITÉ)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

try:
    coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
except Exception as e:
    coinbase = None
    print(f"Coinbase init error: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ÉTAT GLOBAL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
trading_paused       = False
vacation_mode        = False
custom_alerts        = {}
active_stock_trades  = {}
active_crypto_trades = {}
_lock                = threading.Lock()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UTILITAIRES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def progress_bar(current, goal, length=10):
    if goal == 0: return "░" * length
    pct    = min(current / goal, 1.0)
    filled = int(pct * length)
    return f"{'█'*filled}{'░'*(length-filled)} {pct*100:.0f}%"

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log(f"Telegram error: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MÉMOIRE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {
        "trades": [], "hold_portfolio": {},
        "stats": {"wins": 0, "losses": 0, "total_pnl": 0},
        "monthly_stats": {}, "annual_stats": {},
        "patterns": {}, "errors": [], "equity_start": {}
    }

def save_memory(memory):
    with _lock:
        tmp_file = MEMORY_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(memory, f, indent=2)
        os.replace(tmp_file, MEMORY_FILE)

def record_trade(symbol, side, qty, price, pnl=None):
    with _lock:
        memory = load_memory()
        now    = datetime.now()
        month  = now.strftime("%Y-%m")
        year   = now.strftime("%Y")
        trade  = {"date": now.strftime("%Y-%m-%d %H:%M"), "symbol": symbol,
                  "side": side, "qty": qty, "price": price, "pnl": pnl}
        memory["trades"].append(trade)
        if pnl is not None:
            memory["stats"]["total_pnl"] += pnl
            if pnl > 0: memory["stats"]["wins"] += 1
            else:       memory["stats"]["losses"] += 1
            ms = memory["monthly_stats"].setdefault(month, {"wins":0,"losses":0,"pnl":0,"trades":[]})
            ms["pnl"] += pnl
            if pnl > 0: ms["wins"] += 1
            else:       ms["losses"] += 1
            ms["trades"].append(trade)
            ys = memory["annual_stats"].setdefault(year, {"wins":0,"losses":0,"pnl":0})
            ys["pnl"] += pnl
            if pnl > 0: ys["wins"] += 1
            else:       ys["losses"] += 1
            p = memory["patterns"].setdefault(symbol, {"wins":0,"losses":0,"total_pnl":0})
            p["total_pnl"] += pnl
            if pnl > 0: p["wins"] += 1
            else:        p["losses"] += 1
        memory["trades"] = memory["trades"][-200:]
        save_memory(memory)

def record_error(msg):
    memory = load_memory()
    memory["errors"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "error": str(msg)})
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def update_equity_checkpoints(equity):
    memory = load_memory()
    now    = datetime.now()
    es     = memory["equity_start"]
    for key, fmt in [("week","%Y-%W"),("month","%Y-%m"),("year","%Y")]:
        k = now.strftime(fmt)
        if es.get(f"{key}_key") != k:
            es[key] = equity
            es[f"{key}_key"] = k
    memory["equity_start"] = es
    save_memory(memory)

def get_equity_checkpoints():
    return load_memory().get("equity_start", {})

def get_stats():
    m = load_memory(); s = m["stats"]
    total = s["wins"] + s["losses"]
    return {**s, "winrate": (s["wins"]/total*100) if total > 0 else 0, "recent": m["trades"][-5:]}

def get_monthly_stats(month=None):
    if not month: month = datetime.now().strftime("%Y-%m")
    return load_memory()["monthly_stats"].get(month, {"wins":0,"losses":0,"pnl":0,"trades":[]})

def get_annual_stats(year=None):
    if not year: year = datetime.now().strftime("%Y")
    return load_memory()["annual_stats"].get(year, {"wins":0,"losses":0,"pnl":0})

def get_winrate(symbol):
    p = load_memory()["patterns"].get(symbol)
    if not p: return None
    t = p["wins"] + p["losses"]
    return (p["wins"]/t*100) if t > 0 else None

def get_best_worst(trades):
    w = [t for t in trades if t.get("pnl") is not None]
    if not w: return None, None
    return max(w, key=lambda x: x["pnl"]), min(w, key=lambda x: x["pnl"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DONNÉES MARCHÉ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_market_open():
    try:
        clock = trading_client.get_clock()
        return clock.is_open
    except Exception as e:
        record_error(f"Erreur horloge Alpaca: {e}")
        return False

def get_account_info():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "pnl": float(a.equity) - float(a.last_equity)}

def get_positions():
    return {p.symbol: {
        "qty": float(p.qty), "value": float(p.market_value),
        "avg_price": float(p.avg_entry_price),
        "pnl": float(p.unrealized_pl),
        "pnl_pct": float(p.unrealized_plpc) * 100,
        "side": "long" if float(p.qty) > 0 else "short"
    } for p in trading_client.get_all_positions()}

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
    except: return None

def get_crypto_price(symbol):
    try:
        if not coinbase: return None
        pb = coinbase.get_best_bid_ask(product_ids=[symbol])
        return float(pb["pricebooks"][0]["asks"][0]["price"])
    except: return None

def get_crypto_balance(currency):
    try:
        if not coinbase: return 0
        for acc in coinbase.get_accounts()["accounts"]:
            if acc["currency"] == currency:
                return float(acc["available_balance"]["value"])
        return 0
    except: return 0

def get_market_perf(ticker):
    try:
        cur  = data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
        bars = list(data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
            start=datetime.now()-timedelta(days=3)))[ticker])
        return ((cur-bars[-2].close)/bars[-2].close)*100 if len(bars) >= 2 else 0
    except: return 0

def get_spy_perf(): return get_market_perf("SPY")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANALYSE TECHNIQUE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_historical_prices(ticker, days=60):
    try:
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
                               start=datetime.now()-timedelta(days=days))
        return [bar.close for bar in data_client.get_stock_bars(req)[ticker]]
    except: return []

def calculate_rsi(prices, period=14):
    if len(prices) < period+1: return None
    gains  = [max(prices[i]-prices[i-1],0) for i in range(1,len(prices))]
    losses = [max(prices[i-1]-prices[i],0) for i in range(1,len(prices))]
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al == 0: return 100
    return round(100-(100/(1+ag/al)),1)

def get_ta(ticker):
    prices = get_historical_prices(ticker)
    if not prices or len(prices) < 20: return None
    rsi  = calculate_rsi(prices)
    ma20 = sum(prices[-20:])/20
    ma50 = sum(prices[-50:])/50 if len(prices) >= 50 else None
    cur  = prices[-1]
    return {"rsi": rsi, "ma20": ma20, "ma50": ma50, "current": cur,
            "trend": "haussier 📈" if (ma50 and ma20 > ma50) else "baissier 📉",
            "above_ma20": cur > ma20,
            "above_ma50": cur > ma50 if ma50 else None,
            "week_perf": ((cur-prices[-6])/prices[-6]*100) if len(prices) >= 6 else None}

def get_crypto_ta(symbol):
    try:
        if not coinbase: return None
        candles = coinbase.get_candles(
            product_id=symbol,
            start=str(int((datetime.now()-timedelta(days=30)).timestamp())),
            end=str(int(datetime.now().timestamp())),
            granularity="ONE_DAY"
        )
        prices = [float(c["close"]) for c in candles.get("candles",[])]
        if len(prices) < 14: return None
        rsi  = calculate_rsi(prices)
        ma20 = sum(prices[-20:])/20 if len(prices) >= 20 else None
        cur  = prices[-1]
        return {"rsi": rsi, "ma20": ma20, "current": cur,
                "trend": "haussier 📈" if (ma20 and cur > ma20) else "baissier 📉",
                "above_ma20": cur > ma20 if ma20 else None,
                "week_perf": ((cur-prices[-7])/prices[-7]*100) if len(prices) >= 7 else None}
    except: return None

def format_ta(ta):
    if not ta: return "Données indisponibles"
    rsi_txt = ""
    if ta.get("rsi"):
        label = "⬇️ Survendu" if ta["rsi"] < 30 else "⬆️ Suracheté" if ta["rsi"] > 70 else "➡️ Neutre"
        rsi_txt = f"RSI {ta['rsi']} {label}\n"
    wp = f"Perf 7j : {ta['week_perf']:+.1f}%\n" if ta.get("week_perf") else ""
    return f"{rsi_txt}Tendance : {ta['trend']}\nMA20 : {'✅' if ta.get('above_ma20') else '⚠️'}\n{wp}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEWS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_news(ticker, count=5):
    try:
        q = ticker.replace("-USD","").replace("USDT","")
        return requests.get(
            f"https://newsapi.org/v2/everything?q={q}&language=en"
            f"&sortBy=publishedAt&pageSize={count}&apiKey={NEWS_API_KEY}",
            timeout=10
        ).json().get("articles",[])
    except: return []

def format_news(articles, count=3):
    return "\n".join([f"- {a['title']}" for a in articles[:count]]) or "Aucune news récente"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLAUDE IA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROMPT_HOLD = """Tu es un gestionnaire de portefeuille long terme.
Tu choisis dynamiquement les meilleurs actifs selon l'actu, les fondamentaux et le momentum.
Horizons possibles : court (semaines), moyen (mois), long (années) — choisis selon l'opportunité.
Univers : toutes les actions US tradables sur Alpaca.
Réponds UNIQUEMENT en JSON :
{"action":"BUY"|"SELL"|"HOLD","symbol":"TICKER","horizon":"court"|"moyen"|"long","confidence":0-100,"reason":"français court","allocation_pct":1-10}
Si aucune opportunité : {"action":"HOLD","symbol":"","horizon":"","confidence":0,"reason":"pas d'opportunité","allocation_pct":0}"""

PROMPT_STOCKS = """Tu es un day trader professionnel — actions US.
Long + Short selon le setup. RR minimum 1:2. Max 2% du capital par trade.
Univers : toutes les actions US tradables sur Alpaca.
Si tu as perdu récemment sur ce ticker, sois plus prudent.
Réponds UNIQUEMENT en JSON :
{"action":"BUY"|"SHORT"|"HOLD","confidence":0-100,"reason":"français court","risk_pct":1-2,"tp_pct":3-15}"""

PROMPT_CRYPTO = """Tu es un day trader crypto professionnel — analyse 24/7.
Long + Short selon le setup. Minimum 75 de confiance pour agir.
Taille max : 5% du capital de trading par position.
Réponds UNIQUEMENT en JSON :
{"action":"BUY"|"SHORT"|"HOLD","confidence":0-100,"reason":"français court","risk_pct":1-5,"tp_pct":5-30}"""

def ask_claude(prompt, user_msg):
    try:
        res = claude_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=400, system=prompt,
            messages=[{"role":"user","content":user_msg}]
        )
        raw = res.content[0].text.strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {"action":"HOLD","confidence":0,"reason":"JSON introuvable"}
    except Exception as e:
        record_error(f"Claude error: {e}")
        return {"action":"HOLD","confidence":0,"reason":"Erreur Claude"}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ORDRES ALPACA (BRACKET)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def place_order_bracket(symbol, side, qty, tp_pct, label="", reason=""):
    try:
        price = get_price(symbol)
        if not price: return
        
        # SL et TP
        sl_price = round(price * (1 - STOCK_SL_PCT/100), 2)
        tp_price = round(price * (1 + tp_pct/100), 2)
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        req = MarketOrderRequest(
            symbol=symbol, qty=round(abs(qty),4), side=order_side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp_price),
            stop_loss=StopLossRequest(stop_price=sl_price)
        )
        trading_client.submit_order(req)
        
        valeur = round(abs(qty)*price, 2)
        record_trade(symbol, side, round(abs(qty),4), price)
        
        with _lock:
            active_stock_trades[symbol] = {"side":side, "entry":price, "tp_pct":tp_pct, "reason":reason}
            
        send_telegram(f"⚡ <b>ORDRE BRACKET {label}</b> <b>{symbol}</b>\n💵 ~${valeur}\n🎯 TP: ${tp_price} (+{tp_pct}%)\n🛑 SL: ${sl_price} (-{STOCK_SL_PCT}%)\n\n<i>L'ordre est géré par Alpaca.</i>")
    except Exception as e:
        record_error(f"Order {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre échoué</b> {symbol}\n{str(e)[:100]}")

# Fonction générique pour vendre au marché (Hold / Urgence)
def place_order(symbol, side, qty, label=""):
    try:
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=round(abs(qty),4),
            side=order_side, time_in_force=TimeInForce.DAY
        ))
        price  = get_price(symbol)
        valeur = round(abs(qty)*price,2) if price else "?"
        record_trade(symbol, side, round(abs(qty),4), price or 0)
        
        with _lock:
            active_stock_trades.pop(symbol, None)
        send_telegram(f"💰 <b>Ordre {label}</b> <b>{symbol}</b> ~${valeur}")
    except Exception as e:
        record_error(f"Order {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre échoué</b> {symbol}\n{str(e)[:100]}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ORDRES COINBASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def place_crypto_order(symbol, side, amount_usd, tp_pct=None, label="", reason=""):
    try:
        if not coinbase: return
        if side == "buy":
            coinbase.market_order_buy(
                client_order_id=f"bot_{int(time.time())}",
                product_id=symbol, quote_size=str(round(amount_usd,2))
            )
        else:
            price = get_crypto_price(symbol)
            if not price: return
            coinbase.market_order_sell(
                client_order_id=f"bot_{int(time.time())}",
                product_id=symbol, base_size=str(round(amount_usd/price,8))
            )
        price = get_crypto_price(symbol)
        record_trade(symbol, side, round(amount_usd/(price or 1),8), price or 0)
        
        with _lock:
            if side == "buy":
                active_crypto_trades[symbol] = {"side":"long","amount":amount_usd,"entry":price,"tp_pct":tp_pct or CRYPTO_TP_PCT, "reason":reason}
                send_telegram(f"✅ <b>LONG {label}</b> 💎 <b>{symbol}</b>\n💵 ~${amount_usd:.2f}\n🛑 -{CRYPTO_SL_PCT}% | 🎯 +{tp_pct or CRYPTO_TP_PCT}%")
            else:
                active_crypto_trades.pop(symbol, None)
                send_telegram(f"💰 <b>Vente {label}</b> 💎 <b>{symbol}</b> ~${amount_usd:.2f}")
    except Exception as e:
        record_error(f"Crypto {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre crypto échoué</b> {symbol}\n{str(e)[:100]}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POCHE HOLD — ALPACA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def manage_hold_portfolio():
    if trading_paused or vacation_mode or not is_market_open(): return
    account   = get_account_info()
    positions = get_positions()
    memory    = load_memory()
    hold_port = memory.get("hold_portfolio", {})
    hold_pos  = {s: d for s, d in positions.items() if s in hold_port}
    if len(hold_pos) >= MAX_HOLD_POSITIONS: return

    hold_capital  = account["equity"] * HOLD_PCT
    hold_invested = sum(d["value"] for d in hold_pos.values())
    available     = hold_capital - hold_invested
    if available < account["equity"] * 0.01: return

    spy_perf   = get_spy_perf()
    news_macro = format_news(get_news("stocks market economy", count=3))

    signal = ask_claude(PROMPT_HOLD,
        f"Capital hold disponible: ${available:.2f}\n"
        f"SPY aujourd'hui: {spy_perf:+.2f}%\n"
        f"News macro:\n{news_macro}\n"
        f"Positions hold actuelles: {list(hold_port.keys()) or 'aucune'}\n"
        f"Quel actif ajouter ou renforcer dans la poche hold ?"
    )

    if signal.get("action") == "HOLD" or not signal.get("symbol"): return
    symbol  = signal["symbol"].upper()
    conf    = signal.get("confidence", 0)
    reason  = signal.get("reason", "")
    horizon = signal.get("horizon", "moyen")
    alloc   = signal.get("allocation_pct", 3) / 100
    if conf < MIN_CONFIDENCE: return

    if signal["action"] == "BUY":
        amount = available * alloc
        price  = get_price(symbol)
        if not price or amount < 1: return
        memory["hold_portfolio"][symbol] = {"horizon": horizon, "entry": price, "date": datetime.now().strftime("%Y-%m-%d")}
        save_memory(memory)
        send_telegram(f"🔒 <b>HOLD {horizon.upper()}</b>\n<b>{symbol}</b> ${price:.2f}\n{reason}\nConfiance : {conf}%")
        place_order(symbol, "buy", amount/price, label="Hold")

    elif signal["action"] == "SELL" and symbol in hold_pos:
        memory["hold_portfolio"].pop(symbol, None)
        save_memory(memory)
        send_telegram(f"🔒 <b>Sortie Hold</b>\n<b>{symbol}</b>\n{reason}")
        place_order(symbol, "sell", hold_pos[symbol]["qty"], label="Hold")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DAY TRADING — ALPACA (AVEC FILTRE RSI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def scan_stocks():
    if trading_paused or vacation_mode or not is_market_open(): return
    account   = get_account_info()
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio", {}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if len(trade_pos) >= MAX_DAYTRADE_POSITIONS: return

    trade_capital = account["equity"] * DAYTRADE_PCT

    for ticker in DAYTRADE_UNIVERSE:
        if ticker in positions: continue
        price = get_price(ticker)
        ta    = get_ta(ticker)
        
        # FILTRE: N'appelle Claude que si RSI survendu ou suracheté (économie d'API)
        if not price or not ta or (40 < ta['rsi'] < 60): continue

        articles = get_news(ticker, count=2)
        wr       = get_winrate(ticker)

        signal = ask_claude(PROMPT_STOCKS,
            f"Ticker: {ticker} | Prix: ${price:.2f}\n"
            f"Analyse technique:\n{format_ta(ta)}\n"
            f"News:\n{format_news(articles)}\n"
            + (f"Réussite historique: {wr:.0f}%\n" if wr else "")
        )
        action = signal.get("action","HOLD")
        conf   = signal.get("confidence",0)
        reason = signal.get("reason","")
        tp_pct = signal.get("tp_pct", STOCK_TP_PCT)
        risk   = signal.get("risk_pct", 1)
        
        if conf < MIN_CONFIDENCE: continue
        qty = (trade_capital * risk / 100) / price

        if action == "BUY" and account["cash"] >= qty * price:
            place_order_bracket(ticker, "buy", qty, tp_pct=tp_pct, label="Day Trade", reason=reason)
            break # Un seul par scan
        elif action == "SHORT":
            place_order_bracket(ticker, "sell", qty, tp_pct=tp_pct, label="Short Trade", reason=reason)
            break
        time.sleep(0.5)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DAY TRADING — CRYPTO 24/7 (AVEC FILTRE RSI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def scan_crypto():
    if trading_paused or vacation_mode or not coinbase: return
    if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS: return

    btc_val   = get_crypto_balance("BTC") * (get_crypto_price("BTC-USD") or 0)
    eth_val   = get_crypto_balance("ETH") * (get_crypto_price("ETH-USD") or 0)
    hold_val  = btc_val + eth_val
    total_cb  = hold_val / max(CRYPTO_HOLD_PCT * 0.65, 0.01)
    trade_cap = total_cb * CRYPTO_TRADE_PCT

    for symbol in CRYPTO_UNIVERSE:
        if symbol in active_crypto_trades: continue
        price = get_crypto_price(symbol)
        ta    = get_crypto_ta(symbol)
        
        if not price or not ta or (40 < ta['rsi'] < 60): continue

        articles = get_news(symbol, count=2)
        wr       = get_winrate(symbol)

        signal = ask_claude(PROMPT_CRYPTO,
            f"Ticker: {symbol} | Prix: ${price:.4f}\n"
            f"Analyse technique:\n{format_ta(ta)}\n"
            f"News:\n{format_news(articles)}\n"
            + (f"Réussite historique: {wr:.0f}%\n" if wr else "")
        )
        action = signal.get("action","HOLD")
        conf   = signal.get("confidence",0)
        reason = signal.get("reason","")
        tp_pct = signal.get("tp_pct", CRYPTO_TP_PCT)
        risk   = signal.get("risk_pct", 2)
        
        if conf < MIN_CONFIDENCE: continue
        amount = trade_cap * risk / 100
        if amount < 5: continue

        if action == "BUY":
            place_crypto_order(symbol, "buy", amount, tp_pct=tp_pct, label="Day Trade", reason=reason)
            break
        time.sleep(0.3)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GESTION DU RISQUE (CRYPTO ET ALERTES UNIQUEMENT)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Note: Le risque action est maintenant géré par Alpaca (Bracket Orders). 
# On garde check_crypto_risk() pour la crypto car l'API de base Coinbase est moins souple.

def check_crypto_risk():
    if not coinbase: return
    for symbol, trade in list(active_crypto_trades.items()):
        if symbol in CRYPTO_HOLD_STRICT: continue
        price = get_crypto_price(symbol)
        if not price: continue
        entry   = trade.get("entry", price)
        tp_pct  = trade.get("tp_pct", CRYPTO_TP_PCT)
        pnl_pct = ((price-entry)/entry*100) if entry else 0
        currency = symbol.replace("-USD","")
        balance  = get_crypto_balance(currency)
        if balance <= 0: continue
        if pnl_pct <= -CRYPTO_SL_PCT:
            send_telegram(f"🛑 <b>Stop Loss crypto</b> {symbol} -{abs(pnl_pct):.1f}%")
            place_crypto_order(symbol, "sell", balance*price, label="SL")
        elif pnl_pct >= tp_pct:
            send_telegram(f"🎯 <b>Take Profit crypto</b> {symbol} +{pnl_pct:.1f}%")
            place_crypto_order(symbol, "sell", balance*price, label="TP")

def check_market_health():
    global trading_paused
    spy = get_spy_perf()
    if spy <= -10:
        send_telegram(f"🚨 <b>CRASH !</b> SPY {spy:.1f}%\nTape /urgence")
    elif spy <= -5:
        trading_paused = True
        send_telegram(f"⚠️ <b>Forte baisse SPY {spy:.1f}%</b>\nDay trading suspendu.")

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        price = get_crypto_price(symbol) if "-USD" in symbol else get_price(symbol)
        if price and price >= target:
            send_telegram(f"🔔 <b>ALERTE !</b> <b>{symbol}</b> atteint ${price:.2f} ✅")
            del custom_alerts[symbol]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DCA MENSUEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("⏸️ DCA annulé — trading en pause."); return
    send_telegram("💰 <b>DCA mensuel</b> en cours…")
    dca_usd = DCA_MONTHLY_EUR * 1.08
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        amount = dca_usd * 0.70 * alloc
        if amount >= 1:
            place_crypto_order(symbol, "buy", amount, label="DCA")
    
    # Reste pour actions hold
    account = get_account_info()
    signal  = ask_claude(PROMPT_HOLD,
        f"DCA mensuel de ${dca_usd*0.30:.2f} disponible.\nQuel actif hold actions renforcer ce mois-ci ?\nPositions actuelles: {list(load_memory().get('hold_portfolio',{}).keys())}"
    )
    if signal.get("action") == "BUY" and signal.get("symbol"):
        symbol = signal["symbol"].upper()
        price  = get_price(symbol)
        if price:
            place_order(symbol, "buy", (dca_usd*0.30)/price, label="DCA")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RAPPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_daily_report(immediate=False):
    account   = get_account_info()
    positions = get_positions()
    stats     = get_stats()
    spy       = get_spy_perf()
    checkpts  = get_equity_checkpoints()
    hold_syms = set(load_memory().get("hold_portfolio",{}).keys())
    hold_pos  = {s: d for s, d in positions.items() if s in hold_syms}
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    wk        = checkpts.get("week", account["equity"])
    wk_pnl    = account["equity"] - wk
    btc_val   = get_crypto_balance("BTC") * (get_crypto_price("BTC-USD") or 0)
    eth_val   = get_crypto_balance("ETH") * (get_crypto_price("ETH-USD") or 0)
    titre     = "📊 <b>Rapport immédiat</b>" if immediate else "📊 <b>Rapport du soir</b>"

    r  = f"{titre}\n{'='*22}\n\n"
    r += f"💰 Actions : <b>${account['equity']:.2f}</b>\n"
    r += f"₿ Crypto hold : ~${btc_val+eth_val:.2f}\n"
    r += f"💵 Cash : ${account['cash']:.2f}\n"
    r += f"{'📈' if account['pnl']>=0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f} | SPY : {spy:+.2f}%\n\n"
    r += f"🔒 <b>Hold ({int(HOLD_PCT*100)}%) — {len(hold_pos)} positions</b>\n"
    
    for s, d in hold_pos.items():
        hp = load_memory()["hold_portfolio"].get(s,{})
        r += f"  {'🟢' if d['pnl_pct']>=0 else '🔴'} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%) [{hp.get('horizon','?')}]\n"
    if not hold_pos: r += "  (aucune — Claude cherche)\n"
    
    r += f"\n⚡ <b>Day Trade ({int(DAYTRADE_PCT*100)}%) — {len(trade_pos)} actives</b>\n"
    for s, d in trade_pos.items():
        t = active_stock_trades.get(s,{})
        emoji = "📈" if t.get("side","long") == "long" else "📉"
        r += f"  {emoji} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%) 🎯+{t.get('tp_pct',STOCK_TP_PCT)}%\n"
    if not trade_pos: r += "  (aucune)\n"
    
    r += f"\n🎯 Semaine :\n{progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n"
    r += f"\nRéussite : {stats['winrate']:.0f}% | P&amp;L : ${stats['total_pnl']:+.2f}\n"
    r += f"🤖 {'🏖️' if vacation_mode else '⏸️' if trading_paused else '✅'} | {'🟢' if is_market_open() else '🔴'}"
    send_telegram(r)

def send_weekly_report():
    account  = get_account_info()
    stats    = get_stats()
    spy      = get_spy_perf()
    checkpts = get_equity_checkpoints()
    memory   = load_memory()
    wk_ago   = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    wk_trades = [t for t in memory["trades"] if t["date"] >= wk_ago]
    best, worst = get_best_worst(wk_trades)
    wk = checkpts.get("week", account["equity"])
    mo = checkpts.get("month", account["equity"])
    yr = checkpts.get("year", account["equity"])
    wk_pnl = account["equity"] - wk
    mo_pnl = account["equity"] - mo
    yr_pnl = account["equity"] - yr
    vs_spy = account["pnl"] - (account["equity"]*spy/100)

    r  = "📅 <b>RÉSUMÉ SEMAINE</b>\n" + "="*22 + "\n\n"
    r += f"💰 <b>${account['equity']:.2f}</b> | {'📈' if wk_pnl>=0 else '📉'} ${wk_pnl:+.2f}\n"
    r += f"SPY : {spy:+.2f}% | {'✅ Je bats le marché !' if vs_spy>0 else '📉 Marché > moi'}\n\n"
    r += f"🎯 Semaine : {progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n"
    r += f"📅 Mois    : {progress_bar(max(mo_pnl,0), MONTHLY_GOAL_EUR*1.08)} ${mo_pnl:+.2f}\n"
    r += f"🗓️ Année   : {progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)} ${yr_pnl:+.2f}\n\n"
    r += f"📊 {len(wk_trades)} trades | {stats['winrate']:.0f}% réussite\n"
    if best and best.get("pnl"): r += f"🏆 {best['symbol']} +${best['pnl']:.2f}\n"
    if worst and worst.get("pnl"): r += f"💔 {worst['symbol']} ${worst['pnl']:.2f}\n"
    r += "\nBonne semaine ! 💪"
    send_telegram(r)

def send_monthly_report():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    month    = datetime.now().strftime("%Y-%m")
    ms       = get_monthly_stats(month)
    mo       = checkpts.get("month", account["equity"])
    yr       = checkpts.get("year", account["equity"])
    mo_pnl   = account["equity"] - mo
    yr_pnl   = account["equity"] - yr
    proj     = (yr_pnl/max(datetime.now().month,1))*12
    total_m  = ms["wins"]+ms["losses"]

    r  = f"📆 <b>BILAN {datetime.now().strftime('%B %Y').upper()}</b>\n" + "="*22 + "\n\n"
    r += f"💰 <b>${account['equity']:.2f}</b> | Ce mois : ${mo_pnl:+.2f}\n\n"
    r += f"🎯 Mois  : {progress_bar(max(mo_pnl,0), MONTHLY_GOAL_EUR*1.08)} ${mo_pnl:+.2f}\n"
    r += f"🗓️ Année : {progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)} ${yr_pnl:+.2f}\n\n"
    r += f"📊 {len(ms.get('trades',[]))} trades"
    if total_m > 0: r += f" | {ms['wins']/total_m*100:.0f}% réussite"
    r += f"\n📈 Projection annuelle : ~${proj:+.2f}\n"
    send_telegram(r)

def send_annual_report():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    year     = str(datetime.now().year)
    ys       = get_annual_stats(year)
    yr       = checkpts.get("year", account["equity"])
    yr_pnl   = account["equity"] - yr
    total_y  = ys["wins"]+ys["losses"]
    proj_5y  = account["equity"]*((1+yr_pnl/max(yr,1))**5)

    r  = f"🗓️ <b>BILAN ANNUEL {year}</b>\n" + "="*22 + "\n\n"
    r += f"💰 <b>${account['equity']:.2f}</b> | P&amp;L : ${yr_pnl:+.2f}\n\n"
    r += f"🎯 +{ANNUAL_GOAL_PCT}% :\n{progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)} ${yr_pnl:+.2f}\n\n"
    r += f"📊 {total_y} trades"
    if total_y > 0: r += f" | {ys['wins']/total_y*100:.0f}% réussite"
    r += f"\n📈 Projection 5 ans : ~${proj_5y:.2f}\n"
    r += f"\n🎯 Objectif {int(year)+1} : +${max(yr_pnl*1.2,500):.0f}\nBonne année ! 🚀"
    send_telegram(r)

def send_premarket_briefing():
    account  = get_account_info()
    spy      = get_spy_perf()
    checkpts = get_equity_checkpoints()
    wk       = checkpts.get("week", account["equity"])
    wk_pnl   = account["equity"] - wk
    intl     = []
    for ticker, name in [("EWJ","🇯🇵"),("FXI","🇨🇳"),("EWG","🇩🇪"),("EWU","🇬🇧")]:
        p = get_market_perf(ticker)
        intl.append(f"{'🟢' if p>0.5 else '🔴' if p<-0.5 else '🟡'} {name} {p:+.2f}%")
    r  = "📋 <b>BRIEFING PRÉ-MARCHÉ</b>\n" + "="*22 + "\n\n"
    r += f"💼 ${account['equity']:.2f} | Cash ${account['cash']:.2f}\n"
    r += f"🇺🇸 SPY hier : {spy:+.2f}%\n"
    r += "  ".join(intl) + "\n\n"
    r += f"🎯 Semaine : {progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n"
    sentiment = "🟢 Favorable" if spy>0.5 else "🔴 Défavorable" if spy<-0.5 else "🟡 Neutre"
    r += f"\nSentiment : {sentiment}\nC'est parti ! 🚀"
    send_telegram(r)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMANDES TELEGRAM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cmd_aide():
    send_telegram(
        "🤖 <b>Commandes</b>\n\n"
        "📊 /status | /positions | /hold\n"
        "/crypto | /report | /historique\n"
        "/marche | /objectifs\n"
        "/technique NVDA\n\n"
        "🤔 /pourquoi AAPL\n\n"
        "📅 /briefing | /semaine\n"
        "/mois | /annee\n\n"
        "⚙️ /pause | /resume\n"
        "/vacances | /retour\n\n"
        "🔔 /alerte BTC-USD 90000\n"
        "/alertes\n\n"
        "🚨 /urgence — Ferme les trades"
    )

def cmd_pourquoi(symbol):
    symbol = symbol.upper()
    trade = active_stock_trades.get(symbol) or active_crypto_trades.get(symbol)
    if trade:
        send_telegram(f"🤔 <b>Raisonnement pour {symbol} :</b>\n\n{trade.get('reason', 'Raison non sauvegardée.')}")
    else:
        send_telegram(f"Aucun trade actif trouvé pour {symbol}.")

def cmd_status():
    account   = get_account_info()
    stats     = get_stats()
    spy       = get_spy_perf()
    checkpts  = get_equity_checkpoints()
    ms        = get_monthly_stats()
    ys        = get_annual_stats()
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio",{}).keys())
    hold_pos  = {s: d for s, d in positions.items() if s in hold_syms}
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    wk        = checkpts.get("week", account["equity"])
    wk_pnl    = account["equity"] - wk
    btc_val   = get_crypto_balance("BTC") * (get_crypto_price("BTC-USD") or 0)
    eth_val   = get_crypto_balance("ETH") * (get_crypto_price("ETH-USD") or 0)

    send_telegram(
        f"💼 <b>Portefeuille (PAPER)</b>\n\n"
        f"💰 Actions : <b>${account['equity']:.2f}</b>\n"
        f"₿ Crypto hold : ~${btc_val+eth_val:.2f}\n"
        f"💵 Cash : ${account['cash']:.2f}\n"
        f"{'📈' if account['pnl']>=0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f}\n\n"
        f"🔒 Hold : {len(hold_pos)} pos | ⚡ Trade : {len(trade_pos)} pos\n\n"
        f"📅 Mois : ${ms['pnl']:+.2f} | 🗓️ Année : ${ys['pnl']:+.2f}\n\n"
        f"🎯 Semaine :\n{progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n\n"
        f"Réussite : {stats['winrate']:.0f}% | SPY : {spy:+.2f}%\n"
        f"🤖 {'🏖️' if vacation_mode else '⏸️' if trading_paused else '✅'} | {'🟢' if is_market_open() else '🔴'}"
    )

def cmd_hold():
    memory    = load_memory()
    hold_port = memory.get("hold_portfolio",{})
    positions = get_positions()
    if not hold_port:
        send_telegram("🔒 Poche hold vide — Claude cherche des opportunités."); return
    msg = "🔒 <b>Poche HOLD</b>\n\n"
    for s, info in hold_port.items():
        pos  = positions.get(s,{})
        pnl  = f" ({pos['pnl_pct']:+.2f}%)" if pos else ""
        msg += f"<b>{s}</b> [{info.get('horizon','?')}] depuis {info.get('date','?')}{pnl}\n"
    send_telegram(msg)

def cmd_positions():
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio",{}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if not trade_pos:
        send_telegram("⚡ Aucun trade actions actif."); return
    msg = "⚡ <b>Day Trades actifs</b>\n\n"
    for s, d in trade_pos.items():
        t = active_stock_trades.get(s,{})
        emoji = "📈" if t.get("side","long")=="long" else "📉"
        msg += f"{emoji} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%) 🎯+{t.get('tp_pct',STOCK_TP_PCT)}%\n"
    send_telegram(msg)

def cmd_crypto():
    if not coinbase:
        send_telegram("❌ Coinbase non connecté."); return
    lines = []
    total = 0
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        currency = symbol.replace("-USD","")
        price    = get_crypto_price(symbol) or 0
        balance  = get_crypto_balance(currency)
        val      = balance * price
        total   += val
        strict   = "🔒" if symbol in CRYPTO_HOLD_STRICT else "⚖️"
        lines.append(f"{strict} <b>{currency}</b> {balance:.6f} ≈ ${val:.2f}")
    send_telegram(
        f"₿ <b>Poche Hold Crypto</b>\n\n"
        + "\n".join(lines) +
        f"\n\n💰 Total hold : ~${total:.2f}\n"
        f"🔒 BTC/ETH = jamais vendus\n"
        f"⚖️ SOL/XRP/LINK = rééquilibrables\n"
        f"⚡ Day trade actif : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS} pos"
    )

def cmd_marche():
    spy  = get_spy_perf()
    intl = []
    for ticker, name in [("EWJ","🇯🇵 Japon"),("FXI","🇨🇳 Chine"),("EWG","🇩🇪 Allemagne"),("EWU","🇬🇧 UK")]:
        p = get_market_perf(ticker)
        intl.append(f"{'🟢' if p>0.5 else '🔴' if p<-0.5 else '🟡'} {name} : {p:+.2f}%")
    msg  = f"🌍 <b>Marchés</b>\n\n🇺🇸 SPY : {spy:+.2f}% {'🟢' if spy>0.5 else '🔴' if spy<-0.5 else '🟡'}\n\n"
    msg += "\n".join(intl)
    msg += f"\n\n{'🟢 Ouvert' if is_market_open() else '🔴 Fermé'}"
    send_telegram(msg)

def cmd_technique(ticker):
    ta    = get_ta(ticker)
    price = get_price(ticker)
    if not ta or not price:
        send_telegram(f"❌ Impossible d'analyser {ticker}."); return
    wr  = get_winrate(ticker)
    msg = f"📊 <b>{ticker}</b> ${price:.2f}\n\n{format_ta(ta)}"
    if wr: msg += f"🎯 Réussite : {wr:.0f}%"
    send_telegram(msg)

def cmd_objectifs():
    account  = get_account_info()
    checkpts = get_equity_checkpoints()
    wk = checkpts.get("week", account["equity"])
    mo = checkpts.get("month", account["equity"])
    yr = checkpts.get("year", account["equity"])
    wk_pnl = account["equity"] - wk
    mo_pnl = account["equity"] - mo
    yr_pnl = account["equity"] - yr
    send_telegram(
        f"🎯 <b>Objectifs</b>\n\n"
        f"📅 Semaine (+{WEEKLY_GOAL_PCT}%) :\n{progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)}\n${wk_pnl:+.2f}\n\n"
        f"📆 Mois (+{MONTHLY_GOAL_EUR}€) :\n{progress_bar(max(mo_pnl,0), MONTHLY_GOAL_EUR*1.08)}\n${mo_pnl:+.2f}\n\n"
        f"🗓️ Année (+{ANNUAL_GOAL_PCT}%) :\n{progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)}\n${yr_pnl:+.2f}"
    )

def cmd_historique():
    stats = get_stats()
    if not stats["recent"]:
        send_telegram("📭 Aucun trade."); return
    msg = "📜 <b>5 derniers trades</b>\n\n"
    for t in reversed(stats["recent"]):
        pnl  = f" | ${t['pnl']:+.2f}" if t.get("pnl") else ""
        msg += f"{'✅' if t['side']=='buy' else '💰'} {t['date']} — {t['side'].upper()} <b>{t['symbol']}</b> @ ${t['price']:.2f}{pnl}\n"
    msg += f"\n🎯 {stats['winrate']:.0f}% | P&L : ${stats['total_pnl']:+.2f}"
    send_telegram(msg)

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram("⏸️ <b>Pause</b>\nStop loss actif. Tape /resume.")

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False; vacation_mode = False
    send_telegram("✅ <b>Trading repris !</b>")

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions = get_positions()
    hold_syms = set(load_memory().get("hold_portfolio",{}).keys())
    trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
    if not trade_pos:
        send_telegram("ℹ️ Aucun trade actif.\nPoche hold conservée."); return
    send_telegram(f"🚨 <b>URGENCE</b>\nFermeture de {len(trade_pos)} trade(s)…\nPoche hold conservée.")
    for s, d in trade_pos.items():
        place_order(s, "sell", abs(d["qty"]), label="URGENCE")
    send_telegram("✅ Trades fermés.\nTape /resume.")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode = True; trading_paused = True
    send_telegram("🏖️ <b>Mode vacances</b>\n✅ Hold conservé\n✅ Stop loss actif\n❌ Aucun nouveau trade\nTape /retour !")

def cmd_retour():
    global vacation_mode, trading_paused
    vacation_mode = False; trading_paused = False
    send_telegram("👋 <b>Bon retour !</b>")
    send_daily_report(immediate=True)

def cmd_alerte(args):
    try:
        symbol, target = args[0].upper(), float(args[1])
        custom_alerts[symbol] = target
        send_telegram(f"🔔 Alerte : <b>{symbol}</b> → ${target:.2f}")
    except:
        send_telegram("❌ Format : /alerte BTC-USD 90000")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("📭 Aucune alerte."); return
    msg = "🔔 <b>Alertes actives</b>\n\n"
    for s, t in custom_alerts.items():
        p    = get_crypto_price(s) if "-USD" in s else get_price(s)
        diff = f" ({abs((p-t)/t*100):.1f}% restant)" if p else ""
        msg += f"📌 <b>{s}</b> → ${t:.2f}{diff}\n"
    send_telegram(msg)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HANDLERS & THREADS (AVEC NEWS WATCHER)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def handle_telegram():
    last_update_id = None
    while True:
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": last_update_id}, timeout=35
            )
            for update in res.json().get("result",[]):
                last_update_id = update["update_id"] + 1
                text = update.get("message",{}).get("text","").strip()
                cmd  = text.lower().split()[0] if text else ""
                args = text.split()[1:] if len(text.split()) > 1 else []
                if cmd in ["/aide","/start"]:           cmd_aide()
                elif cmd == "/status":                  cmd_status()
                elif cmd == "/positions":               cmd_positions()
                elif cmd == "/hold":                    cmd_hold()
                elif cmd == "/crypto":                  cmd_crypto()
                elif cmd == "/report":                  send_daily_report(immediate=True)
                elif cmd == "/historique":              cmd_historique()
                elif cmd == "/marche":                  cmd_marche()
                elif cmd == "/objectifs":               cmd_objectifs()
                elif cmd == "/briefing":                send_premarket_briefing()
                elif cmd == "/semaine":                 send_weekly_report()
                elif cmd == "/mois":                    send_monthly_report()
                elif cmd == "/annee":                   send_annual_report()
                elif cmd == "/pause":                   cmd_pause()
                elif cmd == "/resume":                  cmd_resume()
                elif cmd == "/urgence":                 cmd_urgence()
                elif cmd == "/vacances":                cmd_vacances()
                elif cmd == "/retour":                  cmd_retour()
                elif cmd == "/alertes":                 cmd_voir_alertes()
                elif cmd == "/technique" and args:      cmd_technique(args[0].upper())
                elif cmd == "/pourquoi" and args:       cmd_pourquoi(args[0].upper())
                elif cmd == "/alerte" and len(args)>=2: cmd_alerte(args)
        except Exception as e:
            pass
        time.sleep(2)

def thread_news_watcher():
    """Surveille les news globales pour détecter des chocs de marché (Toutes les 20 min)"""
    last_news_title = ""
    while True:
        try:
            news = get_news("FED inflation interest rates market crash", count=1)
            if news and news[0]['title'] != last_news_title:
                last_news_title = news[0]['title']
                send_telegram(f"🚨 <b>BREAKING NEWS MACRO</b>\n\n{news[0]['title']}\n<a href='{news[0]['url']}'>Lire l'article</a>")
        except: pass
        time.sleep(1200)

def thread_crypto():
    while True:
        try:
            check_crypto_risk()
            if not trading_paused and not vacation_mode: scan_crypto()
        except Exception as e: record_error(f"thread_crypto: {e}")
        time.sleep(INTERVAL_CRYPTO)

def thread_stocks():
    while True:
        try:
            if not trading_paused and not vacation_mode and is_market_open():
                manage_hold_portfolio()
                scan_stocks()
        except Exception as e: record_error(f"thread_stocks: {e}")
        time.sleep(INTERVAL_STOCKS)

def thread_risk():
    while True:
        try:
            check_custom_alerts()
            # La boucle check_risk locale n'est plus nécessaire pour les actions (géré par Bracket Orders)
        except Exception as e: record_error(f"thread_risk: {e}")
        time.sleep(INTERVAL_RISK)

def thread_scheduler():
    briefing_sent = daily_sent = weekly_sent = monthly_sent = annual_sent = None
    while True:
        try:
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")
            account = get_account_info()
            update_equity_checkpoints(account["equity"])
            check_market_health()

            if now.hour == 15 and now.minute == 25 and briefing_sent != today:
                send_premarket_briefing(); briefing_sent = today
            if now.hour == 21 and now.minute < 5 and daily_sent != today:
                send_daily_report(); daily_sent = today
            wk = now.strftime("%Y-%W")
            if now.weekday() == 0 and now.hour == 8 and now.minute < 5 and weekly_sent != wk:
                send_weekly_report(); weekly_sent = wk
            mo = now.strftime("%Y-%m")
            if now.day == 1 and now.hour == 9 and now.minute < 5 and monthly_sent != mo:
                run_dca(); send_monthly_report(); monthly_sent = mo
            yr = now.strftime("%Y")
            if now.month == 1 and now.day == 1 and now.hour == 10 and now.minute < 5 and annual_sent != yr:
                send_annual_report(); annual_sent = yr
        except Exception as e:
            record_error(f"thread_scheduler: {e}")
        time.sleep(INTERVAL_SCHEDULER)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

def start_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT",8080))), HealthHandler).serve_forever()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    send_telegram(
        "🤖 <b>Trading Agent V2 Démarré !</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 <b>ALPACA (MODE PAPER ACTIF)</b>\n"
        "🔒 Hold 60% — Claude choisit dynamiquement\n"
        "⚡ Trade 40% — Ordres Bracket (SL/TP gérés par Alpaca)\n\n"
        "₿ <b>COINBASE</b>\n"
        "🔒 Hold strict — BTC 25% | ETH 15%\n"
        "⚖️ Hold souple — SOL 5% | XRP 3% | LINK 2%\n"
        "⚡ Trade 50% — Avec sécurité RSI\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Filtre anti-requêtes excessives activé.</i>\n"
        "Tape /aide 👇"
    )

    account = get_account_info()
    update_equity_checkpoints(account["equity"])
    log(f"💼 Capital : ${account['equity']:.2f} | Cash : ${account['cash']:.2f}")

    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram,     daemon=True).start()
    threading.Thread(target=thread_crypto,       daemon=True).start()
    threading.Thread(target=thread_stocks,       daemon=True).start()
    threading.Thread(target=thread_risk,         daemon=True).start()
    threading.Thread(target=thread_scheduler,    daemon=True).start()
    threading.Thread(target=thread_news_watcher, daemon=True).start()

    log("✅ Tous les threads démarrés.")

    while True:
        time.sleep(60)
        log(f"💓 Alive | {'OUVERT' if is_market_open() else 'fermé'} | "
            f"{'⏸️ PAUSE' if trading_paused else '✅ ACTIF'}")

if __name__ == "__main__":
    main()
