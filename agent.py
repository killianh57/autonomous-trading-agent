import os
import time
import schedule
import requests
import anthropic
import alpaca_trade_api as tradeapi
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")

SAFE_ASSETS       = ["VT"]
AGGRESSIVE_ASSETS = ["NVDA", "TSLA", "AAPL"]
DCA_MONTHLY_EUR   = 200
SAFE_RATIO        = 0.60
AGGRESSIVE_RATIO  = 0.40

alpaca = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_news(ticker):
    try:
        url = f"https://newsapi.org/v2/everything?q={ticker}&language=en&pageSize=5&apiKey={NEWS_API_KEY}"
        res = requests.get(url, timeout=10)
        articles = res.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:5]])
    except Exception as e:
        log(f"News error for {ticker}: {e}")
        return "Aucune news disponible."

def get_account_info():
    account = alpaca.get_account()
    return {"equity": float(account.equity), "cash": float(account.cash)}

def get_positions():
    positions = alpaca.list_positions()
    return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value)} for p in positions}

def get_price(ticker):
    try:
        return alpaca.get_latest_bar(ticker).c
    except Exception as e:
        log(f"Price error for {ticker}: {e}")
        return None

def place_order(symbol, side, qty):
    try:
        order = alpaca.submit_order(symbol=symbol, qty=round(qty, 4), side=side, type="market", time_in_force="day")
        log(f"✅ Ordre {side.upper()} {qty} {symbol}")
        return order
    except Exception as e:
        log(f"❌ Ordre échoué {symbol}: {e}")
        return None

SYSTEM_PROMPT = """Tu es un trader professionnel autonome utilisant la méthode Smart Money.
Tes règles :
- Risk/Reward minimum 1:2
- Ne jamais risquer plus de 2% du portefeuille par trade
- Suivre le trend dominant
- Prendre en compte le sentiment des news

Réponds UNIQUEMENT en JSON :
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0-100,
  "reason": "explication courte",
  "risk_percent": 1-2
}"""

def analyze_with_claude(ticker, price, news):
    try:
        import json
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Ticker: {ticker}\nPrix: ${price}\nNews:\n{news}"}]
        )
        text = response.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        log(f"Claude error for {ticker}: {e}")
        return {"action": "HOLD", "confidence": 0, "reason": "Erreur", "risk_percent": 0}

def run_trading_session():
    log("🚀 Début de la session de trading")
    account = get_account_info()
    portfolio_value = account["equity"]
    log(f"💼 Portefeuille: ${portfolio_value:.2f}")

    for ticker in SAFE_ASSETS + AGGRESSIVE_ASSETS:
        log(f"🔍 Analyse de {ticker}...")
        price = get_price(ticker)
        if not price:
            continue
        news   = get_news(ticker)
        signal = analyze_with_claude(ticker, price, news)
        action, confidence = signal.get("action","HOLD"), signal.get("confidence",0)
        log(f"📊 {ticker}: {action} (conf​​​​​​​​​​​​​​​​
