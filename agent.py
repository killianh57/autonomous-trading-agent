import os
import json
import time
import threading
import requests
import anthropic
from http.server import HTTPServer, BaseHTTPRequestHandler
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from coinbase.rest import RESTClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
COINBASE_API_KEY   = os.getenv("COINBASE_API_KEY")
COINBASE_SECRET    = os.getenv("COINBASE_SECRET_KEY")

SAFE_ASSETS   = ["VT"]
TECH_ASSETS   = ["NVDA", "MSFT", "META"]
ETF_ASSETS    = ["QQQ", "XLK"]
ALL_ASSETS    = SAFE_ASSETS + TECH_ASSETS + ETF_ASSETS
CRYPTO_HOLD   = ["BTC-USD", "ETH-USD"]
CRYPTO_OPP    = ["SOL-USD"]

DCA_MONTHLY_EUR = 100
DCA_ALLOCATION  = {
    "VT": 0.20, "NVDA": 0.12, "MSFT": 0.08,
    "META": 0.08, "QQQ": 0.12, "XLK": 0.08,
    "BTC-USD": 0.15, "ETH-USD": 0.10, "CASH": 0.07
}

WEEKLY_GOAL_PCT  = 1.0
MONTHLY_GOAL_EUR = 100
ANNUAL_GOAL_PCT  = 20.0
POLL_INTERVAL    = 300
STOP_LOSS_PCT    = 3.0
MEMORY_FILE      = "trade_memory.json"

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

try:
    coinbase = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_SECRET)
except Exception as e:
    coinbase = None
    print(f"Coinbase init error: {e}")

last_seen_news      = {}
take_profit_targets = {}
trading_paused      = False
vacation_mode       = False
custom_alerts       = {}

def progress_bar(current, goal, length=10):
    if goal == 0: return "░" * length
    pct    = min(current / goal, 1.0)
    filled = int(pct * length)
    return f"{'█' * filled}{'░' * (length - filled)} {pct*100:.0f}%"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {"trades": [], "stats": {"wins": 0, "losses": 0, "total_pnl": 0},
            "monthly_stats": {}, "annual_stats": {}, "patterns": {}, "errors": [],
            "equity_start": {"week": None, "month": None, "year": None}}

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def update_equity_checkpoints(equity):
    memory = load_memory()
    now    = datetime.now()
    es     = memory["equity_start"]
    week   = now.strftime("%Y-%W")
    month  = now.strftime("%Y-%m")
    year   = now.strftime("%Y")
    if not es.get("week_key") or es.get("week_key") != week:
        es["week"] = equity
        es["week_key"] = week
    if not es.get("month_key") or es.get("month_key") != month:
        es["month"] = equity
        es["month_key"] = month
    if not es.get("year_key") or es.get("year_key") != year:
        es["year"] = equity
        es["year_key"] = year
    memory["equity_start"] = es
    save_memory(memory)

def get_equity_checkpoints():
    return load_memory().get("equity_start", {})

def record_trade(symbol, side, qty, price, pnl=None):
    memory = load_memory()
    now    = datetime.now()
    month  = now.strftime("%Y-%m")
    year   = now.strftime("%Y")
    trade  = {"date": now.strftime("%Y-%m-%d %H:%M"), "symbol": symbol, "side": side, "qty": qty, "price": price, "pnl": pnl}
    memory["trades"].append(trade)
    if pnl is not None:
        memory["stats"]["total_pnl"] += pnl
        if pnl > 0: memory["stats"]["wins"] += 1
        else: memory["stats"]["losses"] += 1
        if month not in memory["monthly_stats"]:
            memory["monthly_stats"][month] = {"wins": 0, "losses": 0, "pnl": 0, "trades": []}
        memory["monthly_stats"][month]["pnl"] += pnl
        if pnl > 0: memory["monthly_stats"][month]["wins"] += 1
        else: memory["monthly_stats"][month]["losses"] += 1
        memory["monthly_stats"][month]["trades"].append(trade)
        if year not in memory["annual_stats"]:
            memory["annual_stats"][year] = {"wins": 0, "losses": 0, "pnl": 0}
        memory["annual_stats"][year]["pnl"] += pnl
        if pnl > 0: memory["annual_stats"][year]["wins"] += 1
        else: memory["annual_stats"][year]["losses"] += 1
        memory["patterns"][symbol] = memory["patterns"].get(symbol, {"wins": 0, "losses": 0, "total_pnl": 0})
        memory["patterns"][symbol]["total_pnl"] += pnl
        if pnl > 0: memory["patterns"][symbol]["wins"] += 1
        else: memory["patterns"][symbol]["losses"] += 1
    memory["trades"] = memory["trades"][-200:]
    save_memory(memory)

def record_error(msg):
    memory = load_memory()
    memory["errors"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "error": msg})
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def get_symbol_winrate(symbol):
    pattern = load_memory()["patterns"].get(symbol)
    if not pattern: return None
    total = pattern["wins"] + pattern["losses"]
    return (pattern["wins"] / total * 100) if total > 0 else None

def get_stats():
    memory = load_memory()
    stats  = memory["stats"]
    total  = stats["wins"] + stats["losses"]
    return {"wins": stats["wins"], "losses": stats["losses"], "total_pnl": stats["total_pnl"],
            "winrate": (stats["wins"] / total * 100) if total > 0 else 0,
            "recent": memory["trades"][-5:]}

def get_monthly_stats(month=None):
    if not month: month = datetime.now().strftime("%Y-%m")
    return load_memory()["monthly_stats"].get(month, {"wins": 0, "losses": 0, "pnl": 0, "trades": []})

def get_annual_stats(year=None):
    if not year: year = datetime.now().strftime("%Y")
    return load_memory()["annual_stats"].get(year, {"wins": 0, "losses": 0, "pnl": 0})

def get_best_worst(trades):
    w = [t for t in trades if t.get("pnl") is not None]
    if not w: return None, None
    return max(w, key=lambda x: x["pnl"]), min(w, key=lambda x: x["pnl"])

def get_historical_prices(ticker, days=60):
    try:
        req  = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=days))
        return [bar.close for bar in data_client.get_stock_bars(req)[ticker]]
    except:
        return []

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains  = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)

def get_technical_analysis(ticker):
    prices = get_historical_prices(ticker)
    if not prices or len(prices) < 20: return None
    rsi  = calculate_rsi(prices)
    ma20 = sum(prices[-20:]) / 20
    ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else None
    cur  = prices[-1]
    return {"rsi": rsi, "ma20": ma20, "ma50": ma50, "current": cur,
            "trend": "haussier 📈" if (ma20 and ma50 and ma20 > ma50) else "baissier 📉",
            "above_ma20": cur > ma20, "above_ma50": cur > ma50 if ma50 else None,
            "week_perf": ((cur - prices[-6]) / prices[-6] * 100) if len(prices) >= 6 else None}

def format_ta(ta):
    if not ta: return "Indisponible"
    rsi_txt = f"RSI {ta['rsi']} {'⬇️ Survendu' if ta['rsi'] < 30 else '⬆️ Suracheté' if ta['rsi'] > 70 else '➡️ Neutre'}" if ta["rsi"] else ""
    return f"{rsi_txt}\nTendance : {ta['trend']}\nMA20 : {'✅ Au-dessus' if ta['above_ma20'] else '⚠️ En-dessous'}"

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), HealthHandler).serve_forever()

def is_market_open():
    now = datetime.utcnow()
    if now.weekday() >= 5: return False
    return now.replace(hour=13, minute=30, second=0, microsecond=0) <= now <= now.replace(hour=20, minute=0, second=0, microsecond=0)

def get_account_info():
    a = trading_client.get_account()
    return {"equity": float(a.equity), "cash": float(a.cash), "pnl": float(a.equity) - float(a.last_equity)}

def get_positions():
    return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value), "avg_price": float(p.avg_entry_price),
            "pnl": float(p.unrealized_pl), "pnl_pct": float(p.unrealized_plpc) * 100}
            for p in trading_client.get_all_positions()}

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
    except:
        return None

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

def get_market_performance(ticker):
    try:
        cur  = data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
        bars = list(data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
               start=datetime.now() - timedelta(days=2)))[ticker])
        return ((cur - bars[-2].close) / bars[-2].close) * 100 if len(bars) >= 2 else 0
    except:
        return 0

def get_spy_performance():
    return get_market_performance("SPY")

def get_news(ticker, count=5):
    try:
        q = ticker.replace("-USD", "").replace("USDT", "")
        return requests.get(f"https://newsapi.org/v2/everything?q={q}&language=en&sortBy=publishedAt&pageSize={count}&apiKey={NEWS_API_KEY}", timeout=10).json().get("articles", [])
    except:
        return []

def has_new_news(ticker, articles):
    if not articles: return False
    latest = articles[0].get("publishedAt", "")
    if last_seen_news.get(ticker) != latest:
        last_seen_news[ticker] = latest
        return True
    return False

def place_order(symbol, side, qty, take_profit_pct=None):
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=round(qty, 4),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY))
        price  = get_price(symbol)
        valeur = round(qty * price, 2) if price else "?"
        record_trade(symbol, side, round(qty, 4), price or 0)
        if side == "buy":
            if take_profit_pct: take_profit_targets[symbol] = take_profit_pct
            send_telegram(f"✅ <b>Achat</b>\n<b>{symbol}</b> ~${valeur}\n🛑 -{STOP_LOSS_PCT}% | 🎯 +{take_profit_pct or 5}%")
        else:
            if symbol in take_profit_targets: del take_profit_targets[symbol]
            send_telegram(f"✅ <b>Vente</b>\n<b>{symbol}</b> ~${valeur}")
    except Exception as e:
        record_error(f"Order failed {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre échoué</b>\n{symbol}\n{str(e)}")

def place_crypto_order(symbol, side, amount_usd, take_profit_pct=None):
    try:
        if not coinbase: return
        if side == "buy":
            coinbase.market_order_buy(client_order_id=f"bot_{int(time.time())}", product_id=symbol, quote_size=str(round(amount_usd, 2)))
        else:
            price = get_crypto_price(symbol)
            if not price: return
            coinbase.market_order_sell(client_order_id=f"bot_{int(time.time())}", product_id=symbol, base_size=str(round(amount_usd / price, 8)))
        price = get_crypto_price(symbol)
        record_trade(symbol, side, round(amount_usd / (price or 1), 8), price or 0)
        if side == "buy":
            if take_profit_pct: take_profit_targets[symbol] = take_profit_pct
            send_telegram(f"✅ <b>Achat crypto</b>\n💎 <b>{symbol}</b> ~${amount_usd:.2f}\n🛑 -{STOP_LOSS_PCT}% | 🎯 +{take_profit_pct or 10}%")
        else:
            if symbol in take_profit_targets: del take_profit_targets[symbol]
            send_telegram(f"✅ <b>Vente crypto</b>\n💎 <b>{symbol}</b> ~${amount_usd:.2f}")
    except Exception as e:
        record_error(f"Crypto order failed {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre crypto échoué</b>\n{symbol}\n{str(e)}")

def check_stop_loss_take_profit():
    for symbol, data in get_positions().items():
        pnl_pct = data["pnl_pct"]
        tp_pct  = take_profit_targets.get(symbol, 5.0)
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(f"🛑 <b>Stop loss</b>\n<b>{symbol}</b> -{abs(pnl_pct):.1f}%")
            place_order(symbol, "sell", data["qty"])
        elif pnl_pct >= tp_pct:
            send_telegram(f"🎯 <b>Objectif atteint !</b>\n<b>{symbol}</b> +{pnl_pct:.1f}%")
            place_order(symbol, "sell", data["qty"])

def check_crypto_stops():
    if not coinbase: return
    for symbol in CRYPTO_OPP:
        price = get_crypto_price(symbol)
        if not price: continue
        tp_pct  = take_profit_targets.get(symbol, 10.0)
        entry   = take_profit_targets.get(f"{symbol}_entry", price)
        pnl_pct = ((price - entry) / entry) * 100
        currency = symbol.replace("-USD", "")
        balance  = get_crypto_balance(currency)
        if balance <= 0: continue
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(f"🛑 <b>Stop loss crypto</b>\n{symbol} -{abs(pnl_pct):.1f}%")
            place_crypto_order(symbol, "sell", balance * price)
        elif pnl_pct >= tp_pct:
            send_telegram(f"🎯 <b>Take profit crypto</b>\n{symbol} +{pnl_pct:.1f}%")
            place_crypto_order(symbol, "sell", balance * price)

def check_dip_buying():
    if not is_market_open(): return
    account = get_account_info()
    reserve = account["equity"] * 0.10
    for ticker in ALL_ASSETS:
        prices = get_historical_prices(ticker, days=7)
        if len(prices) < 2: continue
        dip = ((prices[-1] - max(prices[:-1])) / max(prices[:-1])) * 100
        if dip <= -20 and account["cash"] >= reserve * 0.3:
            send_telegram(f"📉 <b>Grosse baisse !</b>\n<b>{ticker}</b> -{abs(dip):.1f}%")
            place_order(ticker, "buy", (reserve * 0.3) / prices[-1])
        elif dip <= -10 and account["cash"] >= reserve * 0.15:
            send_telegram(f"📉 <b>Baisse</b>\n<b>{ticker}</b> -{abs(dip):.1f}%")
            place_order(ticker, "buy", (reserve * 0.15) / prices[-1])
    if coinbase:
        for symbol in CRYPTO_HOLD:
            price = get_crypto_price(symbol)
            if not price: continue
            try:
                candles = coinbase.get_candles(product_id=symbol, start=str(int((datetime.now()-timedelta(days=7)).timestamp())), end=str(int(datetime.now().timestamp())), granularity="ONE_DAY")
                ph = [float(c["close"]) for c in candles.get("candles", [])]
                if len(ph) >= 2:
                    dip = ((price - max(ph[:-1])) / max(ph[:-1])) * 100
                    if dip <= -20: place_crypto_order(symbol, "buy", 20)
                    elif dip <= -10: place_crypto_order(symbol, "buy", 10)
            except:
                pass

def check_market_health():
    global trading_paused
    spy = get_spy_performance()
    if spy <= -10: send_telegram(f"🚨 <b>CRASH !</b>\nSPY : {spy:.1f}%\nTape /urgence ou /pause.")
    elif spy <= -5:
        trading_paused = True
        send_telegram(f"⚠️ <b>Forte baisse</b>\nSPY : {spy:.1f}%\nTrading en pause.")
    elif spy <= -3: send_telegram(f"📉 Marché sous tension ({spy:.1f}%)")

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        price = get_crypto_price(symbol) if "-USD" in symbol else get_price(symbol)
        if price and price >= target:
            send_telegram(f"🔔 <b>ALERTE !</b>\n<b>{symbol}</b> atteint ${price:.2f} ✅")
            del custom_alerts[symbol]

SYSTEM_PROMPT = """Tu es un trader professionnel Smart Money.
Règles : RR 1:2 minimum, max 2% par trade, suivre le trend dominant.
Si tu as perdu récemment sur ce ticker, sois plus prudent.
Réponds UNIQUEMENT en JSON :
{"action":"BUY"|"SELL"|"HOLD","confidence":0-100,"reason":"français court","risk_percent":1-2,"take_profit_pct":5-30}"""

def analyze_with_claude(ticker, price, news_txt, ta_summary, winrate, is_crypto=False):
    try:
        import json
        wr  = f"\nRéussite sur {ticker} : {winrate:.0f}%" if winrate else ""
        ctx = "\n⚠️ Crypto — volatil." if is_crypto else ""
        res = claude.messages.create(model="claude-sonnet-4-6", max_tokens=300, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Ticker:{ticker}\nPrix:${price}\nNews:\n{news_txt}\nAnalyse:\n{ta_summary}{wr}{ctx}"}])
        return json.loads(res.content[0].text.strip().replace("```json", "").replace("```", "").strip())
    except Exception as e:
        record_error(f"Claude error {ticker}: {e}")
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur", "risk_percent": 0, "take_profit_pct": 5}

def analyze_ticker(ticker, is_crypto=False):
    if trading_paused or vacation_mode: return
    if not is_market_open() and not is_crypto: return
    price = get_crypto_price(ticker) if is_crypto else get_price(ticker)
    if not price: return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles): return
    ta      = get_technical_analysis(ticker) if not is_crypto else None
    signal  = analyze_with_claude(ticker, price, "\n".join([f"- {a['title']}" for a in articles[:5]]), format_ta(ta), get_symbol_winrate(ticker), is_crypto)
    action, conf, reason, tp_pct = signal.get("action", "HOLD"), signal.get("confidence", 0), signal.get("reason", ""), signal.get("take_profit_pct", 5)
    if conf < 65: return
    account = get_account_info()
    if is_crypto and ticker in CRYPTO_OPP:
        if action == "BUY":
            send_telegram(f"💡 <b>Signal crypto</b>\n💎 <b>{ticker}</b>\n{reason}\nConfiance : {conf}% | Objectif : +{tp_pct}%")
            place_crypto_order(ticker, "buy", account["equity"] * 0.05, take_profit_pct=tp_pct)
    elif not is_crypto:
        if action == "BUY":
            qty = (account["equity"] * signal.get("risk_percent", 1) / 100) / price
            if qty * price >= 1:
                send_telegram(f"💡 <b>Signal d'achat</b>\n<b>{ticker}</b>\n{reason}\nConfiance : {conf}% | Objectif : +{tp_pct}%")
                place_order(ticker, "buy", qty, take_profit_pct=tp_pct)
        elif action == "SELL":
            pos = get_positions()
            if ticker in pos: place_order(ticker, "sell", pos[ticker]["qty"])

def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("⏸️ DCA annulé.")
        return
    send_telegram("💰 <b>DCA mensuel</b> en cours...")
    account = get_account_info()
    dca_usd = DCA_MONTHLY_EUR * 1.08
    if account["cash"] < dca_usd * 0.85:
        send_telegram(f"⚠️ Pas assez de cash (~${dca_usd:.0f} requis).")
        return
    for ticker, alloc in DCA_ALLOCATION.items():
        if ticker == "CASH": continue
        amount = dca_usd * alloc
        if ticker in ["BTC-USD", "ETH-USD"]:
            place_crypto_order(ticker, "buy", amount)
        else:
            price = get_price(ticker)
            if price: place_order(ticker, "buy", amount / price)

def overnight_analysis():
    signals = []
    for ticker in ALL_ASSETS:
        ta = get_technical_analysis(ticker)
        if not ta: continue
        if ta["rsi"] and ta["rsi"] < 35: signals.append(f"⬇️ <b>{ticker}</b> RSI {ta['rsi']} — survendu")
        elif ta["rsi"] and ta["rsi"] > 65: signals.append(f"⬆️ <b>{ticker}</b> RSI {ta['rsi']} — suracheté")
    return signals

def check_overnight_news():
    important = []
    keywords  = ["earnings", "merger", "FDA", "beat", "miss", "revenue", "acquisition"]
    for ticker in ALL_ASSETS:
        for article in get_news(ticker, count=3):
            if any(kw in article.get("title", "").lower() for kw in keywords):
                important.append(f"📰 <b>{ticker}</b> : {article.get('title', '')[:80]}")
                break
    return important

def check_intl_markets():
    results = []
    for ticker, name in [("EWJ", "🇯🇵 Japon"), ("FXI", "🇨🇳 Chine"), ("EWG", "🇩🇪 Allemagne"), ("EWU", "🇬🇧 UK")]:
        perf  = get_market_performance(ticker)
        emoji = "🟢" if perf > 0.5 else "🔴" if perf < -0.5 else "🟡"
        results.append(f"{emoji} {name} : {perf:+.2f}%")
    return results

def send_premarket_briefing():
    account    = get_account_info()
    spy_perf   = get_spy_performance()
    checkpts   = get_equity_checkpoints()
    week_start = checkpts.get("week", account["equity"])
    week_pnl   = account["equity"] - week_start
    week_goal  = week_start * WEEKLY_GOAL_PCT / 100
    signals    = overnight_analysis()
    news       = check_overnight_news()
    intl       = check_intl_markets()
    briefing   = "📋 <b>BRIEFING PRÉ-MARCHÉ</b>\nOuverture dans 5 min !\n" + "="*20 + "\n\n"
    briefing  += f"💼 ${account['equity']:.2f} | Cash : ${account['cash']:.2f}\n"
    briefing  += f"🌍 SPY hier : {spy_perf:+.2f}%\n\n"
    briefing  += f"🎯 Objectif semaine :\n{progress_bar(max(week_pnl, 0), week_goal)} ${week_pnl:+.2f}/${week_goal:.2f}\n\n"
    if intl: briefing += "🌏 <b>Marchés :</b>\n" + "\n".join(intl) + "\n\n"
    if signals: briefing += "📊 <b>Signaux :</b>\n" + "\n".join(signals[:4]) + "\n\n"
    if news: briefing += "📰 <b>News :</b>\n" + "\n".join(news[:3]) + "\n\n"
    sentiment = "🟢 Favorable" if spy_perf > 0.5 else "🔴 Défavorable" if spy_perf < -0.5 else "🟡 Neutre"
    briefing += f"Sentiment : {sentiment}\n\nC'est parti ! 🚀"
    send_telegram(briefing)

def send_daily_report(immediate=False):
    account    = get_account_info()
    positions  = get_positions()
    stats      = get_stats()
    spy        = get_spy_performance()
    checkpts   = get_equity_checkpoints()
    week_start = checkpts.get("week", account["equity"])
    week_goal  = week_start * WEEKLY_GOAL_PCT / 100
    week_pnl   = account["equity"] - week_start
    btc_p      = get_crypto_price("BTC-USD") or 0
    eth_p      = get_crypto_price("ETH-USD") or 0
    crypto_val = get_crypto_balance("BTC") * btc_p + get_crypto_balance("ETH") * eth_p
    titre  = "📊 <b>Rapport immédiat</b>" if immediate else "📊 <b>Rapport du soir</b>"
    r  = f"{titre}\n{'='*20}\n\n"
    r += f"💰 Actions : <b>${account['equity']:.2f}</b>\n"
    if crypto_val > 0:
        r += f"₿ Crypto : ~${crypto_val:.2f} | Total : ~${account['equity']+crypto_val:.2f}\n"
    r += f"💵 Cash : ${account['cash']:.2f}\n"
    r += f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f} | SPY : {spy:+.2f}%\n\n"
    if positions:
        r += "📌 <b>Actions :</b>\n"
        for s, d in positions.items():
            r += f"{'🟢' if d['pnl_pct'] >= 0 else '🔴'} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%) 🎯+{take_profit_targets.get(s, 5.0)}%\n"
    else:
        r += "📭 100% cash\n"
    r += f"\n🎯 <b>Objectif semaine :</b>\n{progress_bar(max(week_pnl, 0), week_goal)} ${week_pnl:+.2f}/${week_goal:.2f}\n\n"
    r += f"Réussite : {stats['winrate']:.0f}% | P&amp;L : ${stats['total_pnl']:+.2f}\n"
    total_pnl = stats['total_pnl']
    if total_pnl > 0:
        r += f"\n🧾 <b>Si tu retires tes gains :</b>\n"
        for pct in [10, 25, 50, 100]:
            montant = total_pnl * pct / 100
            impot   = montant * 0.30
            net     = montant - impot
            r += f"{pct}% → ${montant:.2f} | impôt ~${impot:.2f} | net ~${net:.2f}\n"
    r += f"\n🤖 {'🏖️' if vacation_mode else '⏸️' if trading_paused else '✅'}"
    send_telegram(r)

def send_postmarket_summary():
    account     = get_account_info()
    positions   = get_positions()
    checkpts    = get_equity_checkpoints()
    month_start = checkpts.get("month", account["equity"])
    month_pnl   = account["equity"] - month_start
    month_goal  = MONTHLY_GOAL_EUR * 1.08
    r  = "🌙 <b>FIN DE SÉANCE</b>\n" + "="*20 + "\n\n"
    r += f"💰 ${account['equity']:.2f} | {'📈' if account['pnl'] >= 0 else '📉'} ${account['pnl']:+.2f}\n\n"
    if positions:
        r += "📌 " + " | ".join([f"{'🟢' if d['pnl_pct'] >= 0 else '🔴'}{s} {d['pnl_pct']:+.2f}%" for s, d in positions.items()]) + "\n\n"
    r += f"📅 Objectif mensuel :\n{progress_bar(max(month_pnl, 0), month_goal)} ${month_pnl:+.2f}/${month_goal:.2f}\n\nÀ demain ! 🤖"
    send_telegram(r)

def send_weekly_report():
    account     = get_account_info()
    stats       = get_stats()
    spy         = get_spy_performance()
    checkpts    = get_equity_checkpoints()
    memory      = load_memory()
    week_ago    = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_trades = [t for t in memory["trades"] if t["date"] >= week_ago]
    best, worst = get_best_worst(week_trades)
    week_start  = checkpts.get("week", account["equity"])
    month_start = checkpts.get("month", account["equity"])
    year_start  = checkpts.get("year", account["equity"])
    week_pnl    = account["equity"] - week_start
    month_pnl   = account["equity"] - month_start
    year_pnl    = account["equity"] - year_start
    week_goal   = week_start * WEEKLY_GOAL_PCT / 100
    month_goal  = MONTHLY_GOAL_EUR * 1.08
    year_goal   = year_start * ANNUAL_GOAL_PCT / 100
    vs_spy      = account["pnl"] - (account["equity"] * spy / 100)
    sector_perf = {t: ta["week_perf"] for t in ALL_ASSETS if (ta := get_technical_analysis(t)) and ta.get("week_perf")}
    r  = "📅 <b>RÉSUMÉ DE LA SEMAINE</b>\n" + "="*25 + "\n\n"
    r += f"💰 <b>${account['equity']:.2f}</b> | {'📈' if week_pnl >= 0 else '📉'} ${week_pnl:+.2f}\n"
    r += f"SPY : {spy:+.2f}% | {'✅ Je bats !' if vs_spy > 0 else '📉 Le marché me bat'}\n\n"
    r += f"🎯 <b>Objectifs :</b>\n"
    r += f"Semaine : {progress_bar(max(week_pnl,0), week_goal)} ${week_pnl:+.2f}\n"
    r += f"Mois : {progress_bar(max(month_pnl,0), month_goal)} ${month_pnl:+.2f}\n"
    r += f"Année : {progress_bar(max(year_pnl,0), year_goal)} ${year_pnl:+.2f}\n\n"
    r += f"📊 {len(week_trades)} trades | {stats['winrate']:.0f}% réussite\n"
    if best and best.get("pnl"): r += f"🏆 {best['symbol']} +${best['pnl']:.2f}\n"
    if worst and worst.get("pnl"): r += f"💔 {worst['symbol']} ${worst['pnl']:.2f}\n"
    if sector_perf:
        bs = max(sector_perf.items(), key=lambda x: x[1])
        ws = min(sector_perf.items(), key=lambda x: x[1])
        r += f"🚀 {bs[0]} ({bs[1]:+.2f}%) | 📉 {ws[0]} ({ws[1]:+.2f}%)\n"
    r += "\nBonne semaine ! 💪"
    send_telegram(r)

def send_monthly_report():
    account     = get_account_info()
    checkpts    = get_equity_checkpoints()
    month       = datetime.now().strftime("%Y-%m")
    prev_month  = (datetime.now() - timedelta(days=30)).strftime("%Y-%m")
    ms          = get_monthly_stats(month)
    prev_ms     = get_monthly_stats(prev_month)
    month_start = checkpts.get("month", account["equity"])
    year_start  = checkpts.get("year", account["equity"])
    month_pnl   = account["equity"] - month_start
    year_pnl    = account["equity"] - year_start
    month_goal  = MONTHLY_GOAL_EUR * 1.08
    year_goal   = year_start * ANNUAL_GOAL_PCT / 100
    best, worst = get_best_worst(ms.get("trades", []))
    proj        = (year_pnl / datetime.now().month) * 12
    total_m     = ms['wins'] + ms['losses']
    r  = f"📆 <b>BILAN DU MOIS — {datetime.now().strftime('%B %Y').upper()}</b>\n" + "="*25 + "\n\n"
    r += f"💰 <b>${account['equity']:.2f}</b> | Ce mois : ${month_pnl:+.2f}\n"
    r += f"Mois précédent : ${prev_ms['pnl']:+.2f} | {'📈 Mieux !' if month_pnl > prev_ms['pnl'] else '📉 Moins bien'}\n\n"
    r += f"🎯 <b>Objectifs :</b>\n"
    r += f"Mois (+{MONTHLY_GOAL_EUR}€) : {progress_bar(max(month_pnl,0), month_goal)} ${month_pnl:+.2f}\n"
    r += f"Année (+{ANNUAL_GOAL_PCT}%) : {progress_bar(max(year_pnl,0), year_goal)} ${year_pnl:+.2f}\n\n"
    r += f"📊 {len(ms.get('trades',[]))} trades"
    if total_m > 0: r += f" | {ms['wins']/total_m*100:.0f}% réussite"
    r += "\n"
    if best and best.get("pnl"): r += f"🏆 {best['symbol']} +${best['pnl']:.2f}\n"
    if worst and worst.get("pnl"): r += f"💔 {worst['symbol']} ${worst['pnl']:.2f}\n\n"
    r += f"📈 Projection {datetime.now().year} : ~${proj:+.2f}\n\n"
    r += f"🧾 {'📈 Gains à déclarer (flat tax 30%)' if month_pnl > 0 else '📉 Rien à déclarer'}\n"
    r += "⚠️ Consulte un comptable."
    send_telegram(r)

def send_annual_report():
    account    = get_account_info()
    checkpts   = get_equity_checkpoints()
    year       = str(datetime.now().year)
    ys         = get_annual_stats(year)
    year_start = checkpts.get("year", account["equity"])
    year_pnl   = account["equity"] - year_start
    year_goal  = year_start * ANNUAL_GOAL_PCT / 100
    memory     = load_memory()
    ym         = {k: v for k, v in memory.get("monthly_stats", {}).items() if k.startswith(year)}
    bm         = max(ym.items(), key=lambda x: x[1]["pnl"]) if ym else None
    wm         = min(ym.items(), key=lambda x: x[1]["pnl"]) if ym else None
    pt         = memory.get("patterns", {})
    bt         = max(pt.items(), key=lambda x: x[1].get("total_pnl", 0)) if pt else None
    total_y    = ys['wins'] + ys['losses']
    proj_5y    = account["equity"] * ((1 + (year_pnl / max(year_start, 1))) ** 5)
    r  = f"🗓️ <b>BILAN ANNUEL {year}</b>\n" + "="*25 + "\n\n"
    r += f"💰 <b>${account['equity']:.2f}</b> | P&amp;L : ${year_pnl:+.2f}\n\n"
    r += f"🎯 <b>Objectif annuel (+{ANNUAL_GOAL_PCT}%) :</b>\n"
    r += f"{progress_bar(max(year_pnl,0), year_goal)} ${year_pnl:+.2f}/${year_goal:.2f}\n\n"
    r += f"📊 {total_y} trades"
    if total_y > 0: r += f" | {ys['wins']/total_y*100:.0f}% réussite"
    r += "\n"
    if bm: r += f"🏆 Meilleur mois : {bm[0]} +${bm[1]['pnl']:.2f}\n"
    if wm: r += f"💔 Pire mois : {wm[0]} ${wm[1]['pnl']:.2f}\n"
    if bt: r += f"🚀 Meilleur actif : {bt[0]} +${bt[1].get('total_pnl',0):.2f}\n\n"
    r += f"📈 Projection 5 ans : ~${proj_5y:.2f}\n\n"
    if year_pnl > 0:
        r += f"🧾 Impôt estimé (30%) : ~${year_pnl*0.30:.2f}\n\n"
    else:
        r += "🧾 Aucun impôt — année déficitaire\n\n"
    new_goal = max(year_pnl * 1.2, 500)
    r += f"🎯 Objectifs {int(year)+1} : +${new_goal:.0f} | Capital cible : ${account['equity']*1.20:.0f}\n\n"
    r += "Bonne année ! 🚀💪"
    send_telegram(r)

def handle_telegram_commands():
    last_update_id = None
    while True:
        try:
            res = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": last_update_id}, timeout=35)
            for update in res.json().get("result", []):
                last_update_id = update["update_id"] + 1
                text = update.get("message", {}).get("text", "").strip()
                cmd  = text.lower().split()[0] if text else ""
                args = text.split()[1:] if len(text.split()) > 1 else []
                if cmd in ["/aide", "/start"]:            cmd_aide()
                elif cmd == "/status":                    cmd_status()
                elif cmd == "/positions":                 cmd_positions()
                elif cmd == "/crypto":                    cmd_crypto()
                elif cmd == "/pause":                     cmd_pause()
                elif cmd == "/resume":                    cmd_resume()
                elif cmd == "/report":                    send_daily_report(immediate=True)
                elif cmd == "/urgence":                   cmd_urgence()
                elif cmd == "/vacances":                  cmd_vacances()
                elif cmd == "/retour":                    cmd_retour()
                elif cmd == "/historique":                cmd_historique()
                elif cmd == "/technique" and args:        cmd_technique(args[0].upper())
                elif cmd == "/alerte" and len(args) >= 2: cmd_alerte(args)
                elif cmd == "/alertes":                   cmd_voir_alertes()
                elif cmd == "/marche":                    cmd_marche()
                elif cmd == "/briefing":                  send_premarket_briefing()
                elif cmd == "/semaine":                   send_weekly_report()
                elif cmd == "/mois":                      send_monthly_report()
                elif cmd == "/annee":                     send_annual_report()
                elif cmd == "/objectifs":                 cmd_objectifs()
        except Exception as e:
            log(f"Telegram error: {e}")
        time.sleep(2)

def cmd_aide():
    send_telegram(
        "🤖 <b>Commandes :</b>\n\n"
        "📊 /status | /positions | /crypto\n"
        "/report | /historique | /marche\n"
        "/technique NVDA | /objectifs\n\n"
        "📅 /briefing | /semaine\n"
        "/mois | /annee\n\n"
        "⚙️ /pause | /resume\n"
        "/vacances | /retour\n\n"
        "🔔 /alerte NVDA 150 | /alertes\n\n"
        "🚨 /urgence — Tout vendre !"
    )

def cmd_objectifs():
    account     = get_account_info()
    checkpts    = get_equity_checkpoints()
    week_start  = checkpts.get("week", account["equity"])
    month_start = checkpts.get("month", account["equity"])
    year_start  = checkpts.get("year", account["equity"])
    week_pnl    = account["equity"] - week_start
    month_pnl   = account["equity"] - month_start
    year_pnl    = account["equity"] - year_start
    week_goal   = week_start * WEEKLY_GOAL_PCT / 100
    month_goal  = MONTHLY_GOAL_EUR * 1.08
    year_goal   = year_start * ANNUAL_GOAL_PCT / 100
    send_telegram(
        f"🎯 <b>Mes objectifs</b>\n\n"
        f"📅 <b>Semaine (+{WEEKLY_GOAL_PCT}%) :</b>\n"
        f"{progress_bar(max(week_pnl,0), week_goal)}\n"
        f"${week_pnl:+.2f} / ${week_goal:.2f}\n\n"
        f"📆 <b>Mois (+{MONTHLY_GOAL_EUR}€) :</b>\n"
        f"{progress_bar(max(month_pnl,0), month_goal)}\n"
        f"${month_pnl:+.2f} / ${month_goal:.2f}\n\n"
        f"🗓️ <b>Année (+{ANNUAL_GOAL_PCT}%) :</b>\n"
        f"{progress_bar(max(year_pnl,0), year_goal)}\n"
        f"${year_pnl:+.2f} / ${year_goal:.2f}"
    )

def cmd_status():
    account     = get_account_info()
    stats       = get_stats()
    spy         = get_spy_performance()
    checkpts    = get_equity_checkpoints()
    ms          = get_monthly_stats()
    ys          = get_annual_stats()
    week_start  = checkpts.get("week", account["equity"])
    week_pnl    = account["equity"] - week_start
    week_goal   = week_start * WEEKLY_GOAL_PCT / 100
    btc_p       = get_crypto_price("BTC-USD") or 0
    eth_p       = get_crypto_price("ETH-USD") or 0
    crypto_val  = get_crypto_balance("BTC") * btc_p + get_crypto_balance("ETH") * eth_p
    send_telegram(
        f"💼 <b>Portefeuille</b>\n\n"
        f"💰 Actions : <b>${account['equity']:.2f}</b>\n"
        f"₿ Crypto : ~${crypto_val:.2f} | Total : ~${account['equity']+crypto_val:.2f}\n"
        f"💵 Cash : ${account['cash']:.2f}\n"
        f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f}\n\n"
        f"📅 Ce mois : ${ms['pnl']:+.2f} | 📆 Année : ${ys['pnl']:+.2f}\n\n"
        f"🎯 Semaine :\n{progress_bar(max(week_pnl,0), week_goal)} ${week_pnl:+.2f}\n\n"
        f"Réussite : {stats['winrate']:.0f}% | SPY : {spy:+.2f}%\n"
        f"🤖 {'🏖️' if vacation_mode else '⏸️' if trading_paused else '✅'} | {'🟢 Ouvert' if is_market_open() else '🔴 Fermé'}"
    )

def cmd_positions():
    positions = get_positions()
    if not positions:
        send_telegram("📭 Aucune action — 100% cash.")
        return
    msg = "📌 <b>Actions :</b>\n\n"
    for s, d in positions.items():
        ta = get_technical_analysis(s)
        msg += f"{'🟢' if d['pnl_pct'] >= 0 else '🔴'} <b>{s}</b> ${d['value']:.2f} ({d['pnl_pct']:+.2f}%) 🎯+{take_profit_targets.get(s,5.0)}%\n{ta['trend'] if ta else '?'}\n\n"
    send_telegram(msg)

def cmd_crypto():
    if not coinbase:
        send_telegram("❌ Coinbase non connecté.")
        return
    btc_p = get_crypto_price("BTC-USD") or 0
    eth_p = get_crypto_price("ETH-USD") or 0
    sol_p = get_crypto_price("SOL-USD") or 0
    btc_b = get_crypto_balance("BTC")
    eth_b = get_crypto_balance("ETH")
    sol_b = get_crypto_balance("SOL")
    send_telegram(
        f"₿ <b>Mes cryptos (Coinbase)</b>\n\n"
        f"🟡 BTC : {btc_b:.6f} (~${btc_b*btc_p:.2f}) | ${btc_p:.2f}\n"
        f"🔵 ETH : {eth_b:.4f} (~${eth_b*eth_p:.2f}) | ${eth_p:.2f}\n"
        f"🟣 SOL : {sol_b:.4f} (~${sol_b*sol_p:.2f}) | ${sol_p:.2f}\n\n"
        f"💰 Total : ~${btc_b*btc_p+eth_b*eth_p+sol_b*sol_p:.2f}\n\n"
        f"🔒 BTC/ETH = accumulation | 🎯 SOL = opportuniste"
    )

def cmd_marche():
    spy  = get_spy_performance()
    intl = check_intl_markets()
    msg  = f"🌍 <b>Marchés</b>\n\n🇺🇸 SPY : {spy:+.2f}% {'🟢' if spy > 0.5 else '🔴' if spy < -0.5 else '🟡'}\n\n"
    msg += "\n".join(intl) + f"\n\n{'🟢 Ouvert' if is_market_open() else '🔴 Fermé'}"
    send_telegram(msg)

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram("⏸️ <b>Pause</b>\nStop loss actif.\nTape /resume.")

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False
    vacation_mode  = False
    send_telegram("✅ <b>Trading repris !</b>")

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions = get_positions()
    if not positions:
        send_telegram("ℹ️ Déjà 100% cash.")
        return
    send_telegram("🚨 <b>URGENCE</b>\nJe vends tout...\n⚠️ Impôts possibles.")
    for s, d in positions.items():
        place_order(s, "sell", d["qty"])
    send_telegram("✅ <b>Tout vendu.</b>\nTape /resume.")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode  = True
    trading_paused = True
    send_telegram("🏖️ <b>Mode vacances !</b>\n✅ Actions gardées\n✅ Stop loss actif\n❌ Aucun achat\n\nTape /retour !")

def cmd_retour():
    global vacation_mode, trading_paused
    vacation_mode  = False
    trading_paused = False
    send_telegram("👋 <b>Bon retour !</b>")
    send_daily_report(immediate=True)

def cmd_historique():
    stats = get_stats()
    if not stats["recent"]:
        send_telegram("📭 Aucun trade.")
        return
    msg = "📜 <b>5 derniers trades :</b>\n\n"
    for t in reversed(stats["recent"]):
        pnl = f" | ${t['pnl']:+.2f}" if t.get("pnl") else ""
        msg += f"{'✅' if t['side']=='buy' else '💰'} {t['date']} — {t['side'].upper()} <b>{t['symbol']}</b> @ ${t['price']:.2f}{pnl}\n"
    msg += f"\n🎯 {stats['winrate']:.0f}% | P&amp;L : ${stats['total_pnl']:+.2f}"
    send_telegram(msg)

def cmd_technique(ticker):
    ta    = get_technical_analysis(ticker)
    price = get_price(ticker)
    if not ta or not price:
        send_telegram(f"❌ Impossible d'analyser {ticker}.")
        return
    wr = get_symbol_winrate(ticker)
    send_telegram(f"📊 <b>{ticker}</b>\n\n💲 ${price:.2f}\n\n{format_ta(ta)}" + (f"\n🎯 Réussite : {wr:.0f}%" if wr else ""))

def cmd_alerte(args):
    try:
        symbol, target = args[0].upper(), float(args[1])
        custom_alerts[symbol] = target
        send_telegram(f"🔔 Alerte : <b>{symbol}</b> → ${target:.2f}")
    except:
        send_telegram("❌ Format : /alerte NVDA 150")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("📭 Aucune alerte.")
        return
    msg = "🔔 <b>Alertes :</b>\n\n"
    for symbol, target in custom_alerts.items():
        price = get_crypto_price(symbol) if "-USD" in symbol else get_price(symbol)
        diff  = f" (encore {abs((price-target)/target*100):.1f}%)" if price else ""
        msg  += f"📌 <b>{symbol}</b> → ${target:.2f}{diff}\n"
    send_telegram(msg)

def main():
    send_telegram(
        "🤖 <b>Trading Agent Ultimate démarré !</b>\n\n"
        "📊 VT + NVDA + MSFT + META + QQQ + XLK\n"
        "₿ BTC + ETH (Coinbase) + SOL opportuniste\n"
        "💶 DCA 100€/mois\n"
        "🛑 Stop loss -3% | 🎯 Take profit dynamique\n"
        "🎯 +1%/sem | +100€/mois | +20%/an\n"
        "📋 Briefing 15h25 | 🌙 Veille nocturne\n\n"
        "Tape /aide 👇"
    )
    account = get_account_info()
    update_equity_checkpoints(account["equity"])
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram_commands, daemon=True).start()

    briefing_sent = daily_sent = postmarket_sent = None
    weekly_sent   = monthly_sent = annual_sent = overnight_checked = None

    while True:
        now   = datetime.now()
        today = now.strftime("%Y-%m-%d")
        account = get_account_info()
        update_equity_checkpoints(account["equity"])

        if now.hour == 15 and now.minute == 25 and briefing_sent != today:
            send_premarket_briefing()
            briefing_sent = today
        if now.hour == 22 and now.minute == 5 and postmarket_sent != today:
            send_postmarket_summary()
            postmarket_sent = today
        if now.hour == 21 and now.minute < 5 and daily_sent != today:
            send_daily_report()
            daily_sent = today
        week_key = now.strftime("%Y-%W")
        if now.weekday() == 0 and now.hour == 8 and now.minute < 5 and weekly_sent != week_key:
            send_weekly_report()
            weekly_sent = week_key
        month_key = now.strftime("%Y-%m")
        if now.day == 1 and now.hour == 9 and now.minute < 5 and monthly_sent != month_key:
            run_dca()
            send_monthly_report()
            monthly_sent = month_key
        year_key = now.strftime("%Y")
        if now.month == 1 and now.day == 1 and now.hour == 10 and now.minute < 5 and annual_sent != year_key:
            send_annual_report()
            annual_sent = year_key

        check_market_health()
        check_stop_loss_take_profit()
        check_crypto_stops()
        check_dip_buying()
        check_custom_alerts()

        hour_key = now.strftime("%Y-%m-%d-%H")
        if not is_market_open() and overnight_checked != hour_key:
            news = check_overnight_news()
            if news:
                send_telegram("🌙 <b>Alerte nocturne</b>\n\n" + "\n".join(news[:3]))
            overnight_checked = hour_key

        if is_market_open():
            for ticker in ALL_ASSETS:
                analyze_ticker(ticker)

        for ticker in CRYPTO_OPP:
            analyze_ticker(ticker, is_crypto=True)

        log(f"⏳ {POLL_INTERVAL//60}min | {'OUVERT' if is_market_open() else 'fermé'}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
