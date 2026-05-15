"""
run_backtest_anual.py
=====================
Wrapper para correr backtest_harness.py con:
  - 52 semanas (1 año completo)
  - DeepSeek REAL para señales y SL/TP
  - OANDA simulado (sin órdenes reales)
  - Telegram desactivado (solo logs locales)
  - Resultados guardados en data/backtesting/backtest_harness_anual.json

Uso:
  python run_backtest_anual.py

Duración estimada: 20–60 minutos (depende de velocidad de DeepSeek API)
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# ── Desactivar Telegram para no recibir spam durante el backtest ──────────────
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

# ── Asegurar que estamos en el directorio correcto ────────────────────────────
ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

print("=" * 60)
print("  BACKTEST ANUAL — Trading Bot v11")
print("  Agentes reales + DeepSeek + OANDA simulado")
print("=" * 60)
print(f"  Inicio     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"  Semanas    : 52 (1 año completo)")
print(f"  Capital    : $200.00")
print(f"  Estrategias: RSI_Bollinger, RSI_Divergence")
print(f"  Telegram   : DESACTIVADO")
print(f"  DeepSeek   : ACTIVO")
print("=" * 60)
print()

# ── Verificar .env y DeepSeek key ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
if not ds_key:
    print("⚠️  DEEPSEEK_API_KEY no encontrado en .env")
    print("   El harness usará el fallback matemático para SL/TP")
    print()
else:
    print(f"✅  DeepSeek API key detectado ({ds_key[:8]}...)")
    print()

# ── Verificar datos históricos ────────────────────────────────────────────────
hist_dir = ROOT / "data" / "historical"
pares_necesarios = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
faltantes = [p for p in pares_necesarios if not (hist_dir / f"{p}_M15.json").exists()]

if faltantes:
    print(f"❌  Faltan datos M15 para: {faltantes}")
    print(f"   Verifica la carpeta: {hist_dir}")
    sys.exit(1)
else:
    print(f"✅  Datos históricos M15 disponibles para {len(pares_necesarios)} pares")
    print()

# ── Redirigir salida del harness a archivo de log ─────────────────────────────
log_file = ROOT / "logs" / "backtest_harness_anual.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

print(f"📋  Log completo: {log_file}")
print(f"📊  Resultados : data/backtesting/backtest_harness_anual.json")
print()
print("Iniciando simulación... (puede tardar 20–60 minutos)")
print("-" * 60)

# ── Parchear output file del harness ─────────────────────────────────────────
# Modificar temporalmente la constante de salida
import importlib
import types

# Importar y parchear antes de correr
from backtest_harness import BacktestHarness
import backtest_harness as _bh

# Redirigir archivo de resultados
_bh.RESULTS_FILE = str(ROOT / "data" / "backtesting" / "backtest_harness_anual.json")

# ── Correr el harness ─────────────────────────────────────────────────────────
harness = BacktestHarness(
    capital   = 200.0,
    n_semanas = 52,
)

try:
    harness.correr()
    print()
    print("=" * 60)
    print("  SIMULACIÓN COMPLETADA")
    print(f"  Fin: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Leer y mostrar resumen
    results_path = ROOT / "data" / "backtesting" / "backtest_harness_anual.json"
    if results_path.exists():
        with open(results_path) as f:
            data = json.load(f)
        r = data.get("resumen", data)
        print()
        print(f"  Capital final   : ${r.get('capital_final', 0):.2f}")
        print(f"  PnL total       : ${r.get('pnl_total', 0):+.2f} ({r.get('pnl_pct', 0):.1f}%)")
        print(f"  Total trades    : {r.get('total_trades', 0)}")
        print(f"  Win rate        : {r.get('win_rate_global', 0)*100:.1f}%")
        print(f"  Max drawdown    : {r.get('max_drawdown_pct', 0):.1f}%")
        print()
        print(f"  Resultados guardados en:")
        print(f"  {results_path}")

except KeyboardInterrupt:
    print()
    print("⛔  Simulación interrumpida por el usuario.")
    print(f"   Resultados parciales en: {log_file}")
except Exception as e:
    print()
    print(f"❌  Error durante la simulación: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
