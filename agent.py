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
# CONFIGURATION & SÉCURITÉ
# ================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Vérification des clés pour éviter le crash "Application exited early"
REQUIRED_VARS = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
missing = [var for var in REQUIRED_VARS if not os.getenv(var)]
if missing:
    log.error(f"❌ CLÉS MANQUANTES SUR RENDER : {', '.join(missing)}")
    # On ne stop pas le script pour que Render puisse nous afficher l'erreur dans les logs

ALPACA_API_KEY      = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")
PAPER_MODE          = os.getenv("PAPER_MODE", "True") == "True"
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY    = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_SECRET_KEY", "")

HOLD_PCT     = 0.65
DAYTRADE_PCT = 0.35
STOCK_SL_PCT = 2.0
STOCK_TP_PCT = 4.0
CRYPTO_SL_PCT = 3.0
CRYPTO_TP_PCT = 6.0
MAX_RISK_PER_TRADE_PCT = 0.02
CONFIDENCE_THRESHOLD = 70

# Watchlist Diversifiée (V11)
STOCK_WATCHLIST  = ["NVDA", "AAPL", "JPM", "UNH", "WMT", "CAT", "XOM"]
CRYPTO_WATCHLIST = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "AVAX-EUR", "LINK-EUR", "ADA-EUR"]
CORE_TARGETS     = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}

EST = ZoneInfo("America/New_York")
agent_paused = False
open_positions_tracker = {}

# Clients
try:
    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
    data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
except Exception as e:
    log.error(f"Erreur Init Clients: {e}")

_cb_client = None
def get_coinbase_client():
    global _cb_client
    if _cb_client is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
        _cb_client = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
    return _cb_client

# ================================================================
# FONCTIONS DE TRADING (SMC & RISK)
# ================================================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except: pass

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
            if not PAPER_MODE: return # Short interdit en live
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
    log.info("Scan des marchés en cours...")
    for ticker in STOCK_WATCHLIST:
        try:
            # Logique simplifiée pour le scan (SMC)
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute5, start=datetime.now(EST) - timedelta(days=1))
            bars = list(data_client.get_stock_bars(req)[ticker])
            price = bars[-1].close
            # Appel Claude
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
# SERVEUR & BOUCLE PRINCIPALE
# ================================================================
class _Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Agent V11 OK")
    def log_message(self, *args): pass

if __name__ == "__main__":
    log.info("AGENT V11 DEMARRAGE...")
    # Serveur pour Render (évite le crash)
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(), daemon=True).start()
    
    schedule.every(15).minutes.do(scan_and_trade)
    schedule.every().day.at("10:00").do(check_rebalancing)

    send_telegram("🤖 *Agent V11 en ligne sur Render*")
    while True:
        schedule.run_pending()
        time.sleep(1)# -*- coding: utf-8 -*-
import os
import json
import time
import threading
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
import schedule
import anthropic

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    MarketOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockSnapshotRequest,
    StockLatestBarRequest,
)
from alpaca.data.timeframe import TimeFrame

from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    from coinbase.rest import RESTClient as CoinbaseClient

    COINBASE_AVAILABLE = True
except ImportError:
    COINBASE_AVAILABLE = False

load_dotenv()

# ================================================================
# CONFIGURATION ET REGLES ABSOLUES
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PAPER_MODE = os.getenv("PAPER_MODE", "True") == "True"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_SECRET_KEY", "")

HOLD_PCT = 0.65
DAYTRADE_PCT = 0.35

STOCK_SL_PCT = 2.0
STOCK_TP_PCT = 4.0
CRYPTO_SL_PCT = 3.0
CRYPTO_TP_PCT = 6.0
MAX_RISK_PER_TRADE_PCT = 0.02
CONFIDENCE_THRESHOLD = 70
MIN_CONFLUENCES = 1

# DIVERSIFICATION SECTORIELLE & CRYPTO
STOCK_WATCHLIST = ["NVDA", "AAPL", "JPM", "UNH", "WMT", "CAT", "XOM"]
CRYPTO_WATCHLIST = [
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "AVAX-EUR",
    "LINK-EUR",
    "ADA-EUR",
]
CORE_TARGETS = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}

EST = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)
BLACKOUT_START = (11, 0)
BLACKOUT_END = (14, 0)

agent_paused = False
open_positions_tracker = {}
last_update_id = 0
TRADES_FILE = "trades.json"
START_CAPITAL = 100_000.0

# ================================================================
# CLIENTS
# ================================================================
trading_client = TradingClient(
    ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE
)
data_client = StockHistoricalDataClient(
    ALPACA_API_KEY, ALPACA_SECRET_KEY
)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_cb_client = None


def get_coinbase_client():
    global _cb_client
    if _cb_client is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
        _cb_client = CoinbaseClient(
            api_key=COINBASE_API_KEY,
            api_secret=COINBASE_API_SECRET,
        )
    return _cb_client


# ================================================================
# UTILITAIRES
# ================================================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
    except Exception as e:
        log.error(f"Telegram: {e}")


def load_trades():
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                return json.load(f)
        except:
            return []
    return []


def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def get_account_info():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash)}


def get_win_rate():
    trades = load_trades()
    if len(trades) < 5:
        return 0.5
    recent = trades[-20:]
    return sum(1 for t in recent if t.get("pnl_usd", 0) > 0) / len(recent)


# ================================================================
# INDICATEURS (SMC, VIX, NEWS)
# ================================================================
def calculate_atr(bars, period=14):
    if len(bars) < period + 1:
        return 0
    return sum(
        max(
            b.high - b.low,
            abs(b.high - p.close),
            abs(b.low - p.close),
        )
        for b, p in zip(bars[1:], bars[:-1])
    )[-period:] / period


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
            StockLatestBarRequest(symbol_or_symbols=["VIXY"])
        )["VIXY"].close
    except:
        return 20.0


def get_smc_intraday(ticker):
    try:
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute5,
            start=datetime.now(EST) - timedelta(days=3),
        )
        bars = list(data_client.get_stock_bars(req)[ticker])
        if len(bars) < 30:
            return None

        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]

        cur = closes[-1]
        sl = min(lows[-20:])
        sh = max(highs[-20:])

        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)

        return {
            "current": cur,
            "atr": calculate_atr(bars),
            "swing_high": sh,
            "swing_low": sl,
            "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "baissier",
            "ema9": ema9,
            "ema21": ema21,
            "ema_bullish": ema9 > ema21,
        }
    except:
        return None


def get_news_sentiment(ticker):
    return {"sentiment": "NEUTRAL", "pause": False, "reason": ""}


def count_confluences(smc, news_sentiment, direction):
    c = []
    if direction == "BUY":
        if not smc.get("ema_bullish"):
            return 0, ["Veto: EMA baissiere"]
        c.append("EMA OK")
        if smc.get("sweep_bullish"):
            c.append("Sweep bull")
    else:
        if smc.get("ema_bullish"):
            return 0, ["Veto: EMA haussiere"]
        c.append("EMA OK")
        if smc.get("sweep_bearish"):
            c.append("Sweep bear")
    return len(c), c


# ================================================================
# CLAUDE IA
# ================================================================
PROMPT_SYSTEM = (
    "Tu es un trader institutionnel. Jamais d'emotion. RR 1:2 minimum.\n"
    "Reponds UNIQUEMENT en JSON strict :\n"
    '{"action":"BUY"|"SHORT"|"HOLD","confidence":0-100,"signal_type":"SMC","reason":"max 10 mots"}'
)


def get_claude_signal(ticker, smc):
    context = (
        f"Ticker: {ticker} | Prix: {smc['current']}$ | "
        f"Trend: {smc['trend']} | "
        f"Sweep Bull: {smc['sweep_bullish']} | "
        f"EMA Bull: {smc['ema_bullish']}"
    )
    try:
        res = claude_client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=100,
            system=PROMPT_SYSTEM,
            messages=[{"role": "user", "content": context}],
        )
        return json.loads(
            res.content[0]
            .text.strip()
            .replace("```json", "")
            .replace("```", "")
        )
    except:
        return None


# ================================================================
# EXECUTION ALPACA (ACTIONS) + AUTO-REBALANCING
# ================================================================
def place_bracket_order(symbol, side, limit_price, sl_pct, tp_pct):
    try:
        account = get_account_info()

        sl_dist = limit_price * (sl_pct / 100.0)
        qty_atr = (
            account["equity"] * MAX_RISK_PER_TRADE_PCT
        ) / sl_dist
        qty = round(qty_atr, 4)

        if qty <= 0:
            return

        if side == "buy":
            sl_p = limit_price * (1 - sl_pct / 100)
            tp_p = limit_price * (1 + tp_pct / 100)

            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(
                    limit_price=round(tp_p, 2)
                ),
                stop_loss=StopLossRequest(
                    stop_price=round(sl_p, 2)
                ),
            )
        else:
            if not PAPER_MODE:
                return

            sl_p = limit_price * (1 + sl_pct / 100)
            tp_p = limit_price * (1 - tp_pct / 100)

            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(
                    limit_price=round(tp_p, 2)
                ),
                stop_loss=StopLossRequest(
                    stop_price=round(sl_p, 2)
                ),
            )

        trading_client.submit_order(req)

        send_telegram(
            f"*{symbol}* {side.upper()} @ {limit_price:.2f}$ | "
            f"SL: {sl_p:.2f} | TP: {tp_p:.2f}"
        )

    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
