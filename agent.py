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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# CONFIGURATION

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALPACA_API_KEY    = os.getenv(“ALPACA_API_KEY”)
ALPACA_SECRET_KEY = os.getenv(“ALPACA_SECRET_KEY”)
ANTHROPIC_API_KEY = os.getenv(“ANTHROPIC_API_KEY”)
NEWS_API_KEY      = os.getenv(“NEWS_API_KEY”)
TELEGRAM_TOKEN    = os.getenv(“TELEGRAM_TOKEN”)
TELEGRAM_CHAT_ID  = os.getenv(“TELEGRAM_CHAT_ID”)
COINBASE_API_KEY  = os.getenv(“COINBASE_API_KEY”)
COINBASE_SECRET   = os.getenv(“COINBASE_SECRET_KEY”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# PORTEFEUILLE ALPACA

# 🔒 HOLD 60%  — Claude choisit dynamiquement

# ⚡ TRADE 40% — Long/Short, tout Alpaca, illimité/jour

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HOLD_PCT               = 0.60
DAYTRADE_PCT           = 0.40
MAX_HOLD_POSITIONS     = 8
MAX_DAYTRADE_POSITIONS = 4
STOCK_SL_PCT           = 3.0
STOCK_TP_PCT           = 6.0
STOCK_MIN_CONFIDENCE   = 72

DAYTRADE_UNIVERSE = [
“AAPL”,“MSFT”,“NVDA”,“AMZN”,“GOOGL”,“META”,“TSLA”,
“AMD”,“INTC”,“QCOM”,“AVGO”,“MU”,
“JPM”,“BAC”,“GS”,“V”,“MA”,“PYPL”,
“LLY”,“UNH”,“PFE”,“MRNA”,“ABBV”,
“XOM”,“CVX”,
“SHOP”,“UBER”,“ABNB”,“COIN”,“PLTR”,“RBLX”,
“SNAP”,“ROKU”,“DKNG”,“HOOD”,“MELI”,
“QQQ”,“SPY”,“TQQQ”,“SQQQ”,“SPXL”,“UVXY”,“ARKK”,
“XLK”,“XLF”,“XLE”,“XLV”,“XLI”,
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# PORTEFEUILLE COINBASE (EUR)

# 🔒 HOLD STRICT 40% — BTC 25% | ETH 15% (jamais vendus)

# 🔒 HOLD SOUPLE 10% — SOL 5% | XRP 3% | LINK 2%

# ⚡ TRADE 50%        — Scalping actif 24/7

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CRYPTO_HOLD_STRICT = [“BTC-EUR”, “ETH-EUR”]
CRYPTO_HOLD_SOUPLE = [“SOL-EUR”, “XRP-EUR”, “LINK-EUR”]
CRYPTO_HOLD_ALL    = CRYPTO_HOLD_STRICT + CRYPTO_HOLD_SOUPLE
CRYPTO_HOLD_ALLOC  = {
“BTC-EUR”: 0.25, “ETH-EUR”: 0.15,
“SOL-EUR”: 0.05, “XRP-EUR”: 0.03, “LINK-EUR”: 0.02,
}

# Paramètres scalping (corrigés pour couvrir les frais Coinbase)

COINBASE_FEE_PCT              = 1.2    # frais aller-retour Coinbase
CRYPTO_SL_PCT                 = 2.5   # SL net > frais (était 1% → trop serré)
CRYPTO_TP_PCT                 = 4.0   # TP net rentable après frais (était 1.5%)
TRAILING_STOP_PCT             = 1.0   # trailing stop après pic
CRYPTO_RISK_PER_TRADE         = 0.15  # 15% du cash dispo par trade
MAX_CRYPTO_POSITIONS          = 8
CRYPTO_MIN_CONFIDENCE         = 65
CRYPTO_CIRCUIT_BREAKER_LOSSES = 3     # pause après 3 pertes consécutives
CRYPTO_CANDLE_WINDOW_HOURS    = 24    # fenêtre bougies 5min

CRYPTO_UNIVERSE_RAW = [
“BTC-EUR”,“ETH-EUR”,“SOL-EUR”,“XRP-EUR”,
“ADA-EUR”,“DOGE-EUR”,“LTC-EUR”,“DOT-EUR”,
“LINK-EUR”,“AVAX-EUR”,“UNI-EUR”,“ATOM-EUR”,
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# OBJECTIFS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WEEKLY_GOAL_PCT  = 1.0
MONTHLY_GOAL_EUR = 100
ANNUAL_GOAL_PCT  = 20.0
DCA_MONTHLY_EUR  = 100
MEMORY_FILE      = “trade_memory.json”

# Intervals threads

INTERVAL_CRYPTO    = 20
INTERVAL_STOCKS    = 120
INTERVAL_RISK      = 30
INTERVAL_SCHEDULER = 60

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# CLIENTS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

try:
coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
except Exception as e:
coinbase = None
print(f”Coinbase init error: {e}”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ÉTAT GLOBAL

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

trading_paused       = False
vacation_mode        = False
custom_alerts        = {}
active_stock_trades  = {}
active_crypto_trades = {}
loss_streak          = 0
_lock                = threading.RLock()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# VALIDATION PRODUITS COINBASE

# (du code fonctionnel — évite les erreurs d’ordre)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_valid_products(retries=3, delay=5):
if not coinbase: return set()
for attempt in range(retries):
try:
response = coinbase.get_products()
products = response.get(“products”, [])
result   = {p[“product_id”] for p in products if “product_id” in p}
if result: return result
except Exception as e:
print(f”Erreur produits (tentative {attempt+1}): {e}”)
if attempt < retries - 1: time.sleep(delay)
return set()

VALID_PRODUCTS = get_valid_products()
CRYPTO_UNIVERSE = [s for s in CRYPTO_UNIVERSE_RAW if s in VALID_PRODUCTS]
print(f”🚀 {len(CRYPTO_UNIVERSE)} crypto actives: {CRYPTO_UNIVERSE}”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# UTILITAIRES

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def progress_bar(current, goal, length=10):
if goal == 0: return “░” * length
pct    = min(current / goal, 1.0)
filled = int(pct * length)
return f”{‘█’*filled}{‘░’*(length-filled)} {pct*100:.0f}%”

def log(msg):
print(f”[{datetime.now().strftime(’%Y-%m-%d %H:%M:%S’)}] {msg}”)

def send_telegram(msg):
try:
requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”,
json={“chat_id”: TELEGRAM_CHAT_ID, “text”: msg, “parse_mode”: “HTML”},
timeout=10
)
except Exception as e:
log(f”Telegram error: {e}”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# MÉMOIRE

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_memory():
if os.path.exists(MEMORY_FILE):
try:
with open(MEMORY_FILE, “r”) as f:
return json.load(f)
except: pass
return {
“trades”: [], “hold_portfolio”: {},
“stats”: {“wins”: 0, “losses”: 0, “total_pnl”: 0},
“monthly_stats”: {}, “annual_stats”: {},
“patterns”: {}, “errors”: [], “equity_start”: {}
}

def save_memory(memory):
with _lock:
tmp = MEMORY_FILE + “.tmp”
with open(tmp, “w”) as f:
json.dump(memory, f, indent=2)
os.replace(tmp, MEMORY_FILE)

def record_trade(symbol, side, qty, price, pnl=None):
with _lock:
memory = load_memory()
now    = datetime.now()
month  = now.strftime(”%Y-%m”)
year   = now.strftime(”%Y”)
trade  = {“date”: now.strftime(”%Y-%m-%d %H:%M”), “symbol”: symbol,
“side”: side, “qty”: qty, “price”: price, “pnl”: pnl}
memory[“trades”].append(trade)
if pnl is not None:
memory[“stats”][“total_pnl”] += pnl
if pnl > 0: memory[“stats”][“wins”] += 1
else:       memory[“stats”][“losses”] += 1
ms = memory[“monthly_stats”].setdefault(month, {“wins”:0,“losses”:0,“pnl”:0,“trades”:[]})
ms[“pnl”] += pnl
if pnl > 0: ms[“wins”] += 1
else:       ms[“losses”] += 1
ms[“trades”].append(trade)
ys = memory[“annual_stats”].setdefault(year, {“wins”:0,“losses”:0,“pnl”:0})
ys[“pnl”] += pnl
if pnl > 0: ys[“wins”] += 1
else:       ys[“losses”] += 1
p = memory[“patterns”].setdefault(symbol, {“wins”:0,“losses”:0,“total_pnl”:0})
p[“total_pnl”] += pnl
if pnl > 0: p[“wins”] += 1
else:        p[“losses”] += 1
memory[“trades”] = memory[“trades”][-200:]
save_memory(memory)

def record_error(msg):
memory = load_memory()
memory[“errors”].append({“date”: datetime.now().strftime(”%Y-%m-%d %H:%M”), “error”: str(msg)})
memory[“errors”] = memory[“errors”][-20:]
save_memory(memory)

def update_equity_checkpoints(equity):
memory = load_memory()
now    = datetime.now()
es     = memory[“equity_start”]
for key, fmt in [(“week”,”%Y-%W”),(“month”,”%Y-%m”),(“year”,”%Y”)]:
k = now.strftime(fmt)
if es.get(f”{key}_key”) != k:
es[key] = equity
es[f”{key}_key”] = k
memory[“equity_start”] = es
save_memory(memory)

def get_equity_checkpoints():
return load_memory().get(“equity_start”, {})

def get_stats():
m = load_memory(); s = m[“stats”]
total = s[“wins”] + s[“losses”]
return {**s, “winrate”: (s[“wins”]/total*100) if total > 0 else 0, “recent”: m[“trades”][-5:]}

def get_monthly_stats(month=None):
if not month: month = datetime.now().strftime(”%Y-%m”)
return load_memory()[“monthly_stats”].get(month, {“wins”:0,“losses”:0,“pnl”:0,“trades”:[]})

def get_annual_stats(year=None):
if not year: year = datetime.now().strftime(”%Y”)
return load_memory()[“annual_stats”].get(year, {“wins”:0,“losses”:0,“pnl”:0})

def get_winrate(symbol):
p = load_memory()[“patterns”].get(symbol)
if not p: return None
t = p[“wins”] + p[“losses”]
return (p[“wins”]/t*100) if t > 0 else None

def get_best_worst(trades):
w = [t for t in trades if t.get(“pnl”) is not None]
if not w: return None, None
return max(w, key=lambda x: x[“pnl”]), min(w, key=lambda x: x[“pnl”])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DONNÉES MARCHÉ — ALPACA

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_market_open():
now = datetime.utcnow()
if now.weekday() >= 5: return False
return (now.replace(hour=13,minute=30,second=0,microsecond=0)
<= now <=
now.replace(hour=20,minute=0,second=0,microsecond=0))

def get_account_info():
a = trading_client.get_account()
return {“equity”: float(a.equity), “cash”: float(a.cash),
“buying_power”: float(a.buying_power),
“pnl”: float(a.equity) - float(a.last_equity)}

def get_positions():
return {p.symbol: {
“qty”: float(p.qty), “value”: float(p.market_value),
“avg_price”: float(p.avg_entry_price),
“pnl”: float(p.unrealized_pl),
“pnl_pct”: float(p.unrealized_plpc) * 100,
“side”: “long” if float(p.qty) > 0 else “short”
} for p in trading_client.get_all_positions()}

def get_price(ticker):
try:
return data_client.get_stock_latest_bar(
StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
except: return None

def get_market_perf(ticker):
try:
cur  = data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
bars = list(data_client.get_stock_bars(StockBarsRequest(
symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
start=datetime.now()-timedelta(days=3)))[ticker])
return ((cur-bars[-2].close)/bars[-2].close)*100 if len(bars) >= 2 else 0
except: return 0

def get_spy_perf(): return get_market_perf(“SPY”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DONNÉES MARCHÉ — COINBASE

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_crypto_price(symbol):
try:
if not coinbase: return None
pb = coinbase.get_best_bid_ask(product_ids=[symbol])
return float(pb[“pricebooks”][0][“asks”][0][“price”])
except: return None

def get_crypto_balance(currency):
try:
if not coinbase: return 0
for acc in coinbase.get_accounts()[“accounts”]:
if acc[“currency”] == currency:
return float(acc[“available_balance”][“value”])
return 0
except: return 0

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ANALYSE TECHNIQUE — ACTIONS (Alpaca)

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
return {“rsi”: rsi, “ma20”: ma20, “ma50”: ma50, “current”: cur,
“trend”: “haussier” if (ma50 and ma20 > ma50) else “baissier”,
“above_ma20”: cur > ma20,
“week_perf”: ((cur-prices[-6])/prices[-6]*100) if len(prices) >= 6 else None}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ANALYSE TECHNIQUE — CRYPTO (bougies 5min Coinbase)

# Du code fonctionnel — bougies 5min, RSI court, breakout

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_crypto_ta(symbol):
try:
if not coinbase: return None
end_ts   = int(time.time())
start_ts = end_ts - CRYPTO_CANDLE_WINDOW_HOURS * 3600
candles  = coinbase.get_candles(
product_id=symbol,
start=str(start_ts),
end=str(end_ts),
granularity=“FIVE_MINUTE”
)
prices = [float(c[“close”]) for c in candles.get(“candles”,[])]
prices = prices[::-1]  # ordre chronologique
if len(prices) < 14: return None
rsi  = calculate_rsi(prices, period=7)
ma20 = sum(prices[-20:])/20 if len(prices) >= 20 else None
cur  = prices[-1]
return {“rsi”: rsi, “ma20”: ma20, “current”: cur,
“trend”: “haussier” if (ma20 and cur > ma20) else “baissier”,
“above_ma20”: cur > ma20 if ma20 else None,
“week_perf”: ((cur-prices[-7])/prices[-7]*100) if len(prices) >= 7 else None,
“prices”: prices}
except: return None

def detect_breakout_setup(prices, threshold=0.03):
“”“Breakout : prix dans les 3% sous le plus haut des 20 dernières bougies.”””
if not prices or len(prices) < 20: return False
recent_high = max(prices[-20:])
return (recent_high - prices[-1]) / recent_high <= threshold

def format_ta(ta):
if not ta: return “Donnees indisponibles”
rsi_txt = “”
if ta.get(“rsi”):
label = “Survendu” if ta[“rsi”] < 30 else “Surachete” if ta[“rsi”] > 70 else “Neutre”
rsi_txt = f”RSI {ta[‘rsi’]} {label}\n”
wp = f”Perf : {ta[‘week_perf’]:+.1f}%\n” if ta.get(“week_perf”) else “”
return f”{rsi_txt}Tendance : {ta[‘trend’]}\nMA20 : {‘OK’ if ta.get(‘above_ma20’) else ‘Attention’}\n{wp}”

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# NEWS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_news(ticker, count=5):
try:
q = ticker.replace(”-EUR”,””).replace(”-USD”,””).replace(“USDT”,””)
return requests.get(
f”https://newsapi.org/v2/everything?q={q}&language=en”
f”&sortBy=publishedAt&pageSize={count}&apiKey={NEWS_API_KEY}”,
timeout=10
).json().get(“articles”,[])
except: return []

def format_news(articles, count=3):
return “\n”.join([f”- {a[‘title’]}” for a in articles[:count]]) or “Aucune news recente”

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# CLAUDE IA — Prompts

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROMPT_HOLD = “”“Tu es un gestionnaire de portefeuille long terme.
Tu choisis dynamiquement les meilleurs actifs selon l’actu, les fondamentaux et le momentum.
Horizons : court (semaines), moyen (mois), long (annees) selon l’opportunite.
Univers : toutes les actions US, ETF sectoriels, ETF internationaux tradables sur Alpaca.
Reponds UNIQUEMENT en JSON :
{“action”:“BUY”|“SELL”|“HOLD”,“symbol”:“TICKER”,“horizon”:“court”|“moyen”|“long”,“confidence”:0-100,“reason”:“francais court”,“allocation_pct”:1-10}
Si aucune opportunite : {“action”:“HOLD”,“symbol”:””,“horizon”:””,“confidence”:0,“reason”:“pas d’opportunite”,“allocation_pct”:0}”””

PROMPT_STOCKS = “”“Tu es un day trader professionnel — actions US.
Long + Short selon le setup. RR minimum 1:2. Max 2% du capital par trade.
Si tu as perdu recemment sur ce ticker, sois plus prudent.
Reponds UNIQUEMENT en JSON :
{“action”:“BUY”|“SHORT”|“HOLD”,“confidence”:0-100,“reason”:“francais court”,“risk_pct”:1-2,“tp_pct”:3-15}”””

def ask_claude(prompt, user_msg):
try:
res = claude_client.messages.create(
model=“claude-sonnet-4-6”, max_tokens=400, system=prompt,
messages=[{“role”:“user”,“content”:user_msg}]
)
raw = res.content[0].text.strip().replace(”`json","").replace("`”,””).strip()
return json.loads(raw)
except Exception as e:
record_error(f”Claude error: {e}”)
return {“action”:“HOLD”,“confidence”:0,“reason”:“Erreur Claude”}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ORDRES — ALPACA

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def place_order(symbol, side, qty, tp_pct=None, label=””):
try:
order_side = OrderSide.BUY if side == “buy” else OrderSide.SELL
trading_client.submit_order(MarketOrderRequest(
symbol=symbol, qty=round(abs(qty),4),
side=order_side, time_in_force=TimeInForce.DAY
))
price  = get_price(symbol)
valeur = round(abs(qty)*price,2) if price else “?”
record_trade(symbol, side, round(abs(qty),4), price or 0)
if side == “buy”:
active_stock_trades[symbol] = {“side”:“long”,“qty”:qty,“entry”:price,“tp_pct”:tp_pct or STOCK_TP_PCT}
send_telegram(f”✅ <b>LONG {label}</b> <b>{symbol}</b>\n~${valeur}\n🛑 -{STOCK_SL_PCT}% | 🎯 +{tp_pct or STOCK_TP_PCT}%”)
else:
active_stock_trades.pop(symbol, None)
send_telegram(f”💰 <b>Cloture {label}</b> <b>{symbol}</b> ~${valeur}”)
except Exception as e:
record_error(f”Order {symbol}: {e}”)
send_telegram(f”❌ <b>Ordre echoue</b> {symbol}\n{str(e)[:100]}”)

def open_short(symbol, qty, tp_pct=None):
try:
trading_client.submit_order(MarketOrderRequest(
symbol=symbol, qty=round(abs(qty),4),
side=OrderSide.SELL, time_in_force=TimeInForce.DAY
))
price  = get_price(symbol)
valeur = round(abs(qty)*price,2) if price else “?”
record_trade(symbol, “short”, round(abs(qty),4), price or 0)
active_stock_trades[symbol] = {“side”:“short”,“qty”:qty,“entry”:price,“tp_pct”:tp_pct or STOCK_TP_PCT}
send_telegram(f”🔻 <b>SHORT Day Trade</b> <b>{symbol}</b>\n~${valeur}\n🛑 +{STOCK_SL_PCT}% | 🎯 -{tp_pct or STOCK_TP_PCT}%”)
except Exception as e:
record_error(f”Short {symbol}: {e}”)
send_telegram(f”❌ <b>Short echoue</b> {symbol}\n{str(e)[:100]}”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ORDRES — COINBASE (EUR)

# Du code fonctionnel — frais déduits, trailing stop, loss streak

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def place_crypto_order(symbol, side, amount_eur, tp_pct=None, label=””, reason=””):
global loss_streak
try:
if not coinbase: return
if side == “buy”:
coinbase.market_order_buy(
client_order_id=f”bot_{int(time.time())}”,
product_id=symbol, quote_size=str(round(amount_eur,2))
)
else:
price = get_crypto_price(symbol)
if not price: return
coinbase.market_order_sell(
client_order_id=f”bot_{int(time.time())}”,
product_id=symbol, base_size=str(round(amount_eur/price,8))
)
price = get_crypto_price(symbol)
record_trade(symbol, side, round(amount_eur/(price or 1),8), price or 0)
with _lock:
if side == “buy”:
active_crypto_trades[symbol] = {
“side”: “long”, “amount”: amount_eur, “entry”: price,
“peak”: price, “tp_pct”: tp_pct or CRYPTO_TP_PCT,
“reason”: reason, “entry_time”: datetime.utcnow().isoformat()
}
send_telegram(f”✅ <b>LONG {label}</b> 💎 <b>{symbol}</b>\n~{amount_eur:.2f}EUR\n🛑 -{CRYPTO_SL_PCT}% | 🎯 +{tp_pct or CRYPTO_TP_PCT}% (nets frais)”)
else:
entry_price = active_crypto_trades.get(symbol, {}).get(“entry”)
if entry_price and price:
net_pnl_pct = ((price-entry_price)/entry_price*100) - COINBASE_FEE_PCT
if net_pnl_pct < 0: loss_streak += 1
else:               loss_streak = 0
active_crypto_trades.pop(symbol, None)
send_telegram(f”💰 <b>Vente {label}</b> 💎 <b>{symbol}</b> ~{amount_eur:.2f}EUR\n(frais deduits)”)
except Exception as e:
record_error(f”Crypto {symbol}: {e}”)
send_telegram(f”❌ <b>Ordre crypto echoue</b> {symbol}\n{str(e)[:100]}”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DÉTECTION POSITIONS EXISTANTES

# Du code fonctionnel — récupère les cryptos déjà détenues

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_existing_holdings():
if not coinbase: return
for symbol in CRYPTO_UNIVERSE:
if symbol in CRYPTO_HOLD_STRICT: continue
if symbol in active_crypto_trades: continue
currency = symbol.replace(”-EUR”,””)
balance  = get_crypto_balance(currency)
if balance <= 0: continue
price = get_crypto_price(symbol)
if not price: continue
valeur = balance * price
if valeur < 1.0: continue
with _lock:
active_crypto_trades[symbol] = {
“side”: “long”, “amount”: valeur, “entry”: price,
“peak”: price, “tp_pct”: CRYPTO_TP_PCT,
“reason”: “Position existante detectee”,
“entry_time”: datetime.utcnow().isoformat()
}
log(f”Position detectee : {symbol} {balance:.6f} = {valeur:.2f}EUR”)
send_telegram(f”📍 <b>Position detectee</b> <b>{symbol}</b>\n{balance:.6f} = ~{valeur:.2f}EUR\nAjoutee au suivi TP/SL”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# POCHE HOLD — ALPACA (Claude décide dynamiquement)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def manage_hold_portfolio():
if trading_paused or vacation_mode or not is_market_open(): return
account   = get_account_info()
positions = get_positions()
memory    = load_memory()
hold_port = memory.get(“hold_portfolio”, {})
hold_pos  = {s: d for s, d in positions.items() if s in hold_port}
if len(hold_pos) >= MAX_HOLD_POSITIONS: return
hold_capital  = account[“equity”] * HOLD_PCT
hold_invested = sum(d[“value”] for d in hold_pos.values())
available     = hold_capital - hold_invested
if available < account[“equity”] * 0.01: return

```
signal = ask_claude(PROMPT_HOLD,
    f"Capital hold disponible: ${available:.2f}\n"
    f"SPY: {get_spy_perf():+.2f}%\n"
    f"News: {format_news(get_news('stocks market economy', count=3))}\n"
    f"Positions hold: {list(hold_port.keys()) or 'aucune'}\n"
    f"Quel actif ajouter ou renforcer ?"
)
if signal.get("action") == "HOLD" or not signal.get("symbol"): return
symbol  = signal["symbol"].upper()
conf    = signal.get("confidence", 0)
reason  = signal.get("reason", "")
horizon = signal.get("horizon", "moyen")
alloc   = signal.get("allocation_pct", 3) / 100
if conf < STOCK_MIN_CONFIDENCE: return

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
    send_telegram(f"🔒 <b>Sortie Hold</b> <b>{symbol}</b>\n{reason}")
    place_order(symbol, "sell", hold_pos[symbol]["qty"], label="Hold")
```

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DAY TRADING — ALPACA (Claude + TA)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def scan_stocks():
if trading_paused or vacation_mode or not is_market_open(): return
account   = get_account_info()
positions = get_positions()
hold_syms = set(load_memory().get(“hold_portfolio”, {}).keys())
trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
if len(trade_pos) >= MAX_DAYTRADE_POSITIONS: return
trade_capital = account[“equity”] * DAYTRADE_PCT

```
for ticker in DAYTRADE_UNIVERSE:
    if ticker in positions: continue
    price    = get_price(ticker)
    if not price: continue
    ta       = get_ta(ticker)
    articles = get_news(ticker, count=3)
    wr       = get_winrate(ticker)

    signal = ask_claude(PROMPT_STOCKS,
        f"Ticker: {ticker} | Prix: ${price:.2f}\n"
        f"TA:\n{format_ta(ta)}\n"
        f"News:\n{format_news(articles)}\n"
        + (f"Winrate: {wr:.0f}%\n" if wr else "")
    )
    action = signal.get("action","HOLD")
    conf   = signal.get("confidence",0)
    reason = signal.get("reason","")
    tp_pct = signal.get("tp_pct", STOCK_TP_PCT)
    risk   = signal.get("risk_pct", 1)
    if conf < STOCK_MIN_CONFIDENCE: continue
    qty = (trade_capital * risk / 100) / price

    if action == "BUY" and account["cash"] >= qty * price:
        send_telegram(f"💡 <b>Signal LONG</b>\n<b>{ticker}</b> ${price:.2f}\n{reason}\nConfiance : {conf}% | 🎯 +{tp_pct}%")
        place_order(ticker, "buy", qty, tp_pct=tp_pct, label="Day Trade")
        break
    elif action == "SHORT":
        send_telegram(f"💡 <b>Signal SHORT</b>\n<b>{ticker}</b> ${price:.2f}\n{reason}\nConfiance : {conf}% | 🎯 -{tp_pct}%")
        open_short(ticker, qty, tp_pct=tp_pct)
        break
    time.sleep(0.5)
```

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DAY TRADING — CRYPTO SCALPING 24/7

# Du code fonctionnel — RSI + breakout + circuit breaker

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def scan_crypto():
if trading_paused or vacation_mode or not coinbase: return
if loss_streak >= CRYPTO_CIRCUIT_BREAKER_LOSSES:
log(f”Circuit breaker: {loss_streak} pertes consecutives, scan suspendu”)
return
if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS: return
cash_eur = get_crypto_balance(“EUR”)
if cash_eur < 5: return

```
for symbol in CRYPTO_UNIVERSE:
    if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS: break
    if symbol in active_crypto_trades: continue
    price = get_crypto_price(symbol)
    ta    = get_crypto_ta(symbol)
    if not price or not ta or not ta.get("rsi"): continue
    if ta.get("week_perf") is None or abs(ta["week_perf"]) < 0.1: continue

    rsi        = ta["rsi"]
    trend      = ta["trend"]
    above_ma20 = ta.get("above_ma20")
    prices     = ta.get("prices", [])
    has_setup  = detect_breakout_setup(prices)

    # Setup valide : RSI survendu + tendance haussière OU breakout en cours
    if (rsi < 35 and trend == "haussier" and above_ma20) or \
       (40 <= rsi <= 60 and has_setup):
        amount = cash_eur * CRYPTO_RISK_PER_TRADE
        if amount < 2: continue
        reason = f"RSI={rsi:.0f} {'breakout' if has_setup else ''} tendance={trend}"
        place_crypto_order(symbol, "buy", amount,
                           tp_pct=CRYPTO_TP_PCT, label="Scalping", reason=reason)
    time.sleep(0.3)
```

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# GESTION DU RISQUE — ACTIONS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_stock_risk():
positions = get_positions()
hold_syms = set(load_memory().get(“hold_portfolio”, {}).keys())
for symbol, data in positions.items():
if symbol in hold_syms: continue
pnl_pct = data[“pnl_pct”]
trade   = active_stock_trades.get(symbol, {})
tp_pct  = trade.get(“tp_pct”, STOCK_TP_PCT)
side    = trade.get(“side”,“long”)
if side == “long”:
if pnl_pct <= -STOCK_SL_PCT:
send_telegram(f”🛑 <b>Stop Loss</b> <b>{symbol}</b> -{abs(pnl_pct):.1f}%”)
place_order(symbol, “sell”, data[“qty”], label=“SL”)
elif pnl_pct >= tp_pct:
send_telegram(f”🎯 <b>Take Profit</b> <b>{symbol}</b> +{pnl_pct:.1f}%”)
place_order(symbol, “sell”, data[“qty”], label=“TP”)
elif side == “short”:
if pnl_pct <= -STOCK_SL_PCT:
send_telegram(f”🛑 <b>Stop Loss SHORT</b> <b>{symbol}</b> -{abs(pnl_pct):.1f}%”)
place_order(symbol, “buy”, abs(data[“qty”]), label=“SL”)
elif pnl_pct >= tp_pct:
send_telegram(f”🎯 <b>Take Profit SHORT</b> <b>{symbol}</b> +{pnl_pct:.1f}%”)
place_order(symbol, “buy”, abs(data[“qty”]), label=“TP”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# GESTION DU RISQUE — CRYPTO

# Du code fonctionnel — trailing stop, frais nets, peak tracking

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_crypto_risk():
if not coinbase: return
for symbol, trade in list(active_crypto_trades.items()):
if symbol in CRYPTO_HOLD_STRICT: continue
price = get_crypto_price(symbol)
if not price: continue
entry         = trade.get(“entry”, price)
tp_pct        = trade.get(“tp_pct”, CRYPTO_TP_PCT)
gross_pnl_pct = ((price-entry)/entry*100) if entry else 0
net_pnl_pct   = gross_pnl_pct - COINBASE_FEE_PCT
currency      = symbol.replace(”-EUR”,””)
balance       = get_crypto_balance(currency)
if balance <= 0: continue

```
    # Mise à jour du pic de prix (trailing stop)
    with _lock:
        current_peak = trade.get("peak", entry)
        if price > current_peak:
            active_crypto_trades[symbol]["peak"] = price
            current_peak = price
    trailing_drop = ((current_peak-price)/current_peak*100) if current_peak else 0

    if net_pnl_pct <= -CRYPTO_SL_PCT:
        send_telegram(f"🛑 <b>Stop Loss crypto</b> {symbol} (Net: -{abs(net_pnl_pct):.1f}%)")
        place_crypto_order(symbol, "sell", balance*price, label="SL")
    elif trailing_drop >= TRAILING_STOP_PCT and net_pnl_pct > 0:
        send_telegram(f"📉 <b>Trailing Stop</b> {symbol} (-{trailing_drop:.1f}% depuis pic)")
        place_crypto_order(symbol, "sell", balance*price, label="TS")
    elif net_pnl_pct >= tp_pct:
        send_telegram(f"🎯 <b>Take Profit crypto</b> {symbol} (Net: +{net_pnl_pct:.1f}%)")
        place_crypto_order(symbol, "sell", balance*price, label="TP")
```

def check_market_health():
global trading_paused
spy = get_spy_perf()
if spy <= -10:
send_telegram(f”🚨 <b>CRASH !</b> SPY {spy:.1f}%\nTape /urgence”)
elif spy <= -5:
trading_paused = True
send_telegram(f”⚠️ <b>Forte baisse SPY {spy:.1f}%</b>\nDay trading suspendu.”)
elif spy <= -3:
send_telegram(f”📉 Marche sous tension SPY {spy:.1f}%”)

def check_custom_alerts():
for symbol, target in list(custom_alerts.items()):
price = get_crypto_price(symbol) if “-EUR” in symbol else get_price(symbol)
if price and price >= target:
send_telegram(f”🔔 <b>ALERTE !</b> <b>{symbol}</b> atteint {price:.2f} ✅”)
del custom_alerts[symbol]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# DCA MENSUEL

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_dca():
if trading_paused or vacation_mode:
send_telegram(“DCA annule — pause.”); return
send_telegram(”<b>DCA mensuel</b> en cours…”)
dca_eur = DCA_MONTHLY_EUR
for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
amount = dca_eur * 0.70 * alloc
if amount >= 1:
place_crypto_order(symbol, “buy”, amount, label=“DCA”)
account = get_account_info()
signal  = ask_claude(PROMPT_HOLD,
f”DCA mensuel de ${dca_eur*0.30:.2f} USD disponible.\n”
f”Positions hold: {list(load_memory().get(‘hold_portfolio’,{}).keys())}\n”
f”Quel actif renforcer ce mois-ci ?”
)
if signal.get(“action”) == “BUY” and signal.get(“symbol”):
price = get_price(signal[“symbol”].upper())
if price:
place_order(signal[“symbol”].upper(), “buy”, (dca_eur*0.30)/price, label=“DCA”)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# RAPPORTS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_daily_report(immediate=False):
account   = get_account_info()
positions = get_positions()
stats     = get_stats()
spy       = get_spy_perf()
checkpts  = get_equity_checkpoints()
hold_syms = set(load_memory().get(“hold_portfolio”,{}).keys())
hold_pos  = {s: d for s, d in positions.items() if s in hold_syms}
trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
wk        = checkpts.get(“week”, account[“equity”])
wk_pnl    = account[“equity”] - wk
btc_val   = get_crypto_balance(“BTC”) * (get_crypto_price(“BTC-EUR”) or 0)
eth_val   = get_crypto_balance(“ETH”) * (get_crypto_price(“ETH-EUR”) or 0)
cash_eur  = get_crypto_balance(“EUR”)
titre     = “<b>Rapport immediat</b>” if immediate else “<b>Rapport du soir</b>”

```
r  = f"{titre}\n{'='*22}\n\n"
r += f"💰 Actions : <b>${account['equity']:.2f}</b>\n"
r += f"₿ Crypto hold : ~{btc_val+eth_val:.2f}EUR\n"
r += f"💵 Cash USD : ${account['cash']:.2f} | EUR : {cash_eur:.2f}EUR\n"
r += f"{'📈' if account['pnl']>=0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f} | SPY : {spy:+.2f}%\n\n"

r += f"🔒 <b>Hold ({int(HOLD_PCT*100)}%) — {len(hold_pos)} pos</b>\n"
for s, d in hold_pos.items():
    hp = load_memory()["hold_portfolio"].get(s,{})
    r += f"  {'🟢' if d['pnl_pct']>=0 else '🔴'} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%) [{hp.get('horizon','?')}]\n"
if not hold_pos: r += "  (Claude cherche)\n"

r += f"\n⚡ <b>Day Trade ({int(DAYTRADE_PCT*100)}%) — {len(trade_pos)} pos</b>\n"
for s, d in trade_pos.items():
    t = active_stock_trades.get(s,{})
    r += f"  {'📈' if t.get('side','long')=='long' else '📉'} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%)\n"
if not trade_pos: r += "  (aucune)\n"

r += f"\n💎 <b>Crypto scalping — {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS} pos</b>\n"
for s, t in active_crypto_trades.items():
    entry   = t.get("entry") or 0
    price   = get_crypto_price(s) or entry
    net_pnl = ((price-entry)/entry*100 - COINBASE_FEE_PCT) if entry else 0
    r += f"  <b>{s}</b> {t['amount']:.2f}EUR ({net_pnl:+.1f}% net)\n"

r += f"\n🎯 Semaine :\n{progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n"
r += f"\nReussite : {stats['winrate']:.0f}% | PnL : ${stats['total_pnl']:+.2f}\n"
r += f"{'🏖️' if vacation_mode else '⏸️' if trading_paused else '✅'} | {'🟢' if is_market_open() else '🔴'}"
send_telegram(r)
```

def send_weekly_report():
account  = get_account_info()
stats    = get_stats()
spy      = get_spy_perf()
checkpts = get_equity_checkpoints()
memory   = load_memory()
wk_ago   = (datetime.now()-timedelta(days=7)).strftime(”%Y-%m-%d”)
wk_trades = [t for t in memory[“trades”] if t[“date”] >= wk_ago]
best, worst = get_best_worst(wk_trades)
wk = checkpts.get(“week”, account[“equity”])
mo = checkpts.get(“month”, account[“equity”])
yr = checkpts.get(“year”, account[“equity”])
wk_pnl = account[“equity”] - wk
mo_pnl = account[“equity”] - mo
yr_pnl = account[“equity”] - yr
vs_spy = account[“pnl”] - (account[“equity”]*spy/100)

```
r  = "<b>RESUME SEMAINE</b>\n" + "="*22 + "\n\n"
r += f"💰 <b>${account['equity']:.2f}</b> | {'📈' if wk_pnl>=0 else '📉'} ${wk_pnl:+.2f}\n"
r += f"SPY : {spy:+.2f}% | {'✅ Je bats le marche !' if vs_spy>0 else '📉 Marche > moi'}\n\n"
r += f"Semaine : {progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n"
r += f"Mois    : {progress_bar(max(mo_pnl,0), MONTHLY_GOAL_EUR)} ${mo_pnl:+.2f}\n"
r += f"Annee   : {progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)} ${yr_pnl:+.2f}\n\n"
r += f"📊 {len(wk_trades)} trades | {stats['winrate']:.0f}% reussite\n"
if best and best.get("pnl"): r += f"🏆 {best['symbol']} +${best['pnl']:.2f}\n"
if worst and worst.get("pnl"): r += f"💔 {worst['symbol']} ${worst['pnl']:.2f}\n"
r += "\nBonne semaine ! 💪"
send_telegram(r)
```

def send_monthly_report():
account  = get_account_info()
checkpts = get_equity_checkpoints()
month    = datetime.now().strftime(”%Y-%m”)
ms       = get_monthly_stats(month)
mo       = checkpts.get(“month”, account[“equity”])
yr       = checkpts.get(“year”, account[“equity”])
mo_pnl   = account[“equity”] - mo
yr_pnl   = account[“equity”] - yr
total_m  = ms[“wins”]+ms[“losses”]

```
r  = f"<b>BILAN {datetime.now().strftime('%B %Y').upper()}</b>\n" + "="*22 + "\n\n"
r += f"💰 <b>${account['equity']:.2f}</b> | Ce mois : ${mo_pnl:+.2f}\n\n"
r += f"Mois  : {progress_bar(max(mo_pnl,0), MONTHLY_GOAL_EUR)} ${mo_pnl:+.2f}\n"
r += f"Annee : {progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)} ${yr_pnl:+.2f}\n\n"
r += f"{len(ms.get('trades',[]))} trades"
if total_m > 0: r += f" | {ms['wins']/total_m*100:.0f}% reussite"
if mo_pnl > 0: r += f"\n🧾 Impot estime (30%) : ~${mo_pnl*0.30:.2f}"
send_telegram(r)
```

def send_annual_report():
account  = get_account_info()
checkpts = get_equity_checkpoints()
year     = str(datetime.now().year)
ys       = get_annual_stats(year)
yr       = checkpts.get(“year”, account[“equity”])
yr_pnl   = account[“equity”] - yr
total_y  = ys[“wins”]+ys[“losses”]
proj_5y  = account[“equity”]*((1+yr_pnl/max(yr,1))**5)

```
r  = f"<b>BILAN ANNUEL {year}</b>\n" + "="*22 + "\n\n"
r += f"💰 <b>${account['equity']:.2f}</b> | PnL : ${yr_pnl:+.2f}\n\n"
r += f"Objectif +{ANNUAL_GOAL_PCT}% :\n{progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)} ${yr_pnl:+.2f}\n\n"
r += f"{total_y} trades"
if total_y > 0: r += f" | {ys['wins']/total_y*100:.0f}% reussite"
r += f"\nProjection 5 ans : ~${proj_5y:.2f}\n"
if yr_pnl > 0: r += f"🧾 Impot estime (30%) : ~${yr_pnl*0.30:.2f}\n"
r += f"\nBonne annee ! 🚀"
send_telegram(r)
```

def send_morning_briefing():
account   = get_account_info()
btc_price = get_crypto_price(“BTC-EUR”) or 0
eth_price = get_crypto_price(“ETH-EUR”) or 0
cash_eur  = get_crypto_balance(“EUR”)
spy       = get_spy_perf()
checkpts  = get_equity_checkpoints()
wk        = checkpts.get(“week”, account[“equity”])
wk_pnl    = account[“equity”] - wk
intl      = []
for ticker, name in [(“EWJ”,“🇯🇵”),(“FXI”,“🇨🇳”),(“EWG”,“🇩🇪”)]:
p = get_market_perf(ticker)
intl.append(f”{‘🟢’ if p>0.5 else ‘🔴’ if p<-0.5 else ‘🟡’} {name} {p:+.2f}%”)

```
r  = "<b>BRIEFING MATIN</b>\n" + "="*22 + "\n\n"
r += f"💼 ${account['equity']:.2f} | Cash EUR : {cash_eur:.2f}EUR\n"
r += f"🇺🇸 SPY : {spy:+.2f}%  {'  '.join(intl)}\n\n"
r += f"₿ BTC : {btc_price:.2f}EUR | ETH : {eth_price:.2f}EUR\n"
r += f"💎 Scalping actif : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS}\n\n"
r += f"🎯 Semaine : {progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n\n"
sentiment = "🟢 Favorable" if spy>0.5 else "🔴 Defavorable" if spy<-0.5 else "🟡 Neutre"
r += f"Sentiment : {sentiment}\nC'est parti !"
send_telegram(r)
```

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# COMMANDES TELEGRAM

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_aide():
send_telegram(
“<b>Commandes</b>\n\n”
“📊 /status | /positions | /hold\n”
“/crypto | /report | /historique\n”
“/marche | /objectifs\n”
“/technique NVDA\n”
“/pourquoi BTC-EUR\n\n”
“📅 /briefing | /semaine\n”
“/mois | /annee\n\n”
“⚙️ /pause | /resume\n”
“/vacances | /retour\n\n”
“🔔 /alerte BTC-EUR 90000\n”
“/alertes\n\n”
“🔍 /scan_holdings\n\n”
“🚨 /urgence — Ferme les trades\n”
“(poche hold conservee)”
)

def cmd_pourquoi(symbol):
“”“Du code fonctionnel — affiche le raisonnement d’un trade actif.”””
symbol = symbol.upper()
trade  = active_crypto_trades.get(symbol)
if trade:
send_telegram(f”<b>Raisonnement {symbol} :</b>\n\n{trade.get(‘reason’, ‘Non sauvegarde.’)}”)
else:
send_telegram(f”Aucun trade actif pour {symbol}.”)

def cmd_status():
account   = get_account_info()
stats     = get_stats()
spy       = get_spy_perf()
ms        = get_monthly_stats()
ys        = get_annual_stats()
positions = get_positions()
hold_syms = set(load_memory().get(“hold_portfolio”,{}).keys())
hold_pos  = {s: d for s, d in positions.items() if s in hold_syms}
trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
checkpts  = get_equity_checkpoints()
wk        = checkpts.get(“week”, account[“equity”])
wk_pnl    = account[“equity”] - wk
btc_val   = get_crypto_balance(“BTC”) * (get_crypto_price(“BTC-EUR”) or 0)
eth_val   = get_crypto_balance(“ETH”) * (get_crypto_price(“ETH-EUR”) or 0)
cash_eur  = get_crypto_balance(“EUR”)

```
send_telegram(
    f"💼 <b>Portefeuille</b>\n\n"
    f"💰 Actions : <b>${account['equity']:.2f}</b>\n"
    f"₿ Crypto hold : ~{btc_val+eth_val:.2f}EUR\n"
    f"💵 Cash USD : ${account['cash']:.2f} | EUR : {cash_eur:.2f}EUR\n"
    f"{'📈' if account['pnl']>=0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f}\n\n"
    f"🔒 Hold : {len(hold_pos)} pos | ⚡ Trade : {len(trade_pos)} pos | 💎 Scalping : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS}\n\n"
    f"📅 Mois : ${ms['pnl']:+.2f} | 🗓️ Annee : ${ys['pnl']:+.2f}\n\n"
    f"🎯 Semaine :\n{progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)} ${wk_pnl:+.2f}\n\n"
    f"Reussite : {stats['winrate']:.0f}% | SPY : {spy:+.2f}%\n"
    f"{'🏖️' if vacation_mode else '⏸️' if trading_paused else '✅'} | {'🟢' if is_market_open() else '🔴'}"
)
```

def cmd_hold():
memory    = load_memory()
hold_port = memory.get(“hold_portfolio”,{})
positions = get_positions()
if not hold_port:
send_telegram(“🔒 Poche hold vide — Claude cherche.”); return
msg = “🔒 <b>Poche HOLD</b>\n\n”
for s, info in hold_port.items():
pos  = positions.get(s,{})
pnl  = f” ({pos[‘pnl_pct’]:+.2f}%)” if pos else “”
msg += f”<b>{s}</b> [{info.get(‘horizon’,’?’)}] depuis {info.get(‘date’,’?’)}{pnl}\n”
send_telegram(msg)

def cmd_positions():
positions = get_positions()
hold_syms = set(load_memory().get(“hold_portfolio”,{}).keys())
trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
if not trade_pos:
send_telegram(“⚡ Aucun day trade actif.”); return
msg = “⚡ <b>Day Trades actions</b>\n\n”
for s, d in trade_pos.items():
t = active_stock_trades.get(s,{})
emoji = “📈” if t.get(“side”,“long”)==“long” else “📉”
msg += f”{emoji} <b>{s}</b> ${d[‘value’]:.2f} ({d[‘pnl_pct’]:+.2f}%) 🎯+{t.get(‘tp_pct’,STOCK_TP_PCT)}%\n”
send_telegram(msg)

def cmd_crypto():
if not coinbase:
send_telegram(“Coinbase non connecte.”); return
lines = []
total = 0
for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
currency = symbol.replace(”-EUR”,””)
price    = get_crypto_price(symbol) or 0
balance  = get_crypto_balance(currency)
val      = balance * price
total   += val
strict   = “🔒” if symbol in CRYPTO_HOLD_STRICT else “⚖️”
lines.append(f”{strict} <b>{currency}</b> {balance:.6f} = {val:.2f}EUR”)
cash_eur = get_crypto_balance(“EUR”)
msg  = “₿ <b>Poche Hold Crypto</b>\n\n” + “\n”.join(lines)
msg += f”\n\nTotal hold : ~{total:.2f}EUR\nCash EUR : {cash_eur:.2f}EUR\n”
msg += f”\n💎 Scalping actif : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS} pos”
if active_crypto_trades:
msg += “\n”
for s, t in active_crypto_trades.items():
entry   = t.get(“entry”) or 0
price   = get_crypto_price(s) or entry
net_pnl = ((price-entry)/entry*100 - COINBASE_FEE_PCT) if entry else 0
msg += f”  <b>{s}</b> {t[‘amount’]:.2f}EUR ({net_pnl:+.1f}% net)\n”
send_telegram(msg)

def cmd_marche():
spy  = get_spy_perf()
intl = []
for ticker, name in [(“EWJ”,“🇯🇵 Japon”),(“FXI”,“🇨🇳 Chine”),(“EWG”,“🇩🇪 Allemagne”),(“EWU”,“🇬🇧 UK”)]:
p = get_market_perf(ticker)
intl.append(f”{‘🟢’ if p>0.5 else ‘🔴’ if p<-0.5 else ‘🟡’} {name} : {p:+.2f}%”)
msg  = f”🌍 <b>Marches</b>\n\n🇺🇸 SPY : {spy:+.2f}% {‘🟢’ if spy>0.5 else ‘🔴’ if spy<-0.5 else ‘🟡’}\n\n”
msg += “\n”.join(intl)
msg += f”\n\n{‘🟢 Ouvert’ if is_market_open() else ‘🔴 Ferme’}”
send_telegram(msg)

def cmd_technique(ticker):
ta    = get_ta(ticker)
price = get_price(ticker)
if not ta or not price:
send_telegram(f”Impossible d’analyser {ticker}.”); return
wr  = get_winrate(ticker)
msg = f”📊 <b>{ticker}</b> ${price:.2f}\n\n{format_ta(ta)}”
if wr: msg += f”Reussite : {wr:.0f}%”
send_telegram(msg)

def cmd_objectifs():
account  = get_account_info()
checkpts = get_equity_checkpoints()
wk = checkpts.get(“week”, account[“equity”])
mo = checkpts.get(“month”, account[“equity”])
yr = checkpts.get(“year”, account[“equity”])
wk_pnl = account[“equity”] - wk
mo_pnl = account[“equity”] - mo
yr_pnl = account[“equity”] - yr
send_telegram(
f”🎯 <b>Objectifs</b>\n\n”
f”Semaine (+{WEEKLY_GOAL_PCT}%) :\n{progress_bar(max(wk_pnl,0), wk*WEEKLY_GOAL_PCT/100)}\n${wk_pnl:+.2f}\n\n”
f”Mois (+{MONTHLY_GOAL_EUR}EUR) :\n{progress_bar(max(mo_pnl,0), MONTHLY_GOAL_EUR)}\n${mo_pnl:+.2f}\n\n”
f”Annee (+{ANNUAL_GOAL_PCT}%) :\n{progress_bar(max(yr_pnl,0), yr*ANNUAL_GOAL_PCT/100)}\n${yr_pnl:+.2f}”
)

def cmd_historique():
stats = get_stats()
if not stats[“recent”]:
send_telegram(“Aucun trade.”); return
msg = “<b>5 derniers trades</b>\n\n”
for t in reversed(stats[“recent”]):
pnl  = f” | ${t[‘pnl’]:+.2f}” if t.get(“pnl”) else “”
msg += f”{‘✅’ if t[‘side’]==‘buy’ else ‘💰’} {t[‘date’]} — {t[‘side’].upper()} <b>{t[‘symbol’]}</b> @ ${t[‘price’]:.2f}{pnl}\n”
msg += f”\n{stats[‘winrate’]:.0f}% | PnL : ${stats[‘total_pnl’]:+.2f}”
send_telegram(msg)

def cmd_pause():
global trading_paused
trading_paused = True
send_telegram(”<b>Pause</b>\nStop loss actif. Tape /resume.”)

def cmd_resume():
global trading_paused, vacation_mode, loss_streak
trading_paused = False; vacation_mode = False; loss_streak = 0
send_telegram(”<b>Trading repris !</b>”)

def cmd_urgence():
global trading_paused
trading_paused = True
positions = get_positions()
hold_syms = set(load_memory().get(“hold_portfolio”,{}).keys())
trade_pos = {s: d for s, d in positions.items() if s not in hold_syms}
crypto_count = len([s for s in active_crypto_trades if s not in CRYPTO_HOLD_STRICT])
if not trade_pos and not crypto_count:
send_telegram(“Aucun trade actif.\nPoche hold conservee.”); return
send_telegram(f”🚨 <b>URGENCE</b>\nFermeture {len(trade_pos)} action(s) + {crypto_count} crypto(s)…\nHold conserve.”)
for s, d in trade_pos.items():
place_order(s, “sell”, abs(d[“qty”]), label=“URGENCE”)
for symbol, trade in list(active_crypto_trades.items()):
if symbol in CRYPTO_HOLD_STRICT: continue
currency = symbol.replace(”-EUR”,””)
balance  = get_crypto_balance(currency)
price    = get_crypto_price(symbol) or 0
if balance > 0 and price > 0:
place_crypto_order(symbol, “sell”, balance*price, label=“URGENCE”)
send_telegram(“Trades fermes.\nTape /resume.”)

def cmd_vacances():
global vacation_mode, trading_paused
vacation_mode = True; trading_paused = True
send_telegram(”<b>Mode vacances</b>\nHold conserve\nStop loss actif\nAucun nouveau trade\nTape /retour !”)

def cmd_retour():
global vacation_mode, trading_paused
vacation_mode = False; trading_paused = False
send_telegram(”<b>Bon retour !</b>”)
send_daily_report(immediate=True)

def cmd_alerte(args):
try:
symbol, target = args[0].upper(), float(args[1])
custom_alerts[symbol] = target
send_telegram(f”🔔 Alerte : <b>{symbol}</b> → {target:.2f}”)
except:
send_telegram(“Format : /alerte BTC-EUR 90000”)

def cmd_voir_alertes():
if not custom_alerts:
send_telegram(“Aucune alerte.”); return
msg = “<b>Alertes actives</b>\n\n”
for s, t in custom_alerts.items():
p    = get_crypto_price(s) if “-EUR” in s else get_price(s)
diff = f” ({abs((p-t)/t*100):.1f}% restant)” if p else “”
msg += f”<b>{s}</b> → {t:.2f}{diff}\n”
send_telegram(msg)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# HANDLER TELEGRAM

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def handle_telegram():
last_update_id = None
while True:
try:
res = requests.get(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates”,
params={“timeout”: 30, “offset”: last_update_id}, timeout=35
)
for update in res.json().get(“result”,[]):
last_update_id = update[“update_id”] + 1
text = update.get(“message”,{}).get(“text”,””).strip()
cmd  = text.lower().split()[0] if text else “”
args = text.split()[1:] if len(text.split()) > 1 else []
if cmd in [”/aide”,”/start”]:           cmd_aide()
elif cmd in [”/status”,”/statut”]:      cmd_status()
elif cmd == “/positions”:               cmd_positions()
elif cmd == “/hold”:                    cmd_hold()
elif cmd == “/crypto”:                  cmd_crypto()
elif cmd == “/report”:                  send_daily_report(immediate=True)
elif cmd == “/historique”:              cmd_historique()
elif cmd == “/marche”:                  cmd_marche()
elif cmd == “/objectifs”:               cmd_objectifs()
elif cmd == “/briefing”:                send_morning_briefing()
elif cmd == “/semaine”:                 send_weekly_report()
elif cmd == “/mois”:                    send_monthly_report()
elif cmd == “/annee”:                   send_annual_report()
elif cmd == “/pause”:                   cmd_pause()
elif cmd == “/resume”:                  cmd_resume()
elif cmd == “/urgence”:                 cmd_urgence()
elif cmd == “/vacances”:                cmd_vacances()
elif cmd == “/retour”:                  cmd_retour()
elif cmd == “/alertes”:                 cmd_voir_alertes()
elif cmd == “/scan_holdings”:           check_existing_holdings(); send_telegram(“Scan termine.”)
elif cmd == “/technique” and args:      cmd_technique(args[0].upper())
elif cmd == “/pourquoi” and args:       cmd_pourquoi(args[0].upper())
elif cmd == “/alerte” and len(args)>=2: cmd_alerte(args)
except Exception:
pass
time.sleep(2)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# THREADS

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def thread_crypto():
“”“Crypto scalping 24/7 — toutes les 20s.”””
while True:
try:
check_existing_holdings()
check_crypto_risk()
if not trading_paused and not vacation_mode:
scan_crypto()
except Exception as e:
record_error(f”thread_crypto: {e}”)
time.sleep(INTERVAL_CRYPTO)

def thread_stocks():
“”“Actions — hold + day trade toutes les 2min (marche ouvert).”””
while True:
try:
if not trading_paused and not vacation_mode and is_market_open():
manage_hold_portfolio()
scan_stocks()
except Exception as e:
record_error(f”thread_stocks: {e}”)
time.sleep(INTERVAL_STOCKS)

def thread_risk():
“”“Stops et TP actions + alertes toutes les 30s.”””
while True:
try:
check_stock_risk()
check_custom_alerts()
except Exception as e:
record_error(f”thread_risk: {e}”)
time.sleep(INTERVAL_RISK)

def thread_news_watcher():
“”“Surveillance macro — breaking news toutes les 20min.”””
last_title = “”
while True:
try:
news = get_news(“FED inflation interest rates market crash”, count=1)
if news and news[0][“title”] != last_title:
last_title = news[0][“title”]
send_telegram(f”📰 <b>BREAKING NEWS MACRO</b>\n\n{news[0][‘title’]}\n{news[0].get(‘url’,’’)}”)
except: pass
time.sleep(1200)

def thread_scheduler():
“”“Rapports, DCA, sante marche toutes les 60s.”””
briefing_sent = daily_sent = weekly_sent = monthly_sent = annual_sent = None
while True:
try:
now   = datetime.now()
today = now.strftime(”%Y-%m-%d”)
account = get_account_info()
update_equity_checkpoints(account[“equity”])
check_market_health()

```
        if now.hour == 8 and now.minute < 5 and briefing_sent != today:
            send_morning_briefing(); briefing_sent = today
        if now.hour == 15 and now.minute == 25 and briefing_sent != f"{today}_pre":
            send_morning_briefing(); briefing_sent = f"{today}_pre"
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
```

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# HEALTH SERVER (Render)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HealthHandler(BaseHTTPRequestHandler):
def do_GET(self):
body = b’{“status”:“ok”}’
self.send_response(200)
self.send_header(“Content-Type”,“application/json”)
self.send_header(“Content-Length”,str(len(body)))
self.end_headers()
self.wfile.write(body)
def log_message(self, *args): pass

def start_health_server():
port = int(os.getenv(“PORT”, 8080))
HTTPServer((“0.0.0.0”, port), HealthHandler).serve_forever()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# MAIN

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
send_telegram(
“<b>Trading Agent V8 — FUSION</b>\n\n”
“━━━━━━━━━━━━━━━━━━━━━\n”
“📈 <b>ALPACA (USD)</b>\n”
f”🔒 Hold {int(HOLD_PCT*100)}% — Claude choisit dynamiquement\n”
f”⚡ Day Trade {int(DAYTRADE_PCT*100)}% — Long/Short toute la bourse\n\n”
“💎 <b>COINBASE (EUR)</b>\n”
“🔒 Hold strict — BTC 25% | ETH 15%\n”
“⚖️ Hold souple — SOL/XRP/LINK\n”
f”⚡ Scalping 24/7 — {len(CRYPTO_UNIVERSE)} cryptos\n”
“━━━━━━━━━━━━━━━━━━━━━\n”
f”🛑 SL : -{STOCK_SL_PCT}% actions | -{CRYPTO_SL_PCT}% crypto (net frais)\n”
f”📉 Trailing stop : -{TRAILING_STOP_PCT}% depuis pic\n”
f”⚡ Circuit breaker : {CRYPTO_CIRCUIT_BREAKER_LOSSES} pertes de suite\n”
f”🤖 Claude AI sur les actions | RSI+breakout sur crypto\n\n”
“Tape /aide 👇”
)

```
check_existing_holdings()
account = get_account_info()
update_equity_checkpoints(account["equity"])
log(f"💼 Capital Alpaca : ${account['equity']:.2f} | Cash : ${account['cash']:.2f}")

threading.Thread(target=start_health_server, daemon=True).start()
threading.Thread(target=handle_telegram,     daemon=True).start()
threading.Thread(target=thread_crypto,       daemon=True).start()
threading.Thread(target=thread_stocks,       daemon=True).start()
threading.Thread(target=thread_risk,         daemon=True).start()
threading.Thread(target=thread_news_watcher, daemon=True).start()
threading.Thread(target=thread_scheduler,    daemon=True).start()

log("✅ Agent V8 — 7 threads actifs")

while True:
    time.sleep(60)
    log(f"💓 {'PAUSE' if trading_paused else 'ACTIF'} | "
        f"Marche {'OUVERT' if is_market_open() else 'ferme'} | "
        f"Actions: {len(active_stock_trades)} | Crypto: {len(active_crypto_trades)} | "
        f"Streak: {loss_streak}")
```

if **name** == “**main**”:
main()
