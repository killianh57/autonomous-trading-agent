# -*- coding: utf-8 -*-
"""
AGENT TRADING IA V10 — SYSTÈME COMPLET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
- SMC + EMA 9/21 + RSI divergence + Volume filter
- ATR + Kelly position sizing (demi-Kelly conservateur)
- News sentiment (NewsAPI) + pause auto si news risque
- Multi-confluence (3 minimum pour setup A+)
- VIX macro filter (pause si VIX > 35)
- Blackout 11h-14h EST (bruit institutionnel)
- Trade logger JSON local + sync Notion
- Daily review 16h30 EST avec apprentissage
- Morning brief 9h EST
- Rebalancing check quotidien
- Confidence ≥ 80 FIXE — jamais adaptatif
- SHORT interdit en LIVE
- Health server pour Render
- Commandes Telegram : /aide /marche /status /positions /trades /pause /resume
"""

import os, json, time, threading, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
import schedule
import anthropic

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, TakeProfitRequest, StopLossRequest,
    StockSnapshotRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockSnapshotRequest as DataSnapshotRequest,
    StockLatestBarRequest
)
from alpaca.data.timeframe import TimeFrame
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv()

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PAPER_MODE        = os.getenv("PAPER_MODE", "True") == "True"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY", "")
NOTION_TOKEN      = os.getenv("NOTION_TOKEN", "")
NOTION_PAGE_ID    = os.getenv("NOTION_PAGE_ID", "3375afb215b4819785c5df026f5cdd75")

# ── Portfolio ──────────────────────────────────
START_CAPITAL          = 100_000.0
CASH_RESERVE_PCT       = 0.15        # 15% cash actif pour trades
MAX_RISK_PER_TRADE_PCT = 0.02        # 2% max risqué par trade
STOCK_SL_PCT           = 2.0        # Stop loss -2%
STOCK_TP_PCT           = 4.0        # Take profit +4% → RR 1:2
CONFIDENCE_THRESHOLD   = 80         # FIXE — jamais adaptatif
MIN_CONFLUENCES        = 3          # Setup A+ minimum

# ── Watchlist active ───────────────────────────
WATCHLIST = ["NVDA", "TSLA", "AAPL", "META", "MSFT"]

# ── Core targets (rebalancing) ─────────────────
CORE_TARGETS = {
    "VT":   0.40,
    "SCHD": 0.15,
    "VNQ":  0.05,
    "QQQ":  0.15,
    "IBIT": 0.10
}

# ── Horaires NYSE EST ──────────────────────────
EST             = ZoneInfo("America/New_York")
MARKET_OPEN     = (9, 30)
MARKET_CLOSE    = (16, 0)
BLACKOUT_START  = (11, 0)   # Éviter 11h-14h
BLACKOUT_END    = (14, 0)

# ── État global ────────────────────────────────
agent_paused           = False
open_positions_tracker = {}   # {symbol: {entry, signal, conviction, time, side}}
last_update_id         = 0
TRADES_FILE            = "trades.json"

# ── News search terms ──────────────────────────
SEARCH_TERMS = {
    "NVDA": "NVIDIA OR NVDA",
    "TSLA": "Tesla OR TSLA",
    "AAPL": "Apple OR AAPL",
    "META": "Meta OR Facebook",
    "MSFT": "Microsoft OR MSFT"
}
HIGH_RISK_KEYWORDS = [
    "earnings", "SEC investigation", "fraud",
    "bankruptcy", "delisted", "lawsuit", "recall", "scandal"
]

# ═══════════════════════════════════════════════════════════
# CLIENTS
# ═══════════════════════════════════════════════════════════
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER_MODE)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude_client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ═══════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[Telegram] {msg[:80]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ═══════════════════════════════════════════════════════════
# TRADE LOGGER
# ═══════════════════════════════════════════════════════════
def load_trades() -> list:
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                return json.load(f)
        except:
            return []
    return []

def save_trades(trades: list):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)

def log_trade_open(symbol, side, entry_price, sl, tp, signal_type, conviction, n_confluences):
    open_positions_tracker[symbol] = {
        "entry":        entry_price,
        "sl":           sl,
        "tp":           tp,
        "signal":       signal_type,
        "conviction":   conviction,
        "confluences":  n_confluences,
        "time":         datetime.now(EST).isoformat(),
        "side":         side
    }

def log_trade_close(symbol: str, exit_price: float):
    if symbol not in open_positions_tracker:
        return None
    pos   = open_positions_tracker.pop(symbol)
    entry = pos["entry"]
    side  = pos["side"]

    pnl_pct = ((exit_price - entry) / entry * 100) if side == "buy" else ((entry - exit_price) / entry * 100)
    pnl_usd = pnl_pct / 100 * entry * 10  # approximation qty=10

    trade = {
        "symbol":      symbol,
        "side":        side,
        "entry":       entry,
        "exit":        exit_price,
        "pnl_pct":     round(pnl_pct, 2),
        "pnl_usd":     round(pnl_usd, 2),
        "signal":      pos["signal"],
        "conviction":  pos["conviction"],
        "confluences": pos["confluences"],
        "entry_hour":  datetime.fromisoformat(pos["time"]).hour,
        "date":        datetime.now(EST).strftime("%Y-%m-%d"),
        "timestamp":   datetime.now(EST).isoformat()
    }

    trades = load_trades()
    trades.append(trade)
    save_trades(trades)
    _log_trade_to_notion(trade)
    return trade

def _log_trade_to_notion(trade: dict):
    if not NOTION_TOKEN:
        return
    emoji   = "🟢" if trade["pnl_usd"] >= 0 else "🔴"
    content = (
        f"{emoji} {trade['symbol']} {trade['side'].upper()} | "
        f"{trade['entry']} → {trade['exit']} | "
        f"PnL {trade['pnl_usd']:+.2f}$ ({trade['pnl_pct']:+.1f}%) | "
        f"Signal: {trade['signal']} | Conviction: {trade['conviction']}/100 | "
        f"{trade['date']}"
    )
    try:
        requests.patch(
            f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json"
            },
            json={"children": [{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]}
            }]},
            timeout=5
        )
    except Exception as e:
        log.error(f"Notion log error: {e}")

# ═══════════════════════════════════════════════════════════
# INDICATEURS TECHNIQUES
# ═══════════════════════════════════════════════════════════
def _calculate_atr(bars, period=14) -> float:
    if len(bars) < period + 1:
        return 0
    tr_list = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr_list[-period:]) / period

def _calculate_rsi(closes, period=14) -> float:
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

def _calculate_ema(closes, period) -> float:
    if len(closes) < period:
        return closes[-1]
    k, ema = 2 / (period + 1), closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_vix() -> float:
    try:
        req = StockLatestBarRequest(symbol_or_symbols=["VIXY"])
        bar = data_client.get_stock_latest_bar(req)
        return bar["VIXY"].close
    except:
        return 20.0

def get_technical_data(ticker: str) -> dict | None:
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute5,
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

        ema9  = _calculate_ema(closes, 9)
        ema21 = _calculate_ema(closes, 21)

        rsi_now  = _calculate_rsi(closes[-15:])
        rsi_prev = _calculate_rsi(closes[-20:-5])

        atr          = _calculate_atr(bars)
        swing_high   = max(highs[-20:])
        swing_low    = min(lows[-20:])
        avg_volume   = sum(volumes[-20:]) / 20

        return {
            "current":        cur,
            "atr":            atr,
            "ema9":           ema9,
            "ema21":          ema21,
            "ema_bullish":    ema9 > ema21,
            "rsi":            rsi_now,
            "rsi_div_bull":   (cur < closes[-20]) and (rsi_now > rsi_prev),
            "rsi_div_bear":   (cur > closes[-20]) and (rsi_now < rsi_prev),
            "swing_high":     swing_high,
            "swing_low":      swing_low,
            "sweep_bullish":  cur > swing_low and min(lows[-5:]) < swing_low * 1.002,
            "sweep_bearish":  cur < swing_high and max(highs[-5:]) > swing_high * 0.998,
            "volume_ok":      volumes[-1] >= avg_volume * 0.8,
            "trend":          "haussier" if cur > closes[-20] else "baissier",
        }
    except Exception as e:
        log.error(f"Technical data {ticker}: {e}")
        return None

# ═══════════════════════════════════════════════════════════
# NEWS SENTIMENT
# ═══════════════════════════════════════════════════════════
def get_news_sentiment(ticker: str) -> dict:
    if not NEWS_API_KEY:
        return {"sentiment": "NEUTRAL", "pause": False, "reason": ""}
    try:
        params = {
            "q":        f"({SEARCH_TERMS.get(ticker, ticker)}) AND (stock OR market OR earnings)",
            "from":     (datetime.now() - timedelta(hours=6)).isoformat(),
            "sortBy":   "relevancy",
            "language": "en",
            "apiKey":   NEWS_API_KEY,
            "pageSize": 5
        }
        r        = requests.get("https://newsapi.org/v2/everything", params=params, timeout=8)
        articles = r.json().get("articles", []) if r.ok else []

        for a in articles:
            title = a.get("title", "").lower()
            for kw in HIGH_RISK_KEYWORDS:
                if kw in title:
                    return {"sentiment": "BEARISH", "pause": True, "reason": title[:80]}

        pos_words = ["surge","rally","gain","bullish","beat","record","growth","up"]
        neg_words = ["drop","fall","crash","bearish","miss","decline","warning","down"]
        pos = sum(1 for a in articles for w in pos_words if w in a.get("title","").lower())
        neg = sum(1 for a in articles for w in neg_words if w in a.get("title","").lower())

        sentiment = "BULLISH" if pos > neg + 1 else "BEARISH" if neg > pos + 1 else "NEUTRAL"
        return {"sentiment": sentiment, "pause": False, "reason": ""}
    except Exception as e:
        log.error(f"News {ticker}: {e}")
        return {"sentiment": "NEUTRAL", "pause": False, "reason": ""}

# ═══════════════════════════════════════════════════════════
# MULTI-CONFLUENCE
# ═══════════════════════════════════════════════════════════
def count_confluences(tech: dict, news_sentiment: str, direction: str) -> tuple[int, list]:
    c = []
    if direction == "BUY":
        if tech["ema_bullish"]:    c.append("EMA9>EMA21")
        if tech["sweep_bullish"]:  c.append("Liquidity sweep bull")
        if tech["rsi_div_bull"]:   c.append("RSI divergence bull")
        if tech["volume_ok"]:      c.append("Volume OK")
        if news_sentiment == "BULLISH": c.append("News bullish")
        if tech["trend"] == "haussier": c.append("Trend haussier")
    else:
        if not tech["ema_bullish"]:c.append("EMA9<EMA21")
        if tech["sweep_bearish"]:  c.append("Liquidity sweep bear")
        if tech["rsi_div_bear"]:   c.append("RSI divergence bear")
        if tech["volume_ok"]:      c.append("Volume OK")
        if news_sentiment == "BEARISH": c.append("News bearish")
        if tech["trend"] == "baissier": c.append("Trend baissier")
    return len(c), c

# ═══════════════════════════════════════════════════════════
# RISK MANAGEMENT — ATR + Kelly
# ═══════════════════════════════════════════════════════════
def calculate_position_size(equity: float, entry_price: float, atr: float) -> float:
    win_rate = get_win_rate()
    rr       = STOCK_TP_PCT / STOCK_SL_PCT

    # ATR sizing
    sl_dist  = max(atr * 1.5, entry_price * STOCK_SL_PCT / 100)
    qty_atr  = (equity * MAX_RISK_PER_TRADE_PCT) / sl_dist if sl_dist > 0 else 0

    # Demi-Kelly conservateur
    kelly    = max(0.01, min(win_rate - (1 - win_rate) / rr, 0.25))
    qty_kelly = (equity * kelly * 0.25) / entry_price

    return max(round(min(qty_atr, qty_kelly), 4), 0)

def get_win_rate() -> float:
    trades = load_trades()
    if len(trades) < 5:
        return 0.5
    recent = trades[-20:]
    return sum(1 for t in recent if t["pnl_usd"] > 0) / len(recent)

# ═══════════════════════════════════════════════════════════
# CLAUDE SIGNAL
# ═══════════════════════════════════════════════════════════
SYSTEM_PROMPT = (
    "Tu es un trader institutionnel. Analyse les données et réponds UNIQUEMENT en JSON strict:\n"
    '{"action":"BUY"|"SHORT"|"HOLD","confidence":0-100,'
    '"signal_type":"SMC"|"SMC+RSI"|"SMC+EMA"|"SMC+RSI+EMA","reason":"max 10 mots"}\n'
    "Règles absolues: RR 1:2 minimum. Confidence < 80 = HOLD. Zéro émotion."
)

def get_claude_signal(ticker: str, tech: dict, news: dict) -> dict | None:
    context = (
        f"Ticker: {ticker} | Prix: {tech['current']}$\n"
        f"Trend: {tech['trend']} | ATR: {tech['atr']:.2f}$\n"
        f"EMA9: {tech['ema9']:.2f} vs EMA21: {tech['ema21']:.2f} ({'BULL' if tech['ema_bullish'] else 'BEAR'})\n"
        f"RSI: {tech['rsi']:.1f} | Div Bull: {tech['rsi_div_bull']} | Div Bear: {tech['rsi_div_bear']}\n"
        f"Sweep Bull: {tech['sweep_bullish']} | Sweep Bear: {tech['sweep_bearish']}\n"
        f"Volume: {'OK' if tech['volume_ok'] else 'FAIBLE'}\n"
        f"News: {news['sentiment']}"
    )
    try:
        res = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": context}]
        )
        raw = res.content[0].text.strip().replace("```json","").replace("```","")
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude signal {ticker}: {e}")
        return None

# ═══════════════════════════════════════════════════════════
# EXÉCUTION BRACKET ORDER
# ═══════════════════════════════════════════════════════════
def place_bracket_order(symbol, side, tech, signal_type, conviction, conf_list):
    try:
        account   = trading_client.get_account()
        equity    = float(account.equity)
        cash      = float(account.cash)
        entry     = tech["current"]

        if cash < equity * CASH_RESERVE_PCT * 0.3:
            log.warning(f"{symbol} — Cash insuffisant")
            return

        qty = calculate_position_size(equity, entry, tech["atr"])
        if qty <= 0:
            log.warning(f"{symbol} — Qty = 0 après risk management")
            return

        if side == "buy":
            sl    = round(entry * (1 - STOCK_SL_PCT / 100), 2)
            tp    = round(entry * (1 + STOCK_TP_PCT / 100), 2)
            oside = OrderSide.BUY
        else:
            if not PAPER_MODE:
                send_telegram(f"⛔ SHORT bloqué `{symbol}` — mode LIVE")
                return
            sl    = round(entry * (1 + STOCK_SL_PCT / 100), 2)
            tp    = round(entry * (1 - STOCK_TP_PCT / 100), 2)
            oside = OrderSide.SELL

        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=oside,
            time_in_force=TimeInForce.DAY,
            limit_price=round(entry, 2),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp),
            stop_loss=StopLossRequest(stop_price=sl)
        )
        trading_client.submit_order(req)

        rr  = STOCK_TP_PCT / STOCK_SL_PCT
        msg = (
            f"✅ *TRADE PLACÉ*\n"
            f"{'📈' if side=='buy' else '📉'} *{symbol}* {side.upper()}\n"
            f"Entry : `{entry}$` | Qty : `{qty}`\n"
            f"SL : `{sl}$` (-{STOCK_SL_PCT}%) | TP : `{tp}$` (+{STOCK_TP_PCT}%)\n"
            f"RR : {rr:.1f}:1 | Conviction : {conviction}/100\n"
            f"Signal : {signal_type}\n"
            f"Confluences ({len(conf_list)}) : {', '.join(conf_list[:3])}"
        )
        send_telegram(msg)
        log_trade_open(symbol, side, entry, sl, tp, signal_type, conviction, len(conf_list))
        log.info(f"Order OK: {symbol} {side} @ {entry} | SL:{sl} TP:{tp} Qty:{qty}")

    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
        send_telegram(f"🚨 Erreur ordre `{symbol}`: `{e}`")

# ═══════════════════════════════════════════════════════════
# MARCHÉ HELPERS
# ═══════════════════════════════════════════════════════════
def is_market_open() -> bool:
    now = datetime.now(EST)
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE

def is_blackout() -> bool:
    t = (datetime.now(EST).hour, datetime.now(EST).minute)
    return BLACKOUT_START <= t < BLACKOUT_END

def arr(v) -> str:
    return "📈" if v >= 0 else "📉"

def get_market_snapshots() -> dict:
    try:
        req = DataSnapshotRequest(symbol_or_symbols=["SPY", "QQQ", "IBIT"])
        snaps = data_client.get_stock_snapshot(req)
        result = {}
        for sym in ["SPY", "QQQ", "IBIT"]:
            try:
                result[sym] = snaps[sym].daily_bar.percent_change * 100
            except:
                result[sym] = 0.0
        return result
    except:
        return {"SPY": 0.0, "QQQ": 0.0, "IBIT": 0.0}

# ═══════════════════════════════════════════════════════════
# SCAN PRINCIPAL
# ═══════════════════════════════════════════════════════════
def scan_and_trade():
    global agent_paused
    if agent_paused or not is_market_open() or is_blackout():
        return

    vix = get_vix()
    if vix > 35:
        log.info(f"VIX={vix:.1f} — Trading suspendu (panique)")
        send_telegram(f"⚠️ VIX `{vix:.1f}` > 35 — Scan suspendu")
        return

    log.info(f"── Scan démarré (VIX:{vix:.1f}) ──")
    for ticker in WATCHLIST:
        if ticker in open_positions_tracker:
            continue
        try:
            tech = get_technical_data(ticker)
            if not tech:
                continue

            news = get_news_sentiment(ticker)
            if news["pause"]:
                log.info(f"{ticker} skip — news risque: {news['reason'][:40]}")
                continue

            signal = get_claude_signal(ticker, tech, news)
            if not signal or signal.get("action") == "HOLD":
                continue
            if signal.get("confidence", 0) < CONFIDENCE_THRESHOLD:
                continue

            action = signal["action"]
            side   = "buy" if action == "BUY" else "sell"

            n_conf, conf_list = count_confluences(tech, news["sentiment"], action)
            if n_conf < MIN_CONFLUENCES:
                log.info(f"{ticker} skip — {n_conf}/{MIN_CONFLUENCES} confluences")
                continue

            log.info(f"{ticker} {action} | conf:{signal['confidence']} | {n_conf} confluences")
            place_bracket_order(
                ticker, side, tech,
                signal.get("signal_type", "SMC"),
                signal["confidence"],
                conf_list
            )
            time.sleep(2)

        except Exception as e:
            log.error(f"Scan {ticker}: {e}")

# ═══════════════════════════════════════════════════════════
# DAILY REVIEW — 16h30 EST
# ═══════════════════════════════════════════════════════════
def daily_review():
    trades       = load_trades()
    today        = datetime.now(EST).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t.get("date") == today]

    try:
        account   = trading_client.get_account()
        equity    = float(account.equity)
        last_eq   = float(account.last_equity)
        day_pnl   = equity - last_eq
        day_pct   = (day_pnl / last_eq * 100) if last_eq > 0 else 0
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
    except Exception as e:
        log.error(f"Daily review account: {e}")
        return

    if not today_trades:
        send_telegram(
            f"📊 *DAILY REVIEW — {today}*\n"
            f"{'─'*22}\n"
            f"Aucun trade aujourd'hui\n"
            f"{'🟢' if day_pct>=0 else '🔴'} Portfolio : `{day_pct:+.2f}%` ({day_pnl:+.0f}$)\n"
            f"{'🟢' if total_ret>=0 else '🔴'} Total : `{total_ret:+.2f}%`"
        )
        return

    wins      = [t for t in today_trades if t["pnl_usd"] > 0]
    losses    = [t for t in today_trades if t["pnl_usd"] <= 0]
    pnl_total = sum(t["pnl_usd"] for t in today_trades)
    win_rate  = len(wins) / len(today_trades) * 100

    # Stats cumulées pour apprentissage
    all_50 = trades[-50:]
    by_signal, by_hour = {}, {}
    for t in all_50:
        s = t.get("signal", "?")
        h = t.get("entry_hour", 0)
        by_signal.setdefault(s, {"w":0,"l":0})
        by_hour.setdefault(h,   {"w":0,"l":0})
        key = "w" if t["pnl_usd"] > 0 else "l"
        by_signal[s][key] += 1
        by_hour[h][key]   += 1

    def best_key(d):
        return max(d, key=lambda k: d[k]["w"] / (d[k]["w"] + d[k]["l"] + 0.001))

    best_sig  = best_key(by_signal) if by_signal else "N/A"
    best_hour = best_key(by_hour)   if by_hour   else "N/A"
    rule      = _generate_rule(by_signal, by_hour)

    lines = [
        f"{'🟢' if t['pnl_usd']>0 else '🔴'} *{t['symbol']}* {t['side'].upper()} | "
        f"`{t['pnl_usd']:+.2f}$` | [{t['signal']}]"
        for t in today_trades
    ]

    send_telegram(
        f"📊 *DAILY REVIEW — {today}*\n"
        f"{'─'*22}\n"
        f"{'🟢' if pnl_total>=0 else '🔴'} PnL trades : `{pnl_total:+.2f}$`\n"
        f"{'🟢' if day_pct>=0 else '🔴'} Portfolio : `{day_pct:+.2f}%` ({day_pnl:+.0f}$)\n"
        f"Win Rate : `{win_rate:.0f}%` ({len(wins)}W / {len(losses)}L)\n\n"
        + "\n".join(lines) + "\n\n"
        f"{'─'*22}\n"
        f"🧠 *APPRENTISSAGE*\n"
        f"Meilleur signal : `{best_sig}`\n"
        f"Meilleure heure : `{best_hour}h`\n\n"
        f"📌 *Règle demain* : {rule}\n"
        f"{'─'*22}\n"
        f"{'🟢' if total_ret>=0 else '🔴'} Performance totale : `{total_ret:+.2f}%`\n"
        f"Valeur : `{equity:,.0f}$`"
    )
    log.info("Daily review envoyé")

def _generate_rule(by_signal: dict, by_hour: dict) -> str:
    for sig, s in by_signal.items():
        total = s["w"] + s["l"]
        if total >= 5 and s["w"] / total < 0.35:
            return f"⚠️ Signal `{sig}` : win rate {s['w']/total*100:.0f}% — surveiller"
    for hour, s in by_hour.items():
        total = s["w"] + s["l"]
        if total >= 5 and s["w"] / total < 0.30:
            return f"⛔ Éviter `{hour}h` — win rate {s['w']/total*100:.0f}%"
    return "✅ Continuer stratégie actuelle"

# ═══════════════════════════════════════════════════════════
# MORNING BRIEF — 9h EST
# ═══════════════════════════════════════════════════════════
def morning_brief():
    try:
        vix    = get_vix()
        snaps  = get_market_snapshots()
        account   = trading_client.get_account()
        equity    = float(account.equity)
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
        win_rate  = get_win_rate() * 100

        vix_status = ("🔴 PANIQUE — scan suspendu" if vix > 35
                      else "🟠 VOLATIL — size réduite" if vix > 25
                      else "🟢 NORMAL")

        send_telegram(
            f"🌅 *MORNING BRIEF*\n"
            f"{'─'*22}\n"
            f"📊 MARCHÉS\n"
            f"{arr(snaps['SPY'])}  S&P 500     `{snaps['SPY']:+.2f}%`\n"
            f"{arr(snaps['QQQ'])}  Nasdaq      `{snaps['QQQ']:+.2f}%`\n"
            f"{arr(snaps['IBIT'])}  Bitcoin ETF `{snaps['IBIT']:+.2f}%`\n"
            f"{'─'*22}\n"
            f"🌡️ VIX : `{vix:.1f}` — {vix_status}\n"
            f"{'─'*22}\n"
            f"💼 PORTFOLIO\n"
            f"{'🟢' if total_ret>=0 else '🔴'} Total : `{total_ret:+.2f}%`\n"
            f"Valeur : `{equity:,.0f}$`\n"
            f"Win Rate : `{win_rate:.0f}%`\n"
            f"{'─'*22}\n"
            f"🤖 Scan actif — seuil `{CONFIDENCE_THRESHOLD}` | `{MIN_CONFLUENCES}` confluences min"
        )
    except Exception as e:
        log.error(f"Morning brief: {e}")

# ═══════════════════════════════════════════════════════════
# REBALANCING CHECK — 10h EST
# ═══════════════════════════════════════════════════════════
def check_rebalancing():
    try:
        account   = trading_client.get_account()
        equity    = float(account.equity)
        positions = {p.symbol: float(p.market_value) for p in trading_client.get_all_positions()}

        alerts = []
        for symbol, target in CORE_TARGETS.items():
            actual = positions.get(symbol, 0) / equity
            drift  = abs(actual - target)
            if drift > 0.05:
                alerts.append(
                    f"⚖️ `{symbol}` cible `{target*100:.0f}%` → actuel `{actual*100:.1f}%` "
                    f"(drift `{drift*100:.1f}%`)"
                )
        if alerts:
            send_telegram(
                f"⚖️ *REBALANCING NÉCESSAIRE*\n{'─'*20}\n" + "\n".join(alerts)
            )
    except Exception as e:
        log.error(f"Rebalancing: {e}")

# ═══════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ═══════════════════════════════════════════════════════════
def process_commands():
    global last_update_id, agent_paused
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 1},
            timeout=5
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
                send_telegram("⏸️ Agent en pause. `/resume` pour reprendre.")
            elif text == "/resume":
                agent_paused = False
                send_telegram("▶️ Agent repris.")
            elif text == "/aide":
                send_telegram(
                    "📋 *COMMANDES*\n"
                    "/marche — Marchés + portfolio\n"
                    "/status — État agent\n"
                    "/positions — Positions ouvertes\n"
                    "/trades — Historique 🟢🔴\n"
                    "/report — Daily review maintenant\n"
                    "/pause — Suspendre\n"
                    "/resume — Reprendre"
                )
    except Exception as e:
        log.error(f"Command polling: {e}")

def _cmd_marche():
    try:
        account   = trading_client.get_account()
        equity    = float(account.equity)
        last_eq   = float(account.last_equity)
        day_pnl   = equity - last_eq
        day_pct   = (day_pnl / last_eq * 100) if last_eq > 0 else 0
        total_ret = (equity - START_CAPITAL) / START_CAPITAL * 100
        snaps     = get_market_snapshots()
        vix       = get_vix()

        send_telegram(
            f"🌍 *MARCHÉS*\n"
            f"{'─'*20}\n"
            f"{arr(snaps['SPY'])}  S&P 500     `{snaps['SPY']:+.2f}%`\n"
            f"{arr(snaps['QQQ'])}  Nasdaq      `{snaps['QQQ']:+.2f}%`\n"
            f"{arr(snaps['IBIT'])}  Bitcoin ETF `{snaps['IBIT']:+.2f}%`\n"
            f"🌡️  VIX          `{vix:.1f}`\n"
            f"{'─'*20}\n"
            f"💼 *NOTRE PORTFOLIO*\n"
            f"{'🟢' if day_pct>=0 else '🔴'}  Aujourd'hui  `{day_pct:+.2f}%` ({day_pnl:+.0f}$)\n"
            f"{'🟢' if total_ret>=0 else '🔴'}  Depuis début `{total_ret:+.2f}%`\n"
            f"    Valeur : `{equity:,.0f}$`\n"
            f"{'─'*20}\n"
            f"Bourse : {'Ouverte 🟢' if is_market_open() else 'Fermée 🔴'}"
        )
    except Exception as e:
        send_telegram(f"🚨 Erreur /marche: `{e}`")

def _cmd_status():
    trades   = load_trades()
    win_rate = get_win_rate() * 100
    vix      = get_vix()
    mode     = "PAPER 📋" if PAPER_MODE else "LIVE 🔴"
    status   = "⏸️ PAUSE" if agent_paused else "🟢 ACTIF"
    send_telegram(
        f"🤖 *STATUS AGENT V10*\n"
        f"{'─'*20}\n"
        f"Mode : `{mode}`\n"
        f"État : {status}\n"
        f"VIX : `{vix:.1f}`\n"
        f"Trades logués : `{len(trades)}`\n"
        f"Win Rate : `{win_rate:.0f}%`\n"
        f"Positions ouvertes : `{len(open_positions_tracker)}`\n"
        f"Confidence seuil : `{CONFIDENCE_THRESHOLD}` (FIXE)\n"
        f"Min confluences : `{MIN_CONFLUENCES}`"
    )

def _cmd_positions():
    try:
        positions = trading_client.get_all_positions()
        if not positions:
            send_telegram("📭 Aucune position ouverte.")
            return
        lines = []
        for p in positions:
            pnl_pct = float(p.unrealized_plpc) * 100
            emoji   = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"{emoji} *{p.symbol}* | qty:`{float(p.qty):.2f}`\n"
                f"  Entry:`{float(p.avg_entry_price):.2f}$` Now:`{float(p.current_price):.2f}$`\n"
                f"  PnL:`{pnl_pct:+.2f}%` ({float(p.unrealized_pl):+.2f}$)"
            )
        send_telegram(f"📊 *POSITIONS ({len(positions)})*\n{'─'*20}\n" + "\n\n".join(lines))
    except Exception as e:
        send_telegram(f"🚨 Erreur /positions: `{e}`")

def _cmd_trades():
    trades = load_trades()
    if not trades:
        send_telegram("📭 Aucun trade enregistré.")
        return
    recent   = trades[-10:]
    wins     = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses   = sum(1 for t in trades if t["pnl_usd"] <= 0)
    total    = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0
    ratio_emoji = "🔥" if win_rate >= 60 else "⚠️" if win_rate >= 40 else "🚨"

    lines = [
        f"{'🟢' if t['pnl_usd']>0 else '🔴'} *{t['symbol']}* {t['side'].upper()} | "
        f"`{t['pnl_usd']:+.2f}$` | [{t['signal']}]"
        for t in reversed(recent)
    ]
    send_telegram(
        f"📊 *HISTORIQUE TRADES*\n"
        f"{'─'*22}\n"
        f"{ratio_emoji} Win Rate : `{win_rate:.0f}%` ({wins}W / {losses}L)\n"
        f"{'─'*22}\n"
        + "\n".join(lines)
    )

# ═══════════════════════════════════════════════════════════
# HEALTH SERVER (Render)
# ═══════════════════════════════════════════════════════════
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Agent V10 OK")
    def log_message(self, *args): pass

def _run_health_server():
    port   = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info(f"Health server on port {port}")
    server.serve_forever()

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("═" * 50)
    log.info("AGENT TRADING IA V10 — DÉMARRAGE")
    log.info(f"MODE    : {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"LISTE   : {WATCHLIST}")
    log.info(f"SEUIL   : confidence ≥ {CONFIDENCE_THRESHOLD} | confluences ≥ {MIN_CONFLUENCES}")
    log.info("═" * 50)

    # Health server (thread daemon)
    threading.Thread(target=_run_health_server, daemon=True).start()

    # Scheduler
    schedule.every(5).minutes.do(scan_and_trade)
    schedule.every(1).minutes.do(process_commands)
    schedule.every().day.at("09:00").do(morning_brief)
    schedule.every().day.at("16:30").do(daily_review)
    schedule.every().day.at("10:00").do(check_rebalancing)

    # Message de démarrage
    send_telegram(
        f"🤖 *AGENT V10 DÉMARRÉ*\n"
        f"Mode : {'PAPER 📋' if PAPER_MODE else 'LIVE 🔴'}\n"
        f"Scan : toutes les 5 min\n"
        f"Confidence ≥ {CONFIDENCE_THRESHOLD} | {MIN_CONFLUENCES} confluences min\n"
        f"Tape /aide pour les commandes"
    )

    # Boucle principale
    while True:
        schedule.run_pending()
        time.sleep(1)
