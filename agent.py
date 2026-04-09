# -*- coding: utf-8 -*-

# Agent Trading V12 - Alpaca + Coinbase

import os, json, time, threading, logging, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
import schedule
import anthropic

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
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
from coinbase.rest import RESTClient as CoinbaseClient
COINBASE_AVAILABLE = True
except ImportError:
COINBASE_AVAILABLE = False
print(”[WARN] coinbase-advanced-py non installe”)

load_dotenv()

# ================================================================

# CONFIGURATION

# ================================================================

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
NEWS_API_KEY        = os.getenv(“NEWS_API_KEY”, “”)
NOTION_TOKEN        = os.getenv(“NOTION_TOKEN”, “”)
NOTION_PAGE_ID      = os.getenv(“NOTION_PAGE_ID”, “3375afb215b4819785c5df026f5cdd75”)

# Allocation Core-Satellite

HOLD_PCT     = 0.65
DAYTRADE_PCT = 0.35

# Risk management

STOCK_SL_PCT           = 2.0
STOCK_TP_PCT           = 4.0
CRYPTO_SL_PCT          = 3.0
CRYPTO_TP_PCT          = 6.0
MAX_RISK_PER_TRADE_PCT = 0.02
CONFIDENCE_THRESHOLD   = 80     # FIXE - jamais adaptatif
MIN_CONFLUENCES        = 3
START_CAPITAL          = 100_000.0

# Watchlists (V11)

STOCK_WATCHLIST  = [“NVDA”, “AAPL”, “JPM”, “UNH”, “WMT”, “CAT”, “XOM”]
CRYPTO_WATCHLIST = [“BTC-EUR”, “ETH-EUR”, “SOL-EUR”, “XRP-EUR”, “AVAX-EUR”, “LINK-EUR”, “ADA-EUR”]
CORE_TARGETS     = {“VT”: 0.40, “SCHD”: 0.15, “VNQ”: 0.05, “QQQ”: 0.15, “IBIT”: 0.10}

# News search terms

SEARCH_TERMS = {
“NVDA”: “NVIDIA OR NVDA”, “AAPL”: “Apple OR AAPL”,
“JPM”:  “JPMorgan OR JPM”, “UNH”: “UnitedHealth OR UNH”,
“WMT”:  “Walmart OR WMT”,  “CAT”: “Caterpillar OR CAT”,
“XOM”:  “ExxonMobil OR XOM”,
“BTC”:  “Bitcoin OR BTC”,  “ETH”: “Ethereum OR ETH”,
“SOL”:  “Solana OR SOL”,   “XRP”: “Ripple OR XRP”
}
HIGH_RISK_KW = [“earnings report”, “SEC investigation”, “fraud”, “bankruptcy”, “delisted”, “lawsuit”]

# Horaires NYSE EST

EST            = ZoneInfo(“America/New_York”)
MARKET_OPEN    = (9, 30)
MARKET_CLOSE   = (16, 0)
BLACKOUT_START = (11, 0)
BLACKOUT_END   = (14, 0)

# Etat global

agent_paused           = False
last_update_id         = 0
open_positions_tracker = {}
TRADES_FILE            = “trades.json”

# ================================================================

# CLIENTS

# ================================================================

try:
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
log.info(“Clients API initialises”)
except Exception as e:
log.error(f”Erreur init clients: {e}”)

_cb_client = None
def get_coinbase_client():
global _cb_client
if _cb_client is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
_cb_client = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
return _cb_client

# ================================================================

# TELEGRAM

# ================================================================

def send_telegram(msg):
if not TELEGRAM_TOKEN:
log.info(f”[TG] {msg[:100]}”)
return
try:
requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”,
json={“chat_id”: TELEGRAM_CHAT_ID, “text”: msg, “parse_mode”: “Markdown”},
timeout=10
)
except Exception as e:
log.error(f”Telegram: {e}”)

# ================================================================

# TRADE LOGGER - JSON local + Notion

# ================================================================

def load_trades():
if os.path.exists(TRADES_FILE):
try:
return json.load(open(TRADES_FILE))
except Exception:
return []
return []

def save_trades(trades):
json.dump(trades, open(TRADES_FILE, “w”), indent=2, default=str)

def log_trade_open(key, side, entry, sl, tp, signal_type, conviction, n_conf, platform=“alpaca”):
open_positions_tracker[key] = {
“entry”: entry, “sl”: sl, “tp”: tp,
“signal”: signal_type, “conviction”: conviction,
“confluences”: n_conf, “side”: side, “platform”: platform,
“time”: datetime.now(EST).isoformat()
}

def log_trade_close(key, exit_price):
if key not in open_positions_tracker:
return None
pos     = open_positions_tracker.pop(key)
entry   = pos[“entry”]
side    = pos[“side”]
pnl_pct = ((exit_price - entry) / entry * 100) if side == “buy” else ((entry - exit_price) / entry * 100)
pnl_usd = pnl_pct / 100 * entry * 10
trade = {
“symbol”: key, “side”: side, “entry”: entry, “exit”: exit_price,
“pnl_pct”: round(pnl_pct, 2), “pnl_usd”: round(pnl_usd, 2),
“signal”: pos[“signal”], “conviction”: pos[“conviction”],
“confluences”: pos[“confluences”],
“entry_hour”: datetime.fromisoformat(pos[“time”]).hour,
“platform”: pos.get(“platform”, “alpaca”),
“date”: datetime.now(EST).strftime(”%Y-%m-%d”),
“timestamp”: datetime.now(EST).isoformat()
}
trades = load_trades()
trades.append(trade)
save_trades(trades)
_log_to_notion(trade)
return trade

def _log_to_notion(trade):
if not NOTION_TOKEN:
return
emoji   = “OK” if trade[“pnl_usd”] >= 0 else “LOSS”
content = (
f”[{emoji}] {trade[‘symbol’]} {trade[‘side’].upper()} | “
f”PnL {trade[‘pnl_usd’]:+.2f}$ ({trade[‘pnl_pct’]:+.1f}%) | “
f”Signal: {trade[‘signal’]} | {trade[‘platform’]} | {trade[‘date’]}”
)
try:
requests.patch(
f”https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children”,
headers={“Authorization”: f”Bearer {NOTION_TOKEN}”,
“Notion-Version”: “2022-06-28”, “Content-Type”: “application/json”},
json={“children”: [{“object”: “block”, “type”: “paragraph”,
“paragraph”: {“rich_text”: [{“type”: “text”, “text”: {“content”: content}}]}}]},
timeout=5
)
except Exception as e:
log.error(f”Notion: {e}”)

# ================================================================

# INDICATEURS TECHNIQUES

# ================================================================

def calculate_atr(bars, period=14):
if len(bars) < period + 1:
return 0
tr_list = []
for i in range(1, len(bars)):
h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
return sum(tr_list[-period:]) / period

def calculate_rsi(closes, period=14):
if len(closes) < period + 1:
return 50
gains, losses = [], []
for i in range(1, len(closes)):
diff = closes[i] - closes[i-1]
gains.append(max(diff, 0))
losses.append(max(-diff, 0))
avg_gain = sum(gains[-period:]) / period
avg_loss = sum(losses[-period:]) / period
if avg_loss == 0:
return 100
return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_ema(closes, period):
if len(closes) < period:
return closes[-1]
k, ema = 2 / (period + 1), closes[0]
for p in closes[1:]:
ema = p * k + ema * (1 - k)
return ema

def get_vix():
try:
return data_client.get_stock_latest_bar(
StockLatestBarRequest(symbol_or_symbols=[“VIXY”])
)[“VIXY”].close
except Exception:
return 20.0

def get_smc_data(ticker):
“”“SMC + EMA + RSI + Volume pour stocks Alpaca.”””
try:
req  = StockBarsRequest(
symbol_or_symbols=ticker, timeframe=TimeFrame.Minute5,
start=datetime.now(EST) - timedelta(days=3)
)
bars = list(data_client.get_stock_bars(req)[ticker])
if len(bars) < 30:
return None
closes  = [b.close  for b in bars]
highs   = [b.high   for b in bars]
lows    = [b.low    for b in bars]
volumes = [b.volume for b in bars]
cur     = closes[-1]
atr     = calculate_atr(bars)
ema9    = calculate_ema(closes, 9)
ema21   = calculate_ema(closes, 21)
rsi_now = calculate_rsi(closes[-15:])
rsi_prev= calculate_rsi(closes[-20:-5])
sh      = max(highs[-20:])
sl      = min(lows[-20:])
avg_vol = sum(volumes[-20:]) / 20
return {
“current”: cur, “atr”: atr,
“swing_high”: sh, “swing_low”: sl,
“sweep_bullish”: cur > sl and min(lows[-5:]) < sl * 1.002,
“sweep_bearish”: cur < sh and max(highs[-5:]) > sh * 0.998,
“trend”: “haussier” if cur > closes[-20] else “baissier”,
“ema9”: ema9, “ema21”: ema21, “ema_bullish”: ema9 > ema21,
“rsi”: rsi_now,
“rsi_div_bull”: (cur < closes[-20]) and (rsi_now > rsi_prev),
“rsi_div_bear”: (cur > closes[-20]) and (rsi_now < rsi_prev),
“volume_ok”: volumes[-1] >= avg_vol * 0.8,
}
except Exception as e:
log.error(f”SMC {ticker}: {e}”)
return None

def get_crypto_smc(product_id):
“”“SMC + EMA + RSI + Volume pour crypto Coinbase.”””
try:
cb    = get_coinbase_client()
if not cb:
return None
end   = int(datetime.now(timezone.utc).timestamp())
start = end - (50 * 5 * 60)
res   = cb.get_candles(product_id=product_id, start=str(start), end=str(end), granularity=“FIVE_MINUTE”)
candles = sorted(res.get(“candles”, []), key=lambda c: c[“start”])
if len(candles) < 25:
return None
closes  = [float(c[“close”])  for c in candles]
highs   = [float(c[“high”])   for c in candles]
lows    = [float(c[“low”])    for c in candles]
volumes = [float(c[“volume”]) for c in candles]
cur     = closes[-1]
ema9    = calculate_ema(closes, 9)
ema21   = calculate_ema(closes, 21)
rsi_now = calculate_rsi(closes[-15:])
rsi_prev= calculate_rsi(closes[-20:-5])
sh      = max(highs[-20:])
sl      = min(lows[-20:])
avg_vol = sum(volumes[-20:]) / 20
atr     = sum(abs(highs[i] - lows[i]) for i in range(-14, 0)) / 14
return {
“current”: cur, “atr”: atr,
“swing_high”: sh, “swing_low”: sl,
“sweep_bullish”: cur > sl and min(lows[-5:]) < sl * 1.002,
“sweep_bearish”: cur < sh and max(highs[-5:]) > sh * 0.998,
“trend”: “haussier” if cur > closes[-20] else “baissier”,
“ema9”: ema9, “ema21”: ema21, “ema_bullish”: ema9 > ema21,
“rsi”: rsi_now,
“rsi_div_bull”: (cur < closes[-20]) and (rsi_now > rsi_prev),
“rsi_div_bear”: (cur > closes[-20]) and (rsi_now < rsi_prev),
“volume_ok”: volumes[-1] >= avg_vol * 0.8,
}
except Exception as e:
log.error(f”Crypto SMC {product_id}: {e}”)
return None

# ================================================================

# NEWS SENTIMENT

# ================================================================

def get_news_sentiment(ticker):
if not NEWS_API_KEY:
return {“sentiment”: “NEUTRAL”, “pause”: False}
try:
params = {
“q”: f”({SEARCH_TERMS.get(ticker, ticker)}) AND (stock OR market OR earnings)”,
“from”: (datetime.now() - timedelta(hours=6)).isoformat(),
“sortBy”: “relevancy”, “language”: “en”,
“apiKey”: NEWS_API_KEY, “pageSize”: 5
}
r = requests.get(“https://newsapi.org/v2/everything”, params=params, timeout=8)
articles = r.json().get(“articles”, []) if r.ok else []
for a in articles:
title = a.get(“title”, “”).lower()
for kw in HIGH_RISK_KW:
if kw in title:
return {“sentiment”: “BEARISH”, “pause”: True}
pos_w = [“surge”,“rally”,“gain”,“bullish”,“beat”,“record”,“growth”,“up”]
neg_w = [“drop”,“fall”,“crash”,“bearish”,“miss”,“decline”,“warning”,“down”]
pos = sum(1 for a in articles for w in pos_w if w in a.get(“title”,””).lower())
neg = sum(1 for a in articles for w in neg_w if w in a.get(“title”,””).lower())
s = “BULLISH” if pos > neg + 1 else “BEARISH” if neg > pos + 1 else “NEUTRAL”
return {“sentiment”: s, “pause”: False}
except Exception as e:
log.error(f”News {ticker}: {e}”)
return {“sentiment”: “NEUTRAL”, “pause”: False}

# ================================================================

# MULTI-CONFLUENCE

# ================================================================

def count_confluences(smc, news_sentiment, direction):
c = []
if direction == “BUY”:
if smc.get(“ema_bullish”):          c.append(“EMA9>EMA21”)
if smc.get(“sweep_bullish”):         c.append(“Sweep bull”)
if smc.get(“rsi_div_bull”):          c.append(“RSI div bull”)
if smc.get(“volume_ok”):             c.append(“Volume OK”)
if news_sentiment == “BULLISH”:      c.append(“News bull”)
if smc.get(“trend”) == “haussier”:   c.append(“Trend haussier”)
else:
if not smc.get(“ema_bullish”):       c.append(“EMA9<EMA21”)
if smc.get(“sweep_bearish”):         c.append(“Sweep bear”)
if smc.get(“rsi_div_bear”):          c.append(“RSI div bear”)
if smc.get(“volume_ok”):             c.append(“Volume OK”)
if news_sentiment == “BEARISH”:      c.append(“News bear”)
if smc.get(“trend”) == “baissier”:   c.append(“Trend baissier”)
return len(c), c

# ================================================================

# RISK MANAGEMENT - ATR + Kelly

# ================================================================

def get_win_rate():
trades = load_trades()
if len(trades) < 5:
return 0.5
recent = trades[-20:]
return sum(1 for t in recent if t[“pnl_usd”] > 0) / len(recent)

def get_account_info():
a = trading_client.get_account()
return {“equity”: float(a.equity), “cash”: float(a.cash)}

# ================================================================

# CLAUDE SIGNAL

# ================================================================

PROMPT_SYSTEM = (
“Tu es un trader institutionnel. Jamais d’emotion. RR 1:2 minimum.\n”
“Reponds UNIQUEMENT en JSON strict:\n”
‘{“action”:“BUY”|“SHORT”|“HOLD”,“confidence”:0-100,’
‘“signal_type”:“SMC”|“SMC+RSI”|“SMC+EMA”|“SMC+RSI+EMA”,“reason”:“max 10 mots”}\n’
“Si confidence < 80 -> action HOLD obligatoire.”
)

def get_claude_signal(ticker, smc, news):
context = (
f”Ticker: {ticker} | Prix: {smc[‘current’]}\n”
f”Trend: {smc[‘trend’]} | ATR: {smc[‘atr’]:.4f}\n”
f”Sweep Bull: {smc[‘sweep_bullish’]} | Sweep Bear: {smc[‘sweep_bearish’]}\n”
f”EMA9: {smc[‘ema9’]:.4f} vs EMA21: {smc[‘ema21’]:.4f} ({‘BULL’ if smc[‘ema_bullish’] else ‘BEAR’})\n”
f”RSI: {smc[‘rsi’]:.1f} | Div Bull: {smc[‘rsi_div_bull’]} | Div Bear: {smc[‘rsi_div_bear’]}\n”
f”Volume: {‘OK’ if smc[‘volume_ok’] else ‘FAIBLE’} | News: {news[‘sentiment’]}”
)
try:
res = claude_client.messages.create(
model=“claude-haiku-4-5-20251001”,
max_tokens=150,
system=PROMPT_SYSTEM,
messages=[{“role”: “user”, “content”: context}]
)
raw = res.content[0].text.strip().replace(”`json","").replace("`”,””)
return json.loads(raw)
except Exception as e:
log.error(f”Claude {ticker}: {e}”)
return None

# ================================================================

# HELPERS MARCHE

# ================================================================

def is_market_open():
now = datetime.now(EST)
if now.weekday() >= 5:
return False
t = (now.hour, now.minute)
return MARKET_OPEN <= t < MARKET_CLOSE

def is_blackout():
t = (datetime.now(EST).hour, datetime.now(EST).minute)
return BLACKOUT_START <= t < BLACKOUT_END

def get_market_snapshots():
try:
req   = StockSnapshotRequest(symbol_or_symbols=[“SPY”,“QQQ”,“IBIT”])
snaps = data_client.get_stock_snapshot(req)
result = {}
for s in [“SPY”,“QQQ”,“IBIT”]:
try:
snap = snaps[s]
pct  = ((snap.daily_bar.close - snap.previous_daily_bar.close) / snap.previous_daily_bar.close) * 100
except Exception:
try:
pct = ((snap.daily_bar.close - snap.daily_bar.open) / snap.daily_bar.open) * 100
except Exception:
pct = 0.0
result[s] = pct
return result
except Exception:
return {“SPY”: 0.0, “QQQ”: 0.0, “IBIT”: 0.0}

# ================================================================

# COINBASE DASHBOARD - V11

# ================================================================

def get_crypto_summary():
“”“Calcule la valeur totale des cryptos sur Coinbase.”””
try:
cb = get_coinbase_client()
if not cb:
return “Non configure”, 0
accounts  = cb.get_accounts()[“accounts”]
total_eur = 0
details   = []
for acc in accounts:
curr = acc[“currency”]
bal  = float(acc[“available_balance”][“value”])
if bal > 0 and curr != “EUR”:
prod = f”{curr}-EUR”
try:
price = float(cb.get_best_bid_ask(product_ids=[prod])[“pricebooks”][0][“bids”][0][“price”])
val   = bal * price
total_eur += val
details.append(f”*{curr}*: {bal:.4f} (~{val:.2f}EUR)”)
except Exception:
continue
elif curr == “EUR”:
total_eur += bal
summary = “\n”.join(details) if details else “Aucun actif crypto”
return summary, total_eur
except Exception as e:
return f”Erreur: {e}”, 0

def liquidate_crypto_for_cash():
“”“Vend toutes les cryptos Coinbase contre EUR.”””
try:
cb = get_coinbase_client()
if not cb:
send_telegram(“Coinbase non configure”)
return
accounts  = cb.get_accounts()[“accounts”]
sold_list = []
for acc in accounts:
currency = acc[“currency”]
balance  = float(acc[“available_balance”][“value”])
if balance > 0 and currency not in [“EUR”,“USD”]:
product_id = f”{currency}-EUR”
if product_id in CRYPTO_WATCHLIST:
cb.market_order_sell(
client_order_id=str(uuid.uuid4()),
product_id=product_id,
base_size=str(balance)
)
sold_list.append(f”{currency} ({balance:.4f})”)
if sold_list:
send_telegram(f”*LIQUIDATION CRYPTO*\n\nVendu: {’, ’.join(sold_list)}”)
else:
send_telegram(“Aucun actif crypto a vendre.”)
except Exception as e:
send_telegram(f”Erreur liquidation: {e}”)

# ================================================================

# EXECUTION STOCKS - Alpaca bracket orders

# ================================================================

def place_bracket_order(symbol, side, limit_price, sl_pct, tp_pct,
signal_type=“SMC”, conviction=80, conf_list=None):
if conf_list is None:
conf_list = []
try:
account  = get_account_info()
equity   = account[“equity”]
sl_dist  = limit_price * (sl_pct / 100.0)
if sl_dist <= 0:
return
# ATR sizing
qty_atr  = (equity * MAX_RISK_PER_TRADE_PCT) / sl_dist
qty_cap  = (equity * DAYTRADE_PCT / len(STOCK_WATCHLIST)) / limit_price
# Kelly conservateur
win_rate = get_win_rate()
rr       = tp_pct / sl_pct
kelly    = max(0.01, min(win_rate - (1 - win_rate) / rr, 0.25))
qty_kelly= (equity * kelly * 0.25) / limit_price
qty      = round(min(qty_atr, qty_cap, qty_kelly), 4)
if qty <= 0:
log.warning(f”{symbol} qty=0”)
return

```
    if side == "buy":
        sl_p, tp_p = limit_price * (1 - sl_pct/100), limit_price * (1 + tp_pct/100)
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(tp_p, 2)),
            stop_loss=StopLossRequest(stop_price=round(sl_p, 2))
        )
    else:
        if not PAPER_MODE:
            send_telegram(f"SHORT bloque {symbol} - LIVE")
            return
        sl_p, tp_p = limit_price * (1 + sl_pct/100), limit_price * (1 - tp_pct/100)
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(tp_p, 2)),
            stop_loss=StopLossRequest(stop_price=round(sl_p, 2))
        )

    trading_client.submit_order(req)
    send_telegram(
        f"*TRADE ALPACA*\n"
        f"{'UP' if side=='buy' else 'DOWN'} *{symbol}* {side.upper()}\n"
        f"Entry `{limit_price:.2f}$` Qty `{qty}`\n"
        f"SL `{sl_p:.2f}$` TP `{tp_p:.2f}$` RR {rr:.1f}:1\n"
        f"Conviction {conviction}/100 | {len(conf_list)} confluences\n"
        f"Signal: {signal_type}"
    )
    log_trade_open(symbol, side, limit_price, sl_p, tp_p, signal_type, conviction, len(conf_list), "alpaca")
    log.info(f"Bracket OK: {symbol} {side} @ {limit_price}")
except Exception as e:
    log.error(f"Bracket {symbol}: {e}")
    send_telegram(f"Erreur ordre {symbol}: {e}")
```

# ================================================================

# EXECUTION CRYPTO - Coinbase market orders

# ================================================================

def place_crypto_order(product_id, side, usd_size, signal_type, conviction, conf_list):
try:
cb = get_coinbase_client()
if not cb:
return
price = float(cb.get_best_bid_ask(product_ids=[product_id])[“pricebooks”][0][“bids”][0][“price”])
if price <= 0:
return
account  = get_account_info()
max_usd  = account[“equity”] * MAX_RISK_PER_TRADE_PCT * 10
usd_size = min(usd_size, max_usd)
order_id = str(uuid.uuid4())
if side == “buy”:
cb.market_order_buy(client_order_id=order_id, product_id=product_id, quote_size=str(round(usd_size, 2)))
else:
if not PAPER_MODE:
send_telegram(f”SHORT crypto bloque {product_id} - LIVE”)
return
cb.market_order_sell(client_order_id=order_id, product_id=product_id, base_size=str(round(usd_size / price, 6)))

```
    sl  = price * (1 - CRYPTO_SL_PCT/100) if side == "buy" else price * (1 + CRYPTO_SL_PCT/100)
    tp  = price * (1 + CRYPTO_TP_PCT/100) if side == "buy" else price * (1 - CRYPTO_TP_PCT/100)
    rr  = CRYPTO_TP_PCT / CRYPTO_SL_PCT
    key = f"CB_{product_id}"
    log_trade_open(key, side, price, sl, tp, signal_type, conviction, len(conf_list), "coinbase")
    send_telegram(
        f"*CRYPTO COINBASE*\n"
        f"{'UP' if side=='buy' else 'DOWN'} *{product_id}* {side.upper()}\n"
        f"Prix `{price:.4f}EUR` Size `{usd_size:.0f}EUR`\n"
        f"SL `{sl:.4f}` TP `{tp:.4f}` RR {rr:.1f}:1\n"
        f"Conviction {conviction}/100 | Signal: {signal_type}\n"
        f"SL/TP surveilles automatiquement"
    )
    log.info(f"Crypto: {product_id} {side} @ {price}")
except Exception as e:
    log.error(f"Coinbase order {product_id}: {e}")
    send_telegram(f"Erreur crypto {product_id}: {e}")
```

def check_crypto_sl_tp():
“”“Surveille SL/TP crypto manuellement chaque minute.”””
crypto_pos = {k: v for k, v in open_positions_tracker.items() if v.get(“platform”) == “coinbase”}
for key, pos in list(crypto_pos.items()):
try:
cb = get_coinbase_client()
if not cb:
continue
product_id = key.replace(“CB_”, “”)
bids = cb.get_best_bid_ask(product_ids=[product_id])[“pricebooks”][0][“bids”]
price = float(bids[0][“price”]) if bids else 0
if price <= 0:
continue
side = pos[“side”]
if side == “buy”:
if price <= pos[“sl”]:
send_telegram(f”SL CRYPTO {product_id} @ {price:.4f}EUR”)
log_trade_close(key, price)
elif price >= pos[“tp”]:
send_telegram(f”TP CRYPTO {product_id} @ {price:.4f}EUR”)
log_trade_close(key, price)
else:
if price >= pos[“sl”]:
send_telegram(f”SL SHORT CRYPTO {product_id} @ {price:.4f}EUR”)
log_trade_close(key, price)
elif price <= pos[“tp”]:
send_telegram(f”TP SHORT CRYPTO {product_id} @ {price:.4f}EUR”)
log_trade_close(key, price)
except Exception as e:
log.error(f”SL/TP {key}: {e}”)

# ================================================================

# SCAN STOCKS

# ================================================================

def scan_and_trade():
global agent_paused
if agent_paused or not is_market_open() or is_blackout():
return
vix = get_vix()
if vix > 35:
send_telegram(f”VIX {vix:.1f} > 35 - scan suspendu”)
return
log.info(f”Scan stocks (VIX:{vix:.1f})”)
for ticker in STOCK_WATCHLIST:
if ticker in open_positions_tracker:
continue
try:
smc = get_smc_data(ticker)
if not smc:
continue
news = get_news_sentiment(ticker)
if news[“pause”]:
continue
signal = get_claude_signal(ticker, smc, news)
if not signal or signal.get(“action”) == “HOLD”:
continue
if signal.get(“confidence”, 0) < CONFIDENCE_THRESHOLD:
continue
action = signal[“action”]
side   = “buy” if action == “BUY” else “sell”
n_conf, conf_list = count_confluences(smc, news[“sentiment”], action)
if n_conf < MIN_CONFLUENCES:
log.info(f”{ticker} skip: {n_conf}/{MIN_CONFLUENCES} confluences”)
continue
place_bracket_order(
ticker, side, smc[“current”], STOCK_SL_PCT, STOCK_TP_PCT,
signal.get(“signal_type”,“SMC”), signal[“confidence”], conf_list
)
time.sleep(2)
except Exception as e:
log.error(f”Scan {ticker}: {e}”)

# ================================================================

# SCAN CRYPTO

# ================================================================

def scan_crypto():
global agent_paused
if agent_paused or not COINBASE_AVAILABLE or not COINBASE_API_KEY:
return
account  = get_account_info()
for product_id in CRYPTO_WATCHLIST:
key = f”CB_{product_id}”
if key in open_positions_tracker:
continue
try:
smc = get_crypto_smc(product_id)
if not smc:
continue
ticker = product_id.replace(”-EUR”,””).replace(”-USD”,””)
news   = get_news_sentiment(ticker)
if news[“pause”]:
continue
signal = get_claude_signal(ticker, smc, news)
if not signal or signal.get(“action”) in [“HOLD”,“SHORT”]:
continue
if signal.get(“confidence”, 0) < CONFIDENCE_THRESHOLD:
continue
n_conf, conf_list = count_confluences(smc, news[“sentiment”], “BUY”)
if n_conf < MIN_CONFLUENCES:
continue
ticker_clean = product_id.replace(”-EUR”,””).replace(”-USD”,””)
usd_size = account[“equity”] * (CRYPTO_BIG_SIZE_PCT if ticker_clean in [“BTC”,“ETH”] else CRYPTO_ALT_SIZE_PCT)
place_crypto_order(product_id, “buy”, usd_size, signal.get(“signal_type”,“SMC”), signal[“confidence”], conf_list)
time.sleep(2)
except Exception as e:
log.error(f”Crypto scan {product_id}: {e}”)

# ================================================================

# REBALANCING - V11 (execute vraiment les ordres)

# ================================================================

def check_rebalancing():
try:
account   = get_account_info()
hold_cap  = account[“equity”] * HOLD_PCT
positions = {p.symbol: float(p.market_value) for p in trading_client.get_all_positions()}
for symbol, target in CORE_TARGETS.items():
target_usd = hold_cap * target
actual_usd = positions.get(symbol, 0)
if (target_usd - actual_usd) / hold_cap > 0.05:
buy_amt = target_usd - actual_usd
if account[“cash”] > buy_amt:
trading_client.submit_order(MarketOrderRequest(
symbol=symbol, notional=round(buy_amt, 2),
side=OrderSide.BUY, time_in_force=TimeInForce.DAY
))
send_telegram(f”*REBALANCING*\nAchat `{symbol}` pour `{buy_amt:.2f}$`”)
except Exception as e:
log.error(f”Rebalance: {e}”)

# ================================================================

# DAILY REVIEW - 16h30 EST

# ================================================================

def daily_review():
trades  = load_trades()
today   = datetime.now(EST).strftime(”%Y-%m-%d”)
t_today = [t for t in trades if t.get(“date”) == today]
try:
account   = get_account_info()
equity    = account[“equity”]
last_eq   = float(trading_client.get_account().last_equity)
day_pnl   = equity - last_eq
day_pct   = (day_pnl / last_eq * 100) if last_eq > 0 else 0
total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
except Exception as e:
log.error(f”Daily review: {e}”)
return

```
if not t_today:
    send_telegram(
        f"*DAILY REVIEW {today}*\n"
        f"Aucun trade\n"
        f"Portfolio: {day_pct:+.2f}% ({day_pnl:+.0f}$) | Total: {total_ret:+.2f}%"
    )
    return

wins      = [t for t in t_today if t["pnl_usd"] > 0]
losses    = [t for t in t_today if t["pnl_usd"] <= 0]
pnl_total = sum(t["pnl_usd"] for t in t_today)
win_rate  = len(wins) / len(t_today) * 100

all_50    = trades[-50:]
by_signal, by_hour = {}, {}
for t in all_50:
    s = t.get("signal","?")
    h = t.get("entry_hour", 0)
    by_signal.setdefault(s, {"w":0,"l":0})
    by_hour.setdefault(h, {"w":0,"l":0})
    k = "w" if t["pnl_usd"] > 0 else "l"
    by_signal[s][k] += 1
    by_hour[h][k]   += 1

def best_k(d):
    return max(d, key=lambda k: d[k]["w"]/(d[k]["w"]+d[k]["l"]+0.001)) if d else "N/A"

lines = [
    f"{'OK' if t['pnl_usd']>0 else 'LOSS'} *{t['symbol']}* {t['side'].upper()} `{t['pnl_usd']:+.2f}$` [{t['signal']}] ({t.get('platform','?')})"
    for t in t_today
]
send_telegram(
    f"*DAILY REVIEW {today}*\n"
    f"PnL: {pnl_total:+.2f}$ | Portfolio: {day_pct:+.2f}%\n"
    f"Win Rate: {win_rate:.0f}% ({len(wins)}W/{len(losses)}L)\n\n"
    + "\n".join(lines) + "\n\n"
    f"Meilleur signal: {best_k(by_signal)} | Meilleure heure: {best_k(by_hour)}h\n"
    f"Regle demain: {_generate_rule(by_signal, by_hour)}\n"
    f"Total: {total_ret:+.2f}% | Valeur: {equity:,.0f}$"
)
```

def _generate_rule(by_signal, by_hour):
for sig, s in by_signal.items():
total = s[“w”] + s[“l”]
if total >= 5 and s[“w”] / total < 0.35:
return f”Signal {sig} win rate {s[‘w’]/total*100:.0f}% - surveiller”
for hour, s in by_hour.items():
total = s[“w”] + s[“l”]
if total >= 5 and s[“w”] / total < 0.30:
return f”Eviter {hour}h - win rate {s[‘w’]/total*100:.0f}%”
return “Continuer strategie actuelle”

# ================================================================

# MORNING BRIEF - 9h EST

# ================================================================

def morning_brief():
try:
vix       = get_vix()
snaps     = get_market_snapshots()
account   = get_account_info()
equity    = account[“equity”]
total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
win_rate  = get_win_rate() * 100
vix_s     = “PANIQUE” if vix > 35 else “VOLATIL” if vix > 25 else “NORMAL”
_, crypto_val = get_crypto_summary()
send_telegram(
f”*MORNING BRIEF*\n”
f”SPY `{snaps['SPY']:+.2f}%` | QQQ `{snaps['QQQ']:+.2f}%` | IBIT `{snaps['IBIT']:+.2f}%`\n”
f”VIX: `{vix:.1f}` ({vix_s})\n\n”
f”*PORTFOLIO*\n”
f”Alpaca: `{equity:,.0f}$` ({total_ret:+.2f}%)\n”
f”Crypto Coinbase: `{crypto_val:.2f}EUR`\n”
f”Win Rate: `{win_rate:.0f}%`\n\n”
f”Scan actif | Confidence >= {CONFIDENCE_THRESHOLD} | {MIN_CONFLUENCES} confluences min”
)
except Exception as e:
log.error(f”Morning brief: {e}”)

# ================================================================

# TELEGRAM COMMANDS - V11 + V10

# ================================================================

def process_commands():
global last_update_id, agent_paused
if not TELEGRAM_TOKEN:
return
try:
r = requests.get(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates”,
params={“offset”: last_update_id + 1, “timeout”: 10}, timeout=15
)
if r.status_code != 200:
return
for update in r.json().get(“result”, []):
last_update_id = update[“update_id”]
msg  = update.get(“message”, {})
text = msg.get(“text”, “”).strip().lower()
chat_id = msg.get(“chat”, {}).get(“id”)
if str(chat_id) != str(TELEGRAM_CHAT_ID):
continue

```
        if text in ["/start", "/aide"]:
            send_telegram(
                "*AGENT V12 - COMMANDES*\n\n"
                "*INFO*\n"
                "/status - Etat global\n"
                "/portfolio - Bilan Alpaca + Coinbase\n"
                "/crypto - Detail crypto Coinbase\n"
                "/marche - Snapshot marches\n"
                "/positions - Trades ouverts\n"
                "/trades - Historique OK/LOSS\n"
                "/report - Daily review maintenant\n\n"
                "*CONTROLE*\n"
                "/pause - Suspendre trading\n"
                "/resume - Reprendre trading\n"
                "/liquidate - Vendre toutes les cryptos"
            )

        elif text == "/status":
            acc      = trading_client.get_account()
            vix      = get_vix()
            trades   = load_trades()
            win_rate = get_win_rate() * 100
            send_telegram(
                f"*STATUS AGENT V12*\n\n"
                f"Mode: `{'PAPER' if PAPER_MODE else 'LIVE'}`\n"
                f"Etat: `{'PAUSE' if agent_paused else 'ACTIF'}`\n"
                f"Equity: `{acc.equity}$`\n"
                f"Cash: `{acc.cash}$`\n"
                f"VIX: `{vix:.1f}`\n"
                f"Trades logues: `{len(trades)}`\n"
                f"Win Rate: `{win_rate:.0f}%`\n"
                f"Positions ouvertes: `{len(open_positions_tracker)}`"
            )

        elif text == "/portfolio":
            acc = trading_client.get_account()
            crypto_details, crypto_val = get_crypto_summary()
            last_eq   = float(acc.last_equity)
            equity    = float(acc.equity)
            total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
            day_pct   = ((equity - last_eq) / last_eq * 100) if last_eq > 0 else 0
            send_telegram(
                f"*BILAN PORTEFEUILLE*\n\n"
                f"*BOURSE (Alpaca)*\n"
                f"Valeur: `{acc.equity}$`\n"
                f"Aujourd'hui: `{day_pct:+.2f}%`\n"
                f"Total: `{total_ret:+.2f}%`\n"
                f"Cash: `{acc.cash}$`\n\n"
                f"*CRYPTO (Coinbase)*\n"
                f"Valeur estimee: `{crypto_val:.2f}EUR`\n"
                f"{crypto_details}\n\n"
                f"Marche: `{'Ouvert' if is_market_open() else 'Ferme'}`"
            )

        elif text == "/crypto":
            details, total = get_crypto_summary()
            send_telegram(
                f"*CRYPTO COINBASE*\n\n"
                f"{details}\n\n"
                f"*TOTAL: {total:.2f}EUR*"
            )

        elif text == "/marche":
            snaps = get_market_snapshots()
            vix   = get_vix()
            send_telegram(
                f"*MARKET SNAPSHOT*\n\n"
                f"SPY: `{snaps['SPY']:+.2f}%`\n"
                f"QQQ: `{snaps['QQQ']:+.2f}%`\n"
                f"IBIT: `{snaps['IBIT']:+.2f}%`\n"
                f"VIX: `{vix:.1f}`\n\n"
                f"Bourse: `{'Ouverte' if is_market_open() else 'Fermee'}`"
            )

        elif text == "/positions":
            pos = trading_client.get_all_positions()
            cb_pos = {k: v for k, v in open_positions_tracker.items() if v.get("platform") == "coinbase"}
            if not pos and not cb_pos:
                send_telegram("Aucune position ouverte.")
            else:
                lines = []
                for p in pos:
                    pnl_pct = float(p.unrealized_plpc) * 100
                    lines.append(f"{'OK' if pnl_pct>=0 else 'LOSS'} *{p.symbol}* (Alpaca) `{pnl_pct:+.2f}%`")
                for key, v in cb_pos.items():
                    lines.append(f"*{key}* (Coinbase) {v['side'].upper()} entry:`{v['entry']:.4f}`")
                send_telegram("*POSITIONS ACTIVES*\n\n" + "\n".join(lines))

        elif text == "/trades":
            trades = load_trades()
            if not trades:
                send_telegram("Aucun trade enregistre.")
            else:
                recent   = trades[-10:]
                wins     = sum(1 for t in trades if t["pnl_usd"] > 0)
                losses   = sum(1 for t in trades if t["pnl_usd"] <= 0)
                total    = wins + losses
                win_rate = wins / total * 100 if total > 0 else 0
                lines    = [
                    f"{'OK' if t['pnl_usd']>0 else 'LOSS'} *{t['symbol']}* {t['side'].upper()} `{t['pnl_usd']:+.2f}$` [{t['signal']}]"
                    for t in reversed(recent)
                ]
                send_telegram(
                    f"*HISTORIQUE TRADES*\n"
                    f"Win Rate: `{win_rate:.0f}%` ({wins}W/{losses}L)\n\n"
                    + "\n".join(lines)
                )

        elif text == "/report":
            daily_review()

        elif text == "/pause":
            agent_paused = True
            send_telegram("Agent en PAUSE. /resume pour reprendre.")

        elif text == "/resume":
            agent_paused = False
            send_telegram("Agent REPRIS.")

        elif text == "/liquidate":
            liquidate_crypto_for_cash()

except Exception as e:
    log.error(f"Commands: {e}")
```

def telegram_loop():
“”“Thread dedie aux commandes Telegram - V11.”””
log.info(“Telegram loop active”)
while True:
process_commands()
time.sleep(5)

# ================================================================

# HEALTH SERVER - Render

# ================================================================

class _Health(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b”Agent V12 OK”)
def log_message(self, *args): pass

# ================================================================

# MAIN

# ================================================================

if **name** == “**main**”:
log.info(”=” * 55)
log.info(“AGENT TRADING IA V12 - FUSION V11 + V10”)
log.info(f”MODE       : {‘PAPER’ if PAPER_MODE else ‘LIVE’}”)
log.info(f”STOCKS     : {STOCK_WATCHLIST}”)
log.info(f”CRYPTO     : {CRYPTO_WATCHLIST}”)
log.info(f”COINBASE   : {‘OK’ if COINBASE_API_KEY else ‘NON CONFIGURE’}”)
log.info(f”CONFIDENCE : >= {CONFIDENCE_THRESHOLD} (FIXE)”)
log.info(f”CONFLUENCES: >= {MIN_CONFLUENCES}”)
log.info(”=” * 55)

```
# Health server
threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(),
    daemon=True
).start()

# Telegram loop thread dedie (V11)
threading.Thread(target=telegram_loop, daemon=True).start()

# Scheduler
schedule.every(15).minutes.do(scan_and_trade)
schedule.every(30).minutes.do(scan_crypto)
schedule.every(1).minutes.do(check_crypto_sl_tp)
schedule.every().day.at("09:00").do(morning_brief)
schedule.every().day.at("10:00").do(check_rebalancing)
schedule.every().day.at("16:30").do(daily_review)

send_telegram(
    f"*AGENT V12 EN LIGNE*\n"
    f"Alpaca stocks OK | Coinbase crypto {'OK' if COINBASE_API_KEY else 'non configure'}\n"
    f"Mode: `{'PAPER' if PAPER_MODE else 'LIVE'}`\n"
    f"Confidence >= {CONFIDENCE_THRESHOLD} | {MIN_CONFLUENCES} confluences min\n"
    f"/aide pour les commandes"
)

while True:
    schedule.run_pending()
    time.sleep(1)
```
