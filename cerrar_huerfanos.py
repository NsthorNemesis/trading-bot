"""
cerrar_huerfanos.py
===================
Script de una sola ejecución para:
  1. Cerrar trades huérfanos en OANDA (ID=285, ID=352)
  2. Verificar el estado final de posiciones abiertas

Uso en el VPS:
  cd /root/trading_bot_v11
  source venv/bin/activate
  python3 cerrar_huerfanos.py

Después de ejecutar, puede eliminarse:
  rm cerrar_huerfanos.py
"""
import os, sys
from pathlib import Path

# Auto-detectar venv
VENV_PY = Path(__file__).parent / "venv" / "bin" / "python"
_in_venv = ("venv" in sys.executable or "VIRTUAL_ENV" in os.environ
             or sys.prefix != sys.base_prefix)
if VENV_PY.exists() and not _in_venv:
    import subprocess
    sys.exit(subprocess.run([str(VENV_PY)] + sys.argv).returncode)

import oandapyV20
import oandapyV20.endpoints.trades as oanda_trades
from dotenv import load_dotenv

load_dotenv()

TOKEN   = os.getenv("OANDA_ACCESS_TOKEN", "")
ACCOUNT = os.getenv("OANDA_ACCOUNT_ID", "")
ENV     = os.getenv("OANDA_ENVIRONMENT", "practice")

if not TOKEN or not ACCOUNT:
    print("ERROR: OANDA_ACCESS_TOKEN o OANDA_ACCOUNT_ID no encontrados en .env")
    sys.exit(1)

api = oandapyV20.API(access_token=TOKEN, environment=ENV)

# IDs a cerrar — ajustar si es necesario
HUERFANOS = ["285", "352"]

print("=" * 60)
print("  CIERRE DE TRADES HUÉRFANOS")
print("=" * 60)

# Estado actual antes de cerrar
print("\n▶ Posiciones abiertas actuales en OANDA:")
r = oanda_trades.OpenTrades(ACCOUNT)
api.request(r)
abiertas = r.response.get("trades", [])
for t in abiertas:
    pnl = float(t.get("unrealizedPL", 0))
    print(f"  ID={t['id']:6} | {t['instrument']:8} | units={t['currentUnits']:6} "
          f"| entry={t['price']} | PnL={pnl:+.4f} USD")

if not abiertas:
    print("  (ninguna posición abierta)")

# Cerrar las posiciones huérfanas
print(f"\n▶ Cerrando IDs huérfanos: {HUERFANOS}")
for trade_id in HUERFANOS:
    # Verificar que el trade sigue abierto
    ids_abiertos = [t["id"] for t in abiertas]
    if trade_id not in ids_abiertos:
        print(f"  ID={trade_id} — ya no está abierto, omitiendo")
        continue

    try:
        r_close = oanda_trades.TradeClose(ACCOUNT, tradeID=trade_id)
        api.request(r_close)
        fill = r_close.response.get("orderFillTransaction", {})
        precio_cierre = fill.get("price", "?")
        pnl_cierre    = fill.get("pl", "?")
        print(f"  ✅ ID={trade_id} cerrado | precio={precio_cierre} | PnL={pnl_cierre} USD")
    except Exception as e:
        print(f"  ❌ ID={trade_id} error al cerrar: {e}")

# Estado final
print("\n▶ Posiciones abiertas tras el cierre:")
r2 = oanda_trades.OpenTrades(ACCOUNT)
api.request(r2)
abiertas2 = r2.response.get("trades", [])
for t in abiertas2:
    pnl = float(t.get("unrealizedPL", 0))
    print(f"  ID={t['id']:6} | {t['instrument']:8} | units={t['currentUnits']:6} "
          f"| entry={t['price']} | PnL={pnl:+.4f} USD")
if not abiertas2:
    print("  (ninguna posición abierta)")

print("\n" + "=" * 60)
print("  Listo. Recuerda reiniciar el bot después de desplegar los fixes.")
print("  sudo systemctl restart trading_bot")
print("=" * 60)
