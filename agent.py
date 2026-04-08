import os
import json
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from coinbase.rest import RESTClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY  = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET   = os.getenv("COINBASE_SECRET_KEY")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PARAMETRES COINBASE (DAY TRADING ACTIF)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRYPTO_HOLD_STRICT = ["BTC-EUR", "ETH-EUR"]
CRYPTO_HOLD_SOUPLE = ["SOL-EUR", "XRP-EUR", "LINK-EUR"]
CRYPTO_HOLD_ALL    = CRYPTO_HOLD_STRICT + CRYPTO_HOLD_SOUPLE
CRYPTO_HOLD_ALLOC  = {
    "BTC-EUR": 0.25, "ETH-EUR": 0.15,
    "SOL-EUR": 0.05, "XRP-EUR": 0.03, "LINK-EUR": 0.02,
}
MAX_CRYPTO_POSITIONS          = 6
CRYPTO_SL_PCT                 = 4.0
CRYPTO_TP_PCT                 = 6.0
TRAILING_STOP_PCT             = 2.5
CRYPTO_RISK_PER_TRADE         = 0.12
CRYPTO_MIN_CONFIDENCE         = 65
COINBASE_FEE_PCT              = 1.2
CRYPTO_CANDLE_WINDOW_HOURS    = 48
CRYPTO_CIRCUIT_BREAKER_LOSSES = 3
CRYPTO_TRADE_ALLOC_PCT        = 0.20

CRYPTO_UNIVERSE_RAW = [
    "BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR",
    "ADA-EUR", "DOGE-EUR", "LTC-EUR", "DOT-EUR",
    "LINK-EUR", "AVAX-EUR", "UNI-EUR", "ATOM-EUR",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OBJECTIFS ET TIMERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MONTHLY_GOAL_EUR = 100
ANNUAL_GOAL_PCT  = 20.0
DCA_MONTHLY_EUR  = 100
MEMORY_FILE      = "trade_memory.json"

INTERVAL_CRYPTO    = 60
INTERVAL_STOCKS    = 300
INTERVAL_RISK      = 30
INTERVAL_SCHEDULER = 60

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLIENTS (LIVE TRADING)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
except Exception as e:
    coinbase = None
    print(f"Coinbase init error: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ETAT GLOBAL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
trading_paused       = False
vacation_mode        = False
custom_alerts        = {}
active_crypto_trades = {}
_lock                = threading.RLock()
loss_streak          = 0

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UTILITAIRES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def progress_bar(current, goal, length=10):
    if goal == 0: return "░" * length
    pct    = min(current / goal, 1.0)
    filled = int(pct * length)
    return f"{'█'*filled}{'░'*(length-filled)} {pct*100:.0f}%"

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_valid_products(retries=3, delay=5):
    """Récupère la liste des produits valides avec retry"""
    if not coinbase:
        return set()
    for attempt in range(retries):
        try:
            response = coinbase.get_products()
            products = response.get("products", [])
            result = {p["product_id"] for p in products if "product_id" in p}
            if result:
                return result
            log(f"⚠️ Liste produits vide, tentative {attempt+1}/{retries}")
        except Exception as e:
            log(f"Erreur récupération produits (tentative {attempt+1}): {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return set()

VALID_PRODUCTS = get_valid_products()
if not VALID_PRODUCTS:
    log("❌ Impossible de récupérer les produits Coinbase. Vérifiez vos clés API.")
CRYPTO_UNIVERSE = [s for s in CRYPTO_UNIVERSE_RAW if s in VALID_PRODUCTS]
log(f"🚀 {len(CRYPTO_UNIVERSE)} crypto actives: {CRYPTO_UNIVERSE}")

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log(f"Telegram error: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MEMOIRE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "trades": [], "hold_portfolio": {},
        "stats": {"wins": 0, "losses": 0, "total_pnl": 0},
        "monthly_stats": {}, "annual_stats": {},
        "patterns": {}, "errors": [], "equity_start": {}
    }

def save_memory(memory):
    with _lock:
        tmp_file = MEMORY_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(memory, f, indent=2)
        os.replace(tmp_file, MEMORY_FILE)

def record_trade(symbol, side, qty, price, pnl=None):
    with _lock:
        memory = load_memory()
        now    = datetime.now()
        month  = now.strftime("%Y-%m")
        year   = now.strftime("%Y")
        trade  = {"date": now.strftime("%Y-%m-%d %H:%M"), "symbol": symbol,
                  "side": side, "qty": qty, "price": price, "pnl": pnl}
        memory["trades"].append(trade)
        if pnl is not None:
            memory["stats"]["total_pnl"] += pnl
            if pnl > 0: memory["stats"]["wins"] += 1
            else:       memory["stats"]["losses"] += 1
            ms = memory["monthly_stats"].setdefault(month, {"wins":0,"losses":0,"pnl":0,"trades":[]})
            ms["pnl"] += pnl
            if pnl > 0: ms["wins"] += 1
            else:       ms["losses"] += 1
            ms["trades"].append(trade)
            ys = memory["annual_stats"].setdefault(year, {"wins":0,"losses":0,"pnl":0})
            ys["pnl"] += pnl
            if pnl > 0: ys["wins"] += 1
            else:       ys["losses"] += 1
            p = memory["patterns"].setdefault(symbol, {"wins":0,"losses":0,"total_pnl":0})
            p["total_pnl"] += pnl
            if pnl > 0: p["wins"] += 1
            else:        p["losses"] += 1
        memory["trades"] = memory["trades"][-200:]
        save_memory(memory)

def record_error(msg):
    memory = load_memory()
    memory["errors"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "error": str(msg)})
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def update_equity_checkpoints(equity):
    memory = load_memory()
    now    = datetime.now()
    es     = memory["equity_start"]
    for key, fmt in [("week","%Y-%W"),("month","%Y-%m"),("year","%Y")]:
        k = now.strftime(fmt)
        if es.get(f"{key}_key") != k:
            es[key] = equity
            es[f"{key}_key"] = k
    memory["equity_start"] = es
    save_memory(memory)

def get_equity_checkpoints():
    return load_memory().get("equity_start", {})

def get_stats():
    m = load_memory(); s = m["stats"]
    total = s["wins"] + s["losses"]
    return {**s, "winrate": (s["wins"]/total*100) if total > 0 else 0, "recent": m["trades"][-5:]}

def get_monthly_stats(month=None):
    if not month: month = datetime.now().strftime("%Y-%m")
    return load_memory()["monthly_stats"].get(month, {"wins":0,"losses":0,"pnl":0,"trades":[]})

def get_annual_stats(year=None):
    if not year: year = datetime.now().strftime("%Y")
    return load_memory()["annual_stats"].get(year, {"wins":0,"losses":0,"pnl":0})

def get_winrate(symbol):
    p = load_memory()["patterns"].get(symbol)
    if not p: return None
    t = p["wins"] + p["losses"]
    return (p["wins"]/t*100) if t > 0 else None

def get_best_worst(trades):
    w = [t for t in trades if t.get("pnl") is not None]
    if not w: return None, None
    return max(w, key=lambda x: x["pnl"]), min(w, key=lambda x: x["pnl"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DONNEES MARCHE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_crypto_price(symbol):
    try:
        if not coinbase: return None
        pb = coinbase.get_best_bid_ask(product_ids=[symbol])
        return float(pb["pricebooks"][0]["asks"][0]["price"])
    except:
        return None

def get_crypto_balance(currency):
    try:
        if not coinbase: return 0
        for acc in coinbase.get_accounts()["accounts"]:
            if acc["currency"] == currency:
                return float(acc["available_balance"]["value"])
        return 0
    except:
        return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANALYSE TECHNIQUE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def calculate_rsi(prices, period=14):
    if len(prices) < period+1: return None
    gains  = [max(prices[i]-prices[i-1],0) for i in range(1,len(prices))]
    losses = [max(prices[i-1]-prices[i],0) for i in range(1,len(prices))]
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al == 0: return 100
    return round(100-(100/(1+ag/al)),1)

def get_crypto_ta(symbol):
    try:
        if not coinbase: return None
        end_ts   = int(time.time())
        start_ts = end_ts - CRYPTO_CANDLE_WINDOW_HOURS * 3600
        candles = coinbase.get_candles(
            product_id=symbol,
            start=str(start_ts),
            end=str(end_ts),
            granularity="FIFTEEN_MINUTE"
        )
        prices = [float(c["close"]) for c in candles.get("candles",[])]
        prices = prices[::-1]  # remettre en ordre chronologique (Coinbase renvoie décroissant)
        if len(prices) < 14: return None
        rsi  = calculate_rsi(prices)
        ma20 = sum(prices[-20:])/20 if len(prices) >= 20 else None
        cur  = prices[-1]
        return {"rsi": rsi, "ma20": ma20, "current": cur,
                "trend": "haussier" if (ma20 and cur > ma20) else "baissier",
                "above_ma20": cur > ma20 if ma20 else None,
                "week_perf": ((cur-prices[-7])/prices[-7]*100) if len(prices) >= 7 else None,
                "prices": prices}
    except:
        return None

def detect_breakout_setup(prices, threshold=0.02):
    """Détecte si le prix approche ou franchit un niveau de résistance récent.
    EARLY: prix dans les `threshold*100`% sous le plus haut des 20 dernières bougies."""
    if not prices or len(prices) < 20:
        return False
    recent_high = max(prices[-20:])
    current     = prices[-1]
    return (recent_high - current) / recent_high <= threshold

def format_ta(ta):
    if not ta: return "Donnees indisponibles"
    rsi_txt = ""
    if ta.get("rsi"):
        label = "Survendu" if ta["rsi"] < 30 else "Surachete" if ta["rsi"] > 70 else "Neutre"
        rsi_txt = f"RSI {ta['rsi']} {label}\n"
    wp = f"Perf : {ta['week_perf']:+.1f}%\n" if ta.get("week_perf") else ""
    return f"{rsi_txt}Tendance : {ta['trend']}\nMA20 : {'OK' if ta.get('above_ma20') else 'Attention'}\n{wp}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEWS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_news(ticker, count=5):
    try:
        q = ticker.replace("-EUR","").replace("-USD","").replace("USDT","")  
        return requests.get(
            f"https://newsapi.org/v2/everything?q={q}&language=en"
            f"&sortBy=publishedAt&pageSize={count}&apiKey={NEWS_API_KEY}",
            timeout=10
        ).json().get("articles",[])
    except:
        return []

def format_news(articles, count=3):
    return "\n".join([f"- {a['title']}" for a in articles[:count]]) or "Aucune news recente"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ORDRES COINBASE (CRYPTO EN EUR)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def place_crypto_order(symbol, side, amount_eur, tp_pct=None, label="", reason=""):
    global loss_streak
    try:
        if not coinbase: return
        if side == "buy":
            coinbase.market_order_buy(
                client_order_id=f"bot_{int(time.time())}",
                product_id=symbol, quote_size=str(round(amount_eur,2))
            )
        else:
            price = get_crypto_price(symbol)
            if not price: return
            coinbase.market_order_sell(
                client_order_id=f"bot_{int(time.time())}",
                product_id=symbol, base_size=str(round(amount_eur/price,8))
            )
        price = get_crypto_price(symbol)
        pnl   = None
        record_trade(symbol, side, round(amount_eur/(price or 1),8), price or 0, pnl)
        with _lock:
            if side == "buy":
                active_crypto_trades[symbol] = {
                    "side": "long", "amount": amount_eur, "entry": price,
                    "peak": price,
                    "tp_pct": tp_pct or CRYPTO_TP_PCT, "reason": reason,
                    "entry_time": datetime.utcnow().isoformat()
                }
                send_telegram(f"<b>LONG {label}</b> <b>{symbol}</b>\n~{amount_eur:.2f}EUR\nSL: -{CRYPTO_SL_PCT}% | TP: +{tp_pct or CRYPTO_TP_PCT}%")
            else:
                entry_price = active_crypto_trades.get(symbol, {}).get("entry")
                if entry_price and price:
                    net_pnl_pct = ((price - entry_price) / entry_price * 100) - COINBASE_FEE_PCT
                    if net_pnl_pct < 0:
                        loss_streak += 1
                    else:
                        loss_streak = 0
                active_crypto_trades.pop(symbol, None)
                send_telegram(f"<b>Vente {label}</b> <b>{symbol}</b> ~{amount_eur:.2f}EUR\n(Frais deduits)")
    except Exception as e:
        record_error(f"Crypto {symbol}: {e}")
        send_telegram(f"<b>Ordre crypto echoue</b> {symbol}\n{str(e)[:100]}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DAY TRADING - CRYPTO (AGRESSIF)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def scan_crypto():
    if trading_paused or vacation_mode or not coinbase: return
    if loss_streak >= CRYPTO_CIRCUIT_BREAKER_LOSSES:
        log(f"Circuit breaker: {loss_streak} pertes consecutives, scan crypto suspendu")
        return
    if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS: return
    cash_eur  = get_crypto_balance("EUR")
    trade_cap = cash_eur
    if trade_cap < 5: return
    for symbol in CRYPTO_UNIVERSE:
        if len(active_crypto_trades) >= MAX_CRYPTO_POSITIONS: break
        if symbol in active_crypto_trades: continue
        price = get_crypto_price(symbol)
        ta    = get_crypto_ta(symbol)
        if not price or not ta or not ta.get("rsi"): continue
        if ta.get("week_perf") is None or abs(ta["week_perf"]) < 0.3: continue
        rsi        = ta["rsi"]
        trend      = ta["trend"]
        above_ma20 = ta.get("above_ma20")
        prices     = ta.get("prices", [])
        has_setup  = detect_breakout_setup(prices)
        if (rsi < 35 and trend == "haussier" and above_ma20) or \
           (40 <= rsi <= 60 and has_setup):
            amount = trade_cap * CRYPTO_RISK_PER_TRADE
            if amount < 2: continue
            reason = f"RSI={rsi:.0f} {'breakout' if has_setup else ''} tendance={trend}"
            place_crypto_order(symbol, "buy", amount, tp_pct=CRYPTO_TP_PCT, label="Day Trade", reason=reason)
        time.sleep(0.3)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GESTION DU RISQUE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_crypto_risk():
    if not coinbase: return
    for symbol, trade in list(active_crypto_trades.items()):
        if symbol in CRYPTO_HOLD_STRICT: continue
        price = get_crypto_price(symbol)
        if not price: continue
        entry         = trade.get("entry", price)
        tp_pct        = trade.get("tp_pct", CRYPTO_TP_PCT)
        gross_pnl_pct = ((price-entry)/entry*100) if entry else 0
        net_pnl_pct   = gross_pnl_pct - COINBASE_FEE_PCT
        currency      = symbol.replace("-EUR","")
        balance       = get_crypto_balance(currency)
        if balance <= 0: continue
        # Met à jour le pic de prix pour le trailing stop
        with _lock:
            current_peak = trade.get("peak", entry)
            if price > current_peak:
                active_crypto_trades[symbol]["peak"] = price
                current_peak = price
        trailing_drop = ((current_peak - price) / current_peak * 100) if current_peak else 0
        if net_pnl_pct <= -CRYPTO_SL_PCT:
            send_telegram(f"<b>Stop Loss crypto</b> {symbol} (Net: -{abs(net_pnl_pct):.1f}%)")
            place_crypto_order(symbol, "sell", balance*price, label="SL")
        elif trailing_drop >= TRAILING_STOP_PCT and net_pnl_pct > 0:
            send_telegram(f"<b>Trailing Stop crypto</b> {symbol} (recul: -{trailing_drop:.1f}% depuis pic)")
            place_crypto_order(symbol, "sell", balance*price, label="TS")
        elif net_pnl_pct >= tp_pct:
            send_telegram(f"<b>Take Profit crypto</b> {symbol} (Net: +{net_pnl_pct:.1f}%)")
            place_crypto_order(symbol, "sell", balance*price, label="TP")

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        price = get_crypto_price(symbol)
        if price and price >= target:
            send_telegram(f"<b>ALERTE !</b> <b>{symbol}</b> atteint {price:.2f}")
            del custom_alerts[symbol]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DCA MENSUEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("DCA annule - trading en pause."); return
    send_telegram("<b>DCA mensuel</b> en cours...")
    dca_eur = DCA_MONTHLY_EUR
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        amount = dca_eur * alloc
        if amount >= 1:
            place_crypto_order(symbol, "buy", amount, label="DCA")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RAPPORTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_daily_report(immediate=False):
    stats    = get_stats()
    btc_val  = get_crypto_balance("BTC") * (get_crypto_price("BTC-EUR") or 0)
    eth_val  = get_crypto_balance("ETH") * (get_crypto_price("ETH-EUR") or 0)
    cash_eur = get_crypto_balance("EUR")
    titre    = "<b>Rapport immediat</b>" if immediate else "<b>Rapport du soir</b>"
    r  = f"{titre}\n{'='*22}\n\n"
    r += f"BTC hold : ~{btc_val:.2f}EUR\n"
    r += f"ETH hold : ~{eth_val:.2f}EUR\n"
    r += f"Cash EUR  : {cash_eur:.2f}EUR\n\n"
    r += f"<b>Day Trades actifs : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS}</b>\n"
    for s, t in active_crypto_trades.items():
        entry = t.get("entry") or 0
        price = get_crypto_price(s) or entry
        pnl_pct = ((price - entry) / entry * 100 - COINBASE_FEE_PCT) if entry else 0
        r += f"  <b>{s}</b> {t['amount']:.2f}EUR ({pnl_pct:+.1f}% net)\n"
    r += f"\nReussite : {stats['winrate']:.0f}% | PnL net : {stats['total_pnl']:+.2f}EUR\n"
    r += f"Bot: {'vacances' if vacation_mode else 'pause' if trading_paused else 'actif'}"
    send_telegram(r)

def send_weekly_report():
    stats   = get_stats()
    memory  = load_memory()
    wk_ago  = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    wk_trades = [t for t in memory["trades"] if t["date"] >= wk_ago]
    best, worst = get_best_worst(wk_trades)
    ms      = get_monthly_stats()
    ys      = get_annual_stats()
    r  = "<b>RESUME SEMAINE</b>\n" + "="*22 + "\n\n"
    r += f"PnL semaine: {ms['pnl']:+.2f}EUR\n"
    r += f"{len(wk_trades)} trades | {stats['winrate']:.0f}% reussite\n"
    if best and best.get("pnl"): r += f"Meilleur: {best['symbol']} +{best['pnl']:.2f}EUR\n"
    if worst and worst.get("pnl"): r += f"Pire: {worst['symbol']} {worst['pnl']:.2f}EUR\n"
    r += f"\nBonne semaine !"
    send_telegram(r)

def send_monthly_report():
    month  = datetime.now().strftime("%Y-%m")
    ms     = get_monthly_stats(month)
    total_m = ms["wins"] + ms["losses"]
    r  = f"<b>BILAN {datetime.now().strftime('%B %Y').upper()}</b>\n" + "="*22 + "\n\n"
    r += f"PnL mois : {ms['pnl']:+.2f}EUR\n"
    r += f"{len(ms.get('trades',[]))} trades"
    if total_m > 0: r += f" | {ms['wins']/total_m*100:.0f}% reussite"
    r += "\n"
    send_telegram(r)

def send_annual_report():
    year = str(datetime.now().year)
    ys   = get_annual_stats(year)
    total_y = ys["wins"] + ys["losses"]
    r  = f"<b>BILAN ANNUEL {year}</b>\n" + "="*22 + "\n\n"
    r += f"PnL annuel : {ys['pnl']:+.2f}EUR\n"
    r += f"{total_y} trades"
    if total_y > 0: r += f" | {ys['wins']/total_y*100:.0f}% reussite"
    r += "\nBonne annee !"
    send_telegram(r)

def send_morning_briefing():
    btc_price = get_crypto_price("BTC-EUR") or 0
    eth_price = get_crypto_price("ETH-EUR") or 0
    cash_eur  = get_crypto_balance("EUR")
    stats     = get_stats()
    r  = "<b>BRIEFING CRYPTO</b>\n" + "="*22 + "\n\n"
    r += f"BTC : {btc_price:.2f}EUR\n"
    r += f"ETH : {eth_price:.2f}EUR\n"
    r += f"Cash EUR : {cash_eur:.2f}EUR\n\n"
    r += f"Trades actifs : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS}\n"
    r += f"Reussite : {stats['winrate']:.0f}% | PnL net : {stats['total_pnl']:+.2f}EUR\n"
    r += "C'est parti !"
    send_telegram(r)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMANDES TELEGRAM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cmd_aide():
    send_telegram(
        "<b>Commandes</b>\n\n"
        "/status | /positions | /hold\n"
        "/crypto | /report | /historique\n"
        "/objectifs\n"
        "/pourquoi BTC-EUR\n\n"
        "/briefing | /semaine\n"
        "/mois | /annee\n\n"
        "/pause | /resume\n"
        "/vacances | /retour\n\n"
        "/alerte BTC-EUR 90000\n"
        "/alertes\n\n"
        "/urgence - Ferme les trades"
    )

def cmd_pourquoi(symbol):
    symbol = symbol.upper()
    trade  = active_crypto_trades.get(symbol)
    if trade:
        send_telegram(f"<b>Raisonnement pour {symbol} :</b>\n\n{trade.get('reason', 'Raison non sauvegardee.')}")
    else:
        send_telegram(f"Aucun trade actif trouve pour {symbol}.")

def cmd_status():
    stats    = get_stats()
    ms       = get_monthly_stats()
    ys       = get_annual_stats()
    btc_val  = get_crypto_balance("BTC") * (get_crypto_price("BTC-EUR") or 0)
    eth_val  = get_crypto_balance("ETH") * (get_crypto_price("ETH-EUR") or 0)
    cash_eur = get_crypto_balance("EUR")
    send_telegram(
        f"<b>Portefeuille Crypto</b>\n\n"
        f"BTC hold : ~{btc_val:.2f}EUR\n"
        f"ETH hold : ~{eth_val:.2f}EUR\n"
        f"Cash EUR  : {cash_eur:.2f}EUR\n\n"
        f"Day Trades : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS}\n\n"
        f"Mois : {ms['pnl']:+.2f}EUR | Annee : {ys['pnl']:+.2f}EUR\n"
        f"Reussite : {stats['winrate']:.0f}%\n"
        f"Bot: {'vacances' if vacation_mode else 'pause' if trading_paused else 'actif'}"
    )

def cmd_hold():
    lines = []
    for symbol in CRYPTO_HOLD_STRICT:
        currency = symbol.replace("-EUR","")
        balance  = get_crypto_balance(currency)
        price    = get_crypto_price(symbol) or 0
        val      = balance * price
        lines.append(f"<b>{currency}</b> {balance:.6f} = {val:.2f}EUR (HOLD strict)")
    if not lines:
        send_telegram("Poche hold crypto vide."); return
    send_telegram("<b>Poche Hold Crypto</b>\n\n" + "\n".join(lines))

def cmd_positions():
    if not active_crypto_trades:
        send_telegram("Aucun day trade crypto actif."); return
    msg = "<b>Day Trades actifs</b>\n\n"
    for s, t in active_crypto_trades.items():
        entry = t.get("entry") or 0
        price = get_crypto_price(s) or entry
        pnl_pct = ((price - entry) / entry * 100 - COINBASE_FEE_PCT) if entry else 0
        msg += f"<b>{s}</b> {t['amount']:.2f}EUR ({pnl_pct:+.1f}% net) TP:+{t.get('tp_pct', CRYPTO_TP_PCT)}%\n"
    send_telegram(msg)

def cmd_crypto():
    if not coinbase:
        send_telegram("Coinbase non connecte."); return
    lines = []
    total = 0
    for symbol, alloc in CRYPTO_HOLD_ALLOC.items():
        currency = symbol.replace("-EUR","")
        price    = get_crypto_price(symbol) or 0
        balance  = get_crypto_balance(currency)
        val      = balance * price
        total   += val
        lines.append(f"<b>{currency}</b> {balance:.6f} = {val:.2f}EUR")
    send_telegram(
        "<b>Poche Hold Crypto</b>\n\n"
        + "\n".join(lines) +
        f"\n\nTotal hold : ~{total:.2f}EUR\n"
        f"BTC/ETH = jamais vendus\n"
        f"SOL/XRP/LINK = reequilibrables\n"
        f"Day trade actif : {len(active_crypto_trades)}/{MAX_CRYPTO_POSITIONS} pos"
    )

def cmd_objectifs():
    stats = get_stats()
    ms    = get_monthly_stats()
    ys    = get_annual_stats()
    send_telegram(
        f"<b>Objectifs</b>\n\n"
        f"Objectif mensuel : +{MONTHLY_GOAL_EUR}EUR\n"
        f"Ce mois : {ms['pnl']:+.2f}EUR\n\n"
        f"Objectif annuel : +{ANNUAL_GOAL_PCT}%\n"
        f"Cette annee : {ys['pnl']:+.2f}EUR\n\n"
        f"Reussite total : {stats['winrate']:.0f}% | PnL net : {stats['total_pnl']:+.2f}EUR"
    )

def cmd_historique():
    stats = get_stats()
    if not stats["recent"]:
        send_telegram("Aucun trade."); return
    msg = "<b>5 derniers trades</b>\n\n"
    for t in reversed(stats["recent"]):
        pnl  = f" | ${t['pnl']:+.2f}" if t.get("pnl") else ""
        msg += f"{t['date']} - {t['side'].upper()} <b>{t['symbol']}</b> @ ${t['price']:.2f}{pnl}\n"
    msg += f"\n{stats['winrate']:.0f}% | PnL net : ${stats['total_pnl']:+.2f}"
    send_telegram(msg)

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram("<b>Pause</b>\nStop loss actif. Tape /resume.")

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False; vacation_mode = False
    send_telegram("<b>Trading repris !</b>")

def cmd_urgence():
    global trading_paused
    trading_paused = True
    if not active_crypto_trades:
        send_telegram("Aucun trade actif."); return
    send_telegram(f"<b>URGENCE</b>\nFermeture de {len(active_crypto_trades)} trade(s) crypto...")
    for symbol, trade in list(active_crypto_trades.items()):
        if symbol in CRYPTO_HOLD_STRICT: continue
        currency = symbol.replace("-EUR","")
        balance  = get_crypto_balance(currency)
        price    = get_crypto_price(symbol) or 0
        if balance > 0 and price > 0:
            place_crypto_order(symbol, "sell", balance*price, label="URGENCE")
    send_telegram("Trades fermes.\nTape /resume.")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode = True; trading_paused = True
    send_telegram("<b>Mode vacances</b>\nHold conserve\nStop loss actif\nAucun nouveau trade\nTape /retour !")

def cmd_retour():
    global vacation_mode, trading_paused
    vacation_mode = False; trading_paused = False
    send_telegram("<b>Bon retour !</b>")
    send_daily_report(immediate=True)

def cmd_alerte(args):
    try:
        symbol, target = args[0].upper(), float(args[1])
        custom_alerts[symbol] = target
        send_telegram(f"Alerte : <b>{symbol}</b> -> {target:.2f}")
    except:
        send_telegram("Format : /alerte BTC-EUR 90000")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("Aucune alerte."); return
    msg = "<b>Alertes actives</b>\n\n"
    for s, t in custom_alerts.items():
        p    = get_crypto_price(s)
        diff = f" ({abs((p-t)/t*100):.1f}% restant)" if p else ""
        msg += f"<b>{s}</b> -> {t:.2f}{diff}\n"
    send_telegram(msg)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HANDLERS & THREADS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def handle_telegram():
    last_update_id = None
    while True:
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": last_update_id}, timeout=35
            )
            for update in res.json().get("result",[]):
                last_update_id = update["update_id"] + 1
                text = update.get("message",{}).get("text","").strip()
                cmd  = text.lower().split()[0] if text else ""
                args = text.split()[1:] if len(text.split()) > 1 else []
                if cmd in ["/aide","/start"]:           cmd_aide()
                elif cmd in ["/status", "/statut"]:     cmd_status()
                elif cmd == "/positions":               cmd_positions()
                elif cmd == "/hold":                    cmd_hold()
                elif cmd == "/crypto":                  cmd_crypto()
                elif cmd == "/report":                  send_daily_report(immediate=True)
                elif cmd == "/historique":              cmd_historique()
                elif cmd == "/objectifs":               cmd_objectifs()
                elif cmd == "/briefing":                send_morning_briefing()
                elif cmd == "/semaine":                 send_weekly_report()
                elif cmd == "/mois":                    send_monthly_report()
                elif cmd == "/annee":                   send_annual_report()
                elif cmd == "/pause":                   cmd_pause()
                elif cmd == "/resume":                  cmd_resume()
                elif cmd == "/urgence":                 cmd_urgence()
                elif cmd == "/vacances":                cmd_vacances()
                elif cmd == "/retour":                  cmd_retour()
                elif cmd == "/alertes":                 cmd_voir_alertes()
                elif cmd == "/pourquoi" and args:       cmd_pourquoi(args[0].upper())
                elif cmd == "/alerte" and len(args)>=2: cmd_alerte(args)
        except Exception:
            pass
        time.sleep(2)

def thread_news_watcher():
    last_news_title = ""
    while True:
        try:
            news = get_news("FED inflation interest rates market crash", count=1)
            if news and news[0]['title'] != last_news_title:
                last_news_title = news[0]['title']
                send_telegram(f"<b>BREAKING NEWS MACRO</b>\n\n{news[0]['title']}\n{news[0].get('url','')}")
        except:
            pass
        time.sleep(1200)

def thread_crypto():
    while True:
        try:
            check_crypto_risk()
            if not trading_paused and not vacation_mode: scan_crypto()
        except Exception as e:
            record_error(f"thread_crypto: {e}")
        time.sleep(INTERVAL_CRYPTO)

def thread_risk():
    while True:
        try:
            check_custom_alerts()
        except Exception as e:
            record_error(f"thread_risk: {e}")
        time.sleep(INTERVAL_RISK)

def thread_scheduler():
    briefing_sent = daily_sent = weekly_sent = monthly_sent = annual_sent = None
    while True:
        try:
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.hour == 8 and now.minute < 5 and briefing_sent != today:
                send_morning_briefing(); briefing_sent = today
            if now.hour == 21 and now.minute < 5 and daily_sent != today:
                send_daily_report(); daily_sent = today
            wk = now.strftime("%Y-%W")
            if now.weekday() == 0 and now.hour == 8 and now.minute < 5 and weekly_sent != wk:
                send_weekly_report(); weekly_sent = wk
            mo = now.strftime("%Y-%m")
            if now.day == 1 and now.hour == 9 and now.minute < 5 and monthly_sent != mo:
                run_dca(); send_monthly_report(); monthly_sent = mo
            yr = now.strftime("%Y")
            if now.month == 1 and now.day == 1 and now.hour == 10 and now.minute < 5 and annual_sent != yr:
                send_annual_report(); annual_sent = yr
        except Exception as e:
            record_error(f"thread_scheduler: {e}")
        time.sleep(INTERVAL_SCHEDULER)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log(f"Health server sur le port {port}")
    server.serve_forever()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    send_telegram(
        "<b>Trading Agent Crypto (Coinbase)</b>\n\n"
        "COINBASE (CRYPTO)\n"
        "Day Trade : 1 Heure (Hyperactif)\n"
        "Les frais Coinbase (~1.2%) sont deduits des PnL.\n\n"
        "Tape /aide"
    )
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram,     daemon=True).start()
    threading.Thread(target=thread_crypto,       daemon=True).start()
    threading.Thread(target=thread_risk,         daemon=True).start()
    threading.Thread(target=thread_scheduler,    daemon=True).start()
    threading.Thread(target=thread_news_watcher, daemon=True).start()

    log("Tous les threads demarres.")

    while True:
        time.sleep(60)
        log(f"Alive | {'PAUSE' if trading_paused else 'ACTIF'} | Trades: {len(active_crypto_trades)}")

if __name__ == "__main__":
    main()