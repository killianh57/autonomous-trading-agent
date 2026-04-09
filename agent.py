# -*- coding: utf-8 -*-
import os, json, time, threading, logging, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
import schedule
import anthropic

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    from coinbase.rest import RESTClient as CoinbaseClient
    COINBASE_AVAILABLE = True
except ImportError:
    COINBASE_AVAILABLE = False

load_dotenv()

# ================================================================
# CONFIGURATION & LOGS DE DIAGNOSTIC
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

HOLD_PCT = 0.65
DAYTRADE_PCT = 0.35
STOCK_SL_PCT = 2.0
STOCK_TP_PCT = 4.0
MAX_RISK_PER_TRADE_PCT = 0.02
CONFIDENCE_THRESHOLD = 70

STOCK_WATCHLIST = ["NVDA", "AAPL", "JPM", "UNH", "WMT", "CAT", "XOM"]
CORE_TARGETS = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}
EST = ZoneInfo("America/New_York")
agent_paused = False
last_update_id = 0

# Clients
try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
    data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    log.info("✅ Clients API initialisés avec succès")
except Exception as e:
    log.error(f"❌ Erreur Init Clients: {e}")

# ================================================================
# TELEGRAM (VERSION BLINDÉE)
# ================================================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e: 
        log.error(f"❌ Erreur envoi Telegram: {e}")

def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN: return
    
    try:
        log.info("🔍 Vérification des nouveaux messages Telegram...")
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 10}
        r = requests.get(url, params=params, timeout=15)
        
        if r.status_code != 200:
            log.error(f"❌ Erreur API Telegram: {r.status_code}")
            return

        updates = r.json().get("result", [])
        if not updates:
            return # Pas de nouveaux messages

        for update in updates:
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = msg.get("chat", {}).get("id")

            log.info(f"📩 Message reçu de {chat_id}: {text}")

            if chat_id != int(TELEGRAM_CHAT_ID):
                log.warning(f"Message ignoré (Chat ID {chat_id} != {TELEGRAM_CHAT_ID})")
                continue

            if text == "/pause": 
                agent_paused = True
                send_telegram("⏸️ *Agent en PAUSE*")
            elif text == "/resume": 
                agent_paused = False
                send_telegram("▶️ *Agent REPRIS*")
            elif text == "/status": 
                try:
                    eq = trading_client.get_account().equity
                    send_telegram(f"📊 *STATUS V11*\nEquity: {eq}$\nMode: {'PAPER' if PAPER_MODE else 'LIVE'}\nPaused: {agent_paused}")
                except Exception as e:
                    send_telegram(f"❌ Erreur status: {e}")
            elif text == "/start":
                send_telegram("🤖 *Agent V11 opérationnel !* Tapez /status pour vérifier.")
            else:
                send_telegram("❓ Commande inconnue. Utilisez /status, /pause ou /resume")

    except Exception as e: 
        log.error(f"❌ Erreur boucle Telegram: {e}")

def telegram_loop():
    log.info("👂 L'oreille Telegram est activée...")
    while True:
        process_commands()
        time.sleep(5)

# ================================================================
# TRADING & REBALANCING
# ================================================================
def get_account_info():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash)}

def place_bracket_order(symbol, side, limit_price, sl_pct, tp_pct):
    try:
        account = get_account_info()
        sl_dist = limit_price * (sl_pct / 100.0)
        qty = round((account["equity"] * MAX_RISK_PER_TRADE_PCT) / sl_dist, 4)
        if qty <= 0: return

        if side == "buy":
            sl_p, tp_p = limit_price * (1 - sl_pct/100), limit_price * (1 + tp_pct/100)
            req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2), order_class=OrderClass.BRACKET, take_profit=TakeProfitRequest(limit_price=round(tp_p, 2)), stop_loss=StopLossRequest(stop_price=round(sl_p, 2)))
        else:
            if not PAPER_MODE: return
            sl_p, tp_p = limit_price * (1 + sl_pct/100), limit_price * (1 - tp_pct/100)
            req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2), order_class=OrderClass.BRACKET, take_profit=TakeProfitRequest(limit_price=round(tp_p, 2)), stop_loss=StopLossRequest(stop_price=round(sl_p, 2)))
        
        trading_client.submit_order(req)
        send_telegram(f"🚀 *TRADE {symbol}* {side.upper()}\nEntry: {limit_price}$ | SL: {sl_p:.2f} | TP: {tp_p:.2f}")
    except Exception as e: log.error(f"Order Error: {e}")

def check_rebalancing():
    try:
        account = get_account_info()
        hold_cap = account["equity"] * HOLD_PCT
        positions = {p.symbol: float(p.market_value) for p in trading_client.get_all_positions()}
        for symbol, target in CORE_TARGETS.items():
            target_usd = hold_cap * target
            actual_usd = positions.get(symbol, 0)
            if (target_usd - actual_usd) / hold_cap > 0.05:
                buy_amt = target_usd - actual_usd
                if account["cash"] > buy_amt:
                    trading_client.submit_order(MarketOrderRequest(symbol=symbol, notional=round(buy_amt, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                    send_telegram(f"⚖️ REBALANCING: Achat {symbol} {buy_amt:.2f}$")
    except Exception as e: log.error(f"Rebalance Error: {e}")

def scan_and_trade():
    if agent_paused: return
    log.info("Scanning markets...")
    for ticker in STOCK_WATCHLIST:
        try:
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute5, start=datetime.now(EST) - timedelta(days=1))
            bars = list(data_client.get_stock_bars(req)[ticker])
            price = bars[-1].close
            res = claude_client.messages.create(
                model="claude-3-5-haiku-20241022", max_tokens=100, 
                system="Trader institutionnel. JSON uniquement: {'action':'BUY'|'SHORT'|'HOLD','confidence':0-100}",
                messages=[{"role": "user", "content": f"Ticker: {ticker}, Price: {price}. Signal?"}]
            )
            signal = json.loads(res.content[0].text.strip().replace("```json", "").replace("```", ""))
            if signal.get("confidence", 0) >= CONFIDENCE_THRESHOLD and signal["action"] != "HOLD":
                place_bracket_order(ticker, "buy" if signal["action"]=="BUY" else "short", price, STOCK_SL_PCT, STOCK_TP_PCT)
        except Exception as e: log.error(f"Scan Error {ticker}: {e}")
        time.sleep(1)

# ================================================================
# SERVEUR DE SANTÉ & MAIN
# ================================================================
class _Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Agent V11 OK")
    def log_message(self, *args): pass

if __name__ == "__main__":
    log.info("🚀 AGENT V11 DEMARRAGE...")
    
    # 1. Santé Render
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(), daemon=True).start()
    
    # 2. ÉCOUTE TELEGRAM (Démarrage forcé)
    threading.Thread(target=telegram_loop, daemon=True).start()
    
    # 3. Planning
    schedule.every(15).minutes.do(scan_and_trade)
    schedule.every().day.at("10:00").do(check_rebalancing)

    send_telegram("🤖 *Agent V11 en ligne et à l'écoute !*")
    log.info("Toutes les routines sont lancées. En attente de commandes...")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
