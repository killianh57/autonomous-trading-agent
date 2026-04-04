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
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

SAFE_ASSETS   = ["VT"]
TECH_ASSETS   = ["NVDA", "MSFT", "META"]
ETF_ASSETS    = ["QQQ", "XLK"]
ALL_ASSETS    = SAFE_ASSETS + TECH_ASSETS + ETF_ASSETS

DCA_MONTHLY_EUR = 100
DCA_ALLOCATION  = {
    "VT": 0.25, "NVDA": 0.15, "MSFT": 0.10,
    "META": 0.10, "QQQ": 0.15, "XLK": 0.10,
    "CASH": 0.15
}

POLL_INTERVAL  = 300
STOP_LOSS_PCT  = 3.0
MEMORY_FILE    = "trade_memory.json"

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

last_seen_news      = {}
take_profit_targets = {}
trading_paused      = False
vacation_mode       = False
custom_alerts       = {}

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
            "above_ma20": cur > ma20, "above_ma50": cur > ma50 if ma50 else None}

def format_ta(ta):
    if not ta: return "Indisponible"
    rsi_txt = f"RSI {ta['rsi']} {'⬇️ Survendu' if ta['rsi'] < 30 else '⬆️ Suracheté' if ta['rsi'] > 70 else '➡️ Neutre'}" if ta["rsi"] else ""
    return f"{rsi_txt}\nTendance : {ta['trend']}\nMA20 : {'✅ Au-dessus' if ta['above_ma20'] else '⚠️ En-dessous'}"

def get_account_info():
    account = trading_client.get_account()
    return {"equity": float(account.equity), "cash": float(account.cash), "pnl": float(account.equity) - float(account.last_equity)}

def get_positions():
    return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value), "avg_price": float(p.avg_entry_price), "pnl": float(p.unrealized_pl), "pnl_pct": float(p.unrealized_plpc) * 100} for p in trading_client.get_all_positions()}

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))[ticker].close
    except:
        return None

def get_spy_performance():
    try:
        current   = data_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols="SPY"))["SPY"].close
        bars_list = list(data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=2)))["SPY"])
        return ((current - bars_list[-2].close) / bars_list[-2].close) * 100 if len(bars_list) >= 2 else 0
    except:
        return 0

def get_news(ticker):
    try:
        return requests.get(f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}", timeout=10).json().get("articles", [])
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
        trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=round(qty, 4), side=OrderSide.BUY if side == "buy" else OrderSide.SELL, time_in_force=TimeInForce.DAY))
        price  = get_price(symbol)
        valeur = round(qty * price, 2) if price else "?"
        record_trade(symbol, side, round(qty, 4), price or 0)
        if side == "buy":
            if take_profit_pct: take_profit_targets[symbol] = take_profit_pct
            send_telegram(f"✅ <b>Achat effectué</b>\n\nAction : <b>{symbol}</b>\nMontant : ~${valeur}\n🛑 Stop loss : -{STOP_LOSS_PCT}%\n🎯 Objectif : +{take_profit_pct or 5}%")
        else:
            if symbol in take_profit_targets: del take_profit_targets[symbol]
            send_telegram(f"✅ <b>Vente effectuée</b>\n\nAction : <b>{symbol}</b>\nMontant : ~${valeur}")
    except Exception as e:
        record_error(f"Order failed {symbol}: {e}")
        send_telegram(f"❌ <b>Ordre échoué</b>\n{symbol} {side.upper()}\n{str(e)}")

def check_stop_loss_take_profit():
    for symbol, data in get_positions().items():
        pnl_pct = data["pnl_pct"]
        tp_pct  = take_profit_targets.get(symbol, 5.0)
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(f"🛑 <b>Stop loss déclenché</b>\n\n<b>{symbol}</b> a perdu {abs(pnl_pct):.1f}%\nJe vends pour limiter la perte.")
            place_order(symbol, "sell", data["qty"])
        elif pnl_pct >= tp_pct:
            send_telegram(f"🎯 <b>Objectif atteint !</b>\n\n<b>{symbol}</b> a gagné {pnl_pct:.1f}%\nJe sécurise le gain.")
            place_order(symbol, "sell", data["qty"])

def check_dip_buying():
    account = get_account_info()
    reserve = account["equity"] * 0.10
    for ticker in ALL_ASSETS:
        prices = get_historical_prices(ticker, days=7)
        if len(prices) < 2: continue
        dip = ((prices[-1] - max(prices[:-1])) / max(prices[:-1])) * 100
        if dip <= -20 and account["cash"] >= reserve * 0.3:
            send_telegram(f"📉 <b>Grosse baisse !</b>\n<b>{ticker}</b> chute de {abs(dip):.1f}%\nJ'achète double !")
            place_order(ticker, "buy", (reserve * 0.3) / prices[-1])
        elif dip <= -10 and account["cash"] >= reserve * 0.15:
            send_telegram(f"📉 <b>Baisse détectée</b>\n<b>{ticker}</b> baisse de {abs(dip):.1f}%\nJe rachète.")
            place_order(ticker, "buy", (reserve * 0.15) / prices[-1])

def check_market_health():
    global trading_paused
    spy = get_spy_performance()
    if spy <= -10:
        send_telegram(f"🚨 <b>CRASH !</b>\nSPY : {spy:.1f}%\nTape /urgence ou /pause.")
    elif spy <= -5:
        trading_paused = True
        send_telegram(f"⚠️ <b>Forte baisse</b>\nSPY : {spy:.1f}%\nTrading en pause automatiquement.")
    elif spy <= -3:
        send_telegram(f"📉 Marché sous tension ({spy:.1f}%)\nJe reste prudent.")

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        price = get_price(symbol)
        if price and price >= target:
            send_telegram(f"🔔 <b>ALERTE !</b>\n<b>{symbol}</b> a atteint ${price:.2f} ✅")
            del custom_alerts[symbol]

SYSTEM_PROMPT = """Tu es un trader professionnel Smart Money.
Règles : RR 1:2 minimum, max 2% par trade, suivre le trend dominant.
Si tu as perdu récemment sur ce ticker, sois plus prudent.
Réponds UNIQUEMENT en JSON :
{"action":"BUY"|"SELL"|"HOLD","confidence":0-100,"reason":"français court","risk_percent":1-2,"take_profit_pct":5-30}
take_profit_pct : news mineure=5-8%, breakout=10-15%, catalyseur majeur=15-30%"""

def analyze_with_claude(ticker, price, news_txt, ta_summary, winrate):
    try:
        import json
        wr_ctx = f"\nTaux de réussite sur {ticker} : {winrate:.0f}% — {'sois prudent' if winrate < 40 else 'bonne track record'}" if winrate else ""
        res = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Ticker:{ticker}\nPrix:${price}\nNews:\n{news_txt}\nAnalyse technique:\n{ta_summary}{wr_ctx}"}]
        )
        text = res.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        record_error(f"Claude error {ticker}: {e}")
        return {"action":"HOLD","confidence":0,"reason":"Erreur","risk_percent":0,"take_profit_pct":5}

def analyze_ticker(ticker):
    if trading_paused or vacation_mode: return
    price    = get_price(ticker)
    if not price: return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles): return
    ta       = get_technical_analysis(ticker)
    winrate  = get_symbol_winrate(ticker)
    signal   = analyze_with_claude(ticker, price, "\n".join([f"- {a['title']}" for a in articles[:5]]), format_ta(ta), winrate)
    action, conf, reason, tp_pct = signal.get("action","HOLD"), signal.get("confidence",0), signal.get("reason",""), signal.get("take_profit_pct",5)
    if conf < 65: return
    account = get_account_info()
    if action == "BUY":
        qty = (account["equity"] * signal.get("risk_percent",1) / 100) / price
        if qty * price >= 1:
            send_telegram(f"💡 <b>Signal d'achat</b>\n\nAction : <b>{ticker}</b>\nRaison : {reason}\nConfiance : {conf}%\nObjectif : +{tp_pct}%\nTendance : {ta['trend'] if ta else '?'}")
            place_order(ticker, "buy", qty, take_profit_pct=tp_pct)
    elif action == "SELL":
        pos = get_positions()
        if ticker in pos:
            send_telegram(f"💡 <b>Signal de vente</b>\n\nAction : <b>{ticker}</b>\nRaison : {reason}\nConfiance : {conf}%")
            place_order(ticker, "sell", pos[ticker]["qty"])

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
        price = get_price(ticker)
        if price: place_order(ticker, "buy", (dca_usd * alloc) / price)

def send_daily_report(immediate=False):
    account   = get_account_info()
    positions = get_positions()
    stats     = get_stats()
    spy       = get_spy_performance()
    titre     = "📊 <b>Rapport immédiat</b>" if immediate else "📊 <b>Rapport du soir</b>"
    report    = f"{titre}\n{'='*20}\n\n"
    report   += f"💰 Valeur : <b>${account['equity']:.2f}</b>\n"
    report   += f"💵 Cash : <b>${account['cash']:.2f}</b>\n"
    report   += f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : <b>${account['pnl']:+.2f}</b>\n"
    report   += f"🌍 SPY : {spy:+.2f}%\n\n"
    if positions:
        report += "📌 <b>Actions :</b>\n"
        for symbol, data in positions.items():
            report += f"{'🟢' if data['pnl_pct'] >= 0 else '🔴'} <b>{symbol}</b> ${data['value']:.2f} ({data['pnl_pct']:+.2f}%) | 🎯 +{take_profit_targets.get(symbol, 5.0)}%\n"
    else:
        report += "📭 100% cash\n"
    report += f"\n🎯 Réussite : {stats['winrate']:.0f}% ({stats['wins']}✅/{stats['losses']}❌)\n"
    report += f"💹 P&amp;L total : ${stats['total_pnl']:+.2f}\n"
    report += f"🤖 Mode : {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    send_telegram(report)

def send_weekly_report():
    account = get_account_info()
    stats   = get_stats()
    spy     = get_spy_performance()
    vs_spy  = account["pnl"] - (account["equity"] * spy / 100)
    send_telegram(
        f"📅 <b>Résumé de la semaine</b>\n\n"
        f"💰 Portefeuille : <b>${account['equity']:.2f}</b>\n"
        f"💹 P&amp;L total : <b>${stats['total_pnl']:+.2f}</b>\n"
        f"🎯 Taux de réussite : <b>{stats['winrate']:.0f}%</b>\n"
        f"✅ {stats['wins']} gagnants | ❌ {stats['losses']} perdants\n\n"
        f"📊 Moi vs SPY : {'✅ Je bats le marché !' if vs_spy > 0 else '📉 Le marché me bat'}\n\n"
        f"Bonne semaine ! 💪"
    )

def send_monthly_fiscal():
    stats = get_stats()
    send_telegram(
        f"🧾 <b>Résumé fiscal du mois</b>\n\n"
        f"💹 P&amp;L réalisé : <b>${stats['total_pnl']:+.2f}</b>\n\n"
        f"{'📈 Gains à déclarer (flat tax 30%)' if stats['total_pnl'] > 0 else '📉 Pertes — rien à déclarer'}\n\n"
        f"⚠️ Consulte un comptable pour ta déclaration."
    )

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
        "/report — Rapport maintenant\n"
        "/historique — Derniers trades\n"
        "/technique NVDA — Analyse technique\n"
        "/marche — Santé du marché\n\n"
        "⚙️ <b>Contrôle</b>\n"
        "/pause — Arrêter le trading\n"
        "/resume — Reprendre\n"
        "/vacances — Mode prudent\n"
        "/retour — Fin vacances\n\n"
        "🔔 <b>Alertes</b>\n"
        "/alerte NVDA 150 — Alerte prix\n"
        "/alertes — Voir alertes actives\n\n"
        "🚨 /urgence — Tout vendre !"
    )

def cmd_status():
    account = get_account_info()
    stats   = get_stats()
    spy     = get_spy_performance()
    send_telegram(
        f"💼 <b>Mon portefeuille</b>\n\n"
        f"💰 Valeur : <b>${account['equity']:.2f}</b>\n"
        f"💵 Cash : <b>${account['cash']:.2f}</b>\n"
        f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : <b>${account['pnl']:+.2f}</b>\n\n"
        f"🎯 Réussite : {stats['winrate']:.0f}% ({stats['wins']}✅/{stats['losses']}❌)\n"
        f"💹 P&amp;L total : ${stats['total_pnl']:+.2f}\n"
        f"🌍 SPY : {spy:+.2f}%\n\n"
        f"🤖 Mode : {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    )

def cmd_positions():
    positions = get_positions()
    if not positions:
        send_telegram("📭 Aucune action en ce moment — 100% cash.")
        return
    msg = "📌 <b>Actions en cours :</b>\n\n"
    for symbol, data in positions.items():
        ta  = get_technical_analysis(symbol)
        msg += f"{'🟢' if data['pnl_pct'] >= 0 else '🔴'} <b>{symbol}</b>\n   ${data['value']:.2f} | {data['pnl_pct']:+.2f}% | 🎯 +{take_profit_targets.get(symbol, 5.0)}%\n   Tendance : {ta['trend'] if ta else '?'}\n\n"
    send_telegram(msg)

def cmd_marche():
    spy = get_spy_performance()
    send_telegram(
        f"🌍 <b>Santé du marché</b>\n\n"
        f"SPY (US) : {spy:+.2f}%\n"
        f"{'🟢 Haussier' if spy > 0.5 else '🔴 Baissier' if spy < -0.5 else '🟡 Neutre'}\n\n"
        f"{'⚠️ Je reste prudent' if spy < -3 else '✅ Je surveille normalement'}"
    )

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram("⏸️ <b>Trading en pause</b>\n\nPlus d'achats.\nStop loss toujours actif.\n\nTape /resume pour reprendre.")

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
        send_telegram("ℹ️ Déjà 100% en cash.")
        return
    send_telegram("🚨 <b>URGENCE</b>\n\nJe vends tout...\n⚠️ Impôts possibles sur les gains.")
    for symbol, data in positions.items():
        place_order(symbol, "sell", data["qty"])
    send_telegram("✅ <b>Tout vendu — 100% cash.</b>\nTape /resume pour reprendre.")

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode  = True
    trading_paused = True
    send_telegram("🏖️ <b>Mode vacances !</b>\n\n✅ Actions gardées\n✅ Stop loss actif\n❌ Aucun achat\n❌ DCA suspendu\n\nTape /retour quand tu reviens !")

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
    send_telegram(f"🔍 Analyse de <b>{ticker}</b>...")
    ta    = get_technical_analysis(ticker)
    price = get_price(ticker)
    if not ta or not price:
        send_telegram(f"❌ Impossible d'analyser {ticker}.")
        return
    winrate = get_symbol_winrate(ticker)
    wr_txt  = f"\n🎯 Mon taux de réussite : {winrate:.0f}%" if winrate else ""
    send_telegram(f"📊 <b>{ticker}</b>\n\n💲 ${price:.2f}\n\n{format_ta(ta)}{wr_txt}")

def cmd_alerte(args):
    try:
        symbol, target = args[0].upper(), float(args[1])
        custom_alerts[symbol] = target
        send_telegram(f"🔔 Alerte créée !\n<b>{symbol}</b> → ${target:.2f}")
    except:
        send_telegram("❌ Format : /alerte NVDA 150")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("📭 Aucune alerte.")
        return
    msg = "🔔 <b>Alertes actives :</b>\n\n"
    for symbol, target in custom_alerts.items():
        price = get_price(symbol)
        diff  = f" (encore {abs((price-target)/target*100):.1f}%)" if price else ""
        msg  += f"📌 <b>{symbol}</b> → ${target:.2f}{diff}\n"
    send_telegram(msg)

def main():
    send_telegram(
        "🤖 <b>Trading Agent démarré !</b>\n\n"
        "📊 <b>Portefeuille optimisé :</b>\n"
        "🛡️ VT — base stable\n"
        "🤖 NVDA/MSFT/META — IA & Tech\n"
        "📈 QQQ/XLK — ETF sectoriels\n"
        "💵 15% cash réserve\n\n"
        "💶 DCA 100€/mois\n"
        "🛑 Stop loss -3%\n"
        "🎯 Take profit dynamique\n"
        "📉 Rachat auto sur baisse\n"
        "🧠 Mémoire des trades\n"
        "📊 Analyse RSI\n"
        "📅 Rapport quotidien 21h\n"
        "📆 Résumé hebdo lundi 8h\n\n"
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
        for ticker in ALL_ASSETS:
            analyze_ticker(ticker)
        log(f"⏳ Prochain check dans {POLL_INTERVAL//60} min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
