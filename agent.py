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
MARKET_CRASH_PCT  = 5.0

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

last_seen_news = {}
defensive_mode = False
take_profit_targets = {}  # {symbol: take_profit_pct} défini par Claude à l'achat

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
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
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def get_news(ticker):
    try:
        url = f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}"
        res = requests.get(url, timeout=10)
        return res.json().get("articles", [])
    except Exception as e:
        log(f"News error {ticker}: {e}")
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
    except Exception as e:
        log(f"Price error {ticker}: {e}")
        return None

def get_spy_performance():
    try:
        req  = StockLatestBarRequest(symbol_or_symbols="SPY")
        bars = data_client.get_stock_latest_bar(req)
        current = bars["SPY"].close
        yesterday_req = StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=2)
        )
        hist      = data_client.get_stock_bars(yesterday_req)
        bars_list = list(hist["SPY"])
        if len(bars_list) >= 2:
            prev_close = bars_list[-2].close
            return ((current - prev_close) / prev_close) * 100
        return 0
    except Exception as e:
        log(f"SPY error: {e}")
        return 0

def place_order(symbol, side, qty, take_profit_pct=None):
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(req)
        price = get_price(symbol)
        msg   = f"✅ ORDRE {side.upper()}\n📌 {round(qty,4)} {symbol}\n💲 ~${round(qty * price, 2) if price else '?'}"
        if side == "buy" and take_profit_pct:
            take_profit_targets[symbol] = take_profit_pct
            target_price = price * (1 + take_profit_pct / 100) if price else None
            msg += f"\n🎯 Take Profit: +{take_profit_pct}% (~${target_price:.2f})" if target_price else f"\n🎯 Take Profit: +{take_profit_pct}%"
        msg += "\n🤖 Trading Agent"
        log(msg)
        send_telegram(msg)
        # Nettoyer le take profit si on vend
        if side == "sell" and symbol in take_profit_targets:
            del take_profit_targets[symbol]
        return order
    except Exception as e:
        error_msg = f"❌ ORDRE ÉCHOUÉ\n{symbol} {side.upper()}\n⚠️ {e}"
        log(error_msg)
        send_telegram(error_msg)
        return None

def check_stop_loss_take_profit():
    """Stop loss fixe -3%, take profit dynamique défini par Claude."""
    positions = get_positions()
    for symbol, data in positions.items():
        pnl_pct = data["pnl_pct"]
        tp_pct  = take_profit_targets.get(symbol, 5.0)  # 5% par défaut si pas défini

        if pnl_pct <= -STOP_LOSS_PCT:
            msg = f"🛑 STOP LOSS\n{symbol}\nPerte: {pnl_pct:.2f}%\nVente automatique !"
            log(msg)
            send_telegram(msg)
            place_order(symbol, "sell", data["qty"])

        elif pnl_pct >= tp_pct:
            msg = f"🎯 TAKE PROFIT\n{symbol}\nGain: {pnl_pct:.2f}% / Objectif: {tp_pct}%\nVente automatique !"
            log(msg)
            send_telegram(msg)
            place_order(symbol, "sell", data["qty"])

def check_defensive_mode():
    global defensive_mode
    spy_perf = get_spy_performance()
    if spy_perf <= -MARKET_CRASH_PCT and not defensive_mode:
        defensive_mode = True
        msg = f"🚨 MODE DÉFENSIF ACTIVÉ\nSPY: {spy_perf:.2f}% aujourd'hui\nFermeture de toutes les positions !"
        log(msg)
        send_telegram(msg)
        positions = get_positions()
        for symbol, data in positions.items():
            place_order(symbol, "sell", data["qty"])
    elif spy_perf > -2.0 and defensive_mode:
        defensive_mode = False
        msg = "✅ MODE DÉFENSIF DÉSACTIVÉ\nMarché stabilisé, reprise du trading normal."
        log(msg)
        send_telegram(msg)

def send_daily_report():
    account   = get_account_info()
    positions = get_positions()
    pnl_emoji = "📈" if account["pnl"] >= 0 else "📉"
    report    = "📊 RAPPORT QUOTIDIEN\n"
    report   += "=" * 25 + "\n"
    report   += f"💼 Portefeuille: ${account['equity']:.2f}\n"
    report   += f"💵 Cash: ${account['cash']:.2f}\n"
    report   += f"{pnl_emoji} P&L jour: ${account['pnl']:.2f}\n\n"
    if positions:
        report += "📌 POSITIONS OUVERTES:\n"
        for symbol, data in positions.items():
            emoji  = "🟢" if data["pnl_pct"] >= 0 else "🔴"
            tp_pct = take_profit_targets.get(symbol, 5.0)
            report += f"{emoji} {symbol}: ${data['value']:.2f} ({data['pnl_pct']:.2f}%) | 🎯 TP: +{tp_pct}%\n"
    else:
        report += "📭 Aucune position ouverte\n"
    report += f"\n🛡️ Mode défensif: {'ON 🚨' if defensive_mode else 'OFF ✅'}"
    send_telegram(report)

SYSTEM_PROMPT = """Tu es un trader professionnel autonome utilisant la méthode Smart Money.
Règles : Risk/Reward 1:2 minimum, max 2% du portefeuille par trade, suivre le trend dominant.

Réponds UNIQUEMENT en JSON :
{
  "action": "BUY"|"SELL"|"HOLD",
  "confidence": 0-100,
  "reason": "explication courte",
  "risk_percent": 1-2,
  "take_profit_pct": 5-30
}

Pour take_profit_pct : estime le potentiel réel du mouvement.
- News mineure = 5-8%
- Breakout technique = 10-15%
- Catalyseur majeur (earnings, annonce produit) = 15-30%"""

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
        error_msg = f"⚠️ ERREUR CLAUDE\n{ticker}: {e}"
        log(error_msg)
        send_telegram(error_msg)
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur", "risk_percent": 0, "take_profit_pct": 5}

def analyze_ticker(ticker):
    if defensive_mode:
        return
    price = get_price(ticker)
    if not price:
        return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles):
        return
    send_telegram(f"🔔 Nouvelles news pour {ticker} !")
    news   = format_news(articles)
    signal = analyze_with_claude(ticker, price, news)
    action = signal.get("action", "HOLD")
    conf   = signal.get("confidence", 0)
    reason = signal.get("reason", "")
    tp_pct = signal.get("take_profit_pct", 5)
    log(f"📊 {ticker}: {action} ({conf}%) TP:{tp_pct}% — {reason}")
    if conf < 65:
        return
    account = get_account_info()
    pv      = account["equity"]
    if action == "BUY":
        qty = (pv * signal.get("risk_percent", 1) / 100) / price
        if qty * price >= 1:
            place_order(ticker, "buy", qty, take_profit_pct=tp_pct)
    elif action == "SELL":
        pos = get_positions()
        if ticker in pos:
            place_order(ticker, "sell", pos[ticker]["qty"])

def run_dca():
    if defensive_mode:
        send_telegram("⚠️ DCA annulé — mode défensif actif")
        return
    send_telegram("💰 DCA mensuel en cours...")
    account  = get_account_info()
    dca_usd  = DCA_MONTHLY_EUR * 1.08
    if account["cash"] < dca_usd:
        send_telegram("⚠️ Cash insuffisant pour le DCA !")
        return
    vt_price = get_price("VT")
    if vt_price:
        place_order("VT", "buy", (dca_usd * SAFE_RATIO) / vt_price)
    for ticker in AGGRESSIVE_ASSETS:
        price = get_price(ticker)
        if price:
            place_order(ticker, "buy", (dca_usd * AGGRESSIVE_RATIO / len(AGGRESSIVE_ASSETS)) / price)

def main():
    send_telegram(
        "🤖 Trading Agent Ultimate démarré !\n"
        "📈 60% VT / 40% NVDA,TSLA,AAPL\n"
        "💶 DCA 200€/mois\n"
        "🛑 Stop Loss: -3%\n"
        "🎯 Take Profit: dynamique (Claude décide)\n"
        "🚨 Mode défensif si SPY -5%\n"
        "📊 Rapport quotidien à 21h\n"
        "⏱ Polling toutes les 5 min"
    )
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()
    while True:
        now = datetime.now()
        if now.day == 1 and now.hour == 16 and now.minute < 5:
            run_dca()
        if now.hour == 21 and now.minute < 5:
            send_daily_report()
        check_defensive_mode()
        check_stop_loss_take_profit()
        for ticker in ALL_ASSETS:
            analyze_ticker(ticker)
        log(f"⏳ Prochain polling dans {POLL_INTERVAL//60} min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
