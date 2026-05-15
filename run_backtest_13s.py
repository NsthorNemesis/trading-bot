"""
run_backtest_13s.py — Lanza el backtest de 13 semanas (3 meses).

Cómo usar:
  1. Copia este archivo a la raíz del proyecto (donde está main.py)
  2. Asegúrate de que backtest_harness.py también está en la raíz
  3. Ejecuta:  python run_backtest_13s.py

Al terminar (~2-3 min) muestra el resumen y guarda:
  data/backtesting/backtest_real_resultado.json

Luego corre:  python analizar_backtest.py
para el análisis detallado por par y estrategia.
"""
import subprocess, sys, time
from pathlib import Path

HARNESS = Path("backtest_harness.py")
if not HARNESS.exists():
    print("[ERROR] backtest_harness.py no encontrado en el directorio actual.")
    print("  Cópialo desde la carpeta de outputs de Claude.")
    sys.exit(1)

print("╔══════════════════════════════════════════════════════╗")
print("║   BACKTEST 13 SEMANAS — Trading Bot v11              ║")
print("║   6 pares | 5 estrategias | Capital $200             ║")
print("╚══════════════════════════════════════════════════════╝")
print()
print("  Pares   : EUR_USD  GBP_USD  USD_JPY  USD_CHF  AUD_USD  USD_CAD")
print("  Período : ~13 semanas atrás → hoy")
print("  Salida  : data/backtesting/backtest_real_resultado.json")
print()
print("Iniciando… (puede tardar 2-4 minutos)\n")

t0 = time.time()
result = subprocess.run(
    [sys.executable, "backtest_harness.py", "--semanas", "13", "--capital", "200"],
    capture_output=False,   # salida directa a consola
)
elapsed = time.time() - t0

print(f"\n── Tiempo total: {elapsed:.1f}s")

if result.returncode == 0:
    print("\n✅ Backtest completado.")
    print("   Para el análisis detallado ejecuta:")
    print("   python analizar_backtest.py")
else:
    print(f"\n❌ El backtest terminó con código {result.returncode}.")
    print("   Revisa el log en logs/backtest_harness.log")
