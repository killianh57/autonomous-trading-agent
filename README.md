# autonomous-trading-agent

Bot trading autonome connecte a Alpaca (paper -> live).

## Stack
- Python 3.11
- alpaca-py (jamais alpaca-trade-api)
- Flask (health server Render)
- Telegram (alertes + commandes)
- Claude API (cerveau analyse)

## Fichiers

| Fichier | Role |
|---------|------|
| agent.py | Bot principal Alpaca (VT/SCHD/VNQ/QQQ/IBIT) |
| alpha_signals.py | Coordinateur signaux alpha crypto |
| cryptopanic_monitor.py | News crypto temps reel + sentiment |
| binance_futures_monitor.py | Funding rates + OI (data public, no key) |

## Portfolio Core-Satellite
- CORE (65%) : VT 40% + SCHD 15% + VNQ 5% -- jamais vendus
- SATELLITE (35%) : QQQ 15% + IBIT 10% + CASH 15%

## Variables d'environnement
Voir .env.example

## Deploy Render
- Build : pip install -r requirements.txt
- Start : python agent.py
- Health : GET / -> 200 OK

## Commandes Telegram
/aide /status /positions /report /pause /resume /urgence
