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

# ── PORTEFEUILLE OPTIMISÉ ─────────────────────────────────────────────────────
SAFE_ASSETS        = ["VT"]                        # 25% base stable
TECH_ASSETS        = ["NVDA", "MSFT", "META"]      # 25% IA & Tech
ETF_ASSETS         = ["QQQ", "XLK"]               # 25% ETF sectoriels
ALL_ASSETS         = SAFE_ASSETS + TECH_ASSETS + ETF_ASSETS
CASH_RESERVE_PCT   = 0.25                          # 25% cash réserve

DCA_MONTHLY_EUR    = 100
DCA_ALLOCATION     = {
    "VT":   0.25,
    "NVDA": 0.15,
    "MSFT": 0.10,
    "META": 0.10,
    "QQQ":  0.15,
    "XLK":  0.10,
    "CASH": 0.15
}

POLL_INTERVAL      = 300
STOP_LOSS_PCT      = 3.0
MEMORY_FILE        = "trade_memory.json"

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

last_seen_news      = {}
take_profit_targets = {}
trading_paused      = False
vacation_mode       = False
custom_alerts       = {}
spy_baseline        = None

# ── MÉMOIRE ───────────────────────────────────────────────────────────────────

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {
        "trades": [],
        "stats": {"wins": 0, "losses": 0, "total_pnl": 0},
        "patterns": {},
        "errors": []
    }

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def record_trade(symbol, side, qty, price, pnl=None):
    memory = load_memory()
    memory["trades"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "symbol": symbol, "side": side,
        "qty": qty, "price": price, "pnl": pnl
    })
    if pnl is not None:
        memory["stats"]["total_pnl"] += pnl
        if pnl > 0:
            memory["stats"]["wins"] += 1
            memory["patterns"][symbol] = memory["patterns"].get(symbol, {"wins": 0, "losses": 0})
            memory["patterns"][symbol]["wins"] += 1
        else:
            memory["stats"]["losses"] += 1
            memory["patterns"][symbol] = memory["patterns"].get(symbol, {"wins": 0, "losses": 0})
            memory["patterns"][symbol]["losses"] += 1
    memory["trades"] = memory["trades"][-100:]
    save_memory(memory)

def record_error(error_msg):
    memory = load_memory()
    memory["errors"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "error": error_msg
    })
    memory["errors"] = memory["errors"][-20:]
    save_memory(memory)

def get_symbol_winrate(symbol):
    memory  = load_memory()
    pattern = memory["patterns"].get(symbol, None)
    if not pattern:
        return None
    total = pattern["wins"] + pattern["losses"]
    return (pattern["wins"] / total * 100) if total > 0 else None

def get_stats():
    memory = load_memory()
    stats  = memory["stats"]
    total  = stats["wins"] + stats["losses"]
    return {
        "wins": stats["wins"],
        "losses": stats["losses"],
        "total_pnl": stats["total_pnl"],
        "winrate": (stats["wins"] / total * 100) if total > 0 else 0,
        "recent": memory["trades"][-5:]
    }

# ── ANALYSE TECHNIQUE ─────────────────────────────────────────────────────────

def get_historical_prices(ticker, days=60):
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=days)
        )
        return [bar.close for bar in data_client.get_stock_bars(req)[ticker]]
    except:
        return []

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains  = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)

def get_technical_analysis(ticker):
    prices = get_historical_prices(ticker)
    if not prices or len(prices) < 20:
        return None
    rsi    = calculate_rsi(prices)
    ma20   = sum(prices[-20:]) / 20
    ma50   = sum(prices[-50:]) / 50 if len(prices) >= 50 else None
    cur    = prices[-1]
    trend  = "haussier 📈" if (ma20 and ma50 and ma20 > ma50) else "baissier 📉"
    return {
        "rsi": rsi, "ma20": ma20, "ma50": ma50,
        "current": cur, "trend": trend,
        "above_ma20": cur > ma20,
        "above_ma50": cur > ma50 if ma50 else None
    }

def format_ta(ta):
    if not ta:
        return "Indisponible"
    rsi_txt = ""
    if ta["rsi"]:
        if ta["rsi"] < 30:
            rsi_txt = f"RSI {ta['rsi']} ⬇️ Survendu — opportunité d'achat"
        elif ta["rsi"] > 70:
            rsi_txt = f"RSI {ta['rsi']} ⬆️ Suracheté — prudence"
        else:
            rsi_txt = f"RSI {ta['rsi']} ➡️ Neutre"
    return (
        f"{rsi_txt}\n"
        f"Tendance : {ta['trend']}\n"
        f"Prix vs MA20 : {'✅ Au-dessus' if ta['above_ma20'] else '⚠️ En-dessous'}\n"
        f"Prix vs MA50 : {'✅ Au-dessus' if ta['above_ma50'] else '⚠️ En-dessous' if ta['above_ma50'] is not None else '—'}"
    )

# ── UTILITAIRES ───────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - Trading Agent Running")
    def log_message(self, format, *args):
        pass

def start_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), HealthHandler).serve_forever()

# ── COMMANDES TELEGRAM ────────────────────────────────────────────────────────

def handle_telegram_commands():
    last_update_id = None
    while True:
        try:
            res  = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": last_update_id},
                timeout=35
            )
            for update in res.json().get("result", []):
                last_update_id = update["update_id"] + 1
                text = update.get("message", {}).get("text", "").strip()
                cmd  = text.lower().split()[0] if text else ""
                args = text.split()[1:] if len(text.split()) > 1 else []
                if cmd in ["/aide", "/start"]:       cmd_aide()
                elif cmd == "/status":               cmd_status()
                elif cmd == "/positions":            cmd_positions()
                elif cmd == "/pause":                cmd_pause()
                elif cmd == "/resume":               cmd_resume()
                elif cmd == "/report":               send_daily_report(immediate=True)
                elif cmd == "/urgence":              cmd_urgence()
                elif cmd == "/vacances":             cmd_vacances()
                elif cmd == "/retour":               cmd_retour()
                elif cmd == "/historique":           cmd_historique()
                elif cmd == "/technique" and args:   cmd_technique(args[0].upper())
                elif cmd == "/alerte" and len(args) >= 2: cmd_alerte(args)
                elif cmd == "/alertes":              cmd_voir_alertes()
                elif cmd == "/marche":               cmd_marche()
        except Exception as e:
            log(f"Telegram error: {e}")
        time.sleep(2)

def cmd_aide():
    send_telegram(
        "🤖 <b>Commandes disponibles :</b>\n\n"
        "📊 <b>Infos</b>\n"
        "/status — Valeur du portefeuille\n"
        "/positions — Actions en cours\n"
        "/report — Rapport complet maintenant\n"
        "/historique — Mes derniers trades\n"
        "/technique NVDA — Analyse technique\n"
        "/marche — Santé du marché\n\n"
        "⚙️ <b>Contrôle</b>\n"
        "/pause — Arrêter le trading\n"
        "/resume — Reprendre le trading\n"
        "/vacances — Mode ultra-prudent\n"
        "/retour — Fin du mode vacances\n\n"
        "🔔 <b>Alertes prix</b>\n"
        "/alerte NVDA 150 — Préviens si NVDA dépasse 150$\n"
        "/alertes — Voir mes alertes actives\n\n"
        "🚨 <b>Urgence</b>\n"
        "/urgence — Tout vendre immédiatement"
    )

def cmd_status():
    account = get_account_info()
    stats   = get_stats()
    spy     = get_spy_performance()
    send_telegram(
        f"💼 <b>Mon portefeuille</b>\n\n"
        f"💰 Valeur totale : <b>${account['equity']:.2f}</b>\n"
        f"💵 Cash disponible : <b>${account['cash']:.2f}</b>\n"
        f"{'📈' if account['pnl'] >= 0 else '📉'} Aujourd'hui : <b>${account['pnl']:+.2f}</b>\n\n"
        f"📊 <b>Mes performances :</b>\n"
        f"🎯 Taux de réussite : {stats['winrate']:.0f}%\n"
        f"✅ Gagnants : {stats['wins']} | ❌ Perdants : {stats['losses']}\n"
        f"💹 P&amp;L total : ${stats['total_pnl']:+.2f}\n\n"
        f"🌍 Marché (SPY) : {spy:+.2f}% aujourd'hui\n\n"
        f"🤖 Mode : {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    )

def cmd_positions():
    positions = get_positions()
    if not positions:
        send_telegram("📭 Aucune action en ce moment — 100% cash.")
        return
    msg = "📌 <b>Tes actions en cours :</b>\n\n"
    for symbol, data in positions.items():
        ta       = get_technical_analysis(symbol)
        winrate  = get_symbol_winrate(symbol)
        wr_txt   = f" | 🎯 Mes succès : {winrate:.0f}%" if winrate else ""
        msg     += (
            f"{'🟢' if data['pnl_pct'] >= 0 else '🔴'} <b>{symbol}</b>\n"
            f"   Valeur : ${data['value']:.2f}\n"
            f"   Gain/Perte : {data['pnl_pct']:+.2f}%{wr_txt}\n"
            f"   Objectif vente : +{take_profit_targets.get(symbol, 5.0)}%\n"
            f"   Protection : -{STOP_LOSS_PCT}%\n"
            f"   Tendance : {ta['trend'] if ta else '?'}\n\n"
        )
    send_telegram(msg)

def cmd_marche():
    spy    = get_spy_performance()
    qqq    = get_spy_performance_for("QQQ")
    status = "🟢 Haussier" if spy > 0.5 else "🔴 Baissier" if spy < -0.5 else "🟡 Neutre"
    send_telegram(
        f"🌍 <b>Santé du marché</b>\n\n"
        f"SPY (marché US) : {spy:+.2f}% {status}\n"
        f"QQQ (Tech) : {qqq:+.2f}%\n\n"
        f"{'⚠️ Marché sous tension — je reste prudent' if spy < -3 else '✅ Marché correct — je continue à surveiller'}"
    )

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram(
        "⏸️ <b>Trading mis en pause</b>\n\n"
        "Plus aucun nouvel achat.\n"
        "Tes actions et stop loss restent actifs.\n\n"
        "Tape /resume pour reprendre."
    )

def cmd_resume():
    global trading_paused, vacation_mode
    trading_paused = False
    vacation_mode  = False
    send_telegram("✅ <b>Trading repris !</b>\n\nJe surveille à nouveau les marchés.")

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions = get_positions()
    if not positions:
        send_telegram("ℹ️ Déjà 100% en cash.")
        return
    send_telegram(
        "🚨 <b>URGENCE</b>\n\n"
        "Je vends tout maintenant...\n"
        "⚠️ Cela peut générer des impôts sur les gains."
    )
    for symbol, data in positions.items():
        place_order(symbol, "sell", data["qty"])
    send_telegram(
        "✅ <b>Tout vendu — 100% cash.</b>\n\n"
        "Tape /resume quand tu veux reprendre."
    )

def cmd_vacances():
    global vacation_mode, trading_paused
    vacation_mode  = True
    trading_paused = True
    send_telegram(
        "🏖️ <b>Mode vacances activé !</b>\n\n"
        "✅ Tes actions actuelles sont gardées\n"
        "✅ Stop loss actif pour te protéger\n"
        "❌ Aucun nouvel achat\n"
        "❌ DCA mensuel suspendu\n\n"
        "Tape /retour quand tu reviens !"
    )

def cmd_retour():
    global vacation_mode, trading_paused
    vacation_mode  = False
    trading_paused = False
    send_telegram("👋 <b>Bon retour !</b>\n\nLe trading reprend. Voici le point :")
    send_daily_report(immediate=True)

def cmd_historique():
    stats = get_stats()
    if not stats["recent"]:
        send_telegram("📭 Aucun trade enregistré.")
        return
    msg = "📜 <b>Mes 5 derniers trades :</b>\n\n"
    for t in reversed(stats["recent"]):
        pnl = f" | P&amp;L: ${t['pnl']:+.2f}" if t.get("pnl") else ""
        msg += f"{'✅' if t['side'] == 'buy' else '💰'} {t['date']} — {t['side'].upper()} <b>{t['symbol']}</b> x{t['qty']:.3f} @ ${t['price']:.2f}{pnl}\n"
    msg += f"\n🎯 Réussite : {stats['winrate']:.0f}% | P&amp;L total : ${stats['total_pnl']:+.2f}"
    send_telegram(msg)

def cmd_technique(ticker):
    send_telegram(f"🔍 Analyse de <b>{ticker}</b>...")
    ta    = get_technical_analysis(ticker)
    price = get_price(ticker)
    if not ta or not price:
        send_telegram(f"❌ Impossible d'analyser {ticker}.")
        return
    winrate = get_symbol_winrate(ticker)
    wr_txt  = f"\n🎯 Mon taux de réussite sur {ticker} : {winrate:.0f}%" if winrate else ""
    send_telegram(
        f"📊 <b>Analyse — {ticker}</b>\n\n"
        f"💲 Prix : ${price:.2f}\n\n"
        f"{format_ta(ta)}{wr_txt}"
    )

def cmd_alerte(args):
    try:
        symbol, target = args[0].upper(), float(args[1])
        custom_alerts[symbol] = target
        send_telegram(f"🔔 Alerte créée !\nJe te préviendrai quand <b>{symbol}</b> atteint <b>${target:.2f}</b>")
    except:
        send_telegram("❌ Format : /alerte NVDA 150")

def cmd_voir_alertes():
    if not custom_alerts:
        send_telegram("📭 Aucune alerte active.")
        return
    msg = "🔔 <b>Alertes actives :</b>\n\n"
    for symbol, target in custom_alerts.items():
        price = get_price(symbol)
        diff  = f" (encore {abs((price-target)/target*100):.1f}% à parcourir)" if price else ""
        msg  += f"📌 <b>{symbol}</b> → ${target:.2f}{diff}\n"
    send_telegram(msg)

# ── MARCHÉ ────────────────────────────────────────────────────────────────────

def get_spy_performance():
    return get_spy_performance_for("SPY")

def get_spy_performance_for(ticker):
    try:
        current   = data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=ticker)
        )[ticker].close
        bars_list = list(data_client.get_stock_bars(
            StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now() - timedelta(days=2))
        )[ticker])
        return ((current - bars_list[-2].close) / bars_list[-2].close) * 100 if len(bars_list) >= 2 else 0
    except:
        return 0

def check_market_health():
    global trading_paused
    spy = get_spy_performance()
    if spy <= -10:
        send_telegram(
            f"🚨 <b>CRASH DÉTECTÉ !</b>\n\n"
            f"Le marché a chuté de <b>{spy:.1f}%</b> aujourd'hui.\n"
            f"Tape /urgence pour tout vendre\n"
            f"ou /pause pour stopper les achats."
        )
    elif spy <= -5:
        trading_paused = True
        send_telegram(
            f"⚠️ <b>Marché en forte baisse</b>\n\n"
            f"Le marché a baissé de <b>{spy:.1f}%</b>.\n"
            f"Trading mis en pause par sécurité.\n"
            f"Tape /resume pour reprendre."
        )
    elif spy <= -3:
        send_telegram(
            f"📉 <b>Marché sous tension</b>\n\n"
            f"Le marché baisse de <b>{spy:.1f}%</b>.\n"
            f"Je reste prudent."
        )

def check_dip_buying():
    """Rachète automatiquement si un actif baisse -10% ou -20%."""
    account = get_account_info()
    cash    = account["cash"]
    reserve = account["equity"] * CASH_RESERVE_PCT
    if cash < 10:
        return
    for ticker in ALL_ASSETS:
        prices = get_historical_prices(ticker, days=7)
        if len(prices) < 2:
            continue
        dip = ((prices[-1] - max(prices[:-1])) / max(prices[:-1])) * 100
        if dip <= -20 and cash >= reserve * 0.3:
            qty = (reserve * 0.3) / prices[-1]
            send_telegram(
                f"📉 <b>Grosse baisse détectée !</b>\n\n"
                f"<b>{ticker}</b> a chuté de {abs(dip):.1f}% cette semaine.\n"
                f"J'achète double pour profiter du prix bas !"
            )
            place_order(ticker, "buy", qty)
        elif dip <= -10 and cash >= reserve * 0.15:
            qty = (reserve * 0.15) / prices[-1]
            send_telegram(
                f"📉 <b>Baisse détectée</b>\n\n"
                f"<b>{ticker}</b> a baissé de {abs(dip):.1f}% cette semaine.\n"
                f"Je rachète pour faire baisser mon prix moyen."
            )
            place_order(ticker, "buy", qty)

def check_custom_alerts():
    for symbol, target in list(custom_alerts.items()):
        price = get_price(symbol)
        if price and price >= target:
            send_telegram(
                f"🔔 <b>ALERTE PRIX !</b>\n\n"
                f"<b>{symbol}</b> a atteint <b>${price:.2f}</b>\n"
                f"Ton objectif était ${target:.2f} ✅"
            )
            del custom_alerts[symbol]

# ── TRADING ───────────────────────────────────────────────────────────────────

def get_account_info():
    account = trading_client.get_account()
    return {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "pnl": float(account.equity) - float(account.last_equity)
    }

def get_positions():
    return {
        p.symbol: {
            "qty": float(p.qty),
            "value": float(p.market_value),
            "avg_price": float(p.avg_entry_price),
            "pnl": float(p.unrealized_pl),
            "pnl_pct": float(p.unrealized_plpc) * 100
        } for p in trading_client.get_all_positions()
    }

def get_price(ticker):
    try:
        return data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=ticker)
        )[ticker].close
    except:
        return None

def get_news(ticker):
    try:
        return requests.get(
            f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}",
            timeout=10
        ).json().get("articles", [])
    except:
        return []

def has_new_news(ticker, articles):
    if not articles:
        return False
    latest = articles[0].get("publishedAt", "")
    if last_seen_news.get(ticker) != latest:
        last_seen_news[ticker] = latest
        return True
    return False

def place_order(symbol, side, qty, take_profit_pct=None):
    try:
        trading_client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        ))
        price  = get_price(symbol)
        valeur = round(qty * price, 2) if price else "?"
        record_trade(symbol, side, round(qty, 4), price or 0)
        if side == "buy":
            if take_profit_pct:
                take_profit_targets[symbol] = take_profit_pct
            send_telegram(
                f"✅ <b>Achat effectué</b>\n\n"
                f"Action : <b>{symbol}</b>\n"
                f"Quantité : {round(qty, 4)}\n"
                f"Montant : ~${valeur}\n"
                f"🛑 Stop loss : -{STOP_LOSS_PCT}%\n"
                f"🎯 Objectif : +{take_profit_pct or 5}%"
            )
        else:
            if symbol in take_profit_targets:
                del take_profit_targets[symbol]
            send_telegram(
                f"✅ <b>Vente effectuée</b>\n\n"
                f"Action : <b>{symbol}</b>\n"
                f"Quantité : {round(qty, 4)}\n"
                f"Montant : ~${valeur}"
            )
    except Exception as e:
        error_msg = f"Ordre échoué {symbol} {side}: {str(e)}"
        record_error(error_msg)
        send_telegram(f"❌ <b>Ordre échoué</b>\n{symbol} {side.upper()}\nErreur : {str(e)}")

def check_stop_loss_take_profit():
    for symbol, data in get_positions().items():
        pnl_pct = data["pnl_pct"]
        tp_pct  = take_profit_targets.get(symbol, 5.0)
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(
                f"🛑 <b>Stop loss déclenché</b>\n\n"
                f"<b>{symbol}</b> a perdu {abs(pnl_pct):.1f}%\n"
                f"Je vends pour limiter la perte."
            )
            place_order(symbol, "sell", data["qty"])
        elif pnl_pct >= tp_pct:
            send_telegram(
                f"🎯 <b>Objectif atteint !</b>\n\n"
                f"<b>{symbol}</b> a gagné {pnl_pct:.1f}%\n"
                f"Objectif était +{tp_pct}%\n"
                f"Je sécurise le gain."
            )
            place_order(symbol, "sell", data["qty"])

SYSTEM_PROMPT = """Tu es un trader professionnel Smart Money.
Règles : RR 1:2 minimum, max 2% par trade, suivre le trend dominant.
Tu analyses : prix actuel + news + analyse technique + historique de tes trades sur ce ticker.
Si tu as déjà perdu sur ce ticker récemment, sois plus prudent (baisse la confiance).

Réponds UNIQUEMENT en JSON :
{
  "action": "BUY"|"SELL"|"HOLD",
  "confidence": 0-100,
  "reason": "explication courte en français",
  "risk_percent": 1-2,
  "take_profit_pct": 5-30
}
take_profit_pct : news mineure=5-8%, breakout=10-15%, catalyseur majeur=15-30%"""

def analyze_with_claude(ticker, price, news, ta_summary, winrate):
    try:
        import json
        wr_context = f"\nMon taux de réussite sur {ticker} : {winrate:.0f}% — sois {'prudent' if winrate < 40 else 'confiant'}" if winrate else ""
        res = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Ticker:{ticker}\nPrix:${price}\nNews:\n{news}\nAnalyse technique:\n{ta_summary}{wr_context}"}]
        )
        text = res.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        record_error(f"Claude error {ticker}: {str(e)}")
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur", "risk_percent": 0, "take_profit_pct": 5}

def analyze_ticker(ticker):
    if trading_paused or vacation_mode:
        return
    price = get_price(ticker)
    if not price:
        return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles):
        return
    ta       = get_technical_analysis(ticker)
    winrate  = get_symbol_winrate(ticker)
    signal   = analyze_with_claude(
        ticker, price,
        "\n".join([f"- {a['title']}" for a in articles[:5]]),
        format_ta(ta),
        winrate
    )
    action, conf, reason, tp_pct = (
        signal.get("action", "HOLD"),
        signal.get("confidence", 0),
        signal.get("reason", ""),
        signal.get("take_profit_pct", 5)
    )
    if conf < 65:
        return
    account = get_account_info()
    if action == "BUY":
        qty = (account["equity"] * signal.get("risk_percent", 1) / 100) / price
        if qty * price >= 1:
            send_telegram(
                f"💡 <b>Signal d'achat</b>\n\n"
                f"Action : <b>{ticker}</b>\n"
                f"Raison : {reason}\n"
                f"Confiance : {conf}%\n"
                f"Objectif : +{tp_pct}%\n"
                f"Tendance : {ta['trend'] if ta else '?'}"
            )
            place_order(ticker, "buy", qty, take_profit_pct=tp_pct)
    elif action == "SELL":
        pos = get_positions()
        if ticker in pos:
            send_telegram(
                f"💡 <b>Signal de vente</b>\n\n"
                f"Action : <b>{ticker}</b>\n"
                f"Raison : {reason}\n"
                f"Confiance : {conf}%"
            )
            place_order(ticker, "sell", pos[ticker]["qty"])

def run_dca():
    if trading_paused or vacation_mode:
        send_telegram("⏸️ DCA annulé — trading en pause.")
        return
    send_telegram("💰 <b>Investissement mensuel (DCA)</b>\n\nJ'achète tes actions du mois...")
    account = get_account_info()
    dca_usd = DCA_MONTHLY_EUR * 1.08
    if account["cash"] < dca_usd:
        send_telegram(f"⚠️ Pas assez de cash pour le DCA. Il faut ${dca_usd:.0f}.")
        return
    for ticker, alloc in DCA_ALLOCATION.items():
        if ticker == "CASH":
            continue
        price = get_price(ticker)
        if price:
            qty = (dca_usd * alloc) / price
            place_order(ticker, "buy", qty)

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
    report   += f"🌍 Marché : {spy:+.2f}%\n\n"
    if positions:
        report += "📌 <b>Actions :</b>\n"
        for symbol, data in positions.items():
            report += f"{'🟢' if data['pnl_pct'] >= 0 else '🔴'} <b>{symbol}</b> ${data['value']:.2f} ({data['pnl_pct']:+.2f}%) | 🎯 +{take_profit_targets.get(symbol, 5.0)}%\n"
    else:
        report += "📭 100% cash\n"
    report += (
        f"\n📊 Réussite : {stats['winrate']:.0f}% "
        f"({stats['wins']}✅/{stats['losses']}❌)\n"
        f"💹 P&amp;L total : ${stats['total_pnl']:+.2f}\n\n"
        f"🤖 Mode : {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    )
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
        f"✅ Gagnants : {stats['wins']} | ❌ Perdants : {stats['losses']}\n\n"
        f"📊 <b>Moi vs Marché :</b>\n"
        f"SPY cette semaine : {spy:+.2f}%\n"
        f"Moi vs SPY : {'✅ Je bats le marché !' if vs_spy > 0 else '📉 Le marché me bat'}\n\n"
        f"Bonne semaine ! 💪"
    )

def send_monthly_fiscal_report():
    """Résumé fiscal mensuel."""
    stats = get_stats()
    send_telegram(
        f"🧾 <b>Résumé fiscal du mois</b>\n\n"
        f"💹 P&amp;L total réalisé : <b>${stats['total_pnl']:+.2f}</b>\n\n"
        f"{'📈 Gains à déclarer aux impôts (flat tax 30% en France)' if stats['total_pnl'] > 0 else '📉 Pertes — rien à déclarer ce mois'}\n\n"
        f"⚠️ Consulte un comptable pour ta déclaration officielle."
    )

def main():
    send_telegram(
        "🤖 <b>Trading Agent démarré !</b>\n\n"
        "📊 <b>Ton portefeuille optimisé :</b>\n"
        "🛡️ 25% VT — base stable\n"
        "🤖 25% NVDA/MSFT/META — IA & Tech\n"
        "📈 25% QQQ/XLK — ETF sectoriels\n"
        "💵 25% Cash — rachats sur baisse\n\n"
        "💶 DCA 100€/mois automatique\n"
        "🛑 Stop loss -3%\n"
        "🎯 Take profit dynamique\n"
        "📉 Rachat automatique sur -10%/-20%\n"
        "📊 Analyse technique RSI\n"
        "🧠 Mémoire des trades\n"
        "📅 Rapport quotidien 21h\n"
        "📆 Résumé hebdo lundi 8h\n"
        "🧾 Résumé fiscal le 1er du mois\n\n"
        "Tape /aide 👇"
    )
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram_commands, daemon=True).start()

    while True:
        now = datetime.now()
        if now.day == 1 and now.hour == 16 and now.minute < 5:
            run_dca()
            send_monthly_fiscal_report()
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
