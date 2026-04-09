# -*- coding: utf-8 -*-

import os, json, re, time, threading, logging, uuid, ast
from datetime import datetime, timedelta
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
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

try:
from coinbase.rest import RESTClient as CoinbaseClient
COINBASE_AVAILABLE = True
except ImportError:
COINBASE_AVAILABLE = False
print(”[WARN] coinbase-advanced-py non installe”)

load_dotenv()

logging.basicConfig(level=logging.INFO, format=”%(asctime)s %(levelname)s %(message)s”)
log = logging.getLogger(**name**)

ALPACA_API_KEY      = os.getenv(“ALPACA_API_KEY”)
ALPACA_SECRET_KEY   = os.getenv(“ALPACA_SECRET_KEY”)
PAPER_MODE          = os.getenv(“PAPER_MODE”, “True”) == “True”
ANTHROPIC_API_KEY   = os.getenv(“ANTHROPIC_API_KEY”)
TELEGRAM_TOKEN      = os.getenv(“TELEGRAM_TOKEN”)
TELEGRAM_CHAT_ID    = os.getenv(“TELEGRAM_CHAT_ID”)
COINBASE_API_KEY    = os.getenv(“COINBASE_API_KEY”, “”)
COINBASE_API_SECRET = os.getenv(“COINBASE_SECRET_KEY”, “”)

HOLD_PCT             = 0.65
STOCK_SL_PCT         = 2.0
STOCK_TP_PCT         = 4.0
MAX_RISK_PER_TRADE   = 0.02
CONFIDENCE_THRESHOLD = 50

STOCK_WATCHLIST = [“NVDA”, “AAPL”, “JPM”, “UNH”, “WMT”, “CAT”, “XOM”]
CRYPTO_WATCHLIST = [“BTC-EUR”, “ETH-EUR”, “SOL-EUR”]
CORE_TARGETS = {“VT”: 0.40, “SCHD”: 0.15, “VNQ”: 0.05, “QQQ”: 0.15, “IBIT”: 0.10}
EST = ZoneInfo(“America/New_York”)

agent_paused   = False
last_update_id = 0

try:
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
log.info(“Clients API OK”)
except Exception as e:
log.error(“Erreur Init Clients: %s”, e)

_cb = None
def get_coinbase_client():
global _cb
if _cb is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
try:
_cb = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
except Exception as e:
log.error(“Coinbase init: %s”, e)
return _cb

# ================================================================

# TELEGRAM

# ================================================================

def send_telegram(msg):
if not TELEGRAM_TOKEN:
return
try:
url = “https://api.telegram.org/bot” + TELEGRAM_TOKEN.strip() + “/sendMessage”
payload = {
“chat_id”: str(TELEGRAM_CHAT_ID).strip(),
“text”: msg,
“parse_mode”: “Markdown”
}
requests.post(url, json=payload, timeout=10)
except Exception as e:
log.error(“Telegram Send Error: %s”, e)

def get_crypto_summary():
cb = get_coinbase_client()
if not cb:
return “Non configure”, 0
try:
accounts = cb.get_accounts().accounts
total_eur = 0
details = []
for acc in accounts:
curr = acc.currency
bal = float(acc.available_balance.value)
if bal <= 0:
continue
if curr == “EUR”:
total_eur += bal
details.append(“EUR: “ + str(round(bal, 2)) + “ (cash)”)
else:
prod = curr + “-EUR”
try:
price = float(cb.get_best_bid_ask(product_ids=[prod]).pricebooks[0].bids[0].price)
val = bal * price
total_eur += val
details.append(curr + “: “ + str(round(bal, 4)) + “ (~” + str(round(val, 2)) + “)”)
except Exception:
continue
return “\n”.join(details) if details else “Aucun actif”, total_eur
except Exception as e:
return “Erreur: “ + str(e), 0

def process_commands():
global last_update_id, agent_paused
if not TELEGRAM_TOKEN:
return
try:
url = “https://api.telegram.org/bot” + TELEGRAM_TOKEN.strip() + “/getUpdates”
params = {“offset”: last_update_id + 1, “timeout”: 10}
r = requests.get(url, params=params, timeout=15)
if r.status_code != 200:
return
updates = r.json().get(“result”, [])
for update in updates:
last_update_id = update[“update_id”]
msg = update.get(“message”, {})
text = msg.get(“text”, “”).strip().lower()
chat_id = str(msg.get(“chat”, {}).get(“id”))
if chat_id != str(TELEGRAM_CHAT_ID).strip():
continue
if text in [”/start”, “/aide”]:
send_telegram(“AGENT V12\n\n/status\n/portfolio\n/positions\n/pause\n/resume\n/scan\n/report”)
elif text == “/status”:
acc = trading_client.get_account()
send_telegram(“STATUS\n\nEquity: “ + str(acc.equity) + “$\nCash: “ + str(acc.cash) + “$\nMode: “ + (“PAPER” if PAPER_MODE else “LIVE”) + “\nPause: “ + (“OUI” if agent_paused else “NON”))
elif text == “/portfolio”:
acc = trading_client.get_account()
crypto_details, crypto_val = get_crypto_summary()
send_telegram(“BILAN\n\nBOURSE\nEquity: “ + str(acc.equity) + “$\nCash: “ + str(acc.cash) + “$\n\nCRYPTO\nValeur: “ + str(round(crypto_val, 2)) + “\n” + crypto_details)
elif text == “/positions”:
pos = trading_client.get_all_positions()
if not pos:
send_telegram(“Aucune position ouverte.”)
else:
lines = [p.symbol + “: “ + str(round(float(p.unrealized_plpc), 2)) + “%” for p in pos]
send_telegram(“POSITIONS:\n\n” + “\n”.join(lines))
elif text == “/marche”:
try:
snaps = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=[“SPY”, “QQQ”]))
lines = [s + “: “ + str(round(snaps[s].daily_bar.percent_change, 2)) + “%” for s in [“SPY”, “QQQ”]]
send_telegram(“MARKET:\n\n” + “\n”.join(lines))
except Exception as e:
send_telegram(“Erreur marche: “ + str(e))
elif text == “/pause”:
agent_paused = True
send_telegram(“Agent en PAUSE”)
elif text == “/resume”:
agent_paused = False
send_telegram(“Agent REPRIS”)
elif text == “/scan”:
send_telegram(“Scan force…”)
threading.Thread(target=scan_and_trade, daemon=True).start()
elif text == “/report”:
acc = trading_client.get_account()
send_telegram(“RAPPORT\n\nEquity: “ + str(acc.equity) + “$\nCash: “ + str(acc.cash) + “$”)
except Exception as e:
log.error(“Telegram Loop Error: %s”, e)

def telegram_loop():
log.info(“Telegram loop active”)
while True:
process_commands()
time.sleep(5)

# ================================================================

# TRADING

# ================================================================

def get_account_info():
a = trading_client.get_account()
return {“equity”: float(a.equity), “cash”: float(a.cash)}

def place_bracket_order(symbol, side, limit_price, sl_pct, tp_pct):
try:
account = get_account_info()
sl_dist = limit_price * (sl_pct / 100.0)
qty = round((account[“equity”] * MAX_RISK_PER_TRADE) / sl_dist, 4)
if qty <= 0:
return
if side == “buy”:
sl_p = round(limit_price * (1 - sl_pct / 100), 2)
tp_p = round(limit_price * (1 + tp_pct / 100), 2)
req = LimitOrderRequest(
symbol=symbol,
qty=qty,
side=OrderSide.BUY,
time_in_force=TimeInForce.DAY,
limit_price=round(limit_price, 2),
order_class=OrderClass.BRACKET,
take_profit=TakeProfitRequest(limit_price=tp_p),
stop_loss=StopLossRequest(stop_price=sl_p)
)
else:
if not PAPER_MODE:
return
sl_p = round(limit_price * (1 + sl_pct / 100), 2)
tp_p = round(limit_price * (1 - tp_pct / 100), 2)
req = LimitOrderRequest(
symbol=symbol,
qty=qty,
side=OrderSide.SELL,
time_in_force=TimeInForce.DAY,
limit_price=round(limit_price, 2),
order_class=OrderClass.BRACKET,
take_profit=TakeProfitRequest(limit_price=tp_p),
stop_loss=StopLossRequest(stop_price=sl_p)
)
trading_client.submit_order(req)
send_telegram(“TRADE “ + symbol + “ “ + side.upper() + “\n\nEntree: “ + str(limit_price) + “$\nSL: “ + str(sl_p) + “$\nTP: “ + str(tp_p) + “$”)
log.info(“Order placed: %s %s entry=%.2f sl=%.2f tp=%.2f”, symbol, side, limit_price, sl_p, tp_p)
except Exception as e:
log.error(“Order Error %s: %s”, symbol, e)
send_telegram(“Erreur order “ + symbol + “: “ + str(e))

def parse_claude_signal(raw):
clean = re.sub(r”```(?:json)?”, “”, raw).strip().rstrip(”`”).strip()
try:
return json.loads(clean)
except json.JSONDecodeError:
return ast.literal_eval(clean)

SYSTEM_STOCK = ‘Trader. JSON only, nothing else. Exact format: {“action”:“BUY”,“confidence”:75} or {“action”:“SHORT”,“confidence”:60} or {“action”:“HOLD”,“confidence”:40}’
SYSTEM_CRYPTO = ‘Trader. JSON only, nothing else. Exact format: {“action”:“BUY”,“confidence”:75} or {“action”:“HOLD”,“confidence”:40}’

def scan_and_trade():
if agent_paused:
return
log.info(“Scanning markets…”)
traded = 0
for ticker in STOCK_WATCHLIST:
try:
req = StockBarsRequest(
symbol_or_symbols=ticker,
timeframe=TimeFrame(5, TimeFrameUnit.Minute),
start=datetime.now(EST) - timedelta(days=1)
)
bars = list(data_client.get_stock_bars(req)[ticker])
if not bars:
log.warning(“No bars for %s”, ticker)
continue
price = bars[-1].close
res = claude_client.messages.create(
model=“claude-haiku-4-5-20251001”,
max_tokens=50,
system=SYSTEM_STOCK,
messages=[{“role”: “user”, “content”: “Ticker: “ + ticker + “, Price: “ + str(price) + “$. Signal?”}]
)
raw = res.content[0].text.strip()
log.info(“Claude signal [%s]: %s”, ticker, raw)
try:
signal = parse_claude_signal(raw)
except Exception as parse_err:
log.error(“Parse error [%s]: %s | raw: %s”, ticker, parse_err, raw)
continue
action = signal.get(“action”, “HOLD”)
confidence = signal.get(“confidence”, 0)
log.info(”%s: action=%s confidence=%s”, ticker, action, confidence)
if confidence >= CONFIDENCE_THRESHOLD and action != “HOLD”:
place_bracket_order(ticker, “buy” if action == “BUY” else “short”, price, STOCK_SL_PCT, STOCK_TP_PCT)
traded += 1
except Exception as e:
log.error(“Scan Error %s: %s”, ticker, e)
time.sleep(1)
log.info(“Scan termine. %d ordre(s).”, traded)
send_telegram(“Scan termine. “ + str(traded) + “ ordre(s) place(s).”)

def scan_crypto():
if agent_paused:
return
try:
cb = get_coinbase_client()
if not cb:
return
for product_id in CRYPTO_WATCHLIST:
price = float(cb.get_best_bid_ask(product_ids=[product_id]).pricebooks[0].bids[0].price)
res = claude_client.messages.create(
model=“claude-haiku-4-5-20251001”,
max_tokens=50,
system=SYSTEM_CRYPTO,
messages=[{“role”: “user”, “content”: “Crypto: “ + product_id + “, Price EUR: “ + str(price) + “. Signal?”}]
)
raw = res.content[0].text.strip()
log.info(“Claude crypto signal [%s]: %s”, product_id, raw)
try:
signal = parse_claude_signal(raw)
except Exception as parse_err:
log.error(“Parse error [%s]: %s | raw: %s”, product_id, parse_err, raw)
continue
if signal.get(“confidence”, 0) >= CONFIDENCE_THRESHOLD and signal.get(“action”) == “BUY”:
usd_size = get_account_info()[“equity”] * 0.02
cb.market_order_buy(
client_order_id=str(uuid.uuid4()),
product_id=product_id,
quote_size=str(round(usd_size, 2))
)
send_telegram(“BUY CRYPTO “ + product_id + “\n\nPrix: “ + str(price) + “\nConfiance: “ + str(signal[“confidence”]) + “%”)
time.sleep(1)
except Exception as e:
log.error(“Crypto Scan Error: %s”, e)

def check_rebalancing():
try:
account = get_account_info()
hold_cap = account[“equity”] * HOLD_PCT
positions = {p.symbol: float(p.market_value) for p in trading_client.get_all_positions()}
for symbol, target in CORE_TARGETS.items():
target_usd = hold_cap * target
actual_usd = positions.get(symbol, 0)
if (target_usd - actual_usd) / hold_cap > 0.05:
buy_amt = target_usd - actual_usd
if account[“cash”] > buy_amt:
trading_client.submit_order(MarketOrderRequest(
symbol=symbol,
notional=round(buy_amt, 2),
side=OrderSide.BUY,
time_in_force=TimeInForce.DAY
))
send_telegram(“REBALANCING “ + symbol + “ +” + str(round(buy_amt, 2)) + “$”)
except Exception as e:
log.error(“Rebalance Error: %s”, e)

# ================================================================

# HEALTH SERVER

# ================================================================

class _Health(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b”Agent V12 OK”)
def log_message(self, *args):
pass

# ================================================================

# MAIN

# ================================================================

if **name** == “**main**”:
log.info(“AGENT V12 DEMARRAGE”)

```
threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(),
    daemon=True
).start()
threading.Thread(target=telegram_loop, daemon=True).start()

schedule.every(15).minutes.do(scan_and_trade)
schedule.every(30).minutes.do(scan_crypto)
schedule.every().day.at("10:00").do(check_rebalancing)

send_telegram("Agent V12 en ligne. Scan dans 5s...")
time.sleep(5)
threading.Thread(target=scan_and_trade, daemon=True).start()

while True:
    schedule.run_pending()
    time.sleep(1)
```
