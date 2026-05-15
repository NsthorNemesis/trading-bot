"""
comparar_resultados.py — Busca todos los resultados de backtest guardados
y los compara cronológicamente.
"""
import json, os
from pathlib import Path
from datetime import datetime

def leer_json(ruta):
    p = Path(ruta)
    if not p.exists():
        return None
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            pass
    return None

# Buscar todos los archivos de resultado
candidatos = []
for patron in [
    "data/backtesting/*.json",
    "data/backtesting/historico/*.json",
    "data/resultados/*.json",
    "resultados/*.json",
    "backtest_*.json",
    "sim_*.json",
    "*.resultado.json",
]:
    for p in Path(".").glob(patron):
        candidatos.append(p)

# También buscar por nombre específico
for nombre in [
    "data/backtesting/backtest_real_resultado.json",
    "data/backtesting/backtest_resultado.json",
    "sim_30dias_resultado.json",
    "simulacion_resultado.json",
]:
    p = Path(nombre)
    if p.exists() and p not in candidatos:
        candidatos.append(p)

print(f"Archivos de resultado encontrados: {len(candidatos)}")
for p in sorted(candidatos):
    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    print(f"  {mtime}  {p}")

print()
print("═"*65)

resultados = []
for p in sorted(candidatos, key=lambda x: x.stat().st_mtime):
    d = leer_json(p)
    if not d:
        continue
    # Extraer métricas clave (distintos formatos)
    cap_ini  = d.get("capital_inicial") or d.get("capital_start") or d.get("initial_capital")
    cap_fin  = d.get("capital_final")   or d.get("capital_end")   or d.get("final_capital")
    retorno  = d.get("retorno_pct")     or d.get("return_pct")    or d.get("retorno_total")
    trades   = d.get("trades_total")    or d.get("total_trades")  or len(d.get("trades",[]))
    wr       = d.get("win_rate")        or d.get("winrate")
    pf       = d.get("profit_factor")  or d.get("pf")
    dd       = d.get("max_drawdown")   or d.get("drawdown")
    semanas  = d.get("semanas")        or d.get("weeks")

    if not cap_fin and not trades:
        continue  # No parece un resultado de backtest

    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    resultados.append({
        "archivo": p.name,
        "fecha":   mtime,
        "cap_ini": cap_ini,
        "cap_fin": cap_fin,
        "retorno": retorno,
        "trades":  trades,
        "wr":      wr,
        "pf":      pf,
        "dd":      dd,
        "semanas": semanas,
    })

if not resultados:
    print("No se encontraron resultados con formato reconocible.")
    print("\nListando contenido de data/backtesting/:")
    bt = Path("data/backtesting")
    if bt.exists():
        for f in sorted(bt.iterdir()):
            print(f"  {f.name}  ({f.stat().st_size} bytes)")
else:
    print(f"\n{'Archivo':35s} {'Fecha':17s} {'Cap.Ini':>8} {'Cap.Fin':>8} {'Ret%':>7} {'Trades':>7} {'WR%':>6} {'PF':>5} {'DD%':>6}")
    print("-"*110)
    for r in resultados:
        wr_pct = (r['wr']*100 if r['wr'] and r['wr']<1 else r['wr']) if r['wr'] else None
        dd_pct = (r['dd']*100 if r['dd'] and r['dd']<1 else r['dd']) if r['dd'] else None
        ret_pct= (r['retorno']*100 if r['retorno'] and abs(r['retorno'])<2 else r['retorno']) if r['retorno'] else None
        print(
            f"  {r['archivo']:33s} {r['fecha']:17s}"
            f" {str(r['cap_ini'] or '?'):>8}"
            f" {str(r['cap_fin'] or '?'):>8}"
            f" {f\"{ret_pct:+.1f}\" if ret_pct is not None else '?':>7}"
            f" {str(r['trades'] or '?'):>7}"
            f" {f\"{wr_pct:.1f}\" if wr_pct is not None else '?':>6}"
            f" {f\"{r['pf']:.2f}\" if r['pf'] else '?':>5}"
            f" {f\"{dd_pct:.1f}\" if dd_pct is not None else '?':>6}"
        )

    if len(resultados) >= 2:
        print("\n── COMPARACIÓN: PRIMERO vs ÚLTIMO ─────────────────────────────────")
        p0, p1 = resultados[0], resultados[-1]
        def fmt(v, mul=1, prefix=""):
            if v is None: return "?"
            v2 = float(v)*mul if abs(float(v)) < 2 and mul > 1 else float(v)
            return f"{prefix}{v2:+.2f}" if prefix else f"{v2:.2f}"

        print(f"  {'':25s}  {'PRIMERO':>12}  {'ÚLTIMO':>12}  {'DELTA':>10}")
        print("  " + "-"*60)

        campos = [
            ("Capital final $",  "cap_fin",  1,   "$", False),
            ("Retorno %",        "retorno",  100, "",  True),
            ("Trades",           "trades",   1,   "",  False),
            ("Win Rate %",       "wr",       100, "",  True),
            ("Profit Factor",    "pf",       1,   "",  False),
            ("Max Drawdown %",   "dd",       100, "",  True),
        ]
        for label, key, mul, prefix, is_pct in campos:
            v0 = p0.get(key)
            v1 = p1.get(key)
            if v0 is None and v1 is None:
                continue
            def conv(v):
                if v is None: return None
                f = float(v)
                if is_pct and abs(f) < 2: f *= 100
                return f
            f0, f1 = conv(v0), conv(v1)
            delta = f"{f1-f0:+.2f}" if f0 is not None and f1 is not None else "?"
            s0 = f"{f0:.2f}" if f0 is not None else "?"
            s1 = f"{f1:.2f}" if f1 is not None else "?"
            print(f"  {label:25s}: {s0:>12}  {s1:>12}  {delta:>10}")
