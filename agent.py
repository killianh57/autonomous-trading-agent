# -*- coding: utf-8 -*-
"""
AGENT TRADING IA V10 — BASE V9 + TOUTES AMÉLIORATIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture :
  Alpaca   -> Stocks (NVDA, TSLA, AAPL, META, MSFT) + ETFs Core-Satellite
  Coinbase -> Crypto direct (BTC-USD, ETH-USD)

V9 conservé : Coinbase RESTClient, bracket orders, SMC, ATR sizing, allocation HOLD/DAYTRADE
V10 ajouté  : EMA 9/21, RSI divergence, volume filter, VIX filter, blackout 11h-14h,
              news sentiment, multi-confluence, Kelly sizing, trade logger JSON+Notion,
              daily review 16h30, morning brief 9h, rebalancing check, crypto SL/TP manuel,
              commandes Telegram enrichies, health server Render
"""

import os, json, time, threading, logging, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
import schedule
import anthropic

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, TakeProfitRequest, StopLossRequest
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
    print("[WARN] coinbase-advanced-py non installe - crypto desactive")

load_dotenv()

# ================================================================
# CONFIGURATION
# ================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

ALPACA_API_KEY      = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY   = os.getenv("ALPACA_SECRET_KEY")
PAPER_MODE          = os.getenv("PAPER_MODE", "True") == "True"
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY        = os.getenv("NEWS_API_KEY", "")
NOTION_TOKEN        = os.getenv("NOTION_TOKEN", "")
NOTION_PAGE_ID      = os.getenv("NOTION_PAGE_ID", "3375afb215b4819785c5df026f5cdd75")
COINBASE_API_KEY    = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")

# Allocation V9 : HOLD (core passif) / DAYTRADE (satellite actif)
HOLD_PCT     = 0.65
DAYTRADE_PCT = 0.35

# Risk management
STOCK_SL_PCT           = 2.0
STOCK_TP_PCT           = 4.0
CRYPTO_SL_PCT          = 3.0
CRYPTO_TP_PCT          = 6.0
MAX_RISK_PER_TRADE_PCT = 0.02
CONFIDENCE_THRESHOLD   = 80      # FIXE - jamais adaptatif
MIN_CONFLUENCES        = 3

# Watchlists
STOCK_WATCHLIST  = ["NVDA", "TSLA", "AAPL", "META", "MSFT"]
CRYPTO_WATCHLIST = ["BTC-USD", "ETH-USD"]

# Core-Satellite targets pour rebalancing
CORE_TARGETS = {"VT": 0.40, "SCHD": 0.15, "VNQ": 0.05, "QQQ": 0.15, "IBIT": 0.10}

# Horaires NYSE EST
EST            = ZoneInfo("America/New_York")
MARKET_OPEN    = (9, 30)
MARKET_CLOSE   = (16, 0)
BLACKOUT_START = (11, 0)
BLACKOUT_END   = (14, 0)

# News
SEARCH_TERMS = {
    "NVDA": "NVIDIA OR NVDA", "TSLA": "Tesla OR TSLA",
    "AAPL": "Apple OR AAPL",  "META": "Meta OR Facebook",
    "MSFT": "Microsoft OR MSFT",
    "BTC":  "Bitcoin OR BTC", "ETH": "Ethereum OR ETH"
}
HIGH_RISK_KW = ["earnings", "SEC investigation", "fraud", "bankruptcy", "delisted", "lawsuit"]

# Etat global
agent_paused           = False
open_positions_tracker = {}
last_update_id         = 0
TRADES_FILE            = "trades.json"
START_CAPITAL          = 100_000.0

# ================================================================
# CLIENTS
# ================================================================
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_cb_client     = None

def get_coinbase_client():
    global _cb_client
    if _cb_client is None and COINBASE_AVAILABLE and COINBASE_API_KEY:
        _cb_client = CoinbaseClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
    return _cb_client

# ================================================================
# TELEGRAM
# ================================================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("[TG] " + msg[:100])
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        log.error(f"Telegram: {e}")

# ================================================================
# TRADE LOGGER
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
    pos = open_positions_tracker.pop(key)
    entry = pos["entry"]
    side  = pos["side"]
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
    if not NOTION_TOKEN:
        return
    emoji = "OK" if trade["pnl_usd"] >= 0 else "LOSS"
    content = (
        f"[{emoji}] {trade['symbol']} {trade['side'].upper()} | "
        f"{trade['entry']} -> {trade['exit']} | "
        f"PnL {trade['pnl_usd']:+.2f}$ ({trade['pnl_pct']:+.1f}%) | "
        f"Signal: {trade['signal']} | {trade['platform']} | {trade['date']}"
    )
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
        return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=["VIXY"]))["VIXY"].close
    except Exception:
        return 20.0

def get_smc_intraday(ticker):
    """V9 SMC + V10 EMA/RSI/Volume."""
    try:
        req  = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute5,
                                start=datetime.now(EST) - timedelta(days=3))
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
            "current": cur, "atr": atr,
            "swing_high": sh, "swing_low": sl,
            "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "baissier",
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema9 > ema21,
            "rsi": rsi_now,
            "rsi_div_bull": (cur < closes[-20]) and (rsi_now > rsi_prev),
            "rsi_div_bear": (cur > closes[-20]) and (rsi_now < rsi_prev),
            "volume_ok": volumes[-1] >= avg_vol * 0.8,
        }
    except Exception as e:
        log.error(f"SMC {ticker}: {e}")
        return None

# ================================================================
# NEWS SENTIMENT
# ================================================================
def get_news_sentiment(ticker):
    if not NEWS_API_KEY:
        return {"sentiment": "NEUTRAL", "pause": False, "reason": ""}
    try:
        params = {
            "q": f"({SEARCH_TERMS.get(ticker, ticker)}) AND (stock OR market OR earnings)",
            "from": (datetime.now() - timedelta(hours=6)).isoformat(),
            "sortBy": "relevancy", "language": "en", "apiKey": NEWS_API_KEY, "pageSize": 5
        }
        r = requests.get("https://newsapi.org/v2/everything", params=params, timeout=8)
        articles = r.json().get("articles", []) if r.ok else []
        for a in articles:
            title = a.get("title", "").lower()
            for kw in HIGH_RISK_KW:
                if kw in title:
                    return {"sentiment": "BEARISH", "pause": True, "reason": title[:80]}
        pos_w = ["surge","rally","gain","bullish","beat","record","growth","up"]
        neg_w = ["drop","fall","crash","bearish","miss","decline","warning","down"]
        pos = sum(1 for a in articles for w in pos_w if w in a.get("title","").lower())
        neg = sum(1 for a in articles for w in neg_w if w in a.get("title","").lower())
        s = "BULLISH" if pos > neg + 1 else "BEARISH" if neg > pos + 1 else "NEUTRAL"
        return {"sentiment": s, "pause": False, "reason": ""}
    except Exception as e:
        log.error(f"News {ticker}: {e}")
        return {"sentiment": "NEUTRAL", "pause": False, "reason": ""}

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
        if smc.get("trend") == "haussier":  c.append("Trend haussier")
    else:
        if not smc.get("ema_bullish"):      c.append("EMA9<EMA21")
        if smc.get("sweep_bearish"):        c.append("Sweep bear")
        if smc.get("rsi_div_bear"):         c.append("RSI div bear")
        if smc.get("volume_ok"):            c.append("Volume OK")
        if news_sentiment == "BEARISH":     c.append("News bear")
        if smc.get("trend") == "baissier":  c.append("Trend baissier")
    return len(c), c

# ================================================================
# RISK MANAGEMENT — ATR + Kelly
# ================================================================
def get_win_rate():
    trades = load_trades()
    if len(trades) < 5:
        return 0.5
    recent = trades[-20:]
    return sum(1 for t in recent if t["pnl_usd"] > 0) / len(recent)

def get_account_info():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash)}

# ================================================================
# BRACKET ORDER STOCKS — Alpaca
# ================================================================
def place_bracket_order(symbol, side, capital_allocated, limit_price, sl_pct, tp_pct,
                        signal_type="SMC", conviction=80, conf_list=None):
    if conf_list is None:
        conf_list = []
    try:
        account  = get_account_info()
        equity   = account["equity"]
        sl_dist  = limit_price * (sl_pct / 100.0)
        if sl_dist <= 0:
            return
        # ATR-based qty
        qty_atr  = (equity * MAX_RISK_PER_TRADE_PCT) / sl_dist
        qty_cap  = capital_allocated / limit_price
        # Kelly conservateur
        win_rate = get_win_rate()
        rr       = tp_pct / sl_pct
        kelly    = max(0.01, min(win_rate - (1 - win_rate) / rr, 0.25))
        qty_kelly= (equity * kelly * 0.25) / limit_price
        qty      = round(min(qty_atr, qty_cap, qty_kelly), 4)
        if qty <= 0:
            log.warning(f"{symbol} qty=0 apres risk management")
            return
        if side == "buy":
            sl_price   = round(limit_price * (1 - sl_pct / 100.0), 2)
            tp_price   = round(limit_price * (1 + tp_pct / 100.0), 2)
            order_side = OrderSide.BUY
        else:
            if not PAPER_MODE:
                send_telegram(f"SHORT bloque {symbol} - mode LIVE")
                return
            sl_price   = round(limit_price * (1 + sl_pct / 100.0), 2)
            tp_price   = round(limit_price * (1 - tp_pct / 100.0), 2)
            order_side = OrderSide.SELL
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp_price),
            stop_loss=StopLossRequest(stop_price=sl_price)
        )
        trading_client.submit_order(req)
        send_telegram(
            f"OK *TRADE ALPACA*\n"
            f"{'UP' if side=='buy' else 'DOWN'} *{symbol}* {side.upper()}\n"
            f"Entry `{limit_price}$` Qty `{qty}`\n"
            f"SL `{sl_price}$` (-{sl_pct}%) TP `{tp_price}$` (+{tp_pct}%)\n"
            f"RR {rr:.1f}:1 Conviction {conviction}/100\n"
            f"Signal {signal_type} | {len(conf_list)} confluences"
        )
        log_trade_open(symbol, side, limit_price, sl_price, tp_price, signal_type, conviction, len(conf_list), "alpaca")
        log.info(f"Bracket OK: {symbol} {side} @ {limit_price} SL:{sl_price} TP:{tp_price} qty:{qty}")
    except Exception as e:
        log.error(f"Bracket {symbol}: {e}")
        send_telegram(f"ERREUR ordre {symbol}: {e}")

# ================================================================
# CRYPTO ORDERS — Coinbase
# ================================================================
def get_crypto_price(product_id):
    try:
        cb  = get_coinbase_client()
        if not cb:
            return 0.0
        res = cb.get_best_bid_ask(product_ids=[product_id])
        bids = res["pricebooks"][0]["bids"]
        return float(bids[0]["price"]) if bids else 0.0
    except Exception as e:
        log.error(f"Coinbase price {product_id}: {e}")
        return 0.0

def get_crypto_candles(product_id, limit=50):
    try:
        cb    = get_coinbase_client()
        if not cb:
            return []
        end   = int(datetime.now(timezone.utc).timestamp())
        start = end - (limit * 5 * 60)
        res   = cb.get_candles(product_id=product_id, start=str(start), end=str(end), granularity="FIVE_MINUTE")
        return sorted(res.get("candles", []), key=lambda c: c["start"])
    except Exception as e:
        log.error(f"Coinbase candles {product_id}: {e}")
        return []

def get_crypto_smc(product_id):
    try:
        candles = get_crypto_candles(product_id)
        if len(candles) < 25:
            return None
        closes  = [float(c["close"])  for c in candles]
        highs   = [float(c["high"])   for c in candles]
        lows    = [float(c["low"])    for c in candles]
        volumes = [float(c["volume"]) for c in candles]
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
            "current": cur, "atr": atr,
            "swing_high": sh, "swing_low": sl,
            "sweep_bullish": cur > sl and min(lows[-5:]) < sl * 1.002,
            "sweep_bearish": cur < sh and max(highs[-5:]) > sh * 0.998,
            "trend": "haussier" if cur > closes[-20] else "baissier",
            "ema9": ema9, "ema21": ema21, "ema_bullish": ema9 > ema21,
            "rsi": rsi_now,
            "rsi_div_bull": (cur < closes[-20]) and (rsi_now > rsi_prev),
            "rsi_div_bear": (cur > closes[-20]) and (rsi_now < rsi_prev),
            "volume_ok": volumes[-1] >= avg_vol * 0.8,
        }
    except Exception as e:
        log.error(f"Crypto SMC {product_id}: {e}")
        return None

def place_crypto_order(product_id, side, usd_size, signal_type, conviction, conf_list):
    try:
        cb = get_coinbase_client()
        if not cb:
            send_telegram("Coinbase non configure")
            return
        price = get_crypto_price(product_id)
        if price <= 0:
            return
        account  = get_account_info()
        max_usd  = account["equity"] * MAX_RISK_PER_TRADE_PCT * 10
        usd_size = min(usd_size, max_usd)
        order_id = str(uuid.uuid4())
        if side == "buy":
            cb.market_order_buy(client_order_id=order_id, product_id=product_id, quote_size=str(round(usd_size, 2)))
        else:
            if not PAPER_MODE:
                send_telegram(f"SHORT crypto bloque {product_id} - LIVE")
                return
            cb.market_order_sell(client_order_id=order_id, product_id=product_id, base_size=str(round(usd_size / price, 6)))
        sl  = price * (1 - CRYPTO_SL_PCT/100) if side == "buy" else price * (1 + CRYPTO_SL_PCT/100)
        tp  = price * (1 + CRYPTO_TP_PCT/100) if side == "buy" else price * (1 - CRYPTO_TP_PCT/100)
        rr  = CRYPTO_TP_PCT / CRYPTO_SL_PCT
        key = f"CB_{product_id}"
        log_trade_open(key, side, price, sl, tp, signal_type, conviction, len(conf_list), "coinbase")
        send_telegram(
            f"BTC *CRYPTO COINBASE*\n"
            f"{'UP' if side=='buy' else 'DOWN'} *{product_id}* {side.upper()}\n"
            f"Prix `{price:,.2f}$` Size `{usd_size:.0f}$`\n"
            f"SL `{sl:,.2f}$` (-{CRYPTO_SL_PCT}%) TP `{tp:,.2f}$` (+{CRYPTO_TP_PCT}%)\n"
            f"RR {rr:.1f}:1 Conviction {conviction}/100 | Signal {signal_type}"
        )
        log.info(f"Crypto: {product_id} {side} @ {price} size={usd_size}")
    except Exception as e:
        log.error(f"Coinbase order {product_id}: {e}")
        send_telegram(f"ERREUR crypto {product_id}: {e}")

def check_crypto_sl_tp():
    crypto_pos = {k: v for k, v in open_positions_tracker.items() if v.get("platform") == "coinbase"}
    for key, pos in list(crypto_pos.items()):
        try:
            product_id = key.replace("CB_", "")
            price = get_crypto_price(product_id)
            if price <= 0:
                continue
            side = pos["side"]
            if side == "buy":
                if price <= pos["sl"]:
                    send_telegram(f"SL CRYPTO {product_id} @ {price:,.2f}$")
                    log_trade_close(key, price)
                elif price >= pos["tp"]:
                    send_telegram(f"TP CRYPTO {product_id} @ {price:,.2f}$")
                    log_trade_close(key, price)
            else:
                if price >= pos["sl"]:
                    send_telegram(f"SL CRYPTO SHORT {product_id} @ {price:,.2f}$")
                    log_trade_close(key, price)
                elif price <= pos["tp"]:
                    send_telegram(f"TP CRYPTO SHORT {product_id} @ {price:,.2f}$")
                    log_trade_close(key, price)
        except Exception as e:
            log.error(f"SL/TP check {key}: {e}")

# ================================================================
# CLAUDE SIGNAL
# ================================================================
PROMPT_SYSTEM = (
    "Tu es un trader institutionnel. Jamais d'emotion. RR 1:2 minimum.\n"
    "Reponds UNIQUEMENT en JSON strict :\n"
    '{"action":"BUY"|"SHORT"|"HOLD","confidence":0-100,'
    '"signal_type":"SMC"|"SMC+RSI"|"SMC+EMA"|"SMC+RSI+EMA","reason":"max 10 mots"}\n'
    "Si confidence < 80 -> action HOLD obligatoire."
)

def get_claude_signal(ticker, smc, news):
    context = (
        f"Ticker: {ticker} | Prix: {smc['current']}$\n"
        f"Trend: {smc['trend']} | ATR: {smc['atr']:.2f}$\n"
        f"Swing High: {smc['swing_high']} | Swing Low: {smc['swing_low']}\n"
        f"Sweep Bull: {smc['sweep_bullish']} | Sweep Bear: {smc['sweep_bearish']}\n"
        f"EMA9: {smc['ema9']:.2f} vs EMA21: {smc['ema21']:.2f} ({'BULL' if smc['ema_bullish'] else 'BEAR'})\n"
        f"RSI: {smc['rsi']:.1f} | Div Bull: {smc['rsi_div_bull']} | Div Bear: {smc['rsi_div_bear']}\n"
        f"Volume: {'OK' if smc['volume_ok'] else 'FAIBLE'} | News: {news['sentiment']}"
    )
    try:
        res = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=PROMPT_SYSTEM,
            messages=[{"role": "user", "content": context}]
        )
        raw = res.content[0].text.strip().replace("```json","").replace("```","")
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude {ticker}: {e}")
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

def arr(v):
    return "UP" if v >= 0 else "DOWN"

def get_market_snapshots():
    try:
        req   = StockSnapshotRequest(symbol_or_symbols=["SPY","QQQ","IBIT"])
        snaps = data_client.get_stock_snapshot(req)
        return {s: snaps[s].daily_bar.percent_change * 100 for s in ["SPY","QQQ","IBIT"]}
    except Exception:
        return {"SPY": 0.0, "QQQ": 0.0, "IBIT": 0.0}

# ================================================================
# SCAN STOCKS
# ================================================================
def scan_and_trade():
    global agent_paused
    if agent_paused or not is_market_open() or is_blackout():
        return
    vix = get_vix()
    if vix > 35:
        send_telegram(f"VIX {vix:.1f} > 35 - Scan stocks suspendu")
        return
    account       = get_account_info()
    n_tickers     = len(STOCK_WATCHLIST)
    trade_capital = account["equity"] * DAYTRADE_PCT / n_tickers
    log.info(f"Scan stocks (VIX:{vix:.1f})")
    for ticker in STOCK_WATCHLIST:
        if ticker in open_positions_tracker:
            continue
        try:
            smc = get_smc_intraday(ticker)
            if not smc:
                continue
            news = get_news_sentiment(ticker)
            if news["pause"]:
                continue
            signal = get_claude_signal(ticker, smc, news)
            if not signal or signal.get("action") == "HOLD":
                continue
            if signal.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue
            action = signal["action"]
            side   = "buy" if action == "BUY" else "sell"
            n_conf, conf_list = count_confluences(smc, news["sentiment"], action)
            if n_conf < MIN_CONFLUENCES:
                continue
            place_bracket_order(
                ticker, side, trade_capital, smc["current"],
                STOCK_SL_PCT, STOCK_TP_PCT,
                signal.get("signal_type", "SMC"), signal["confidence"], conf_list
            )
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
    account  = get_account_info()
    usd_size = account["equity"] * 0.05
    for product_id in CRYPTO_WATCHLIST:
        key = f"CB_{product_id}"
        if key in open_positions_tracker:
            continue
        try:
            smc = get_crypto_smc(product_id)
            if not smc:
                continue
            ticker = product_id.replace("-USD","")
            news   = get_news_sentiment(ticker)
            if news["pause"]:
                continue
            signal = get_claude_signal(ticker, smc, news)
            if not signal or signal.get("action") in ["HOLD","SHORT"]:
                continue
            if signal.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue
            n_conf, conf_list = count_confluences(smc, news["sentiment"], "BUY")
            if n_conf < MIN_CONFLUENCES:
                continue
            place_crypto_order(product_id, "buy", usd_size, signal.get("signal_type","SMC"), signal["confidence"], conf_list)
            time.sleep(2)
        except Exception as e:
            log.error(f"Crypto scan {product_id}: {e}")

# ================================================================
# DAILY REVIEW — 16h30 EST
# ================================================================
def daily_review():
    trades  = load_trades()
    today   = datetime.now(EST).strftime("%Y-%m-%d")
    t_today = [t for t in trades if t.get("date") == today]
    try:
        account   = get_account_info()
        equity    = account["equity"]
        last_eq   = float(trading_client.get_account().last_equity)
        day_pnl   = equity - last_eq
        day_pct   = (day_pnl / last_eq * 100) if last_eq > 0 else 0
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
    except Exception as e:
        log.error(f"Daily review: {e}")
        return
    if not t_today:
        send_telegram(
            f"DAILY REVIEW {today}\n"
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
        f"{'OK' if t['pnl_usd']>0 else 'LOSS'} {t['symbol']} {t['side'].upper()} {t['pnl_usd']:+.2f}$ [{t['signal']}] ({t.get('platform','alpaca')})"
        for t in t_today
    ]
    send_telegram(
        f"DAILY REVIEW {today}\n"
        f"PnL trades: {pnl_total:+.2f}$ | Portfolio: {day_pct:+.2f}%\n"
        f"Win Rate: {win_rate:.0f}% ({len(wins)}W/{len(losses)}L)\n\n"
        + "\n".join(lines) + "\n\n"
        f"Meilleur signal: {best_k(by_signal)} | Meilleure heure: {best_k(by_hour)}h\n"
        f"Regle demain: {_generate_rule(by_signal, by_hour)}\n"
        f"Total: {total_ret:+.2f}% | Valeur: {equity:,.0f}$"
    )

def _generate_rule(by_signal, by_hour):
    for sig, s in by_signal.items():
        total = s["w"] + s["l"]
        if total >= 5 and s["w"] / total < 0.35:
            return f"Signal {sig} win rate {s['w']/total*100:.0f}% - surveiller"
    for hour, s in by_hour.items():
        total = s["w"] + s["l"]
        if total >= 5 and s["w"] / total < 0.30:
            return f"Eviter {hour}h - win rate {s['w']/total*100:.0f}%"
    return "Continuer strategie actuelle"

# ================================================================
# MORNING BRIEF — 9h EST
# ================================================================
def morning_brief():
    try:
        vix   = get_vix()
        snaps = get_market_snapshots()
        a     = get_account_info()
        equity    = a["equity"]
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
        win_rate  = get_win_rate() * 100
        vix_s     = "PANIQUE" if vix > 35 else "VOLATIL" if vix > 25 else "NORMAL"
        send_telegram(
            f"MORNING BRIEF\n"
            f"SPY {snaps['SPY']:+.2f}% | QQQ {snaps['QQQ']:+.2f}% | IBIT {snaps['IBIT']:+.2f}%\n"
            f"VIX: {vix:.1f} ({vix_s})\n"
            f"Portfolio: {total_ret:+.2f}% | Valeur: {equity:,.0f}$ | Win Rate: {win_rate:.0f}%\n"
            f"Crypto: {'Coinbase actif' if COINBASE_API_KEY else 'Non configure'}"
        )
    except Exception as e:
        log.error(f"Morning brief: {e}")

# ================================================================
# REBALANCING CHECK — 10h EST
# ================================================================
def check_rebalancing():
    try:
        account   = get_account_info()
        equity    = account["equity"]
        positions = {p.symbol: float(p.market_value) for p in trading_client.get_all_positions()}
        alerts    = []
        for symbol, target in CORE_TARGETS.items():
            actual = positions.get(symbol, 0) / equity
            drift  = abs(actual - target)
            if drift > 0.05:
                alerts.append(f"{symbol}: cible {target*100:.0f}% -> actuel {actual*100:.1f}% (drift {drift*100:.1f}%)")
        if alerts:
            send_telegram("REBALANCING NECESSAIRE\n" + "\n".join(alerts))
    except Exception as e:
        log.error(f"Rebalancing: {e}")

# ================================================================
# TELEGRAM COMMANDS
# ================================================================
def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 1}, timeout=5
        )
        if not r.ok:
            return
        for update in r.json().get("result", []):
            last_update_id = update["update_id"]
            text = update.get("message", {}).get("text", "").strip().lower()
            if   text == "/marche":    _cmd_marche()
            elif text == "/status":    _cmd_status()
            elif text == "/positions": _cmd_positions()
            elif text == "/trades":    _cmd_trades()
            elif text == "/report":    daily_review()
            elif text == "/pause":
                agent_paused = True
                send_telegram("Agent en pause. /resume pour reprendre.")
            elif text == "/resume":
                agent_paused = False
                send_telegram("Agent repris.")
            elif text == "/aide":
                send_telegram(
                    "COMMANDES:\n"
                    "/marche - Marches + portfolio\n"
                    "/status - Etat agent\n"
                    "/positions - Positions ouvertes\n"
                    "/trades - Historique OK/LOSS\n"
                    "/report - Daily review maintenant\n"
                    "/pause - Suspendre\n"
                    "/resume - Reprendre"
                )
    except Exception as e:
        log.error(f"Commands: {e}")

def _cmd_marche():
    try:
        a         = get_account_info()
        equity    = a["equity"]
        last_eq   = float(trading_client.get_account().last_equity)
        day_pnl   = equity - last_eq
        day_pct   = (day_pnl / last_eq * 100) if last_eq > 0 else 0
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
        snaps     = get_market_snapshots()
        vix       = get_vix()
        send_telegram(
            f"MARCHES\n"
            f"SPY {snaps['SPY']:+.2f}% | QQQ {snaps['QQQ']:+.2f}% | IBIT {snaps['IBIT']:+.2f}%\n"
            f"VIX: {vix:.1f}\n\n"
            f"PORTFOLIO\n"
            f"Aujourd'hui: {day_pct:+.2f}% ({day_pnl:+.0f}$)\n"
            f"Total: {total_ret:+.2f}% | Valeur: {equity:,.0f}$\n"
            f"Bourse: {'Ouverte' if is_market_open() else 'Fermee'} | Crypto: {'Coinbase actif' if COINBASE_API_KEY else 'Non configure'}"
        )
    except Exception as e:
        send_telegram(f"Erreur /marche: {e}")

def _cmd_status():
    trades   = load_trades()
    win_rate = get_win_rate() * 100
    vix      = get_vix()
    send_telegram(
        f"STATUS AGENT V10\n"
        f"Mode: {'PAPER' if PAPER_MODE else 'LIVE'}\n"
        f"Etat: {'PAUSE' if agent_paused else 'ACTIF'}\n"
        f"VIX: {vix:.1f} | Trades: {len(trades)} | Win Rate: {win_rate:.0f}%\n"
        f"Positions ouvertes: {len(open_positions_tracker)}\n"
        f"Alpaca stocks: OK | Coinbase crypto: {'OK' if COINBASE_API_KEY else 'Non configure'}\n"
        f"Confidence seuil: {CONFIDENCE_THRESHOLD} (FIXE) | Min confluences: {MIN_CONFLUENCES}"
    )

def _cmd_positions():
    try:
        alpaca_pos = trading_client.get_all_positions()
        lines = []
        for p in alpaca_pos:
            pnl_pct = float(p.unrealized_plpc) * 100
            status  = "OK" if pnl_pct >= 0 else "LOSS"
            lines.append(f"[{status}] {p.symbol} (Alpaca) qty:{float(p.qty):.2f} entry:{float(p.avg_entry_price):.2f}$ now:{float(p.current_price):.2f}$ PnL:{pnl_pct:+.2f}%")
        cb_pos = {k: v for k, v in open_positions_tracker.items() if v.get("platform") == "coinbase"}
        for key, pos in cb_pos.items():
            price = get_crypto_price(key.replace("CB_",""))
            if price > 0:
                pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                status  = "OK" if pnl_pct >= 0 else "LOSS"
                lines.append(f"[{status}] {key} (Coinbase) {pos['side'].upper()} entry:{pos['entry']:,.2f}$ now:{price:,.2f}$ PnL:{pnl_pct:+.2f}%")
        if not lines:
            send_telegram("Aucune position ouverte.")
            return
        send_telegram(f"POSITIONS ({len(lines)})\n" + "\n".join(lines))
    except Exception as e:
        send_telegram(f"Erreur /positions: {e}")

def _cmd_trades():
    trades = load_trades()
    if not trades:
        send_telegram("Aucun trade enregistre.")
        return
    recent   = trades[-10:]
    wins     = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses   = sum(1 for t in trades if t["pnl_usd"] <= 0)
    total    = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0
    lines    = [
        f"{'OK' if t['pnl_usd']>0 else 'LOSS'} {t['symbol']} {t['side'].upper()} {t['pnl_usd']:+.2f}$ [{t['signal']}]"
        for t in reversed(recent)
    ]
    send_telegram(
        f"HISTORIQUE TRADES\n"
        f"Win Rate: {win_rate:.0f}% ({wins}W/{losses}L)\n"
        + "\n".join(lines)
    )

# ================================================================
# HEALTH SERVER — Render
# ================================================================
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Agent V10 OK")
    def log_message(self, *args): pass

def _run_health():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _Health).serve_forever()

# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    log.info("=" * 55)
    log.info("AGENT TRADING IA V10 — ALPACA + COINBASE")
    log.info(f"MODE       : {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"STOCKS     : {STOCK_WATCHLIST}")
    log.info(f"CRYPTO     : {CRYPTO_WATCHLIST} ({'Coinbase OK' if COINBASE_API_KEY else 'Coinbase MANQUANT'})")
    log.info(f"CONFIDENCE : >= {CONFIDENCE_THRESHOLD} (FIXE)")
    log.info(f"CONFLUENCES: >= {MIN_CONFLUENCES}")
    log.info("=" * 55)

    threading.Thread(target=_run_health, daemon=True).start()

    schedule.every(5).minutes.do(scan_and_trade)
    schedule.every(5).minutes.do(scan_crypto)
    schedule.every(1).minutes.do(check_crypto_sl_tp)
    schedule.every(1).minutes.do(process_commands)
    schedule.every().day.at("09:00").do(morning_brief)
    schedule.every().day.at("16:30").do(daily_review)
    schedule.every().day.at("10:00").do(check_rebalancing)

    send_telegram(
        f"AGENT V10 DEMARRE\n"
        f"Stocks Alpaca OK | Crypto Coinbase {'OK' if COINBASE_API_KEY else 'non configure'}\n"
        f"Mode: {'PAPER' if PAPER_MODE else 'LIVE'}\n"
        f"Scan 5min | Confidence >= {CONFIDENCE_THRESHOLD} | {MIN_CONFLUENCES} confluences min\n"
        f"/aide pour les commandes"
    )

    while True:
        schedule.run_pending()
        time.sleep(1)
