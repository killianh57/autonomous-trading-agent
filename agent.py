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
from alpaca.data.requests import StockLatestBarRequest
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")

SAFE_ASSETS       = ["VT"]
AGGRESSIVE_ASSETS = ["NVDA", "TSLA", "AAPL"]
DCA_MONTHLY_EUR   = 200
SAFE_RATIO        = 0.60
AGGRESSIVE_RATIO  = 0.40
POLL_INTERVAL     = 300  # 5 minutes

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Mémoire des dernières news vues
last_seen_news = {}

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

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
        articles = res.json().get("articles", [])
        return articles
    except Exception as e:
        log(f"News error {ticker}: {e}")
        return []

def has_new_news(ticker, articles):
    """Retourne True si des nouvelles news sont détectées."""
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
    return {"equity": float(account.equity), "cash": float(account.cash)}

def get_positions():
    positions = trading_client.get_all_positions()
    return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value)} for p in positions}

def get_price(ticker):
    try:
        req  = StockLatestBarRequest(symbol_or_symbols=ticker)
        bars = data_client.get_stock_latest_bar(req)
        return bars[ticker].close
    except Exception as e:
        log(f"Price error {ticker}: {e}")
        return None

def place_order(symbol, side, qty):
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=round(qty, 4),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(req)
        log(f"✅ {side.upper()} {round(qty,4)} {symbol}")
        return order
    except Exception as e:
        log(f"❌ Ordre échoué {symbol}: {e}")
        return None

SYSTEM_PROMPT = """Tu es un trader professionnel autonome utilisant la méthode Smart Money.
Règles : Risk/Reward 1:2 minimum, max 2% du portefeuille par trade, suivre le trend dominant.
Réponds UNIQUEMENT en JSON :
{"action":"BUY"|"SELL"|"HOLD","confidence":0-100,"reason":"court","risk_percent":1-2}"""

def analyze_with_claude(ticker, price, news):
    try:
        import json
        res = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role":"user","content":f"Ticker:{ticker}\nPrix:${price}\nNews:\n{news}"}]
        )
        text = res.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        log(f"Claude error {ticker}: {e}")
        return {"action":"HOLD","confidence":0,"reason":"Erreur","risk_percent":0}

def analyze_ticker(ticker):
    """Analyse un ticker et trade si signal fort."""
    price = get_price(ticker)
    if not price:
        return
    articles = get_news(ticker)
    if not has_new_news(ticker, articles):
        return  # Pas de nouvelles news → on skip
    log(f"🔔 Nouvelles news détectées pour {ticker} !")
    news    = format_news(articles)
    signal  = analyze_with_claude(ticker, price, news)
    action  = signal.get("action", "HOLD")
    conf    = signal.get("confidence", 0)
    log(f"📊 {ticker}: {action} ({conf}%) — {signal.get('reason','')}")
    if conf < 65:
        return
    account = get_account_info()
    pv      = account["equity"]
    if action == "BUY":
        qty = (pv * signal.get("risk_percent", 1) / 100) / price
        if qty * price >= 1:
            place_order(ticker, "buy", qty)
    elif action == "SELL":
        pos = get_positions()
        if ticker in pos:
            place_order(ticker, "sell", pos[ticker]["qty"])

def run_dca():
    log("💰 DCA mensuel")
    account  = get_account_info()
    dca_usd  = DCA_MONTHLY_EUR * 1.08
    if account["cash"] < dca_usd:
        log("⚠️ Cash insuffisant")
        return
    vt_price = get_price("VT")
    if vt_price:
        place_order("VT", "buy", (dca_usd * SAFE_RATIO) / vt_price)
    for ticker in AGGRESSIVE_ASSETS:
        price = get_price(ticker)
        if price:
            place_order(ticker, "buy", (dca_usd * AGGRESSIVE_RATIO / len(AGGRESSIVE_ASSETS)) / price)
    log("✅ DCA exécuté")

def main():
    log("🤖 Agent démarré — polling news toutes les 5 min")
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    while True:
        now = datetime.now()
        # DCA le 1er du mois à 16h
        if now.day == 1 and now.hour == 16 and now.minute < 5:
            run_dca()
        # Analyser tous les actifs
        for ticker in SAFE_ASSETS + AGGRESSIVE_ASSETS:
            analyze_ticker(ticker)
        log(f"⏳ Prochain polling dans {POLL_INTERVAL//60} min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
