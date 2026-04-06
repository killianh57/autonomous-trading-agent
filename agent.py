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

ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY   = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET    = os.getenv("COINBASE_SECRET_KEY")

SAFE_ASSETS   = ["VT"]
TECH_ASSETS   = ["NVDA", "MSFT", "META"]
ETF_ASSETS    = ["QQQ", "XLK"]
ALL_ASSETS    = SAFE_ASSETS + TECH_ASSETS + ETF_ASSETS
CRYPTO_HOLD   = ["BTC-USD", "ETH-USD"]
CRYPTO_OPP    = ["SOL-USD"]

DCA_MONTHLY_EUR = 100
DCA_ALLOCATION  = {
    "VT": 0.20, "NVDA": 0.12, "MSFT": 0.08,
    "META": 0.08, "QQQ": 0.12, "XLK": 0.08,
    "BTC-USD": 0.15, "ETH-USD": 0.10, "CASH": 0.07
}

WEEKLY_GOAL_PCT  = 1.0
MONTHLY_GOAL_EUR = 100
ANNUAL_GOAL_PCT  = 20.0
POLL_INTERVAL    = 300
STOP_LOSS_PCT    = 3.0
MEMORY_FILE      = "trade_memory.json"

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

try:
    coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
except Exception as e:
    coinbase = None
    print(f"Coinbase init error: {e}")

last_seen_news      = {}
take_profit_targets = {}
trading_paused      = False
vacation_mode       = False
custom_alerts       = {}

def progress_bar(current, goal, length=10):
    if goal == 0: return "░" * length
    pct    = min(current / goal, 1.0)
    filled = int(pct * length)
    return f"{'█' * filled}{'░' * (length - filled)} {pct*100:.0f}%"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {"trades": [], "stats": {"wins": 0, "losses": 0, "total_pnl": 0}, "monthly_stats": {}, "annual_stats": {}, "patterns": {}, "errors": [], "equity_start": {}}

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def update_equity_checkpoints(equity):
    memory = load_memory()
    now    = datetime.now()
    es     = memory.get("equity_start", {})
    week   = now.strftime("%Y-%W")
    month  = now.strftime("%Y-%m")
    year   = now.strftime("%Y")
    if not es.get("week_key") or es.get("week_key") != week:
        es["week"] = equity
        es["week_key"] = week
    if not es.get("month_key") or es.get("month_key") != month:
        es["month"] = equity
        es["month_key"] = month
    if not es.get("year_key") or es.get("year_key") != year:
        es["year"] = equity
        es["year_key"] = year
    memory["equity_start"] = es
    save_memory(memory)

def get_equity_checkpoints():
    return load_memory().get("equity_start", {})

def record_trade(symbol, side, qty, price, pnl=None):
    memory = load_memory()
    now    = datetime.now()
    month  = now.strftime("%Y-%m")
    year   = now.strftime("%Y")
    trade  = {"date": now.strftime("%Y-%m-%d %H:%M"), "symbol": symbol, "side": side, "qty": qty, "price": price, "pnl": pnl}
    memory["trades"].append(trade)
    if pnl is not None:
        memory["stats"]["total_pnl"] += pnl
        if pnl > 0: memory["stats"]["wins"] += 1
        else: memory["stats"]["losses"] += 1
        if month not in memory["monthly_stats"]:
            memory["monthly_stats"][month] = {"wins": 0, "losses": 0, "pnl": 0, "trades": []}
        memory["monthly_stats"][month]["pnl"] += pnl
        if pnl > 0: memory["monthly_stats"][month]["wins"] += 1
        else: memory["monthly_stats"][month]["losses"] += 1
        memory["monthly_stats"][month]["trades"].append(trade)
        if year not in memory["annual_stats"]:
            memory["annual_stats"][year] = {"wins": 0, "losses": 0, "pnl": 0}
        memory["annual_stats"][year]["pnl"] += pnl
        if pnl > 0: memory["annual_stats"][year]["wins"] += 1
        else: memory["annual_stats"][year]["losses"] += 1
        memory["patterns"][symbol] = memory["patterns"].get(symbol, {"wins": 0, "losses": 0, "total_pnl": 0})
        memory["patterns"][symbol]["total_pnl"] += pnl
        if pnl > 0: memory["patterns"][symbol]["wins"] += 1
        else: memory["patterns"][symbol]["losses"] += 1
    memory["trades"] = memory["trades"][-200:]
    save_memory(memory)

def record_error(msg):
    memory = load_memory()
    memory["errors"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "error": msg})
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def get_symbol_winrate(symbol):
    pattern = load_memory()["patterns"].get(symbol)
    if not pattern: return None
    total = pattern["wins"] + pattern["losses"]
    return (pattern["wins"] / total * 100) if total > 0 else None

def get_stats():
    stats = load_memory()["stats"]
    total = stats["wins"] + stats["losses"]
    return {"wins": stats["wins"], "losses": stats["losses"], "total_pnl": stats["total_pnl"], "winrate": (stats["wins"] / total * 100) if total > 0 else 0, "recent": load_memory()["trades"][-5:]}

def get_monthly_stats(month=None):
    if not month: month = datetime.now().strftime("%Y-%m")
    return load_memory()["monthly_stats"].get(month, {"wins": 0, "losses": 0, "pnl": 0, "trades": []})

def get_annual_stats(year=None):
    if not year: year = datetime.now().strftime("%Y")
    return load_memory()["annual_stats"].get(year, {"wins": 0, "losses": 0, "pnl": 0})

def get_best_worst(trades):
    w = [t for t in trades if t.get("pnl") is not None]
    if not w: return None, None
    return max(w, key=lambda x: x["pnl"]), min(w, key=lambda x: x["pnl"])

def get_historical_prices(ticker, days=60):
    try:
        req  = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=days))
        return [bar.close for bar in data_client.get_stock_bars(req)[ticker]]
    except:
        return []

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains  = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)

def get_technical_analysis(ticker):
    prices = get_historical_prices(ticker)
    if not prices or len(prices) < 20: return None
    rsi  = calculate_rsi(prices)
    ma20 = sum(prices[-20:]) / 20
    ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else None
    cur  = prices[-1]
    return {"rsi": rsi, "ma20": ma20, "ma50": ma50, "current": cur,
            "trend": "haussier 📈" if (ma20 and ma50 and ma20 > ma50) else "baissier 📉",
            "above_ma20": cur > ma20, "above_ma50": cur > ma50 if ma50 else None,
            "week_perf": ((cur - prices[-6]) / prices[-6] * 100) if len(prices) >= 6 else None}

def format_ta(ta):
    if not ta: return "Indisponible"
    rsi_txt = f"RSI {ta['rsi']} {'⬇️ Survendu' if ta['rsi'] < 30 else '⬆️ Suracheté' if ta['rsi'] > 70 else '➡️ Neutre'}" if ta["rsi"] else ""
    return f"{rsi_txt}\nTendance : {ta['trend']}\nMA20 : {'✅ Au-dessus' if ta['above_ma20'] else '⚠️ En-dessous'}"

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), HealthHandler).serve_forever()

def is_market_open():
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return now.replace(hour=13, minute=30, second=0, microsecond=0) <= now <= now.replace(hour=20, minute=0, second=0, microsecond=0)

def get_account_info():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash), "pnl": float(a.equity) - float(a.last_equity)}

def get_positions():
    return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value), "avg_price": float(p.avg_entry_price), "pnl": float(p.unrealized_pl), "pnl_pct": float(p.unrealized_plpc) * 100} for p in trading_client.get_all_positions()}

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
    except:
        return None

def get_crypto_price(symbol):
    try:
        if not coinbase: return None
        pb = coinbase.get_best_bid_ask(product_ids=[symbol])
        return float(pb["pricebooks"][0]["asks"][0]["price"])
    except:
        return None

def get_crypto_balance(currency):
    try:
        if not coinbase: return 0
        for acc in coinbase.get_accounts()["accounts"]:
            if acc["currency"] == currency:
                return float(acc["available_balance"]["value"])
        return 0
    except:
        return 0

def get_market_performance(ticker):
    try:
        cur  = data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
        bars = list(data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=2)))[ticker])
        return ((cur - bars[-2].close) / bars[-2].close) * 100 if len(bars) >= 2 else 0
    except:
        return 0

def get_spy_performance():
    return get_market_performance("SPY")

def get_news(ticker, count=5):
    try:
        q = ticker.replace("-USD","").replace("USDT","")
        return requests.get(f"https://newsapi.org/v2/everything?q={q}&language=en&sortBy=publishedAt&pageSize={count}&apiKey={NEWS_API_KEY}", timeout=10).json().get("articles", [])
    except:
        return []

def has_new_news(ticker, articles):
    if not articles: return False
    latest = articles[0].get("publishedAt", "")
    if last_seen_news.get(ticker) != latest:
        last_seen_news[ticker] = latest
        return True
    return False

def place_order(symbol, side, qty, take_profit_pct=None):
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=round(qty, 4), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY))
        price  = get_price(symbol)
        valeur = round(qty * price, 2) if price else "?"
        record_trade(symbol, side, round(qty, 4), price or 0)
        if side == "buy":​​​​​​​​​​​​​​​​
