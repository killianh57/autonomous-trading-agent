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
from binance.client import Client as BinanceClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# ── PORTEFEUILLE ──────────────────────────────────────────────────────────────
SAFE_ASSETS   = ["VT"]
TECH_ASSETS   = ["NVDA", "MSFT", "META"]
ETF_ASSETS    = ["QQQ", "XLK"]
STOCK_ASSETS  = SAFE_ASSETS + TECH_ASSETS + ETF_ASSETS

CRYPTO_HOLD   = ["BTCUSDT", "ETHUSDT"]   # Accumulation long terme
CRYPTO_OPP    = ["SOLUSDT"]              # Opportuniste sur catalyseurs

DCA_MONTHLY_EUR = 100
DCA_ALLOCATION  = {
    "VT": 0.20, "NVDA": 0.12, "MSFT": 0.08,
    "META": 0.08, "QQQ": 0.12, "XLK": 0.08,
    "BTCUSDT": 0.15, "ETHUSDT": 0.10, "CASH": 0.07
}

POLL_INTERVAL  = 300
STOP_LOSS_PCT  = 3.0
MEMORY_FILE    = "trade_memory.json"

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
binance        = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)

last_seen_news      = {}
take_profit_targets = {}
trading_paused      = False
vacation_mode       = False
custom_alerts       = {}

# ── MÉMOIRE ───────────────────────────────────────────────────────────────────

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {"trades": [], "stats": {"wins": 0, "losses": 0, "total_pnl": 0}, "patterns": {}, "errors": []}

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def record_trade(symbol, side, qty, price, pnl=None):
    memory = load_memory()
    memory["trades"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "symbol": symbol, "side": side, "qty": qty, "price": price, "pnl": pnl})
    if pnl is not None:
        memory["stats"]["total_pnl"] += pnl
        if pnl > 0: memory["stats"]["wins"] += 1
        else: memory["stats"]["losses"] += 1
        memory["patterns"][symbol] = memory["patterns"].get(symbol, {"wins": 0, "losses": 0})
        if pnl > 0: memory["patterns"][symbol]["wins"] += 1
        else: memory["patterns"][symbol]["losses"] += 1
    memory["trades"] = memory["trades"][-100:]
    save_memory(memory)

def record_error(msg):
    memory = load_memory()
    memory["errors"].append({"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "error": msg})
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def get_symbol_winrate(symbol):
    memory  = load_memory()
    pattern = memory["patterns"].get(symbol)
    if not pattern: return None
    total = pattern["wins"] + pattern["losses"]
    return (pattern["wins"] / total * 100) if total > 0 else None

def get_stats():
    memory = load_memory()
    stats  = memory["stats"]
    total  = stats["wins"] + stats["losses"]
    return {"wins": stats["wins"], "losses": stats["losses"], "total_pnl": stats["total_pnl"], "winrate": (stats["wins"] / total * 100) if total > 0 else 0, "recent": memory["trades"][-5:]}

# ── ANALYSE TECHNIQUE ─────────────────────────────────────────────────────────

def get_historical_prices(ticker, days=60):
    try:
        req  = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=days))
        return [bar.close for bar in data_client.get_stock_bars(req)[ticker]]
    except:
        return []

def get_crypto_prices(symbol, days=60):
    try:
        klines = binance.get_historical_klines(symbol, BinanceClient.KLINE_INTERVAL_1DAY, f"{days} day ago UTC")
        return [float(k[4]) for k in klines]
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

def get_technical_analysis(ticker, is_crypto=False):
    prices = get_crypto_prices(ticker) if is_crypto else get_historical_prices(ticker)
    if not prices or len(prices) < 20: return None
    rsi  = calculate_rsi(prices)
    ma20 = sum(prices[-20:]) / 20
    ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else None
    cur  = prices[-1]
    return {"rsi": rsi, "ma20": ma20, "ma50": ma50, "current": cur, "trend": "haussier 📈" if (ma20 and ma50 and ma20 > ma50) else "baissier 📉", "above_ma20": cur > ma20, "above_ma50": cur > ma50 if ma50 else None}

def format_ta(ta):
    if not ta: return "Indisponible"
    rsi_txt = f"RSI {ta['rsi']} {'⬇️ Survendu' if ta['rsi'] < 30 else '⬆️ Suracheté' if ta['rsi'] > 70 else '➡️ Neutre'}" if ta["rsi"] else ""
    return f"{rsi_txt}\nTendance : {ta['trend']}\nPrix vs MA20 : {'✅ Au-dessus' if ta['above_ma20'] else '⚠️ En-dessous'}"

# ── UTILITAIRES ───────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
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

# ── PRIX ──────────────────────────────────────────────────────────────────────

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
    except:
        return None

def get_crypto_price(symbol):
    try:
        return float(binance.get_symbol_ticker(symbol=symbol)["price"])
    except:
        return None

def get_spy_performance():
    try:
        current   = data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols="SPY"))["SPY"].close
        bars_list = list(data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=2)))["SPY"])
        return ((current - bars_list[-2].close) / bars_list[-2].close) * 100 if len(bars_list) >= 2 else 0
    except:
        return 0

# ── COMPTES ───────────────────────────────────────────────────────────────────

def get_account_info():
    account = trading_client.get_account()
    return {"equity": float(account.equity), "cash": float(account.cash), "pnl": float(account.equity) - float(account.last_equity)}

def get_positions():
    return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value), "avg_price": float(p.avg_entry_price), "pnl": float(p.unrealized_pl), "pnl_pct": float(p.unrealized_plpc) * 100} for p in trading_client.get_all_positions()}

def get_binance_balance():
    try:
        balances = binance.get_account()["balances"]
        result   = {}
        for b in balances:
            free = float(b["free"])
            if free > 0:
                result[b["asset"]] = free
        return result
    except:
        return {}

# ── ORDRES ────────────────────────────────────────────────────────────────────

def place_stock_order(symbol, side, qty, take_profit_pct=None):
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=round(qty, 4), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY))
        price  = get_price(symbol)
        valeur = round(qty * price, 2) if price else "?"
        record_trade(symbol, side, round(qty, 4), price or 0)
        if side == "buy":
            if take_profit_pct: take_profit_targets[symbol] = take_profit_pct
            send_telegram(f"✅ <b>Achat action</b>\n\nAction : <b>{symbol}</b>\nMontant : ~${valeur}\n🛑 Stop loss : -{STOP_LOSS_PCT}%\n🎯 Objectif : +{take_profit_pct or 5}%")
        else:
            if symbol in take_profit_targets: del take_profit_targets[symbol]
            send_telegram(f"✅ <b>Vente action</b>\n\nAction : <b>{symbol}</b>\nMontant : ~${valeur}")
    except Exception as e:
        record_error(f"Stock order failed {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre action échoué</b>\n{symbol}\n{str(e)}")

def place_crypto_order(symbol, side, amount_usd, take_profit_pct=None):
    """Trade crypto sur Binance. amount_usd = montant en dollars."""
    try:
        price = get_crypto_price(symbol)
        if not price: return
        qty   = amount_usd / price
        # Binance attend la quantité en base asset (ex: BTC pour BTCUSDT)
        if side == "buy":
            order = binance.order_market_buy(symbol=symbol, quoteOrderQty=round(amount_usd, 2))
        else:
            order = binance.order_market_sell(symbol=symbol, quantity=round(qty, 6))
        record_trade(symbol, side, round(qty, 6), price)
        if side == "buy":
            if take_profit_pct: take_profit_targets[symbol] = take_profit_pct
            send_telegram(f"✅ <b>Achat crypto</b>\n\n💎 <b>{symbol}</b>\nMontant : ~${amount_usd:.2f}\nPrix : ${price:.2f}\n🛑 Stop loss : -{STOP_LOSS_PCT}%\n🎯 Objectif : +{take_profit_pct or 10}%")
        else:
            if symbol in take_profit_targets: del take_profit_targets[symbol]
            send_telegram(f"✅ <b>Vente crypto</b>\n\n💎 <b>{symbol}</b>\nMontant : ~${amount_usd:.2f}")
    except Exception as e:
        record_error(f"Crypto order failed {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre crypto échoué</b>\n{symbol}\n{str(e)}")

# ── STOP LOSS / TAKE PROFIT ───────────────────────────────────────────────────

def check_stop_loss_take_profit():
    # Actions Alpaca
    for symbol, data in get_positions().items():
        pnl_pct = data["pnl_pct"]
        tp_pct  = take_profit_targets.get(symbol, 5.0)
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(f"🛑 <b>Stop loss déclenché</b>\n\n<b>{symbol}</b> a perdu {abs(pnl_pct):.1f}%")
            place_stock_order(symbol, "sell", data["qty"])
        elif pnl_pct >= tp_pct:
            send_telegram(f"🎯 <b>Objectif atteint !</b>\n\n<b>{symbol}</b> a gagné {pnl_pct:.1f}%")
            place_stock_order(symbol, "sell", data["qty"])

    # Crypto Binance (stop loss uniquement sur opportuniste SOL)
    for symbol in CRYPTO_OPP:
        prices = get_crypto_prices(symbol, days=3)
        if len(prices) < 2: continue
        entry  = take_profit_targets.get(f"{symbol}_entry", prices[0])
        cur    = prices[-1]
        pnl_pct = ((cur - entry) / entry) * 100
        tp_pct  = take_profit_targets.get(symbol, 10.0)
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(f"🛑 <b>Stop loss crypto</b>\n\n<b>{symbol}</b> a perdu {abs(pnl_pct):.1f}%")
            balances = get_binance_balance()
            asset    = symbol.replace("USDT", "")
            qty      = balances.get(asset, 0)
            if qty > 0: place_crypto_order(symbol, "sell", qty * cur)
        elif pnl_pct >= tp_pct:
            send_telegram(f"🎯 <b>Take profit crypto !</b>\n\n<b>{symbol}</b> a gagné {pnl_pct:.1f}%")
            balances = get_binance_balance()
            asset    = symbol.replace("USDT", "")
            qty      = balances.get(asset, 0)
            if qty > 0: place_crypto_order(symbol, "sell", qty * cur)

# ── RACHAT SUR BAISSE ─────────────────────────────────────────────────────────

def check_dip_buying():
    account = get_account_info()
    cash    = account["cash"]
    reserve = account["equity"] * 0.10

    # Actions
    for ticker in STOCK_ASSETS:
        prices = get_historical_prices(ticker, days=7)
        if len(prices) < 2: continue
        dip = ((prices[-1] - max(prices[:-1])) / max(prices[:-1])) * 100
        if dip <= -20 and cash >= reserve * 0.3:
            send_telegram(f"📉 <b>Grosse baisse !</b>\n<b>{ticker}</b> chute de {abs(dip):.1f}%\nJ'achète double !")
            place_stock_order(ticker, "buy", (reserve * 0.3) / prices[-1])
        elif dip <= -10 and cash >= reserve * 0.15:
            send_telegram(f"📉 <b>Baisse détectée</b>\n<b>{ticker}</b> baisse de {abs(dip):.1f}%\nJe rachète.")
            place_stock_order(ticker, "buy", (reserve * 0.15) / prices[-1])

    # Crypto BTC/ETH — accumulation sur baisse
    for symbol in CRYPTO_HOLD:
        prices = get_crypto_prices(symbol, days=7)
        if len(prices) < 2: continue
        dip = ((prices[-1] - max(prices[:-1])) / max(prices[:-1])) * 100
        if dip <= -20:
            send_telegram(f"📉 <b>Grosse baisse crypto !</b>\n<b>{symbol}</b> chute de {abs(dip):.1f}%\nJ'accumule !")
            place_crypto_order(symbol, "buy", 20)
        elif dip <= -10:
            send_telegram(f"📉 <b>Baisse crypto</b>\n<b>{symbol}</b> baisse de {abs(dip):.1f}%\nJ'achète un peu.")
            place_crypto_order(symbol, "buy", 10)

# ── NEWS ──────────────────────────────────────────────────────────────────────

def get_news(ticker):
    try:
        query = ticker.replace("USDT", "")
        return requests.get(f"https://newsapi.org/v2/everything?q={query}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}", timeout=10).json().get("articles", [])
    except:
        return []

def has_new_news(ticker, articles):
    if not articles: return False
    latest = articles[0].get("publishedAt", "")
    if last_seen_news.get(ticker) != latest:
        last_seen_news[ticker] = latest
        return True
    return False

# ── ANALYSE CLAUDE ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu es un trader professionnel Smart Money et crypto.
Règles : RR 1:2 minimum, max 2% par trade actions, max 5% par trade crypto.
Tu analyses : prix + news + RSI + tendance + historique de tes trades.
Si tu as perdu récemment sur ce ticker, sois plus prudent.

Réponds UNIQUEMENT en JSON :
{
  "action": "BUY"|"SELL"|"HOLD",
  "confidence": 0-100,
  "reason": "explication courte en français",
  "risk_percent": 1-2,
  "take_profit_pct": 5-30
}"""

def analyze_with_claude(ticker, price, news_txt, ta_summary, winrate, is_crypto=False):
    try:
        import json
        wr_ctx = f"\nTaux de réussite sur {ticker} : {winrate:.0f}% — {'sois prudent' if winrate < 40 else 'bonne track record'}" if winrate else ""
        crypto_ctx = "\n⚠️ C'est du crypto — plus volatile, take profit plus élevé possible." if is_crypto else ""
        res = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Ticker:{ticker}\nPrix:${price}\nNews:\n{news_txt}\nAnalyse technique:\n{ta_summary}{wr_ctx}{crypto_ctx}"}]
        )
        text = res.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        record_error(f"Claude error {ticker}: {e}")
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur", "risk_percent": 0, "take_profit_pct": 5}

def analyze_ticker(ticker, is_crypto=False):
    if trading_paused or vacation_mode: return
    price    = get_crypto_price(ticker) if is_crypto else get_price(ticker)
    if not price: return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles): return
    ta       = get_technical_analysis(ticker, is_crypto=is_crypto)
    winrate  = get_symbol_winrate(ticker)
    signal   = analyze_with_claude(ticker, price, "\n".join([f"- {a['title']}" for a in articles[:5]]), format_ta(ta), winrate, is_crypto=is_crypto)
    action, conf, reason, tp_pct = signal.get("action","HOLD"), signal.get("confidence",0), signal.get("reason",""), signal.get("take_profit_pct",5)
    if conf < 65: return
    account = get_account_info()

    if is_crypto and ticker in CRYPTO_OPP:
        # SOL — opportuniste seulement
        if action == "BUY":
            send_telegram(f"💡 <b>Signal crypto</b>\n\n💎 <b>{ticker}</b>\nRaison : {reason}\nConfiance : {conf}%\nObjectif : +{tp_pct}%")
            place_crypto_order(ticker, "buy", account["equity"] * 0.05, take_profit_pct=tp_pct)
    elif not is_crypto:
        if action == "BUY":
            qty = (account["equity"] * signal.get("risk_percent",1) / 100) / price
            if qty * price >= 1:
                send_telegram(f"💡 <b>Signal d'achat</b>\n\nAction : <b>{ticker}</b>\nRaison : {reason}\nConfiance : {conf}%\nObjectif : +{tp_pct}%")
                place_stock_order(ticker, "buy", qty, take_profit_pct=tp_pct)
        elif action == "SELL":
            pos = get_positions()
            if ticker in pos:
                place_stock_order(ticker, "sell", pos[ticker]["qty"])

# ── DCA ───────────────────────────────────────────────────────────────────────

def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("⏸️ DCA annulé — trading en pause.")
        return
    send_telegram("💰 <b>Investissement mensuel (DCA)</b>\n\nJ'achète tes actifs du mois...")
    account = get_account_info()
    dca_usd = DCA_MONTHLY_EUR * 1.08
    if account["cash"] < dca_usd * 0.85:
        send_telegram(f"⚠️ Pas assez de cash. Il faut ~${dca_usd:.0f}.")
        return
    for ticker, alloc in DCA_ALLOCATION.items():
        if ticker == "CASH": continue
        amount = dca_usd * alloc
        if ticker in ["BTCUSDT", "ETHUSDT"]:
            place_crypto_order(ticker, "buy", amount)
        else:
            price = get_price(ticker)
            if price: place_stock_order(ticker, "buy", amount / price)

# ── RAPPORTS ──────────────────────────────────────────────────────────────────

def send_daily_report(immediate=False):
    account   = get_account_info()
    positions = get_positions()
    stats     = get_stats()
    spy       = get_spy_performance()
    balances  = get_binance_balance()
    btc_price = get_crypto_price("BTCUSDT") or 0
    eth_price = get_crypto_price("ETHUSDT") or 0
    crypto_val = (balances.get("BTC", 0) * btc_price) + (balances.get("ETH", 0) * eth_price)
    titre     = "📊 <b>Rapport immédiat</b>" if immediate else "📊 <b>Rapport du soir</b>"
    report    = f"{titre}\n{'='*20}\n\n"
    report   += f"💼 <b>Actions (Alpaca)</b>\n"
    report   += f"💰 Valeur : ${account['equity']:.2f}\n"
    report   += f"💵 Cash : ${account['cash']:.2f}\n"
    report   += f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f}\n\n"
    report   += f"₿ <b>Crypto (Binance)</b>\n"
    report   += f"BTC : {balances.get('BTC', 0):.6f} (~${balances.get('BTC', 0) * btc_price:.2f})\n"
    report   += f"ETH : {balances.get('ETH', 0):.4f} (~${balances.get('ETH', 0) * eth_price:.2f})\n"
    report   += f"Total crypto : ~${crypto_val:.2f}\n\n"
    report   += f"💹 <b>Total portefeuille : ~${account['equity'] + crypto_val:.2f}</b>\n\n"
    if positions:
        report += "📌 <b>Actions ouvertes :</b>\n"
        for symbol, data in positions.items():
            report += f"{'🟢' if data['pnl_pct'] >= 0 else '🔴'} <b>{symbol}</b> ${data['value']:.2f} ({data['pnl_pct']:+.2f}%)\n"
    report += f"\n🎯 Réussite : {stats['winrate']:.0f}% | P&amp;L : ${stats['total_pnl']:+.2f}\n"
    report += f"🌍 Marché : {spy:+.2f}%\n"
    report += f"🤖 Mode : {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    send_telegram(report)

def send_weekly_report():
    account   = get_account_info()
    stats     = get_stats()
    spy       = get_spy_performance()
    vs_spy    = account["pnl"] - (account["equity"] * spy / 100)
    balances  = get_binance_balance()
    btc_price = get_crypto_price("BTCUSDT") or 0
    eth_price = get_crypto_price("ETHUSDT") or 0
    crypto_val = (balances.get("BTC", 0) * btc_price) + (balances.get("ETH", 0) * eth_price)
    send_telegram(
        f"📅 <b>Résumé de la semaine</b>\n\n"
        f"💰 Actions : ${account['equity']:.2f}\n"
        f"₿ Crypto : ~${crypto_val:.2f}\n"
        f"💹 Total : ~${account['equity'] + crypto_val:.2f}\n\n"
        f"🎯 Taux de réussite : {stats['winrate']:.0f}%\n"
        f"✅ {stats['wins']} gagnants | ❌ {stats['losses']} perdants\n"
        f"💹 P&amp;L total : ${stats['total_pnl']:+.2f}\n\n"
        f"📊 Moi vs marché : {'✅ Je bats le SPY !' if vs_spy > 0 else '📉 Le marché me bat'}\n\n"
        f"Bonne semaine ! 💪"
    )

def send_monthly_fiscal():
    stats = get_stats()
    send_telegram(
        f"🧾 <b>Résumé fiscal du mois</b>\n\n"
        f"💹 P&amp;L réalisé : <b>${stats['total_pnl']:+.2f}</b>\n\n"
        f"{'📈 Gains à déclarer (flat tax 30% en France)' if stats['total_pnl'] > 0 else '📉 Pertes — rien à déclarer ce mois'}\n\n"
        f"⚠️ Consulte un comptable pour ta déclaration officielle."
    )

# ── COMMANDES TELEGRAM ────────────────────────────────────────────────────────

def handle_telegram_commands():
    last_update_id = None
    while True:
        try:
            res  = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"timeout": 30, "offset": last_update_id}, timeout=35)
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
        except Exception as e:
            log(f"Telegram error: {e}")
        time.sleep(2)

def cmd_aide():
    send_telegram(
        "🤖 <b>Commandes disponibles :</b>\n\n"
        "📊 <b>Infos</b>\n"
        "/status — Portefeuille complet\n"
        "/positions — Actions en cours\n"
        "/crypto — Mes cryptos\n"
        "/report — Rapport maintenant\n"
        "/historique — Derniers trades\n"
        "/technique NVDA — Analyse technique\n"
        "/marche — Santé du marché\n\n"
        "⚙️ <b>Contrôle</b>\n"
        "/pause — Arrêter le trading\n"
        "/resume — Reprendre\n"
        "/vacances — Mode prudent\n"
        "/retour — Fin vacances\n\n"
        "🔔 /alerte NVDA 150\n"
        "/alertes — Voir alertes actives\n\n"
        "🚨 /urgence — Tout vendre !"
    )

def cmd_status():
    account   = get_account_info()
    stats     = get_stats()
    spy       = get_spy_performance()
    balances  = get_binance_balance()
    btc_price = get_crypto_price("BTCUSDT") or 0
    eth_price = get_crypto_price("ETHUSDT") or 0
    crypto_val = (balances.get("BTC", 0) * btc_price) + (balances.get("ETH", 0) * eth_price)
    send_telegram(
        f"💼 <b>Portefeuille complet</b>\n\n"
        f"📈 Actions : ${account['equity']:.2f}\n"
        f"₿ Crypto : ~${crypto_val:.2f}\n"
        f"💰 <b>Total : ~${account['equity'] + crypto_val:.2f}</b>\n"
        f"💵 Cash dispo : ${account['cash']:.2f}\n"
        f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : ${account['pnl']:+.2f}\n\n"
        f"🎯 Réussite : {stats['winrate']:.0f}% ({stats['wins']}✅/{stats['losses']}❌)\n"
        f"💹 P&amp;L total : ${stats['total_pnl']:+.2f}\n"
        f"🌍 SPY : {spy:+.2f}%\n\n"
        f"🤖 Mode : {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    )

def cmd_positions():
    positions = get_positions()
    if not positions:
        send_telegram("📭 Aucune action en ce moment.")
        return
    msg = "📌 <b>Actions en cours :</b>\n\n"
    for symbol, data in positions.items():
        ta  = get_technical_analysis(symbol)
        msg += f"{'🟢' if data['pnl_pct'] >= 0 else '🔴'} <b>{symbol}</b>\n   ${data['value']:.2f} | {data['pnl_pct']:+.2f}% | 🎯 +{take_profit_targets.get(symbol, 5.0)}%\n   Tendance : {ta['trend'] if ta else '?'}\n\n"
    send_telegram(msg)

def cmd_crypto():
    balances  = get_binance_balance()
    btc_price = get_crypto_price("BTCUSDT") or 0
    eth_price = get_crypto_price("ETHUSDT") or 0
    sol_price = get_crypto_price("SOLUSDT") or 0
    btc_val   = balances.get("BTC", 0) * btc_price
    eth_val   = balances.get("ETH", 0) * eth_price
    sol_val   = balances.get("SOL", 0) * sol_price
    send_telegram(
        f"₿ <b>Mes cryptos (Binance)</b>\n\n"
        f"🟡 BTC : {balances.get('BTC', 0):.6f} (~${btc_val:.2f})\n"
        f"   Prix : ${btc_price:.2f}\n\n"
        f"🔵 ETH : {balances.get('ETH', 0):.4f} (~${eth_val:.2f})\n"
        f"   Prix : ${eth_price:.2f}\n\n"
        f"🟣 SOL : {balances.get('SOL', 0):.4f} (~${sol_val:.2f})\n"
        f"   Prix : ${sol_price:.2f}\n\n"
        f"💰 Total crypto : ~${btc_val + eth_val + sol_val:.2f}\n\n"
        f"🔒 BTC/ETH = accumulation long terme\n"
        f"🎯 SOL = opportuniste sur catalyseurs"
    )

def cmd_marche():
    spy = get_spy_performance()
    send_telegram(
        f"🌍 <b>Santé du marché</b>\n\n"
        f"SPY (US) : {spy:+.2f}%\n"
        f"{'🟢 Marché haussier' if spy > 0.5 else '🔴 Marché baissier' if spy < -0.5 else '🟡 Marché neutre'}\n\n"
        f"{'⚠️ Je reste prudent' if spy < -3 else '✅ Je continue à surveiller'}"
    )

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram("⏸️ <b>Trading mis en pause</b>\n\nPlus aucun achat.\nStop loss toujours actif.\n\nTape /resume pour reprendre.")

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False
    vacation_mode  = False
    send_telegram("✅ <b>Trading repris !</b>")

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions = get_positions()
    balances  = get_binance_balance()
    if not positions and not any(balances.get(a, 0) > 0 for a in ["BTC", "ETH", "SOL"]):
        send_telegram("ℹ️ Déjà 100% en cash.")
        return
    send_telegram("🚨 <b>URGENCE</b>\n\nJe vends tout...\n⚠️ Impôts possibles sur les gains.")
    for symbol, data in positions.items():
        place_stock_order(symbol, "sell", data["qty"])
    for symbol, asset in [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]:
        qty = balances.get(asset, 0)
        if qty > 0:
            price = get_crypto_price(symbol) or 0
            place_crypto_order(symbol, "sell", qty * price)
    send_telegram("✅ <b>Tout vendu.</b>\nTape /resume pour reprendre.")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode  = True
    trading_paused = True
    send_telegram("🏖️ <b>Mode vacances !</b>\n\n✅ Actifs gardés\n✅ Stop loss actif\n❌ Aucun achat\n❌ DCA suspendu\n\nTape /retour quand tu reviens !")

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
    is_crypto = "USDT" in ticker
    send_telegram(f"🔍 Analyse de <b>{ticker}</b>...")
    ta    = get_technical_analysis(ticker, is_crypto=is_crypto)
    price = get_crypto_price(ticker) if is_crypto else get_price(ticker)
    if not ta or not price:
        send_telegram(f"❌ Impossible d'analyser {ticker}.")
        return
    send_telegram(f"📊 <b>{ticker}</b>\n\n💲 ${price:.2f}\n\n{format_ta(ta)}")

def cmd_alerte(args):
    try:
        symbol, target = args[0].upper(), float(args[1])
        custom_alerts[symbol] = target
        send_telegram(f"🔔 Alerte : <b>{symbol}</b> à <b>${target:.2f}</b>")
    except:
        send_telegram("❌ Format : /alerte BTC 70000")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("📭 Aucune alerte.")
        return
    msg = "🔔 <b>Alertes actives :</b>\n\n"
    for symbol, target in custom_alerts.items():
        msg += f"📌 <b>{symbol}</b> → ${target:.2f}\n"
    send_telegram(msg)

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        is_crypto = "USDT" in symbol
        price = get_crypto_price(symbol) if is_crypto else get_price(symbol)
        if price and price >= target:
            send_telegram(f"🔔 <b>ALERTE !</b>\n<b>{symbol}</b> a atteint ${price:.2f} ✅")
            del custom_alerts[symbol]

def check_market_health():
    global trading_paused
    spy = get_spy_performance()
    if spy <= -10:
        send_telegram(f"🚨 <b>CRASH !</b>\nSPY : {spy:.1f}%\nTape /urgence ou /pause.")
    elif spy <= -5:
        trading_paused = True
        send_telegram(f"⚠️ <b>Forte baisse</b>\nSPY : {spy:.1f}%\nTrading en pause.")
    elif spy <= -3:
        send_telegram(f"📉 Marché sous tension ({spy:.1f}%)")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    send_telegram(
        "🤖 <b>Trading Agent Ultimate démarré !</b>\n\n"
        "📊 <b>Portefeuille :</b>\n"
        "🛡️ VT — base stable\n"
        "🤖 NVDA/MSFT/META — IA & Tech\n"
        "📈 QQQ/XLK — ETF sectoriels\n"
        "₿ BTC/ETH — accumulation crypto\n"
        "🎯 SOL — opportuniste\n\n"
        "💶 DCA 100€/mois\n"
        "🛑 Stop loss -3%\n"
        "🎯 Take profit dynamique\n"
        "📉 Rachat auto sur baisse\n"
        "📊 RSI + tendances\n"
        "🧠 Mémoire des trades\n\n"
        "Tape /aide 👇"
    )
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram_commands, daemon=True).start()

    while True:
        now = datetime.now()
        if now.day == 1 and now.hour == 16 and now.minute < 5:
            run_dca()
            send_monthly_fiscal()
        if now.hour == 21 and now.minute < 5:
            send_daily_report()
        if now.weekday() == 0 and now.hour == 8 and now.minute < 5:
            send_weekly_report()
        check_market_health()
        check_stop_loss_take_profit()
        check_dip_buying()
        check_custom_alerts()
        for ticker in STOCK_ASSETS:
            analyze_ticker(ticker)
        for ticker in CRYPTO_OPP:
            analyze_ticker(ticker, is_crypto=True)
        log(f"⏳ Prochain check dans {POLL_INTERVAL//60} min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
