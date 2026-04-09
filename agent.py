```python
# -*- coding: utf-8 -*-
# Agent Trading V12 - Portfolio calibre + Coinbase actif + Toutes commandes

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

START_CAPITAL          = 100_000.0
HOLD_PCT               = 0.65
DAYTRADE_PCT           = 0.35

# Stocks calibres
STOCK_SL_PCT           = 2.0
STOCK_TP_PCT           = 4.0

# Crypto calibres
CRYPTO_SL_PCT          = 3.0
CRYPTO_TP1_PCT         = 15.0   # Partiel
CRYPTO_TP2_PCT         = 35.0   # Objectif final

# Risk
MAX_RISK_PER_TRADE_PCT = 0.02
MAX_CRYPTO_SIZE_PCT    = 0.02   # 2% portfolio max par trade crypto
CONFIDENCE_THRESHOLD   = 80
MIN_CONFLUENCES        = 3

# Portfolio calibre
STOCK_WATCHLIST  = ["NVDA", "AAPL", "JPM", "UNH", "WMT", "CAT", "XOM"]
CRYPTO_WATCHLIST = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "AVAX-EUR"]

# Core-Satellite targets
CORE_TARGETS = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}

# QQQ et IBIT calibres
QQQ_SL_PCT   = 10.0
QQQ_TP_PCT   = 30.0
IBIT_SL_PCT  = 20.0
IBIT_TP1_PCT = 25.0
IBIT_TP2_PCT = 50.0

EST            = ZoneInfo("America/New_York")
MARKET_OPEN    = (9, 30)
MARKET_CLOSE   = (16, 0)
BLACKOUT_START = (11, 0)
BLACKOUT_END   = (14, 0)

HIGH_RISK_KW = ["earnings report", "SEC investigation", "fraud", "bankruptcy", "delisted"]
SEARCH_TERMS = {
    "NVDA": "NVIDIA OR NVDA", "AAPL": "Apple OR AAPL",
    "JPM": "JPMorgan OR JPM", "UNH": "UnitedHealth OR UNH",
    "WMT": "Walmart OR WMT", "CAT": "Caterpillar OR CAT",
    "XOM": "ExxonMobil OR XOM", "BTC": "Bitcoin OR BTC",
    "ETH": "Ethereum OR ETH", "SOL": "Solana OR SOL",
    "XRP": "Ripple OR XRP", "AVAX": "Avalanche OR AVAX"
}

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
    log.info("Clients API OK")
except Exception as e:
    log.error(f"Init clients: {e}")

_cb = None
def get_cb():
    global _cb
    if _cb is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
        try:
            _cb = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
        except Exception as e:
            log.error(f"Coinbase init: {e}")
    return _cb

# ================================================================
# TELEGRAM
# ================================================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        log.info(f"[TG] {msg[:80]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN.strip()}/sendMessage",
            json={"chat_id": str(TELEGRAM_CHAT_ID).strip(), "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram: {e}")

# ================================================================
# TRADE LOGGER
# ================================================================
def load_trades():
    try:
        return json.load(open(TRADES_FILE)) if os.path.exists(TRADES_FILE) else []
    except Exception:
        return []

def save_trades(trades):
    json.dump(trades, open(TRADES_FILE, "w"), indent=2, default=str)

def log_trade_open(key, side, entry, sl, tp, signal, conviction, n_conf, platform="alpaca"):
    open_positions_tracker[key] = {
        "entry": entry, "sl": sl, "tp": tp, "signal": signal,
        "conviction": conviction, "confluences": n_conf, "side": side,
        "platform": platform, "time": datetime.now(EST).isoformat()
    }

def log_trade_close(key, exit_price):
    if key not in open_positions_tracker:
        return
    pos     = open_positions_tracker.pop(key)
    pnl_pct = ((exit_price - pos["entry"]) / pos["entry"] * 100) if pos["side"] == "buy" else ((pos["entry"] - exit_price) / pos["entry"] * 100)
    trade   = {
        "symbol": key, "side": pos["side"], "entry": pos["entry"], "exit": exit_price,
        "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_pct / 100 * pos["entry"] * 10, 2),
        "signal": pos["signal"], "conviction": pos["conviction"],
        "entry_hour": datetime.fromisoformat(pos["time"]).hour,
        "platform": pos.get("platform", "alpaca"),
        "date": datetime.now(EST).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(EST).isoformat()
    }
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)
    _log_notion(trade)
    return trade

def _log_notion(trade):
    if not NOTION_TOKEN:
        return
    label = "OK" if trade["pnl_usd"] >= 0 else "LOSS"
    content = f"[{label}] {trade['symbol']} {trade['side'].upper()} | PnL {trade['pnl_usd']:+.2f}$ | {trade['signal']} | {trade['platform']} | {trade['date']}"
    try:
        requests.patch(
            f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
            headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
            json={"children": [{"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]}}]},
            timeout=5
        )
    except Exception as e:
        log.error(f"Notion: {e}")

# ================================================================
# INDICATEURS TECHNIQUES
# ================================================================
def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return 0
    trs = [max(bars[i].high - bars[i].low, abs(bars[i].high - bars[i-1].close), abs(bars[i].low - bars[i-1].close)) for i in range(1, len(bars))]
    return sum(trs[-period:]) / period

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100

def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k, ema = 2 / (period + 1), closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_vix():
    try:
        return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=["VIXY"]))["VIXY"].close
    except Exception:
        return 20.0

def get_smc(ticker):
    try:
        bars = list(data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Minute5,
            start=datetime.now(EST) - timedelta(days=3)
        ))[ticker])
        if len(bars) < 30:
            return None
        closes  = [b.close  for b in bars]
        highs   = [b.high   for b in bars]
        lows    = [b.low    for b in bars]
        volumes = [b.volume for b in bars]
        cur = closes[-1]
        sh  = max(highs[-20:])
        sl  = min(lows[-20:])
        avg_vol = sum(volumes[-20:]) / 20
        ema9    = calc_ema(closes, 9)
        ema21   = calc_ema(closes, 21)
        rsi_now = calc_rsi(closes[-15:])
        rsi_prev= calc_rsi(closes[-20:-5])
        return {
            "current": cur, "atr": calc_atr(bars),
            "swing_high": sh, "swing_low": sl,
            "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "baissier",
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema9 > ema21,
            "rsi": rsi_now,
            "rsi_div_bull": cur < closes[-20] and rsi_now > rsi_prev,
            "rsi_div_bear": cur > closes[-20] and rsi_now < rsi_prev,
            "volume_ok": volumes[-1] >= avg_vol * 0.8,
        }
    except Exception as e:
        log.error(f"SMC {ticker}: {e}")
        return None

def get_crypto_smc(product_id):
    try:
        cb  = get_cb()
        if not cb:
            return None
        end   = int(datetime.now(timezone.utc).timestamp())
        start = end - (50 * 5 * 60)
        candles = sorted(cb.get_candles(product_id=product_id, start=str(start), end=str(end), granularity="FIVE_MINUTE").get("candles", []), key=lambda c: c["start"])
        if len(candles) < 25:
            return None
        closes  = [float(c["close"])  for c in candles]
        highs   = [float(c["high"])   for c in candles]
        lows    = [float(c["low"])    for c in candles]
        volumes = [float(c["volume"]) for c in candles]
        cur = closes[-1]
        sh  = max(highs[-20:])
        sl  = min(lows[-20:])
        avg_vol = sum(volumes[-20:]) / 20
        ema9    = calc_ema(closes, 9)
        ema21   = calc_ema(closes, 21)
        rsi_now = calc_rsi(closes[-15:])
        rsi_prev= calc_rsi(closes[-20:-5])
        return {
            "current": cur, "atr": sum(abs(highs[i] - lows[i]) for i in range(-14, 0)) / 14,
            "swing_high": sh, "swing_low": sl,
            "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "baissier",
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema9 > ema21,
            "rsi": rsi_now,
            "rsi_div_bull": cur < closes[-20] and rsi_now > rsi_prev,
            "rsi_div_bear": cur > closes[-20] and rsi_now < rsi_prev,
            "volume_ok": volumes[-1] >= avg_vol * 0.8,
        }
    except Exception as e:
        log.error(f"Crypto SMC {product_id}: {e}")
        return None

# ================================================================
# NEWS SENTIMENT
# ================================================================
def get_news(ticker):
    if not NEWS_API_KEY:
        return {"sentiment": "NEUTRAL", "pause": False}
    try:
        r = requests.get("https://newsapi.org/v2/everything", params={
            "q": f"({SEARCH_TERMS.get(ticker, ticker)}) AND (stock OR market)",
            "from": (datetime.now() - timedelta(hours=6)).isoformat(),
            "sortBy": "relevancy", "language": "en", "apiKey": NEWS_API_KEY, "pageSize": 5
        }, timeout=8)
        articles = r.json().get("articles", []) if r.ok else []
        for a in articles:
            if any(kw in a.get("title", "").lower() for kw in HIGH_RISK_KW):
                return {"sentiment": "BEARISH", "pause": True}
        pos = sum(1 for a in articles for w in ["surge","rally","gain","bullish","beat"] if w in a.get("title","").lower())
        neg = sum(1 for a in articles for w in ["drop","fall","crash","bearish","miss"] if w in a.get("title","").lower())
        return {"sentiment": "BULLISH" if pos > neg + 1 else "BEARISH" if neg > pos + 1 else "NEUTRAL", "pause": False}
    except Exception:
        return {"sentiment": "NEUTRAL", "pause": False}

# ================================================================
# MULTI-CONFLUENCE
# ================================================================
def count_confluences(smc, news_sentiment, direction):
    c = []
    if direction == "BUY":
        if smc.get("ema_bullish"):         c.append("EMA9>EMA21")
        if smc.get("sweep_bullish"):        c.append("Sweep bull")
        if smc.get("rsi_div_bull"):         c.append("RSI div bull")
        if smc.get("volume_ok"):            c.append("Volume OK")
        if news_sentiment == "BULLISH":     c.append("News bull")
        if smc.get("trend") == "haussier":  c.append("Trend UP")
    else:
        if not smc.get("ema_bullish"):      c.append("EMA9<EMA21")
        if smc.get("sweep_bearish"):        c.append("Sweep bear")
        if smc.get("rsi_div_bear"):         c.append("RSI div bear")
        if smc.get("volume_ok"):            c.append("Volume OK")
        if news_sentiment == "BEARISH":     c.append("News bear")
        if smc.get("trend") == "baissier":  c.append("Trend DOWN")
    return len(c), c

# ================================================================
# RISK + ACCOUNT
# ================================================================
def get_win_rate():
    trades = load_trades()
    if len(trades) < 5:
        return 0.5
    recent = trades[-20:]
    return sum(1 for t in recent if t.get("pnl_usd", 0) > 0) / len(recent)

def get_account():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash), "last_equity": float(a.last_equity)}

# ================================================================
# CLAUDE SIGNAL
# ================================================================
PROMPT = (
    "Trader institutionnel. Jamais d emotion. RR 1:2 minimum.\n"
    "Reponds UNIQUEMENT en JSON: "
    '{"action":"BUY"|"SHORT"|"HOLD","confidence":0-100,'
    '"signal_type":"SMC"|"SMC+RSI"|"SMC+EMA"|"SMC+RSI+EMA","reason":"max 10 mots"}\n'
    "Si confidence < 80 -> HOLD obligatoire."
)

def get_signal(ticker, smc, news):
    ctx = (
        f"Ticker: {ticker} | Prix: {smc['current']}\n"
        f"Trend: {smc['trend']} | EMA: {'BULL' if smc['ema_bullish'] else 'BEAR'}\n"
        f"RSI: {smc['rsi']:.1f} | DivBull: {smc['rsi_div_bull']} | DivBear: {smc['rsi_div_bear']}\n"
        f"Sweep Bull: {smc['sweep_bullish']} | Sweep Bear: {smc['sweep_bearish']}\n"
        f"Volume: {'OK' if smc['volume_ok'] else 'FAIBLE'} | News: {news['sentiment']}"
    )
    try:
        res = claude_client.messages.create(
            model="claude-3-5-haiku-20241022", max_tokens=150,
            system=PROMPT, messages=[{"role": "user", "content": ctx}]
        )
        return json.loads(res.content[0].text.strip().replace("```json","").replace("```",""))
    except Exception as e:
        log.error(f"Claude {ticker}: {e}")
        return None

# ================================================================
# HELPERS MARCHE
# ================================================================
def is_open():
    now = datetime.now(EST)
    return now.weekday() < 5 and MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE

def is_blackout():
    t = (datetime.now(EST).hour, datetime.now(EST).minute)
    return BLACKOUT_START <= t < BLACKOUT_END

def get_snapshots():
    try:
        snaps = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=["SPY","QQQ","IBIT"]))
        result = {}
        for s in ["SPY","QQQ","IBIT"]:
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
        return {"SPY": 0.0, "QQQ": 0.0, "IBIT": 0.0}

# ================================================================
# COINBASE SUMMARY
# ================================================================
def get_crypto_summary():
    cb = get_cb()
    if not cb:
        return "Non configure", 0
    try:
        accounts  = cb.get_accounts().get("accounts", [])
        total_eur = 0
        details   = []
        for acc in accounts:
            curr = acc.get("currency")
            bal  = float(acc.get("available_balance", {}).get("value", 0))
            if bal <= 0:
                continue
            if curr == "EUR":
                total_eur += bal
                details.append(f"EUR: {bal:.2f}EUR (cash)")
            else:
                prod = f"{curr}-EUR"
                try:
                    price = float(cb.get_best_bid_ask(product_ids=[prod])["pricebooks"][0]["bids"][0]["price"])
                    val   = bal * price
                    total_eur += val
                    details.append(f"*{curr}*: {bal:.4f} (~{val:.2f}EUR)")
                except Exception:
                    details.append(f"*{curr}*: {bal:.4f} (prix N/A)")
        return "\n".join(details) if details else "Aucun actif", total_eur
    except Exception as e:
        return f"Erreur: {e}", 0

def get_crypto_price(product_id):
    try:
        cb = get_cb()
        if not cb:
            return 0.0
        bids = cb.get_best_bid_ask(product_ids=[product_id])["pricebooks"][0]["bids"]
        return float(bids[0]["price"]) if bids else 0.0
    except Exception:
        return 0.0

def liquidate_crypto():
    cb = get_cb()
    if not cb:
        send_telegram("Coinbase non configure")
        return
    try:
        sold = []
        for acc in cb.get_accounts().get("accounts", []):
            curr = acc.get("currency")
            bal  = float(acc.get("available_balance", {}).get("value", 0))
            if bal > 0 and curr not in ["EUR","USD"]:
                prod = f"{curr}-EUR"
                if prod in CRYPTO_WATCHLIST:
                    cb.market_order_sell(client_order_id=str(uuid.uuid4()), product_id=prod, base_size=str(bal))
                    sold.append(f"{curr} ({bal:.4f})")
        send_telegram(f"*LIQUIDATION*\nVendu: {', '.join(sold)}" if sold else "Rien a vendre")
    except Exception as e:
        send_telegram(f"Erreur liquidation: {e}")

# ================================================================
# ORDRES STOCKS - Alpaca
# ================================================================
def place_bracket(symbol, side, limit_price, sl_pct, tp_pct, signal_type, conviction, conf_list):
    try:
        acc    = get_account()
        equity = acc["equity"]
        sl_dist= limit_price * sl_pct / 100
        if sl_dist <= 0:
            return
        win_rate = get_win_rate()
        rr       = tp_pct / sl_pct
        kelly    = max(0.01, min(win_rate - (1 - win_rate) / rr, 0.25))
        qty      = round(min(
            (equity * MAX_RISK_PER_TRADE_PCT) / sl_dist,
            (equity * DAYTRADE_PCT / len(STOCK_WATCHLIST)) / limit_price,
            (equity * kelly * 0.25) / limit_price
        ), 4)
        if qty <= 0:
            return
        if side == "buy":
            sl_p, tp_p, oside = round(limit_price * (1 - sl_pct/100), 2), round(limit_price * (1 + tp_pct/100), 2), OrderSide.BUY
        else:
            if not PAPER_MODE:
                send_telegram(f"SHORT bloque {symbol} - LIVE")
                return
            sl_p, tp_p, oside = round(limit_price * (1 + sl_pct/100), 2), round(limit_price * (1 - tp_pct/100), 2), OrderSide.SELL
        trading_client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=oside, time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2), order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp_p),
            stop_loss=StopLossRequest(stop_price=sl_p)
        ))
        send_telegram(
            f"*TRADE ALPACA*\n{'UP' if side=='buy' else 'DOWN'} *{symbol}* {side.upper()}\n"
            f"Entry `{limit_price:.2f}$` Qty `{qty}`\n"
            f"SL `{sl_p:.2f}$` TP `{tp_p:.2f}$` RR {rr:.1f}:1\n"
            f"Signal: {signal_type} | Conv: {conviction}/100 | {len(conf_list)} confluences"
        )
        log_trade_open(symbol, side, limit_price, sl_p, tp_p, signal_type, conviction, len(conf_list), "alpaca")
    except Exception as e:
        log.error(f"Bracket {symbol}: {e}")
        send_telegram(f"Erreur ordre {symbol}: {e}")

# ================================================================
# ORDRES CRYPTO - Coinbase
# ================================================================
def place_crypto_order(product_id, side, signal_type, conviction, conf_list):
    try:
        cb = get_cb()
        if not cb:
            return
        price = get_crypto_price(product_id)
        if price <= 0:
            return
        acc      = get_account()
        eur_size = round(min(acc["equity"] * MAX_CRYPTO_SIZE_PCT, acc["equity"] * MAX_RISK_PER_TRADE_PCT * 10), 2)
        if side == "buy":
            cb.market_order_buy(client_order_id=str(uuid.uuid4()), product_id=product_id, quote_size=str(eur_size))
        else:
            if not PAPER_MODE:
                send_telegram(f"SHORT crypto bloque {product_id} - LIVE")
                return
            cb.market_order_sell(client_order_id=str(uuid.uuid4()), product_id=product_id, base_size=str(round(eur_size / price, 6)))
        sl  = price * (1 - CRYPTO_SL_PCT/100)
        tp1 = price * (1 + CRYPTO_TP1_PCT/100)
        tp2 = price * (1 + CRYPTO_TP2_PCT/100)
        key = f"CB_{product_id}"
        log_trade_open(key, side, price, sl, tp2, signal_type, conviction, len(conf_list), "coinbase")
        send_telegram(
            f"*CRYPTO COINBASE*\nUP *{product_id}* BUY\n"
            f"Prix `{price:.4f}EUR` Size `{eur_size}EUR`\n"
            f"SL `{sl:.4f}` TP1 `{tp1:.4f}` TP2 `{tp2:.4f}`\n"
            f"Signal: {signal_type} | Conv: {conviction}/100"
        )
        log.info(f"Crypto order: {product_id} {side} @ {price}")
    except Exception as e:
        log.error(f"Crypto order {product_id}: {e}")
        send_telegram(f"Erreur crypto {product_id}: {e}")

def check_crypto_sl_tp():
    for key, pos in list({k: v for k, v in open_positions_tracker.items() if v.get("platform") == "coinbase"}.items()):
        try:
            product_id = key.replace("CB_","")
            price = get_crypto_price(product_id)
            if price <= 0:
                continue
            if pos["side"] == "buy":
                if price <= pos["sl"]:
                    send_telegram(f"SL CRYPTO {product_id} @ {price:.4f}EUR")
                    log_trade_close(key, price)
                elif price >= pos["tp"]:
                    send_telegram(f"TP CRYPTO {product_id} @ {price:.4f}EUR")
                    log_trade_close(key, price)
        except Exception as e:
            log.error(f"SL/TP {key}: {e}")

# ================================================================
# REBALANCING
# ================================================================
def check_rebalancing():
    try:
        acc      = get_account()
        hold_cap = acc["equity"] * HOLD_PCT
        positions= {p.symbol: float(p.market_value) for p in trading_client.get_all_positions()}
        for symbol, target in CORE_TARGETS.items():
            target_usd = hold_cap * target
            actual_usd = positions.get(symbol, 0)
            if (target_usd - actual_usd) / hold_cap > 0.05 and acc["cash"] > (target_usd - actual_usd):
                buy_amt = target_usd - actual_usd
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, notional=round(buy_amt, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                ))
                send_telegram(f"*REBALANCING*\nAchat `{symbol}` -> `{buy_amt:.2f}$`")
    except Exception as e:
        log.error(f"Rebalancing: {e}")

# ================================================================
# SCAN STOCKS
# ================================================================
def scan_and_trade():
    global agent_paused
    if agent_paused or not is_open() or is_blackout():
        return
    vix = get_vix()
    if vix > 35:
        send_telegram(f"VIX {vix:.1f} > 35 - scan suspendu")
        return
    log.info(f"Scan stocks VIX:{vix:.1f}")
    for ticker in STOCK_WATCHLIST:
        if ticker in open_positions_tracker:
            continue
        try:
            smc = get_smc(ticker)
            if not smc:
                continue
            news = get_news(ticker)
            if news["pause"]:
                continue
            sig = get_signal(ticker, smc, news)
            if not sig or sig.get("action") == "HOLD" or sig.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue
            action = sig["action"]
            n_conf, conf_list = count_confluences(smc, news["sentiment"], action)
            if n_conf < MIN_CONFLUENCES:
                continue
            place_bracket(ticker, "buy" if action == "BUY" else "sell", smc["current"],
                         STOCK_SL_PCT, STOCK_TP_PCT, sig.get("signal_type","SMC"), sig["confidence"], conf_list)
            time.sleep(2)
        except Exception as e:
            log.error(f"Scan {ticker}: {e}")

# ================================================================
# SCAN CRYPTO
# ================================================================
def scan_crypto():
    global agent_paused
    if agent_paused or not COINBASE_AVAILABLE or not COINBASE_API_KEY:
        return
    log.info("Scan crypto Coinbase")
    for product_id in CRYPTO_WATCHLIST:
        key = f"CB_{product_id}"
        if key in open_positions_tracker:
            continue
        try:
            smc = get_crypto_smc(product_id)
            if not smc:
                continue
            ticker = product_id.replace("-EUR","").replace("-USD","")
            news   = get_news(ticker)
            if news["pause"]:
                continue
            sig = get_signal(ticker, smc, news)
            if not sig or sig.get("action") in ["HOLD","SHORT"] or sig.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue
            n_conf, conf_list = count_confluences(smc, news["sentiment"], "BUY")
            if n_conf < MIN_CONFLUENCES:
                log.info(f"{product_id} skip: {n_conf}/{MIN_CONFLUENCES} confluences")
                continue
            place_crypto_order(product_id, "buy", sig.get("signal_type","SMC"), sig["confidence"], conf_list)
            time.sleep(2)
        except Exception as e:
            log.error(f"Crypto scan {product_id}: {e}")

# ================================================================
# DAILY REVIEW
# ================================================================
def daily_review():
    trades  = load_trades()
    today   = datetime.now(EST).strftime("%Y-%m-%d")
    today_t = [t for t in trades if t.get("date") == today]
    try:
        acc       = get_account()
        equity    = acc["equity"]
        day_pnl   = equity - acc["last_equity"]
        day_pct   = (day_pnl / acc["last_equity"] * 100) if acc["last_equity"] > 0 else 0
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
    except Exception:
        return
    if not today_t:
        send_telegram(f"*DAILY REVIEW {today}*\nAucun trade\nPortfolio: {day_pct:+.2f}% | Total: {total_ret:+.2f}%")
        return
    wins     = [t for t in today_t if t.get("pnl_usd",0) > 0]
    losses   = [t for t in today_t if t.get("pnl_usd",0) <= 0]
    pnl_tot  = sum(t.get("pnl_usd",0) for t in today_t)
    wr       = len(wins) / len(today_t) * 100
    all_50   = trades[-50:]
    by_sig   = {}
    by_hour  = {}
    for t in all_50:
        s = t.get("signal","?")
        h = t.get("entry_hour", 0)
        by_sig.setdefault(s, {"w":0,"l":0})
        by_hour.setdefault(h, {"w":0,"l":0})
        k = "w" if t.get("pnl_usd",0) > 0 else "l"
        by_sig[s][k] += 1
        by_hour[h][k] += 1
    best_s = max(by_sig, key=lambda k: by_sig[k]["w"]/(by_sig[k]["w"]+by_sig[k]["l"]+0.001)) if by_sig else "N/A"
    best_h = max(by_hour, key=lambda k: by_hour[k]["w"]/(by_hour[k]["w"]+by_hour[k]["l"]+0.001)) if by_hour else "N/A"
    lines  = [f"{'OK' if t.get('pnl_usd',0)>0 else 'LOSS'} *{t['symbol']}* `{t.get('pnl_usd',0):+.2f}$` [{t.get('signal','?')}]" for t in today_t]
    rule   = _gen_rule(by_sig, by_hour)
    send_telegram(
        f"*DAILY REVIEW {today}*\n"
        f"PnL: `{pnl_tot:+.2f}$` | Portfolio: `{day_pct:+.2f}%`\n"
        f"Win Rate: `{wr:.0f}%` ({len(wins)}W/{len(losses)}L)\n\n"
        + "\n".join(lines) +
        f"\n\nMeilleur signal: {best_s} | Meilleure heure: {best_h}h\n"
        f"Regle demain: {rule}\n"
        f"Total: `{total_ret:+.2f}%` | Valeur: `{equity:,.0f}$`"
    )

def _gen_rule(by_sig, by_hour):
    for s, v in by_sig.items():
        t = v["w"] + v["l"]
        if t >= 5 and v["w"]/t < 0.35:
            return f"Signal {s} win rate {v['w']/t*100:.0f}% - surveiller"
    for h, v in by_hour.items():
        t = v["w"] + v["l"]
        if t >= 5 and v["w"]/t < 0.30:
            return f"Eviter {h}h - win rate {v['w']/t*100:.0f}%"
    return "Continuer strategie actuelle"

# ================================================================
# MORNING BRIEF
# ================================================================
def morning_brief():
    try:
        vix       = get_vix()
        snaps     = get_snapshots()
        acc       = get_account()
        equity    = acc["equity"]
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
        wr        = get_win_rate() * 100
        _, cv     = get_crypto_summary()
        vix_s     = "PANIQUE" if vix > 35 else "VOLATIL" if vix > 25 else "NORMAL"
        send_telegram(
            f"*MORNING BRIEF*\n"
            f"SPY `{snaps['SPY']:+.2f}%` | QQQ `{snaps['QQQ']:+.2f}%` | IBIT `{snaps['IBIT']:+.2f}%`\n"
            f"VIX: `{vix:.1f}` ({vix_s})\n\n"
            f"Alpaca: `{equity:,.0f}$` ({total_ret:+.2f}%)\n"
            f"Crypto: `{cv:.2f}EUR`\n"
            f"Win Rate: `{wr:.0f}%`"
        )
    except Exception as e:
        log.error(f"Morning brief: {e}")

# ================================================================
# TELEGRAM COMMANDS (V12 COMPLETE)
# ================================================================
def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN.strip()}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 10}, timeout=15
        )
        if r.status_code != 200:
            return
        for update in r.json().get("result", []):
            last_update_id = update["update_id"]
            msg     = update.get("message", {})
            text    = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id"))
            if chat_id != str(TELEGRAM_CHAT_ID).strip():
                continue

            if text in ["/start", "/aide"]:
                send_telegram(
                    "*AGENT V12*\n\n"
                    "*INFO*\n"
                    "/status - Etat agent\n"
                    "/portfolio - Alpaca + Coinbase\n"
                    "/crypto - Detail crypto\n"
                    "/marche - Snapshot marches\n"
                    "/positions - Positions ouvertes\n"
                    "/trades - Historique OK/LOSS\n"
                    "/report - Daily review\n\n"
                    "*CONTROLE*\n"
                    "/pause - Suspendre\n"
                    "/resume - Reprendre\n"
                    "/liquidate - Vendre cryptos"
                )

            elif text == "/status":
                acc      = get_account()
                vix      = get_vix()
                trades   = load_trades()
                wr       = get_win_rate() * 100
                send_telegram(
                    f"*STATUS AGENT V12*\n\n"
                    f"Mode: `{'PAPER' if PAPER_MODE else 'LIVE'}`\n"
                    f"Etat: `{'PAUSE' if agent_paused else 'ACTIF'}`\n"
                    f"Equity: `{acc['equity']:.2f}$`\n"
                    f"Cash: `{acc['cash']:.2f}$`\n"
                    f"VIX: `{vix:.1f}`\n"
                    f"Trades: `{len(trades)}` | Win Rate: `{wr:.0f}%`\n"
                    f"Positions: `{len(open_positions_tracker)}`\n"
                    f"Coinbase: `{'OK' if COINBASE_API_KEY else 'Non configure'}`"
                )

            elif text == "/portfolio":
                acc       = get_account()
                equity    = acc["equity"]
                day_pnl   = equity - acc["last_equity"]
                day_pct   = (day_pnl / acc["last_equity"] * 100) if acc["last_equity"] > 0 else 0
                total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
                details, cv = get_crypto_summary()
                send_telegram(
                    f"*PORTEFEUILLE GLOBAL*\n\n"
                    f"*BOURSE (Alpaca)*\n"
                    f"Valeur: `{equity:.2f}$`\n"
                    f"Aujourd hui: `{day_pct:+.2f}%` ({day_pnl:+.0f}$)\n"
                    f"Total: `{total_ret:+.2f}%`\n"
                    f"Cash: `{acc['cash']:.2f}$`\n\n"
                    f"*CRYPTO (Coinbase)*\n"
                    f"Valeur: `{cv:.2f}EUR`\n"
                    f"{details}"
                )

            elif text == "/crypto":
                details, total = get_crypto_summary()
                send_telegram(f"*CRYPTO COINBASE*\n\n{details}\n\n*TOTAL: {total:.2f}EUR*")

            elif text == "/marche":
                snaps = get_snapshots()
                vix   = get_vix()
                send_telegram(
                    f"*MARCHES*\n\n"
                    f"SPY: `{snaps['SPY']:+.2f}%`\n"
                    f"QQQ: `{snaps['QQQ']:+.2f}%`\n"
                    f"IBIT: `{snaps['IBIT']:+.2f}%`\n"
                    f"VIX: `{vix:.1f}`\n\n"
                    f"Bourse: `{'Ouverte' if is_open() else 'Fermee'}`"
                )

            elif text == "/positions":
                pos    = trading_client.get_all_positions()
                cb_pos = {k: v for k, v in open_positions_tracker.items() if v.get("platform") == "coinbase"}
                if not pos and not cb_pos:
                    send_telegram("Aucune position ouverte.")
                else:
                    lines = []
                    for p in pos:
                        pnl = float(p.unrealized_plpc) * 100
                        lines.append(f"{'OK' if pnl>=0 else 'LOSS'} *{p.symbol}* (Alpaca) `{pnl:+.2f}%`")
                    for key, v in cb_pos.items():
                        price = get_crypto_price(key.replace("CB_",""))
                        pnl   = (price - v["entry"]) / v["entry"] * 100 if price > 0 else 0
                        lines.append(f"{'OK' if pnl>=0 else 'LOSS'} *{key}* (Coinbase) entry:`{v['entry']:.4f}` `{pnl:+.2f}%`")
                    send_telegram("*POSITIONS*\n\n" + "\n".join(lines))

            elif text == "/trades":
                trades = load_trades()
                if not trades:
                    send_telegram("Aucun trade enregistre.")
                else:
                    recent = trades[-10:]
                    wins   = sum(1 for t in trades if t.get("pnl_usd",0) > 0)
                    total  = len(trades)
                    wr     = wins / total * 100 if total > 0 else 0
                    lines  = [
                        f"{'OK' if t.get('pnl_usd',0)>0 else 'LOSS'} *{t['symbol']}* `{t.get('pnl_usd',0):+.2f}$` [{t.get('signal','?')}] ({t.get('platform','?')})"
                        for t in reversed(recent)
                    ]
                    send_telegram(f"*HISTORIQUE TRADES*\nWin Rate: `{wr:.0f}%` ({wins}W/{total-wins}L)\n\n" + "\n".join(lines))

            elif text == "/report":
                daily_review()

            elif text == "/pause":
                agent_paused = True
                send_telegram("Agent PAUSE. /resume pour reprendre.")

            elif text == "/resume":
                agent_paused = False
                send_telegram("Agent REPRIS.")

            elif text == "/liquidate":
                liquidate_crypto()

    except Exception as e:
        log.error(f"Commands: {e}")

def telegram_loop():
    log.info("Telegram loop active")
    while True:
        process_commands()
        time.sleep(3)

# ================================================================
# HEALTH SERVER
# ================================================================
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Agent V12 OK")
    def log_message(self, *args): pass

# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("AGENT V12 - ALPACA + COINBASE")
    log.info(f"MODE: {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"STOCKS: {STOCK_WATCHLIST}")
    log.info(f"CRYPTO: {CRYPTO_WATCHLIST}")
    log.info(f"COINBASE: {'OK' if COINBASE_API_KEY else 'NON CONFIGURE'}")
    log.info("=" * 50)

    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever(), daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()

    schedule.every(15).minutes.do(scan_and_trade)
    schedule.every(30).minutes.do(scan_crypto)
    schedule.every(1).minutes.do(check_crypto_sl_tp)
    schedule.every().day.at("09:00").do(morning_brief)
    schedule.every().day.at("10:00").do(check_rebalancing)
    schedule.every().day.at("16:30").do(daily_review)

    send_telegram(
        f"*AGENT V12 EN LIGNE*\n"
        f"Alpaca OK | Coinbase {'OK' if COINBASE_API_KEY else 'non configure'}\n"
        f"Mode: {'PAPER' if PAPER_MODE else 'LIVE'}\n"
        f"Stocks scan 15min | Crypto scan 30min\n"
        f"/aide pour les commandes"
    )

    while True:
        schedule.run_pending()
        time.sleep(1)
