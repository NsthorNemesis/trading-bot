"""
backtest_variantes.py — 3 variantes en un solo run
====================================================
  A) RSI_Bollinger SOLO en M15
  B) Plan A M15 (RSI_Bollinger + EMA_Crossover + RSI_Divergence) + DS Estricto
  C) RSI_Bollinger SOLO + DS Estricto

Motor compartido: backtest_engine.py
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backtest_engine import BacktestConfig, cargar_datos, imprimir_resumen, run_backtest

PARAMS_FILE = Path("data/calibration/strategy_params.json")
OUT_FILE    = Path("data/backtesting/backtest_variantes.json")

np.random.seed(42)


# ── DeepSeek filtro ESTRICTO (~55% aprobación) ───────────────────────────────

def deepseek_estricto(
    par, dir_, estrat, rsi, ema20, ema50, macd_dif,
    bbl, bbu, precio, trend_h4, regime, sesion
) -> bool:
    """Umbral calibrado para ~55% de aprobación."""
    score = 0.50

    if estrat == "RSI_Bollinger":
        if dir_ == "long":
            if   rsi < 24: score += 0.35
            elif rsi < 27: score += 0.20
            elif rsi < 30: score += 0.08
            else:          score -= 0.18
            dist_bbl = (bbl - precio) / bbl if bbl > 0 else 0
            if dist_bbl > 0.002: score += 0.12
            elif dist_bbl > 0.001: score += 0.05
            if trend_h4 == "up": score += 0.06
        else:
            if   rsi > 76: score += 0.35
            elif rsi > 73: score += 0.20
            elif rsi > 70: score += 0.08
            else:          score -= 0.18
            dist_bbu = (precio - bbu) / bbu if bbu > 0 else 0
            if dist_bbu > 0.002: score += 0.12
            elif dist_bbu > 0.001: score += 0.05
            if trend_h4 == "down": score += 0.06

    elif estrat == "EMA_Crossover":
        macd_abs = abs(macd_dif)
        if   macd_abs > 0.0010: score += 0.30
        elif macd_abs > 0.0006: score += 0.15
        elif macd_abs > 0.0003: score += 0.03
        else:                   score -= 0.30
        if dir_ == "long"  and trend_h4 == "up":   score += 0.22
        if dir_ == "short" and trend_h4 == "down":  score += 0.22
        if dir_ == "long"  and trend_h4 == "down":  score -= 0.40
        if dir_ == "short" and trend_h4 == "up":    score -= 0.40
        if trend_h4 == "rango":                      score -= 0.20

    elif estrat == "RSI_Divergence":
        if dir_ == "long"  and rsi < 38: score += 0.25
        if dir_ == "short" and rsi > 62: score += 0.25
        if dir_ == "long"  and rsi < 30: score += 0.15
        if dir_ == "short" and rsi > 70: score += 0.15
        if dir_ == "long"  and trend_h4 == "down": score += 0.08
        if dir_ == "short" and trend_h4 == "up":   score += 0.08
        if dir_ == "long"  and trend_h4 == "up":   score -= 0.10
        if dir_ == "short" and trend_h4 == "down":  score -= 0.10

    if sesion == "overlap":  score += 0.07
    elif sesion == "london": score += 0.03
    if estrat == "RSI_Bollinger" and regime < 0.35: score += 0.07
    if estrat == "EMA_Crossover" and regime > 0.65: score += 0.07

    score += np.random.uniform(-0.08, 0.08)
    return score > 0.62


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0  = time.time()
    cfg = BacktestConfig.desde_json(PARAMS_FILE)

    print("=" * 70)
    print("  BACKTEST VARIANTES — RSI_Bollinger Solo + DS Estricto")
    print("=" * 70)
    print(f"\nCargando datos M15 ({len(cfg.pares)} pares)...")

    dfs = cargar_datos(cfg.pares, cfg.data_dir, verbose=True)
    print(f"  Listo en {time.time() - t0:.1f}s\n")

    variantes = [
        ("A  RSI_Bollinger Solo",          {"RSI_Bollinger"},                          None),
        ("B  Plan A M15 + DS Estricto",    {"RSI_Bollinger","EMA_Crossover","RSI_Divergence"}, deepseek_estricto),
        ("C  RSI_Bollinger + DS Estricto", {"RSI_Bollinger"},                          deepseek_estricto),
    ]

    resultados = []
    for label, estrats, ds_fn in variantes:
        print(f"▶ Corriendo variante: {label} ...")
        r = run_backtest(cfg, estrategias=estrats, ds_func=ds_fn,
                         dfs_cache=dfs, label=label, seed=42)
        resultados.append(r)
        roi_str = f"{r['roi']:+.1f}%"
        apr_str = f" | DS aprobación: {r['apr_rate']:.1f}%" if r["apr_rate"] else ""
        print(f"  → Capital: ${r['capital']:.2f} | ROI: {roi_str} | WR: {r['wr']}% "
              f"| DD: {r['dd']}% | Trades: {r['trades']}{apr_str}\n")

    # Tabla comparativa con resultados históricos
    historico = [
        {"label": "V11 M15 (actual)",      "capital": 173.34, "roi": -13.3, "wr": 33.0, "dd": 26.2, "trades": 594,  "sem_pos": "11/52", "apr_rate": None},
        {"label": "Plan A H1",             "capital":  36.27, "roi": -81.9, "wr": 28.0, "dd": 86.0, "trades": 644,  "sem_pos": "20/52", "apr_rate": None},
        {"label": "Plan A M15",            "capital": 280.43, "roi": +40.2, "wr": 34.5, "dd": 64.3, "trades": 1705, "sem_pos": "25/52", "apr_rate": None},
        {"label": "Plan A M15 + DS (86%)", "capital": 109.34, "roi": -45.3, "wr": 33.2, "dd": 66.7, "trades": 1586, "sem_pos": "23/52", "apr_rate": 86.1},
    ]

    imprimir_resumen(
        resultados, cfg,
        historico=historico,
        titulo="TABLA COMPARATIVA COMPLETA — 7 versiones",
    )

    print(f"\n  Tiempo total: {time.time() - t0:.1f}s")

    # Guardar JSON
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "variantes": [
            {k: v for k, v in r.items() if k not in ("by_estrat", "ds_por_estrat", "metricas")}
            for r in resultados
        ],
        "historico": historico,
    }, ensure_ascii=False, indent=2))
    print(f"  -> {OUT_FILE}")


if __name__ == "__main__":
    main()
