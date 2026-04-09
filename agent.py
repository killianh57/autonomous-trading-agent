# -*- coding: utf-8 -*-

# Agent Trading V12 - Alpaca + Coinbase - FINAL STABLE

import os
import json
import time
import threading
import logging
import uuid
import requests
import schedule
import anthropic

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, TakeProfitRequest, StopLossRequest, MarketOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockSnapshotRequest, StockLatestBarRequest
)
from alpaca.data.timeframe import TimeFrame

try:
    from coinbase.rest import RESTClient as CoinbaseClient
    COINBASE_AVAILABLE = True
except ImportError:
    COINBASE_AVAILABLE = False
    print("[WARN] coinbase-advanced-py non installe")

load_dotenv()

# ================================================================
# CONFIGURATION
# ================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALPACA_API_KEY      = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")
PAPER_MODE          = os.getenv("PAPER_MODE", "True") == "True"
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY    = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_SECRET_KEY", "")
NEWS_API_KEY        = os.getenv("NEWS_API_KEY", "")
NOTION_TOKEN        = os.getenv("NOTION_TOKEN", "")
NOTION_PAGE_ID      = os.getenv("NOTION_PAGE_ID", "3375afb215b4819785c5df026f5cdd75")

HOLD_PCT     = 0.65
DAYTRADE_PCT = 0.35
STOCK_SL_PCT           = 2.0
STOCK_TP_PCT           = 4.0
CRYPTO_SL_PCT          = 3.0
CRYPTO_TP_PCT          = 6.0
MAX_RISK_PER_TRADE_PCT = 0.02
CONFIDENCE_THRESHOLD   = 80
MIN_CONFLUENCES        = 3
START_CAPITAL          = 100_000.0

STOCK_WATCHLIST  = ["NVDA", "AAPL", "JPM", "UNH", "WMT", "CAT", "XOM"]
CRYPTO_WATCHLIST = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "AVAX-EUR", "LINK-EUR", "ADA-EUR"]
CORE_TARGETS     = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}

SEARCH_TERMS = {
    "NVDA": "NVIDIA OR NVDA", "AAPL": "Apple OR AAPL",
    "JPM":  "JPMorgan OR JPM", "UNH": "UnitedHealth OR UNH",
    "WMT":  "Walmart OR WMT",  "CAT": "Caterpillar OR CAT",
    "XOM":  "ExxonMobil OR XOM",
    "BTC":  "Bitcoin OR BTC",  "ETH": "Ethereum OR ETH",
    "SOL":  "Solana OR SOL",   "XRP": "Ripple OR XRP"
}
HIGH_RISK_KW = ["earnings report", "SEC investigation", "fraud", "bankruptcy", "delisted", "lawsuit"]

EST            = ZoneInfo("America/New_York")
MARKET_OPEN    = (9, 30)
MARKET_CLOSE   = (16, 0)
BLACKOUT_START = (11, 0)
BLACKOUT_END   = (14, 0)

agent_paused           = False
last_update_id         = 0
open_positions_tracker = {}
TRADES_FILE            = "trades.json"

# ================================================================
# CLIENTS
# ================================================================

try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
    data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    log.info("Clients API initialises")
except Exception as e:
    log.error(f"Erreur init clients: {e}")

_cb_client = None
def get_coinbase_client():
    global _cb_client
    if _cb_client is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
        _cb_client = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
    return _cb_client

# ================================================================
# TELEGRAM - FIXÉ (Suppression des erreurs adapters)
# ================================================================

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TG] {msg[:100]}")
        return
    try:
        token = str(TELEGRAM_TOKEN).strip()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": str(TELEGRAM_CHAT_ID).strip(), "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram Send Error: {e}")

# ================================================================
# LOGGERS & TOOLS
# ================================================================

def load_trades():
    if os.path.exists(TRADES_FILE):
        try:
            return json.load(open(TRADES_FILE))
        except Exception:
            return []
    return []

def save_trades(trades):
    json.dump(trades, open(TRADES_FILE, "w"), indent=2, default=str)

def log_trade_open(key, side, entry, sl, tp, signal_type, conviction, n_conf, platform="alpaca"):
    open_positions_tracker[key] = {
        "entry": entry, "sl": sl, "tp": tp,
        "signal": signal_type, "conviction": conviction,
        "confluences": n_conf, "side": side, "platform": platform,
        "time": datetime.now(EST).isoformat()
    }

def log_trade_close(key, exit_price):
    if key not in open_positions_tracker:
        return None
    pos     = open_positions_tracker.pop(key)
    entry   = pos["entry"]
    side    = pos["side"]
    pnl_pct = ((exit_price - entry) / entry * 100) if side == "buy" else ((entry - exit_price) / entry * 100)
    pnl_usd = pnl_pct / 100 * entry * 10
    trade = {
        "symbol": key, "side": side, "entry": entry, "exit": exit_price,
        "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2),
        "signal": pos["signal"], "conviction": pos["conviction"],
        "confluences": pos["confluences"],
        "entry_hour": datetime.fromisoformat(pos["time"]).hour,
        "platform": pos.get("platform", "alpaca"),
        "date": datetime.now(EST).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(EST).isoformat()
    }
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)
    _log_to_notion(trade)
    return trade

def _log_to_notion(trade):
    if not NOTION_TOKEN: return
    emoji = "OK" if trade["pnl_usd"] >= 0 else "LOSS"
    content = f"[{emoji}] {trade['symbol']} | PnL {trade['pnl_usd']}$ | {trade['platform']}"
    try:
        requests.patch(
            f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
            headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
            json={"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]}}]},
            timeout=5
        )
    except Exception as e: log.error(f"Notion: {e}")

# ================================================================
# INDICATEURS & ANALYSE
# ================================================================

def calculate_atr(bars, period=14):
    if len(bars) < period + 1: return 0
    tr_list = [max(bars[i].high - bars[i].low, abs(bars[i].high - bars[i-1].close), abs(bars[i].low - bars[i-1].close)) for i in range(1, len(bars))]
    return sum(tr_list[-period:]) / period

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains)/period; avg_loss = sum(losses)/period
    return 100 - (100 / (1 + avg_gain/max(avg_loss, 0.00001)))

def calculate_ema(closes, period):
    if len(closes) < period: return closes[-1]
    k = 2 / (period + 1); ema = closes[0]
    for p in closes[1:]: ema = p * k + ema * (1 - k)
    return ema

def get_vix():
    try: return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=["VIXY"]))["VIXY"].close
    except: return 20.0

def get_smc_data(ticker):
    try:
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute5, start=datetime.now(EST)-timedelta(days=3))
        bars = list(data_client.get_stock_bars(req)[ticker])
        if len(bars) < 30: return None
        closes = [b.close for b in bars]; highs = [b.high for b in bars]; lows = [b.low for b in bars]; volumes = [b.volume for b in bars]
        cur = closes[-1]; ema9 = calculate_ema(closes, 9); ema21 = calculate_ema(closes, 21); rsi = calculate_rsi(closes)
        sh = max(highs[-20:]); sl = min(lows[-20:])
        return {
            "current": cur, "atr": calculate_atr(bars), "swing_high": sh, "swing_low": sl,
            "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "baissier",
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema9 > ema21,
            "rsi": rsi, "rsi_div_bull": (cur < closes[-20]) and (rsi > calculate_rsi(closes[:-5])),
            "rsi_div_bear": (cur > closes[-20]) and (rsi < calculate_rsi(closes[:-5])),
            "volume_ok": volumes[-1] >= (sum(volumes[-20:])/20) * 0.8
        }
    except Exception as e: log.error(f"SMC {ticker}: {e}"); return None

def get_crypto_smc(product_id):
    try:
        cb = get_coinbase_client()
        if not cb: return None
        res = cb.get_candles(product_id=product_id, start=str(int(time.time()-15000)), end=str(int(time.time())), granularity="FIVE_MINUTE")
        candles = sorted(res.get("candles", []), key=lambda c: c["start"])
        if len(candles) < 25: return None
        closes = [float(c["close"]) for c in candles]; highs = [float(c["high"]) for c in candles]; lows = [float(c["low"]) for c in candles]
        cur = closes[-1]; ema9 = calculate_ema(closes, 9); ema21 = calculate_ema(closes, 21); rsi = calculate_rsi(closes)
        sh = max(highs[-20:]); sl = min(lows[-20:])
        return {
            "current": cur, "atr": (sum(abs(highs[i]-lows[i]) for i in range(-14, 0))/14),
            "swing_high": sh, "swing_low": sl, "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "ba Baissier",
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema9 > ema21, "rsi": rsi,
            "volume_ok": True # CB API volume check simplifie
        }
    except Exception as e: log.error(f"Crypto SMC {product_id}: {e}"); return None

# ================================================================
# SENTIMENT & CLAUDE
# ================================================================

def get_news_sentiment(ticker):
    if not NEWS_API_KEY: return {"sentiment": "NEUTRAL", "pause": False}
    try:
        r = requests.get(f"https://newsapi.org/v2/everything?q={ticker}&apiKey={NEWS_API_KEY}&pageSize=5", timeout=8)
        articles = r.json().get("articles", [])
        for a in articles:
            if any(kw in a.get("title","").lower() for kw in HIGH_RISK_KW): return {"sentiment": "BEARISH", "pause": True}
        return {"sentiment": "NEUTRAL", "pause": False}
    except: return {"sentiment": "NEUTRAL", "pause": False}

PROMPT_SYSTEM = "Tu es un trader institutionnel. Reponds UNIQUEMENT en JSON strict: {\"action\":\"BUY\"|\"SHORT\"|\"HOLD\",\"confidence\":0-100,\"signal_type\":\"SMC\",\"reason\":\"max 10 mots\"}"

def get_claude_signal(ticker, smc, news):
    try:
        res = claude_client.messages.create(
            model="claude-3-haiku-20240307", max_tokens=150, system=PROMPT_SYSTEM,
            messages=[{"role": "user", "content": f"Ticker: {ticker} Price: {smc['current']} Trend: {smc['trend']} RSI: {smc['rsi']}"}]
        )
        return json.loads(res.content[0].text)
    except: return None

# ================================================================
# EXECUTION
# ================================================================

def place_bracket_order(symbol, side, limit_price, sl_pct, tp_pct, signal_type="SMC", conviction=80, conf_list=None):
    try:
        acc = trading_client.get_account()
        qty = round((float(acc.equity) * MAX_RISK_PER_TRADE_PCT) / (limit_price * sl_pct/100), 2)
        if qty <= 0: return
        sl_p = limit_price * (1 - sl_pct/100) if side == "buy" else limit_price * (1 + sl_pct/100)
        tp_p = limit_price * (1 + tp_pct/100) if side == "buy" else limit_price * (1 - tp_pct/100)
        
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(tp_p, 2)),
            stop_loss=StopLossRequest(stop_price=round(sl_p, 2))
        )
        trading_client.submit_order(req)
        send_telegram(f"Order {side.upper()} {symbol} at {limit_price}")
        log_trade_open(symbol, side, limit_price, sl_p, tp_p, signal_type, conviction, 0)
    except Exception as e: log.error(f"Order Error: {e}")

# ================================================================
# LOOPS & SCANS
# ================================================================

def scan_and_trade():
    if agent_paused: return
    for ticker in STOCK_WATCHLIST:
        smc = get_smc_data(ticker)
        if not smc: continue
        sig = get_claude_signal(ticker, smc, get_news_sentiment(ticker))
        if sig and sig["action"] != "HOLD" and sig["confidence"] >= CONFIDENCE_THRESHOLD:
            place_bracket_order(ticker, sig["action"].lower(), smc["current"], STOCK_SL_PCT, STOCK_TP_PCT)

# ================================================================
# TELEGRAM LOOP - FIXÉ
# ================================================================

def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN: return
    try:
        token = str(TELEGRAM_TOKEN).strip()
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        r = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
        if r.status_code != 200: return
        for update in r.json().get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            if text == "/status":
                send_telegram(f"Agent V12 Online. Paused: {agent_paused}")
            elif text == "/pause":
                agent_paused = True; send_telegram("Trading Paused.")
            elif text == "/resume":
                agent_paused = False; send_telegram("Trading Resumed.")
    except Exception as e: log.error(f"Poll Error: {e}")

def telegram_loop():
    while True:
        process_commands()
        time.sleep(5)

# ================================================================
# MAIN
# ================================================================

class _Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(), daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    
    schedule.every(15).minutes.do(scan_and_trade)
    
    send_telegram("🚀 Agent V12 Initialisé sur Render")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
