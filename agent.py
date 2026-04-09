# -*- coding: utf-8 -*-
import os, json, time, threading, logging, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import requests, schedule, anthropic
from http.server import HTTPServer, BaseHTTPRequestHandler

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, TakeProfitRequest, StopLossRequest, MarketOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest, StockLatestBarRequest
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
COINBASE_API_KEY    = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_SECRET_KEY", "")

HOLD_PCT = 0.65
DAYTRADE_PCT = 0.35
STOCK_SL_PCT = 2.0
STOCK_TP_PCT = 4.0
CRYPTO_SL_PCT = 3.0
CRYPTO_TP1_PCT = 15.0
CRYPTO_TP2_PCT = 35.0
MAX_RISK_PER_TRADE_PCT = 0.02
MAX_CRYPTO_SIZE_PCT = 0.02
CONFIDENCE_THRESHOLD = 80

STOCK_WATCHLIST = ["NVDA", "AAPL", "JPM", "UNH", "WMT", "CAT", "XOM"]
CRYPTO_WATCHLIST = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "AVAX-EUR"]
CORE_TARGETS = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}
EST = ZoneInfo("America/New_York")
agent_paused = False
last_update_id = 0

# Clients
try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
    data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    log.info("✅ Clients API OK")
except Exception as e:
    log.error(f"❌ Erreur Init Clients: {e}")

_cb = None
def get_coinbase_client():
    global _cb
    if _cb is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
        try:
            _cb = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
        except Exception as e:
            log.error(f"Coinbase init: {e}")
    return _cb

# ================================================================
# TELEGRAM & DASHBOARD
# ================================================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN.strip()}/sendMessage"
        payload = {"chat_id": str(TELEGRAM_CHAT_ID).strip(), "text": msg, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e: log.error(f"❌ Telegram Send Error: {e}")

def get_crypto_summary():
    cb = get_coinbase_client()
    if not cb: return "Non configure", 0
    try:
        accounts = cb.get_accounts().get("accounts", [])
        total_eur = 0
        details = []
        for acc in accounts:
            curr = acc.get("currency")
            bal = float(acc.get("available_balance", {}).get("value", 0))
            if bal <= 0: continue
            if curr == "EUR":
                total_eur += bal
                details.append(f"💶 EUR: {bal:.2f}€ (cash)")
            else:
                prod = f"{curr}-EUR"
                try:
                    price = float(cb.get_best_bid_ask(product_ids=[prod])["pricebooks"][0]["bids"][0]["price"])
                    val = bal * price
                    total_eur += val
                    details.append(f"🔹 *{curr}*: {bal:.4f} (~{val:.2f}€)")
                except: continue
        return "\n".join(details) if details else "Aucun actif", total_eur
    except Exception as e:
        return f"Erreur: {e}", 0

def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN.strip()}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 10}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200: return
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id"))
            if chat_id != str(TELEGRAM_CHAT_ID).strip(): continue

            if text in ["/start", "/aide"]:
                send_telegram("🤖 *AGENT V12 ULTIME*\n\n"
                              "📊 *INFOS*\n/status - État global\n/portfolio - Bilan Bourse + Crypto\n/crypto - Détail Crypto\n/marche - État du marché\n/positions - Trades ouverts\n\n"
                              "⚙️ *CONTRÔLE*\n/pause - Suspendre trading\n/resume - Reprendre trading\n/liquidate - Vendre cryptos\n\n"
                              "📋 *AUTRES*\n/report - Bilan rapide")
            elif text == "/status":
                acc = trading_client.get_account()
                send_telegram(f"📊 *STATUS SYSTÈME*\n\n💰 Equity: `{acc.equity}$`\n💵 Cash: `{acc.cash}$`\n\n🛠 Mode: `{'PAPER' if PAPER_MODE else 'LIVE'}`\n⏸️ Pause: `{'OUI' if agent_paused else 'NON'}`")
            elif text == "/portfolio":
                acc = trading_client.get_account()
                crypto_details, crypto_val = get_crypto_summary()
                send_telegram(f"💰 *BILAN GLOBAL*\n\n📈 *BOURSE (Alpaca)*\nValeur: `{acc.equity}$`\nCash: `{acc.cash}$`\n\n🪙 *CRYPTO (Coinbase)*\nValeur: `{crypto_val:.2f}€`\n{crypto_details}\n\n━━━━━━━━━━━━━━━━━━\n🚀 *SANTÉ: OK*")
            elif text == "/crypto":
                details, total = get_crypto_summary()
                send_telegram(f"🪙 *DÉTAIL CRYPTO*\n\n{details}\n\n━━━━━━━━━━━━━━━━━━\n💰 *TOTAL: {total:.2f}€*")
            elif text == "/positions":
                pos = trading_client.get_all_positions()
                if not pos: send_telegram("❌ Aucune position ouverte.")
                else:
                    lines = [f"🔹 {p.symbol}: `{p.unrealized_plpc:.2f}%`" for p in pos]
                    send_telegram("📉 *POSITIONS ACTIVES:*\n\n" + "\n".join(lines))
            elif text == "/marche":
                snaps = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=["SPY", "QQQ", "IBIT"]))
                lines = [f"🔹 {s}: `{snaps[s].daily_bar.percent_change:.2f}%`" for s in ["SPY", "QQQ", "IBIT"]]
                send_telegram("🌐 *MARKET SNAPSHOT:*\n\n" + "\n".join(lines))
            elif text == "/pause":
                agent_paused = True
                send_telegram("⏸️ *Agent mis en PAUSE*")
            elif text == "/resume":
                agent_paused = False
                send_telegram("▶️ *Agent REPRIS*")
            elif text == "/liquidate":
                liquidate_crypto()
            elif text == "/report":
                acc = trading_client.get_account()
                send_telegram(f"📋 *RAPPORT RAPIDE*\n\n💰 Equity: `{acc.equity}$`\n💵 Cash: `{acc.cash}$`")
    except Exception as e: log.error(f"❌ Telegram Loop Error: {e}")

def telegram_loop():
    log.info("👂 L'oreille Telegram est activée...")
    while True:
        process_commands()
        time.sleep(5)

# ================================================================
# TRADING CORE
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
        send_telegram(f"🚀 *TRADE {symbol}* {side.upper()}\n\n🎯 Entrée: `{limit_price}$`\n🛑 SL: `{sl_p:.2f}$`\n✅ TP: `{tp_p:.2f}$`")
    except Exception as e: log.error(f"Order Error: {e}")

def liquidate_crypto():
    try:
        cb = get_coinbase_client()
        if not cb: return
        accounts = cb.get_accounts()["accounts"]
        sold_list = []
        for acc in accounts:
            curr = acc["currency"]
            bal = float(acc["available_balance"]["value"])
            if bal > 0 and curr not in ["EUR", "USD"]:
                prod = f"{curr}-EUR"
                if prod in CRYPTO_WATCHLIST:
                    cb.market_order_sell(client_order_id=str(uuid.uuid4()), product_id=prod, base_size=str(bal))
                    sold_list.append(f"{curr} ({bal})")
        send_telegram(f"💰 *LIQUIDATION CRYPTO*\n\nFonds vendus: {', '.join(sold_list)}" if sold_list else "Rien à vendre")
    except Exception as e: send_telegram(f"❌ Erreur liquidation: {e}")

def scan_crypto():
    if agent_paused: return
    try:
        cb = get_coinbase_client()
        if not cb: return
        for product_id in CRYPTO_WATCHLIST:
            price = float(cb.get_best_bid_ask(product_ids=[product_id])["pricebooks"][0]["bids"][0]["price"])
            res = claude_client.messages.create(
                model="claude-3-5-haiku-20241022", max_tokens=100, 
                system="Trader institutionnel. JSON uniquement: {'action':'BUY'|'HOLD','confidence':0-100}",
                messages=[{"role": "user", "content": f"Crypto: {product_id}, Price: {price}. Signal?"}]
            )
            signal = json.loads(res.content[0].text.strip().replace("```json", "").replace("```", ""))
            if signal.get("confidence", 0) >= CONFIDENCE_THRESHOLD and signal["action"] == "BUY":
                usd_size = get_account_info()["equity"] * 0.02
                cb.market_order_buy(client_order_id=str(uuid.uuid4()), product_id=product_id, quote_size=str(round(usd_size, 2)))
                send_telegram(f"🪙 *BUY CRYPTO {product_id}*\n\nPrix: `{price}$`\nConfiance: `{signal['confidence']}%`")
            time.sleep(1)
    except Exception as e: log.error(f"Crypto Scan Error: {e}")

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
                    send_telegram(f"⚖️ *REBALANCING*\n\nSoutien de l'actif: `{symbol}`\nMontant: `{buy_amt:.2f}$`")
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
# SERVEUR & MAIN
# ================================================================
class _Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Agent V12 OK")
    def log_message(self, *args): pass

if __name__ == "__main__":
    log.info("🚀 AGENT V12 ULTIME DASHBOARD DEMARRAGE...")
    
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(), daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    
    schedule.every(15).minutes.do(scan_and_trade)
    schedule.every(30).minutes.do(scan_crypto)
    schedule.every().day.at("10:00").do(check_rebalancing)

    send_telegram("🤖 *Agent V12 ULTIME en ligne !*\n\nL'oreille est active. Tapez /portfolio pour voir vos actifs ou /aide pour les commandes.")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
