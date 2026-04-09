# -*- coding: utf-8 -*-

# Agent Trading V12 - Alpaca + Coinbase - RECOVERY V11 COMMANDS

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
START_CAPITAL       = 100_000.0
EST = ZoneInfo("America/New_York")

agent_paused = False
last_update_id = 0
open_positions_tracker = {}

# Initialisation Clients
try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
    data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
except Exception as e:
    log.error(f"Erreur Initialisation: {e}")

# ================================================================
# HELPERS & TELEGRAM
# ================================================================

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN.strip()}/sendMessage"
        requests.post(url, json={"chat_id": str(TELEGRAM_CHAT_ID).strip(), "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: log.error(f"TG Error: {e}")

def get_vix():
    try: return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=["VIXY"]))["VIXY"].close
    except: return 20.0

# ================================================================
# COMMANDES V11 RECOVERY
# ================================================================

def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN.strip()}/getUpdates"
        r = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
        if r.status_code != 200: return
        
        for update in r.json().get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = msg.get("chat", {}).get("id")
            
            if str(chat_id) != str(TELEGRAM_CHAT_ID).strip(): continue

            # --- COMMANDE /AIDE ---
            if text in ["/aide", "/start"]:
                menu = (
                    "🤖 *AGENT V12 ULTIME*\n\n"
                    "📊 *INFOS*\n"
                    "/status - État global\n"
                    "/portfolio - Bilan Bourse + Crypto\n"
                    "/crypto - Détail Crypto\n"
                    "/marche - État du marché\n\n"
                    "⚙️ *CONTRÔLE*\n"
                    "/pause - Suspendre trading\n"
                    "/resume - Reprendre trading\n"
                    "/liquidate - Vendre cryptos\n\n"
                    "📋 *AUTRES*\n"
                    "/positions - Trades ouverts\n"
                    "/report - Bilan rapide"
                )
                send_telegram(menu)

            # --- COMMANDE /STATUS ---
            elif text == "/status":
                acc = trading_client.get_account()
                vix = get_vix()
                status_msg = (
                    "📊 *STATUS SYSTÈME*\n\n"
                    f"💰 Equity: `{float(acc.equity):.2f}$`\n"
                    f"💵 Cash: `{float(acc.cash):.2f}$`\n\n"
                    f"🛠 Mode: `{'PAPER' if PAPER_MODE else 'LIVE'}`\n"
                    f"📈 VIX: `{vix:.2f}`\n"
                    f"🤖 État: `{'PAUSE' if agent_paused else 'ACTIF'}`"
                )
                send_telegram(status_msg)

            # --- COMMANDE /PORTFOLIO ---
            elif text == "/portfolio":
                acc = trading_client.get_account()
                total_ret = ((float(acc.equity) - START_CAPITAL) / START_CAPITAL) * 100
                port_msg = (
                    "🏦 *BILAN PORTEFEUILLE*\n\n"
                    f"Valeur Totale: `{float(acc.equity):.2f}$`\n"
                    f"Performance: `{total_ret:+.2f}%`\n"
                    f"Pouvoir d'achat: `{float(acc.buying_power):.2f}$`"
                )
                send_telegram(port_msg)

            # --- COMMANDE /MARCHE ---
            elif text == "/marche":
                vix = get_vix()
                msg = (
                    "🌍 *ÉTAT DU MARCHÉ*\n\n"
                    f"VIX (Volatilité): `{vix:.2f}`\n"
                    f"Sentiment: `{'PRUDENCE' if vix > 25 else 'STABLE'}`"
                )
                send_telegram(msg)

            # --- COMMANDE /POSITIONS ---
            elif text == "/positions":
                pos = trading_client.get_all_positions()
                if not pos:
                    send_telegram("📭 Aucune position ouverte.")
                else:
                    lines = [f"✅ *{p.symbol}*: {float(p.qty)} @ {float(p.avg_entry_price):.2f}$ ({float(p.unrealized_plpc)*100:+.2f}%)" for p in pos]
                    send_telegram("📑 *POSITIONS ACTIVES*\n\n" + "\n".join(lines))

            # --- CONTRÔLES ---
            elif text == "/pause":
                agent_paused = True
                send_telegram("🛑 *TRADING SUSPENDU*")
            
            elif text == "/resume":
                agent_paused = False
                send_telegram("🚀 *TRADING REPRIS*")

    except Exception as e:
        log.error(f"Command Error: {e}")

# ================================================================
# MAIN & SERVER
# ================================================================

def telegram_loop():
    while True:
        process_commands()
        time.sleep(3)

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"AGENT V12 OK")
    def log_message(self, *args): pass

if __name__ == "__main__":
    # Démarrage Serveur Santé
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(), daemon=True).start()
    
    # Démarrage Boucle Telegram
    threading.Thread(target=telegram_loop, daemon=True).start()
    
    send_telegram("🤖 *Agent V12 ULTIME en ligne !*\n\nL'oreille est active. Tapez /portfolio pour voir vos actifs.")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
