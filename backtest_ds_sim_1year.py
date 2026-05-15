"""
backtest_ds_sim_1year.py — Plan A M15 + DeepSeek simulado
==========================================================
Replica el comportamiento observado de DeepSeek en producción:
  - Aprueba ~55% de señales (rechazo ~45%)
  - Favorece RSI extremo (< 28 / > 72)
  - Favorece alineación con tendencia H4
  - Favorece MACD_DIF fuerte en EMA_Crossover
  - Penaliza señales débiles cerca del umbral
  - Algo de ruido aleatorio (±10%) para realismo

Motor compartido: backtest_engine.py
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backtest_engine import BacktestConfig, cargar_datos, run_backtest

PARAMS_FILE = Path("data/calibration/strategy_params.json")
OUT_FILE    = Path("data/backtesting/backtest_ds_sim_1year.json")

np.random.seed(42)


# ── DeepSeek simulado (~55% aprobación) ──────────────────────────────────────

def deepseek_aprueba(
    par, dir_, estrat, rsi, ema20, ema50, macd_dif,
    bbl, bbu, precio, trend_h4, regime, sesion
) -> bool:
    """
    Replica la lógica de filtrado de DeepSeek basada en comportamiento observado:
    - RSI_Bollinger: aprueba cuando RSI es realmente extremo y precio en banda
    - EMA_Crossover: aprueba cuando MACD confirma fuerte y H4 alinea
    - RSI_Divergence: aprueba cuando la divergencia es clara
    - Ruido: ~10% de decisiones aleatorias (simula incertidumbre del LLM)
    """
    score = 0.50

    if estrat == "RSI_Bollinger":
        if dir_ == "long":
            if   rsi < 25: score += 0.30
            elif rsi < 28: score += 0.18
            elif rsi < 30: score += 0.08
            else:          score -= 0.10
            dist_bbl = (bbl - precio) / bbl if bbl > 0 else 0
            if dist_bbl > 0.001: score += 0.10
            if trend_h4 == "up": score += 0.05
        else:
            if   rsi > 75: score += 0.30
            elif rsi > 72: score += 0.18
            elif rsi > 70: score += 0.08
            else:          score -= 0.10
            dist_bbu = (precio - bbu) / bbu if bbu > 0 else 0
            if dist_bbu > 0.001: score += 0.10
            if trend_h4 == "down": score += 0.05

    elif estrat == "EMA_Crossover":
        macd_abs = abs(macd_dif)
        if   macd_abs > 0.0008: score += 0.25
        elif macd_abs > 0.0004: score += 0.12
        elif macd_abs > 0.0001: score += 0.02
        else:                   score -= 0.20
        if dir_ == "long"  and trend_h4 == "up":   score += 0.20
        if dir_ == "short" and trend_h4 == "down":  score += 0.20
        if dir_ == "long"  and trend_h4 == "down":  score -= 0.30
        if dir_ == "short" and trend_h4 == "up":    score -= 0.30
        if trend_h4 == "rango":                      score -= 0.10

    elif estrat == "RSI_Divergence":
        if dir_ == "long"  and rsi < 42: score += 0.20
        if dir_ == "short" and rsi > 58: score += 0.20
        if dir_ == "long"  and rsi < 35: score += 0.10
        if dir_ == "short" and rsi > 65: score += 0.10
        if dir_ == "long"  and trend_h4 == "down": score += 0.05
        if dir_ == "short" and trend_h4 == "up":   score += 0.05

    if sesion == "overlap":    score += 0.08
    elif sesion == "london":   score += 0.03
    if estrat == "RSI_Bollinger" and regime < 0.35: score += 0.08
    if estrat == "EMA_Crossover" and regime > 0.60: score += 0.08

    score += np.random.uniform(-0.10, 0.10)
    return score > 0.50


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0  = time.time()
    cfg = BacktestConfig.desde_json(PARAMS_FILE)

    with open(PARAMS_FILE, encoding="utf-8") as f:
        raw_params = json.load(f)
    estrategias_activas = set(raw_params["estrategias_activas"])

    print("=" * 66)
    print("  BACKTEST PLAN A M15 + DeepSeek SIMULADO | 52 semanas")
    print("=" * 66)
    print(f"  Estrategias: {sorted(estrategias_activas)}")
    print(f"  Modelo DS: aprobación ~55% | basado en comportamiento producción")
    print()

    print("Cargando datos M15...")
    dfs = cargar_datos(cfg.pares, cfg.data_dir, verbose=True)
    print(f"  Cargado en {time.time() - t0:.1f}s\n")

    # Callback para output semanal verbose
    def _on_semana(semana, m, tracker):
        pass  # el verbose=True del run_backtest ya imprime la línea semanal

    resultado = run_backtest(
        cfg,
        estrategias = estrategias_activas,
        ds_func     = deepseek_aprueba,
        dfs_cache   = dfs,
        label       = "Plan A M15 + DS Sim",
        seed        = 42,
        verbose     = True,
    )

    # Resumen comparativo final
    r = resultado
    tot_pnl = r["pnl"]
    ds_por  = r["ds_por_estrat"]
    ds_tot  = sum(v["total"]    for v in ds_por.values())
    ds_apr  = sum(v["aprobadas"] for v in ds_por.values())

    print()
    print("═" * 66)
    print("  COMPARATIVA FINAL — 4 versiones")
    print("═" * 66)
    print(f"  {'Métrica':<24} {'V11 M15':>10} {'PlanA H1':>10} {'PlanA M15':>10} {'DS Sim':>10}")
    print(f"  {'─'*24} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")
    def row(n, a, b, c, d): print(f"  {n:<24} {a:>10} {b:>10} {c:>10} {d:>10}")
    row("Capital final",  "$173.34", "$36.27",  "$280.43", f"${r['capital']:.2f}")
    row("Retorno",        "-13.3%",  "-81.9%",  "+40.2%",  f"{tot_pnl/cfg.capital_ini*100:+.1f}%")
    row("Win Rate",       "33.0%",   "28.0%",   "34.5%",   f"{r['wr']:.1f}%")
    row("Max Drawdown",   "26.2%",   "86.0%",   "64.3%",   f"{r['dd']:.1f}%")
    row("Trades/año",     "594",     "644",     "1,705",   str(r["trades"]))
    row("Semanas +",      "11/52",   "20/52",   "25/52",   f"{r['sem_pos']}/52")
    print()
    if ds_tot > 0:
        print(f"  DeepSeek sim: {ds_apr}/{ds_tot} aprobadas "
              f"({ds_apr/ds_tot*100:.1f}% approval rate)")
    print()
    print("  Por estrategia (con DS):")
    for e, s in sorted(r["by_estrat"].items()):
        wr_e    = s["won"] / s["n"] if s["n"] > 0 else 0
        ds_e    = ds_por.get(e, {})
        apr_pct = ds_e.get("aprobadas", 0) / ds_e.get("total", 1) * 100
        print(f"    {e:<22}  N={s['n']:4d}  WR={wr_e*100:5.1f}%  "
              f"PnL=${s['pnl']:+.2f}  DS_apr={apr_pct:.0f}%")
    print(f"\n  Tiempo: {time.time() - t0:.1f}s")
    print("═" * 66)

    # Guardar JSON
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ds_stats = {
        "candidatos":    ds_tot,
        "aprobadas":     ds_apr,
        "rechazadas":    ds_tot - ds_apr,
        "approval_rate": round(ds_apr / ds_tot, 3) if ds_tot > 0 else 0,
        "por_estrategia": {
            e: {"total": v["total"], "aprobadas": v["aprobadas"],
                "tasa": round(v["aprobadas"] / v["total"], 3)}
            for e, v in ds_por.items()
        },
    }
    OUT_FILE.write_text(json.dumps({
        "config": {
            "version": "Plan A M15 + DeepSeek Sim",
            "capital_ini": cfg.capital_ini, "n_semanas": cfg.n_semanas,
            "sim_start":   str(cfg.sim_start.date()),
            "estrategias": sorted(estrategias_activas),
            "min_confidence": cfg.min_conf, "rr_ratio": cfg.rr_ratio,
            "timeframe": "M15", "deepseek": "simulado",
        },
        "deepseek_stats": ds_stats,
        "resumen": {
            "capital_final":      r["capital"],
            "pnl_total":          r["pnl"],
            "pnl_pct":            round(tot_pnl / cfg.capital_ini * 100, 2),
            "total_trades":       r["trades"],
            "win_rate_global":    round(r["wr"] / 100, 3),
            "max_drawdown_pct":   r["dd"],
            "semanas_positivas":  r["sem_pos"],
        },
        "comparativa": {
            "v11_m15":   {"capital_final": 173.34, "pnl_pct": -13.33, "wr": 0.33},
            "planA_h1":  {"capital_final":  36.27, "pnl_pct": -81.87, "wr": 0.28},
            "planA_m15": {"capital_final": 280.43, "pnl_pct":  40.20, "wr": 0.345},
        },
        "por_estrategia": {
            e: {"trades": s["n"],
                "win_rate": round(s["won"] / s["n"], 3) if s["n"] > 0 else 0,
                "pnl": round(s["pnl"], 2)}
            for e, s in r["by_estrat"].items()
        },
        "semanas": r["metricas"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  -> {OUT_FILE}")


if __name__ == "__main__":
    main()
