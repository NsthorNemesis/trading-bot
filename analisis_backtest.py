"""
analisis_backtest.py — Trading Bot v11
========================================
Análisis completo del backtest de 4 semanas.
Compara con baseline (30 días sin rolling window).

Uso (desde C:\\Users\\na_sc\\trading_bot_v11\\):
    python analisis_backtest.py
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# ── Rutas ─────────────────────────────────────────────────────────
RESULT_FILE  = Path("data/backtesting/backtest_real_resultado.json")
TRADES_LOG   = Path("data/trades/trades_log.json")
PARAMS_FILE  = Path("data/calibration/strategy_params.json")

# ── Baseline (simulación anterior sin rolling window) ─────────────
BASELINE = {
    "retorno":    22.18,
    "wr":         38.5,
    "pf":         1.10,
    "max_dd":     42.5,
    "trades":     192,
    "capital_ini": 200.0,
    "capital_fin": 244.36,
}

SEP  = "═" * 60
SEP2 = "─" * 60

def pct_change(new, old):
    if old == 0:
        return 0
    return ((new - old) / abs(old)) * 100

def signo(val):
    return "+" if val >= 0 else ""

def semaforo(nueva, baseline, mayor_es_mejor=True):
    diff = nueva - baseline
    if mayor_es_mejor:
        return "✅" if diff > 0 else ("⚠️ " if diff > -5 else "❌")
    else:
        return "✅" if diff < 0 else ("⚠️ " if diff < 5 else "❌")

# ══════════════════════════════════════════════════════════════════
# Cargar datos
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  ANÁLISIS POST-BACKTEST — 4 SEMANAS (Rolling Window)")
print(f"{SEP}\n")

if not RESULT_FILE.exists():
    print(f"[ERROR] No encontrado: {RESULT_FILE}")
    print("  Asegúrate de correr desde C:\\Users\\na_sc\\trading_bot_v11\\")
    exit(1)

with open(RESULT_FILE, encoding="utf-8") as f:
    resultado = json.load(f)

trades_log = []
if TRADES_LOG.exists():
    try:
        trades_log = json.loads(TRADES_LOG.read_text(encoding="utf-8"))
    except Exception:
        pass

params_nuevos = {}
if PARAMS_FILE.exists():
    try:
        params_nuevos = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════
# 1. Métricas globales vs Baseline
# ══════════════════════════════════════════════════════════════════
print("1. MÉTRICAS GLOBALES")
print(f"{SEP2}")

cap_ini  = resultado.get("capital_inicial", 200.0)
cap_fin  = resultado.get("capital_final",   cap_ini)
retorno  = resultado.get("retorno_total",   0.0)
wr       = resultado.get("win_rate",        0.0)
pf       = resultado.get("profit_factor",   0.0)
max_dd   = resultado.get("max_drawdown",    0.0)
n_trades = resultado.get("trades_totales",  0)

# Convertir a % si vienen en decimales
if isinstance(retorno, float) and abs(retorno) < 5:
    retorno = retorno * 100
if isinstance(wr, float) and wr <= 1.0:
    wr = wr * 100
if isinstance(max_dd, float) and max_dd <= 1.0:
    max_dd = max_dd * 100

metricas = [
    ("Retorno total",  retorno,  BASELINE["retorno"],  True,  "%"),
    ("Win Rate",       wr,       BASELINE["wr"],       True,  "%"),
    ("Profit Factor",  pf,       BASELINE["pf"],       True,  "x"),
    ("Max Drawdown",   max_dd,   BASELINE["max_dd"],   False, "%"),
    ("Trades totales", n_trades, BASELINE["trades"],   True,  ""),
]

print(f"  {'Métrica':<18} {'Baseline':>10} {'4-Semanas':>10} {'Cambio':>10}  ")
print(f"  {'-'*18} {'-'*10} {'-'*10} {'-'*10}")
for nombre, nueva, base, mayor_mejor, unidad in metricas:
    diff  = nueva - base
    emoji = semaforo(nueva, base, mayor_mejor)
    print(f"  {nombre:<18} {base:>9.1f}{unidad} {nueva:>9.1f}{unidad} "
          f"{signo(diff)}{diff:>8.1f}{unidad}  {emoji}")

print(f"\n  Capital: ${cap_ini:.2f} → ${cap_fin:.2f}")

# ══════════════════════════════════════════════════════════════════
# 2. Análisis por par (desde trades_log)
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP2}")
print("2. RENDIMIENTO POR PAR")
print(f"{SEP2}")

if trades_log:
    pares = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades_log:
        if "pnl" not in t:
            continue
        par = t.get("par", "UNKNOWN")
        pnl = float(t.get("pnl", 0))
        if pnl > 0:
            pares[par]["wins"] += 1
        else:
            pares[par]["losses"] += 1
        pares[par]["pnl"] += pnl

    print(f"  {'Par':<12} {'Trades':>7} {'WR':>7} {'PnL':>10} {'Estado'}")
    print(f"  {'-'*12} {'-'*7} {'-'*7} {'-'*10} {'-'*8}")

    ranking = sorted(pares.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for par, stats in ranking:
        total = stats["wins"] + stats["losses"]
        wr_par = (stats["wins"] / total * 100) if total > 0 else 0
        pnl_par = stats["pnl"]
        estado = "⭐ MEJOR" if pnl_par > 0 and wr_par >= 45 else (
                  "❌ CORTAR" if pnl_par < -5 else "➖ NEUTRO")
        print(f"  {par:<12} {total:>7} {wr_par:>6.1f}% {pnl_par:>+9.2f}  {estado}")
else:
    print("  Sin datos en trades_log.json")
    # Usar calibraciones del resultado si están disponibles
    cals = resultado.get("calibraciones", [])
    if cals:
        print(f"  ({len(cals)} calibraciones registradas en el resultado)")

# ══════════════════════════════════════════════════════════════════
# 3. Análisis por estrategia
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP2}")
print("3. RENDIMIENTO POR ESTRATEGIA")
print(f"{SEP2}")

if trades_log:
    estrategias = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades_log:
        if "pnl" not in t:
            continue
        est = t.get("estrategia", "UNKNOWN")
        pnl = float(t.get("pnl", 0))
        if pnl > 0:
            estrategias[est]["wins"] += 1
        else:
            estrategias[est]["losses"] += 1
        estrategias[est]["pnl"] += pnl

    print(f"  {'Estrategia':<22} {'Trades':>7} {'WR':>7} {'PnL':>10} {'Acción'}")
    print(f"  {'-'*22} {'-'*7} {'-'*7} {'-'*10} {'-'*10}")

    ranking_est = sorted(estrategias.items(),
                         key=lambda x: x[1]["pnl"], reverse=True)
    for est, stats in ranking_est:
        total = stats["wins"] + stats["losses"]
        wr_est = (stats["wins"] / total * 100) if total > 0 else 0
        pnl_est = stats["pnl"]
        accion = "✅ MANTENER" if pnl_est > 0 else (
                  "❌ PAUSAR" if pnl_est < -3 else "⚠️  REVISAR")
        print(f"  {est:<22} {total:>7} {wr_est:>6.1f}% {pnl_est:>+9.2f}  {accion}")
else:
    print("  Sin datos en trades_log.json")

# ══════════════════════════════════════════════════════════════════
# 4. Calibraciones realizadas
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP2}")
print("4. CALIBRACIONES DURANTE LA SIMULACIÓN")
print(f"{SEP2}")

calibraciones = resultado.get("calibraciones", [])
if calibraciones:
    print(f"  Total calibraciones: {len(calibraciones)}")
    print()
    for i, cal in enumerate(calibraciones, 1):
        ts  = cal.get("timestamp", cal.get("ts", "?"))
        sem = cal.get("semana", "?")
        n   = cal.get("trades_analizados", cal.get("n_trades", "?"))
        wr_c = cal.get("wr", None)
        pf_c = cal.get("pf", None)
        linea = f"  #{i:02d}  Semana {sem}  {ts[:16] if isinstance(ts, str) else ts}"
        if wr_c is not None:
            linea += f"  WR={wr_c:.1%}  PF={pf_c:.2f}" if pf_c else f"  WR={wr_c:.1%}"
        if n:
            linea += f"  ({n} trades)"
        print(linea)
else:
    print("  Sin registro de calibraciones en el resultado.")
    print("  Verifica que CALIBRACION_SCHEDULE se activó durante la simulación.")

# ══════════════════════════════════════════════════════════════════
# 5. Parámetros finales (strategy_params.json)
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP2}")
print("5. PARÁMETROS FINALES (strategy_params.json)")
print(f"{SEP2}")

if params_nuevos:
    campos = ["rr_ratio", "sl_atr_mult", "min_sl_pips",
              "riesgo_pct", "min_confidence", "cooldown_minutos"]
    for campo in campos:
        val = params_nuevos.get(campo, "—")
        if isinstance(val, float):
            if campo == "riesgo_pct":
                print(f"  {campo:<22} {val:.3%}")
            else:
                print(f"  {campo:<22} {val}")
        else:
            print(f"  {campo:<22} {val}")

    activas = params_nuevos.get("estrategias_activas", [])
    if activas:
        print(f"\n  Estrategias activas: {', '.join(activas)}")
else:
    print("  strategy_params.json no encontrado o vacío.")

# ══════════════════════════════════════════════════════════════════
# 6. Veredicto y recomendaciones
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("6. VEREDICTO Y PRÓXIMOS PASOS")
print(f"{SEP}\n")

mejoro_wr  = wr    > BASELINE["wr"]
mejoro_pf  = pf    > BASELINE["pf"]
bajo_dd    = max_dd < BASELINE["max_dd"]
es_rentable = retorno > 0

print(f"  Rentabilidad:   {'✅ Positiva' if es_rentable else '❌ Negativa'}")
print(f"  Win Rate:       {'✅ Mejora vs baseline' if mejoro_wr else '❌ Empeora vs baseline'}")
print(f"  Profit Factor:  {'✅ Mejora vs baseline' if mejoro_pf else '❌ Empeora vs baseline'}")
print(f"  Drawdown:       {'✅ Menor que baseline' if bajo_dd else '❌ Mayor que baseline'}")

print()
if mejoro_pf and bajo_dd:
    print("  ✅ Rolling window mejora la calidad de calibración.")
    print("     Los parámetros finales son candidatos para live trading.")
    print()
    print("  Próximos pasos:")
    print("    1. Subir min_confidence a 0.50-0.55 para filtrar señales débiles")
    print("    2. Aplicar strategy_params.json resultante al live bot")
    print("    3. Iniciar paper trading 1-2 semanas antes de ir live")
elif not bajo_dd:
    print("  ⚠️  El drawdown sigue siendo elevado.")
    print("     Acción prioritaria: reducir riesgo por trade.")
    print()
    print("  Próximos pasos:")
    print("    1. Reducir riesgo_pct de 0.5% a 0.3% (máx pérdida por trade)")
    print("    2. Subir min_confidence a 0.50 para entrar en menos trades")
    print("    3. Repetir simulación de 2 semanas con esos parámetros")
else:
    print("  ⚠️  Resultados mixtos. Revisar estrategias individuales.")
    print("     Pausar estrategias con PnL negativo consistente.")

print(f"\n{SEP}\n")
