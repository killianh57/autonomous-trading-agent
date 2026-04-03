import os
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

SAFE_ASSETS       = ["VT"]
AGGRESSIVE_ASSETS = ["NVDA", "TSLA", "AAPL"]
ALL_ASSETS        = SAFE_ASSETS + AGGRESSIVE_ASSETS
DCA_MONTHLY_EUR   = 200
SAFE_RATIO        = 0.60
AGGRESSIVE_RATIO  = 0.40
POLL_INTERVAL     = 300
STOP_LOSS_PCT     = 3.0

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

last_seen_news      = {}
take_profit_targets = {}
trading_paused      = False

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
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ── TELEGRAM COMMANDS ─────────────────────────────────────────────────────────

def handle_telegram_commands():
    """Écoute les commandes Telegram en continu."""
    last_update_id = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 30, "offset": last_update_id}
            res  = requests.get(url, params=params, timeout=35)
            data = res.json()
            for update in data.get("result", []):
                last_update_id = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                if text == "/status":
                    cmd_status()
                elif text == "/positions":
                    cmd_positions()
                elif text == "/pause":
                    cmd_pause()
                elif text == "/resume":
                    cmd_resume()
                elif text == "/report":
                    cmd_report()
                elif text == "/urgence":
                    cmd_urgence()
                elif text == "/aide" or text == "/start":
                    cmd_aide()
        except Exception as e:
            log(f"Telegram polling error: {e}")
        time.sleep(2)

def cmd_aide():
    send_telegram(
        "🤖 <b>Commandes disponibles :</b>\n\n"
        "/status — Voir mon portefeuille\n"
        "/positions — Voir mes actions en cours\n"
        "/report — Rapport complet maintenant\n"
        "/pause — Arrêter le trading\n"
        "/resume — Reprendre le trading\n"
        "/urgence — ⚠️ Tout vendre immédiatement"
    )

def cmd_status():
    account = get_account_info()
    pnl_emoji = "📈" if account["pnl"] >= 0 else "📉"
    send_telegram(
        f"💼 <b>Mon portefeuille</b>\n\n"
        f"💰 Valeur totale : <b>${account['equity']:.2f}</b>\n"
        f"💵 Cash disponible : <b>${account['cash']:.2f}</b>\n"
        f"{pnl_emoji} Gain/Perte aujourd'hui : <b>${account['pnl']:+.2f}</b>\n\n"
        f"🤖 Trading : {'⏸️ En pause' if trading_paused else '✅ Actif'}"
    )

def cmd_positions():
    positions = get_positions()
    if not positions:
        send_telegram("📭 Tu n'as aucune action en ce moment.")
        return
    msg = "📌 <b>Tes actions en cours :</b>\n\n"
    for symbol, data in positions.items():
        emoji  = "🟢" if data["pnl_pct"] >= 0 else "🔴"
        tp_pct = take_profit_targets.get(symbol, 5.0)
        msg   += (
            f"{emoji} <b>{symbol}</b>\n"
            f"   Valeur : ${data['value']:.2f}\n"
            f"   Gain/Perte : {data['pnl_pct']:+.2f}%\n"
            f"   Objectif vente : +{tp_pct}%\n"
            f"   Protection perte : -{STOP_LOSS_PCT}%\n\n"
        )
    send_telegram(msg)

def cmd_pause():
    global trading_paused
    trading_paused = True
    send_telegram(
        "⏸️ <b>Trading mis en pause</b>\n\n"
        "Le bot n'achètera plus rien.\n"
        "Tes actions actuelles sont gardées.\n"
        "Les stop loss restent actifs pour te protéger.\n\n"
        "Tape /resume pour reprendre."
    )

def cmd_resume():
    global trading_paused
    trading_paused = False
    send_telegram(
        "✅ <b>Trading repris !</b>\n\n"
        "Le bot surveille à nouveau les marchés\n"
        "et va trader normalement."
    )

def cmd_report():
    send_daily_report(immediate=True)

def cmd_urgence():
    global trading_paused
    trading_paused = True
    positions = get_positions()
    if not positions:
        send_telegram("ℹ️ Aucune action à vendre. Tu es déjà en cash.")
        return
    send_telegram(
        "🚨 <b>URGENCE DÉCLENCHÉE</b>\n\n"
        "Je vends toutes tes actions maintenant...\n"
        "⚠️ Attention : cela peut générer des impôts sur les plus-values."
    )
    for symbol, data in positions.items():
        place_order(symbol, "sell", data["qty"])
    send_telegram(
        "✅ <b>Toutes les actions ont été vendues.</b>\n\n"
        "💵 Tu es maintenant 100% en cash.\n"
        "Le trading est mis en pause.\n"
        "Tape /resume quand tu veux reprendre."
    )

# ── MARCHÉ ────────────────────────────────────────────────────────────────────

def get_spy_performance():
    try:
        req     = StockLatestBarRequest(symbol_or_symbols="SPY")
        bars    = data_client.get_stock_latest_bar(req)
        current = bars["SPY"].close
        hist_req = StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=2)
        )
        hist      = data_client.get_stock_bars(hist_req)
        bars_list = list(hist["SPY"])
        if len(bars_list) >= 2:
            return ((current - bars_list[-2].close) / bars_list[-2].close) * 100
        return 0
    except:
        return 0

def check_market_health():
    global trading_paused
    spy = get_spy_performance()
    if spy <= -10:
        send_telegram(
            f"🚨 <b>CRASH DÉTECTÉ !</b>\n\n"
            f"Le marché américain a chuté de <b>{spy:.1f}%</b> aujourd'hui.\n"
            f"C'est une situation exceptionnelle.\n\n"
            f"Tape /urgence pour tout vendre\n"
            f"ou /pause pour arrêter les nouveaux achats."
        )
    elif spy <= -5:
        trading_paused = True
        send_telegram(
            f"⚠️ <b>Marché en forte baisse</b>\n\n"
            f"Le marché a baissé de <b>{spy:.1f}%</b> aujourd'hui.\n"
            f"J'ai mis le trading en pause par sécurité.\n"
            f"Tes actions actuelles sont gardées.\n\n"
            f"Tape /resume pour reprendre si tu le souhaites."
        )
    elif spy <= -3:
        send_telegram(
            f"📉 <b>Marché sous tension</b>\n\n"
            f"Le marché baisse de <b>{spy:.1f}%</b> aujourd'hui.\n"
            f"Je reste prudent mais je continue à surveiller."
        )

# ── TRADING ───────────────────────────────────────────────────────────────────

def get_news(ticker):
    try:
        url = f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}"
        res = requests.get(url, timeout=10)
        return res.json().get("articles", [])
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

def format_news(articles):
    return "\n".join([f"- {a['title']}" for a in articles[:5]])

def get_account_info():
    account = trading_client.get_account()
    return {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "pnl": float(account.equity) - float(account.last_equity)
    }

def get_positions():
    positions = trading_client.get_all_positions()
    return {
        p.symbol: {
            "qty": float(p.qty),
            "value": float(p.market_value),
            "avg_price": float(p.avg_entry_price),
            "pnl": float(p.unrealized_pl),
            "pnl_pct": float(p.unrealized_plpc) * 100
        } for p in positions
    }

def get_price(ticker):
    try:
        req  = StockLatestBarRequest(symbol_or_symbols=ticker)
        bars = data_client.get_stock_latest_bar(req)
        return bars[ticker].close
    except:
        return None

def place_order(symbol, side, qty, take_profit_pct=None):
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(req)
        price = get_price(symbol)
        valeur = round(qty * price, 2) if price else "?"
        if side == "buy":
            tp_txt = f"\n🎯 Objectif de vente : +{take_profit_pct}%" if take_profit_pct else ""
            msg = (
                f"✅ <b>Achat effectué</b>\n\n"
                f"Action : <b>{symbol}</b>\n"
                f"Quantité : {round(qty, 4)} action(s)\n"
                f"Montant : ~${valeur}\n"
                f"🛑 Protection perte : -{STOP_LOSS_PCT}%"
                f"{tp_txt}"
            )
            if take_profit_pct:
                take_profit_targets[symbol] = take_profit_pct
        else:
            msg = (
                f"✅ <b>Vente effectuée</b>\n\n"
                f"Action : <b>{symbol}</b>\n"
                f"Quantité : {round(qty, 4)} action(s)\n"
                f"Montant : ~${valeur}"
            )
            if symbol in take_profit_targets:
                del take_profit_targets[symbol]
        log(msg)
        send_telegram(msg)
    except Exception as e:
        send_telegram(
            f"❌ <b>Problème lors d'un ordre</b>\n\n"
            f"Action : {symbol}\n"
            f"Type : {side.upper()}\n"
            f"Erreur technique : {str(e)}"
        )

def check_stop_loss_take_profit():
    positions = get_positions()
    for symbol, data in positions.items():
        pnl_pct = data["pnl_pct"]
        tp_pct  = take_profit_targets.get(symbol, 5.0)
        if pnl_pct <= -STOP_LOSS_PCT:
            send_telegram(
                f"🛑 <b>Protection activée</b>\n\n"
                f"<b>{symbol}</b> a perdu {abs(pnl_pct):.1f}%\n"
                f"Je vends automatiquement pour limiter la perte."
            )
            place_order(symbol, "sell", data["qty"])
        elif pnl_pct >= tp_pct:
            send_telegram(
                f"🎯 <b>Objectif atteint !</b>\n\n"
                f"<b>{symbol}</b> a gagné {pnl_pct:.1f}%\n"
                f"Objectif était +{tp_pct}%\n"
                f"Je vends pour sécuriser le gain."
            )
            place_order(symbol, "sell", data["qty"])

SYSTEM_PROMPT = """Tu es un trader professionnel autonome utilisant la méthode Smart Money.
Règles : Risk/Reward 1:2 minimum, max 2% du portefeuille par trade, suivre le trend dominant.
Réponds UNIQUEMENT en JSON :
{
  "action": "BUY"|"SELL"|"HOLD",
  "confidence": 0-100,
  "reason": "explication courte en français",
  "risk_percent": 1-2,
  "take_profit_pct": 5-30
}
Pour take_profit_pct : news mineure=5-8%, breakout=10-15%, catalyseur majeur=15-30%"""

def analyze_with_claude(ticker, price, news):
    try:
        import json
        res = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Ticker:{ticker}\nPrix:${price}\nNews:\n{news}"}]
        )
        text = res.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        send_telegram(f"⚠️ Problème d'analyse pour {ticker} — je passe mon tour.")
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur", "risk_percent": 0, "take_profit_pct": 5}

def analyze_ticker(ticker):
    if trading_paused:
        return
    price = get_price(ticker)
    if not price:
        return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles):
        return
    send_telegram(f"🔔 Nouvelles informations détectées sur <b>{ticker}</b> — analyse en cours...")
    news   = format_news(articles)
    signal = analyze_with_claude(ticker, price, news)
    action = signal.get("action", "HOLD")
    conf   = signal.get("confidence", 0)
    reason = signal.get("reason", "")
    tp_pct = signal.get("take_profit_pct", 5)
    if conf < 65:
        return
    account = get_account_info()
    pv      = account["equity"]
    if action == "BUY":
        qty = (pv * signal.get("risk_percent", 1) / 100) / price
        if qty * price >= 1:
            send_telegram(
                f"💡 <b>Signal d'achat détecté</b>\n\n"
                f"Action : <b>{ticker}</b>\n"
                f"Raison : {reason}\n"
                f"Confiance : {conf}%\n"
                f"Objectif : +{tp_pct}%"
            )
            place_order(ticker, "buy", qty, take_profit_pct=tp_pct)
    elif action == "SELL":
        pos = get_positions()
        if ticker in pos:
            send_telegram(
                f"💡 <b>Signal de vente détecté</b>\n\n"
                f"Action : <b>{ticker}</b>\n"
                f"Raison : {reason}\n"
                f"Confiance : {conf}%"
            )
            place_order(ticker, "sell", pos[ticker]["qty"])

def run_dca():
    if trading_paused:
        send_telegram("⏸️ DCA du mois annulé — le trading est en pause.")
        return
    send_telegram("💰 <b>Investissement mensuel automatique (DCA)</b>\n\nJ'achète tes actions du mois...")
    account  = get_account_info()
    dca_usd  = DCA_MONTHLY_EUR * 1.08
    if account["cash"] < dca_usd:
        send_telegram(f"⚠️ Pas assez de cash pour le DCA du mois.\nIl faut au moins ${dca_usd:.0f}.")
        return
    vt_price = get_price("VT")
    if vt_price:
        place_order("VT", "buy", (dca_usd * SAFE_RATIO) / vt_price)
    for ticker in AGGRESSIVE_ASSETS:
        price = get_price(ticker)
        if price:
            place_order(ticker, "buy", (dca_usd * AGGRESSIVE_RATIO / len(AGGRESSIVE_ASSETS)) / price)

def send_daily_report(immediate=False):
    account   = get_account_info()
    positions = get_positions()
    pnl_emoji = "📈" if account["pnl"] >= 0 else "📉"
    titre     = "📊 <b>Rapport immédiat</b>" if immediate else "📊 <b>Rapport du soir</b>"
    report    = f"{titre}\n{'='*20}\n\n"
    report   += f"💰 Valeur totale : <b>${account['equity']:.2f}</b>\n"
    report   += f"💵 Cash disponible : <b>${account['cash']:.2f}</b>\n"
    report   += f"{pnl_emoji} Gain/Perte aujourd'hui : <b>${account['pnl']:+.2f}</b>\n\n"
    if positions:
        report += "📌 <b>Actions en cours :</b>\n"
        for symbol, data in positions.items():
            emoji  = "🟢" if data["pnl_pct"] >= 0 else "🔴"
            tp_pct = take_profit_targets.get(symbol, 5.0)
            report += (
                f"{emoji} <b>{symbol}</b> — ${data['value']:.2f}\n"
                f"   {data['pnl_pct']:+.2f}% | Objectif : +{tp_pct}%\n\n"
            )
    else:
        report += "📭 Aucune action en ce moment — 100% cash\n"
    report += f"\n🤖 Trading : {'⏸️ En pause' if trading_paused else '✅ Actif'}"
    send_telegram(report)

def main():
    send_telegram(
        "🤖 <b>Agent de trading démarré !</b>\n\n"
        "Voici ce que je fais pour toi :\n"
        "📈 J'investis 60% en sécurité (VT)\n"
        "🚀 40% sur NVDA, TSLA, AAPL\n"
        "💶 DCA automatique de 200€/mois\n"
        "🛑 Je protège chaque achat (-3%)\n"
        "🎯 Je vends quand l'objectif est atteint\n"
        "📰 Je surveille les news toutes les 5 min\n"
        "📊 Rapport tous les soirs à 21h\n\n"
        "Tape /aide pour voir les commandes 👇"
    )
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=handle_telegram_commands, daemon=True).start()
    while True:
        now = datetime.now()
        if now.day == 1 and now.hour == 16 and now.minute < 5:
            run_dca()
        if now.hour == 21 and now.minute < 5:
            send_daily_report()
        check_market_health()
        check_stop_loss_take_profit()
        for ticker in ALL_ASSETS:
            analyze_ticker(ticker)
        log(f"⏳ Prochain check dans {POLL_INTERVAL//60} min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
