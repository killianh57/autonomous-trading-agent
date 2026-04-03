import os
import time
import schedule
import requests
import anthropic
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

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
claude         = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_news(ticker):
    try:
        url = f"https://newsapi.org/v2/everything?q={ticker}&language=en&pageSize=5&apiKey={NEWS_API_KEY}"
        res = requests.get(url, timeout=10)
        articles = res.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:5]])
    except Exception as e:
        log(f"News error {ticker}: {e}")
        return "​​​​​​​​​​​​​​​​
