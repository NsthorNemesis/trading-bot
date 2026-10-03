"""Cierra los trades zombie 1267 y 1263 via OANDA API."""
import sys, os
sys.path.insert(0, '/root/trading_bot_v11')
from dotenv import load_dotenv
load_dotenv('/root/trading_bot_v11/.env')

import json, requests
from datetime import datetime, timezone
from pathlib import Path

TOKEN   = os.getenv('OANDA_ACCESS_TOKEN')
ACCOUNT = os.getenv('OANDA_ACCOUNT_ID')
BASE    = 'https://api-fxpractice.oanda.com/v3'
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
TRADES_LOG = Path('/root/trading_bot_v11/logs/trades.json')

ZOMBIES = ['1267', '1263']

for oid in ZOMBIES:
    url = f'{BASE}/accounts/{ACCOUNT}/trades/{oid}/close'
    r = requests.put(url, headers=HEADERS, json={})
    print(f'Trade {oid}: HTTP {r.status_code}')
    data = r.json()
    print(json.dumps(data, indent=2)[:400])

    # Actualizar trades.json con pnl y closed_at
    if r.status_code == 200:
        try:
            fill = data.get('orderFillTransaction', {})
            pnl  = float(fill.get('pl', 0))
            trades = json.loads(TRADES_LOG.read_text())
            for t in trades:
                if t.get('oanda_id') == oid and 'pnl' not in t:
                    t['pnl']       = round(pnl, 4)
                    t['closed_at'] = datetime.now(timezone.utc).isoformat()
                    t['estado']    = 'cerrado'
                    print(f'  -> PnL registrado: {pnl}')
                    break
            TRADES_LOG.write_text(json.dumps(trades, indent=2))
        except Exception as e:
            print(f'  Error actualizando trades.json: {e}')
    print()
