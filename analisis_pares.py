"""
analisis_pares.py — Trading Bot v11
=====================================
Análisis completo por par:
  1. Rendimiento real en backtest (trades_log.json)
  2. Características del histórico M15 (ATR, tendencia, volatilidad, spread estimado)
  3. Ranking final con veredicto: MANTENER / REDUCIR / CORTAR

Uso (desde C:\\Users\\na_sc\\trading_bot_v11\\):
    python analisis_pares.py
"""
import json, csv, os
from pathlib import Path
from collections import defaultdict
import statistics

BASE = Path(".")
TRADES_LOG   = BASE / "data" / "trades" / "trades_log.json"
RESULT_FILE  = BASE / "data" / "backtesting" / "backtest_real_resultado.json"
HIST_DIR     = BASE / "data" / "historical"

SEP  = "═" * 66
SEP2 = "─" * 66

# ── Spread estimado por par (pips) — referencia OANDA ──────────────
SPREAD_EST = {
    "EUR_USD": 0.8, "GBP_USD": 1.2, "USD_JPY": 0.9,
    "USD_CAD": 1.1, "AUD_USD": 1.0, "NZD_USD": 1.5,
    "EUR_GBP": 1.4, "EUR_JPY": 1.3, "GBP_JPY": 2.0,
    "USD_CHF": 1.0, "AUD_JPY": 1.6, "EUR_CAD": 1.8,
}

def pip_value(par):
    jpy_pairs = ["JPY"]
    return 0.01 if any(x in par for x in jpy_pairs) else 0.0001

print(f"\n{SEP}")
print("  ANÁLISIS DE PARES — Trading Bot v11")
print(f"{SEP}\n")

# ══════════════════════════════════════════════════════════════════
# 1. Cargar trades del backtest
# ══════════════════════════════════════════════════════════════════
trades_log = []
if TRADES_LOG.exists():
    try:
        trades_log = json.loads(TRADES_LOG.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] trades_log: {e}")

resultado = {}
if RESULT_FILE.exists():
    try:
        resultado = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════
# 2. Estadísticas de trades por par
# ══════════════════════════════════════════════════════════════════
pares_trades = defaultdict(lambda: {
    "wins": 0, "losses": 0, "pnl_list": [], "pnl_total": 0.0,
    "estrategias": defaultdict(int),
})

for t in trades_log:
    if "pnl" not in t:
        continue
    par = t.get("par", t.get("instrument", "UNKNOWN")).replace("/", "_")
    pnl = float(t.get("pnl", 0))
    pares_trades[par]["pnl_list"].append(pnl)
    pares_trades[par]["pnl_total"] += pnl
    if pnl > 0:
        pares_trades[par]["wins"] += 1
    else:
        pares_trades[par]["losses"] += 1
    est = t.get("estrategia", t.get("strategy", "backtest"))
    pares_trades[par]["estrategias"][est] += 1

# ══════════════════════════════════════════════════════════════════
# 3. Análisis del histórico M15 por par
# ══════════════════════════════════════════════════════════════════
def leer_m15(par):
    """Lee el CSV de M15 para el par dado. Devuelve lista de dicts."""
    nombres = [
        f"{par}_M15.csv", f"{par}_m15.csv",
        f"{par.replace('_','')}_M15.csv",
        f"M15_{par}.csv",
    ]
    for nombre in nombres:
        ruta = HIST_DIR / nombre
        if ruta.exists():
            filas = []
            try:
                with open(ruta, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        filas.append(row)
            except Exception:
                pass
            return filas
    # Buscar en subcarpetas
    for sub in HIST_DIR.iterdir() if HIST_DIR.exists() else []:
        if sub.is_dir() and par.replace("_","").upper() in sub.name.upper():
            for archivo in sub.glob("*.csv"):
                if "15" in archivo.name or "m15" in archivo.name.lower():
                    filas = []
                    try:
                        with open(archivo, encoding="utf-8") as f:
                            reader = csv.DictReader(f)
                            for row in reader:
                                filas.append(row)
                    except Exception:
                        pass
                    return filas
    return []

def calcular_atr(velas, periodo=14):
    """ATR simple sobre lista de velas con keys high/low/close."""
    trs = []
    for i in range(1, len(velas)):
        try:
            h = float(velas[i].get("high", velas[i].get("High", 0)))
            l = float(velas[i].get("low",  velas[i].get("Low", 0)))
            pc = float(velas[i-1].get("close", velas[i-1].get("Close", 0)))
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        except Exception:
            continue
    if not trs:
        return 0
    return statistics.mean(trs[-periodo*10:]) if len(trs) >= periodo else statistics.mean(trs)

def calcular_tendencia(velas, ventana=200):
    """Slope normalizado de EMA simple sobre los últimos N cierres."""
    cierres = []
    for v in velas[-ventana:]:
        try:
            cierres.append(float(v.get("close", v.get("Close", 0))))
        except Exception:
            continue
    if len(cierres) < 50:
        return 0
    n = len(cierres)
    xs = list(range(n))
    xm = statistics.mean(xs)
    ym = statistics.mean(cierres)
    num = sum((xs[i] - xm) * (cierres[i] - ym) for i in range(n))
    den = sum((xs[i] - xm) ** 2 for i in range(n))
    slope = num / den if den else 0
    return slope / (ym if ym else 1) * 1000  # normalizado

def calcular_rango_diario(velas):
    """Promedio del rango H-L de cada día (agrupando velas M15)."""
    dias = defaultdict(lambda: {"h": -1e9, "l": 1e9})
    for v in velas:
        ts = v.get("time", v.get("Time", v.get("datetime", "")))
        dia = str(ts)[:10]
        try:
            h = float(v.get("high", v.get("High", 0)))
            l = float(v.get("low", v.get("Low", 0)))
            dias[dia]["h"] = max(dias[dia]["h"], h)
            dias[dia]["l"] = min(dias[dia]["l"], l)
        except Exception:
            continue
    rangos = [d["h"] - d["l"] for d in dias.values() if d["h"] > d["l"]]
    return statistics.mean(rangos) if rangos else 0

# Detectar pares disponibles
pares_disponibles = set()
if HIST_DIR.exists():
    for f in HIST_DIR.rglob("*.csv"):
        nombre = f.stem.upper()
        for par in list(SPREAD_EST.keys()) + list(pares_trades.keys()):
            p_clean = par.replace("_","").upper()
            if p_clean in nombre or par.upper() in nombre:
                pares_disponibles.add(par)
    for sub in HIST_DIR.iterdir():
        if sub.is_dir():
            for par in SPREAD_EST:
                if par.replace("_","").upper() in sub.name.upper():
                    pares_disponibles.add(par)

# Añadir pares que aparecen en trades aunque no tengan histórico detectado
pares_disponibles.update(pares_trades.keys())

if not pares_disponibles:
    print("  [INFO] No se encontraron datos históricos en data/historical/")
    print("         Asegúrate de correr desde C:\\Users\\na_sc\\trading_bot_v11\\")
    print("         Mostrando solo análisis de trades_log...\n")

pares_hist = {}
print(f"  Leyendo histórico M15...")
for par in sorted(pares_disponibles):
    velas = leer_m15(par)
    if velas:
        atr   = calcular_atr(velas)
        trend = calcular_tendencia(velas)
        rango = calcular_rango_diario(velas)
        pip   = pip_value(par)
        pares_hist[par] = {
            "velas": len(velas),
            "atr_pips": round(atr / pip, 1) if pip else 0,
            "rango_diario_pips": round(rango / pip, 1) if pip else 0,
            "tendencia": round(trend, 4),
        }
        print(f"    {par:<12} {len(velas):>7} velas  ATR={round(atr/pip,1) if pip else '?':>6} pips  Rango/día={round(rango/pip,1) if pip else '?':>6} pips")
    else:
        print(f"    {par:<12} sin CSV encontrado")

# ══════════════════════════════════════════════════════════════════
# 4. Calcular Profit Factor por par
# ══════════════════════════════════════════════════════════════════
def profit_factor(pnl_list):
    ganancias = sum(p for p in pnl_list if p > 0)
    perdidas  = abs(sum(p for p in pnl_list if p < 0))
    return round(ganancias / perdidas, 2) if perdidas > 0 else (99.0 if ganancias > 0 else 0.0)

def esperanza(pnl_list):
    return statistics.mean(pnl_list) if pnl_list else 0

# ══════════════════════════════════════════════════════════════════
# 5. Tabla ranking por par
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP2}")
print("RANKING DE PARES — rendimiento backtest")
print(f"{SEP2}")

todos_pares = pares_disponibles | set(pares_trades.keys())
filas = []

for par in todos_pares:
    td = pares_trades.get(par, {})
    wins   = td.get("wins", 0)
    losses = td.get("losses", 0)
    total  = wins + losses
    pnl_list = td.get("pnl_list", [])
    pnl_tot  = td.get("pnl_total", 0.0)
    wr  = (wins / total * 100) if total > 0 else 0
    pf  = profit_factor(pnl_list)
    esp = esperanza(pnl_list)
    hist = pares_hist.get(par, {})
    atr_pips  = hist.get("atr_pips", 0)
    rango_pip = hist.get("rango_diario_pips", 0)
    spread    = SPREAD_EST.get(par, 1.5)
    score_spread = atr_pips / spread if spread and atr_pips else 0  # ATR/spread → mayor es mejor

    filas.append({
        "par": par,
        "total": total,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "pf": pf,
        "pnl": pnl_tot,
        "esp": esp,
        "atr": atr_pips,
        "rango": rango_pip,
        "spread": spread,
        "score_spread": score_spread,
    })

# Ordenar por PnL desc, luego PF
filas.sort(key=lambda x: (x["pnl"], x["pf"]), reverse=True)

def veredicto(f):
    if f["total"] == 0:
        # Sin trades — juzgar solo por spread ratio
        if f["score_spread"] >= 15:
            return "⚠️  SIN DATOS"
        return "❓ SIN DATOS"
    if f["pf"] >= 1.2 and f["wr"] >= 35:
        return "✅ MANTENER"
    if f["pf"] >= 1.0 and f["pnl"] >= 0:
        return "⚠️  REVISAR"
    if f["pf"] < 1.0 or f["pnl"] < -2:
        return "❌ CORTAR"
    return "⚠️  REVISAR"

print(f"\n  {'Par':<12} {'Trades':>7} {'WR':>7} {'PF':>6} {'PnL':>9} {'ATR pip':>8} {'Spread':>7}  Veredicto")
print(f"  {'-'*12} {'-'*7} {'-'*7} {'-'*6} {'-'*9} {'-'*8} {'-'*7}  {'-'*12}")

for f in filas:
    verd = veredicto(f)
    print(
        f"  {f['par']:<12} {f['total']:>7} {f['wr']:>6.1f}% "
        f"{f['pf']:>6.2f} {f['pnl']:>+8.2f} "
        f"{f['atr']:>8.1f} {f['spread']:>6.1f}p  {verd}"
    )

# ══════════════════════════════════════════════════════════════════
# 6. Análisis de ATR vs spread (liquidez estructural)
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP2}")
print("ANÁLISIS ESTRUCTURAL — ATR vs Spread (independiente del backtest)")
print(f"{SEP2}")
print(f"\n  El ratio ATR/Spread indica cuántas veces el movimiento real")
print(f"  supera el costo de entrada. >20 = excelente, 10-20 = aceptable,")
print(f"  <10 = el spread come demasiado del movimiento.")
print()

hist_filas = [(par, d) for par, d in pares_hist.items() if d.get("atr_pips")]
hist_filas.sort(key=lambda x: x[1]["atr_pips"] / SPREAD_EST.get(x[0], 1.5), reverse=True)

print(f"  {'Par':<12} {'ATR (pip)':>10} {'Rango/día':>10} {'Spread':>8} {'Ratio':>8}  Calidad")
print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*8}  {'-'*10}")
for par, d in hist_filas:
    sp = SPREAD_EST.get(par, 1.5)
    ratio = d["atr_pips"] / sp if sp else 0
    calidad = "🟢 Excelente" if ratio >= 20 else ("🟡 Aceptable" if ratio >= 10 else "🔴 Evitar")
    print(
        f"  {par:<12} {d['atr_pips']:>10.1f} {d['rango_diario_pips']:>10.1f} "
        f"{sp:>8.1f} {ratio:>8.1f}  {calidad}"
    )

# ══════════════════════════════════════════════════════════════════
# 7. Veredicto consolidado
# ══════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("VEREDICTO FINAL")
print(f"{SEP}\n")

mantener = [f["par"] for f in filas if "MANTENER" in veredicto(f)]
revisar  = [f["par"] for f in filas if "REVISAR"  in veredicto(f)]
cortar   = [f["par"] for f in filas if "CORTAR"   in veredicto(f)]
sin_datos = [f["par"] for f in filas if "SIN DATOS" in veredicto(f)]

if mantener:
    print(f"  ✅ MANTENER  ({len(mantener)}): {', '.join(mantener)}")
if revisar:
    print(f"  ⚠️  REVISAR   ({len(revisar)}): {', '.join(revisar)}")
if cortar:
    print(f"  ❌ CORTAR    ({len(cortar)}): {', '.join(cortar)}")
if sin_datos:
    print(f"  ❓ SIN DATOS ({len(sin_datos)}): {', '.join(sin_datos)} — esperar backtest 52 sem")

print()
if cortar:
    print("  Acción recomendada para pares a cortar:")
    print("    1. Abrir agents/signal_agent/signal_agent.py")
    print("    2. Remover los pares de la lista INSTRUMENTS")
    print(f"       Pares a eliminar: {cortar}")
    print("    3. Reiniciar el sistema")
    print()
    print("  Alternativa conservadora:")
    print("    - Subir min_confidence a 0.60 solo para esos pares")
    print("    - Observar 2 semanas antes de cortar definitivamente")

print(f"\n{SEP}\n")
