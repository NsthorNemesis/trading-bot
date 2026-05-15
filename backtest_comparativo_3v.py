"""
backtest_comparativo_3v.py — Comparativo definitivo de 3 configuraciones
=========================================================================
Corre las 3 variantes sobre los MISMOS datos, mismo período, mismo motor.

  Variante A — RSI_Bollinger solo
  Variante B — EMA_Crossover + Hammer + Doji  (config "óptima" 2026-05-14)
  Variante C — RSI_Bollinger + RSI_Divergence  (propuesta)

Motor: backtest_engine.py (compartido, determinista, seed=42)
Datos: 52 semanas M15 desde 2025-05-09 | 6 pares
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backtest_engine import BacktestConfig, cargar_datos, run_backtest

# ── Parámetros base (iguales para las 3 variantes) ───────────────────────────
CAPITAL_INI = 200.0
N_SEMANAS   = 52
SIM_START   = datetime(2025, 5, 9, tzinfo=timezone.utc)
PARES       = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
DATA_DIR    = ROOT / "data" / "historical"

BASE_CONFIG = BacktestConfig(
    capital_ini   = CAPITAL_INI,
    n_semanas     = N_SEMANAS,
    sim_start     = SIM_START,
    pares         = PARES,
    data_dir      = DATA_DIR,
    min_conf      = 0.40,
    sl_atr_mult   = 2.0,
    min_sl_pips   = 10.0,
    max_sl_pips   = 50.0,
    rr_ratio      = 2.0,
    cooldown_mins = 15,
    max_pos       = 3,
    riesgo_pct    = 0.015,
    sesiones_act  = {"london", "new_york"},   # overlap excluido — backtest 3y lo penaliza
)


# ── Descarga de datos si no existen ──────────────────────────────────────────

def _descargar_datos():
    """Descarga 52 semanas de velas M15 para los 6 pares vía OANDA."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Leer .env manualmente (heredoc no soporta load_dotenv)
    env_path = ROOT / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    token   = os.environ["OANDA_ACCESS_TOKEN"]
    account = os.environ["OANDA_ACCOUNT_ID"]

    import oandapyV20
    import oandapyV20.endpoints.instruments as instruments

    client = oandapyV20.API(access_token=token, environment="practice")

    # 52 semanas ≈ 364 días × 96 velas/día = ~34,944 velas por par
    # OANDA limita a 5000 por request → necesitamos ~7 requests por par
    from datetime import timedelta

    print("  Descargando datos históricos M15 (52 semanas)...")

    for par in PARES:
        out_file = DATA_DIR / f"{par}_M15.json"
        if out_file.exists():
            existing = json.loads(out_file.read_text())
            if len(existing) >= 30_000:
                print(f"    {par}: ya existe ({len(existing):,} velas) — saltando")
                continue

        all_candles = []
        # Bajar en bloques desde SIM_START hasta hoy
        bloque_start = SIM_START
        bloque_size  = timedelta(weeks=8)   # ~8 semanas por request

        while bloque_start < datetime.now(timezone.utc):
            bloque_end = min(bloque_start + bloque_size, datetime.now(timezone.utc))
            params = {
                "granularity": "M15",
                "from": bloque_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":   bloque_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "price": "M",
            }
            try:
                ep  = instruments.InstrumentsCandles(instrument=par, params=params)
                rv  = client.request(ep)
                raw = rv.get("candles", [])
                for c in raw:
                    mid = c.get("mid", {})
                    all_candles.append({
                        "timestamp": c["time"],
                        "open":   float(mid.get("o", 0)),
                        "high":   float(mid.get("h", 0)),
                        "low":    float(mid.get("l", 0)),
                        "close":  float(mid.get("c", 0)),
                        "volume": int(c.get("volume", 0)),
                    })
            except Exception as e:
                print(f"    {par} bloque {bloque_start.date()} → error: {e}")
            bloque_start = bloque_end
            time.sleep(0.3)

        # Deduplicar y ordenar
        seen  = set()
        dedup = []
        for c in all_candles:
            if c["timestamp"] not in seen:
                seen.add(c["timestamp"]); dedup.append(c)
        dedup.sort(key=lambda x: x["timestamp"])

        out_file.write_text(json.dumps(dedup, indent=2))
        print(f"    {par}: {len(dedup):,} velas descargadas ✅")


# ── DeepSeek simulado (mismo filtro estricto en las 3 variantes) ─────────────

def deepseek_estricto(
    par, dir_, estrat, rsi, ema20, ema50, macd_dif,
    bbl, bbu, precio, trend_h4, regime, sesion
) -> bool:
    """
    Filtro DeepSeek simulado calibrado a ~55% de aprobación.
    Idéntico al usado en backtest_variantes.py para comparación válida.
    """
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
        if dir_ == "long"  and trend_h4 == "up":    score += 0.22
        if dir_ == "short" and trend_h4 == "down":  score += 0.22
        if dir_ == "long"  and trend_h4 == "down":  score -= 0.40
        if dir_ == "short" and trend_h4 == "up":    score -= 0.40
        if trend_h4 == "rango":                      score -= 0.20

    elif estrat in ("Hammer", "Doji"):
        # Hammer: largo alcista en sobreventa
        if estrat == "Hammer" and dir_ == "long":
            if rsi < 35: score += 0.20
            if rsi < 30: score += 0.15
            if trend_h4 == "down": score += 0.10   # reversión contra tendencia H4
        # Doji: incertidumbre — preferir en extremos RSI
        elif estrat == "Doji":
            if dir_ == "long"  and rsi < 32: score += 0.25
            if dir_ == "short" and rsi > 68: score += 0.25
            if dir_ == "long"  and rsi < 27: score += 0.15
            if dir_ == "short" and rsi > 73: score += 0.15

    elif estrat == "RSI_Divergence":
        if dir_ == "long"  and rsi < 38: score += 0.25
        if dir_ == "short" and rsi > 62: score += 0.25
        if dir_ == "long"  and rsi < 30: score += 0.15
        if dir_ == "short" and rsi > 70: score += 0.15
        if dir_ == "long"  and trend_h4 == "down": score += 0.08
        if dir_ == "short" and trend_h4 == "up":   score += 0.08

    if sesion == "london": score += 0.03
    if estrat == "RSI_Bollinger" and regime < 0.35: score += 0.07
    if estrat == "EMA_Crossover" and regime > 0.65: score += 0.07

    score += np.random.uniform(-0.08, 0.08)
    return score > 0.62


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()

    print("=" * 70)
    print("  BACKTEST COMPARATIVO — 3 VARIANTES")
    print("  Período: 52 semanas (2025-05-09 → 2026-05-09)")
    print("  Capital: $200 | Riesgo: 1.5%/trade | RR: 2.0 | SL: 2×ATR")
    print("  Sesiones: London + New York | DS: filtro estricto (~55% aprobación)")
    print("=" * 70)

    # Verificar / descargar datos
    faltantes = [p for p in PARES
                 if not (DATA_DIR / f"{p}_M15.json").exists()]
    if faltantes:
        print(f"\n  Faltan datos para: {faltantes}")
        _descargar_datos()
    else:
        print(f"\n  Datos locales encontrados para {len(PARES)} pares ✅")

    print(f"\n  Cargando y precalculando indicadores...")
    dfs = cargar_datos(PARES, DATA_DIR, verbose=True)
    print(f"  Listo en {time.time() - t0:.1f}s\n")

    variantes = [
        ("A  RSI_Bollinger solo",
         {"RSI_Bollinger"},
         "La estrategia validada en 52 semanas (checkpoint May-9)"),

        ("B  EMA_Crossover + Hammer + Doji",
         {"EMA_Crossover", "Hammer", "Doji"},
         "Config 'óptima' 2026-05-14 (WR=42% PnL=+$280 en periodo corto)"),

        ("C  RSI_Bollinger + RSI_Divergence",
         {"RSI_Bollinger", "RSI_Divergence"},
         "Propuesta: mejor cobertura sin introducir trend-following"),
    ]

    resultados = []
    for label, estrats, descripcion in variantes:
        print(f"  ▶ {label}")
        print(f"    {descripcion}")
        r = run_backtest(
            BASE_CONFIG,
            estrategias=estrats,
            ds_func=deepseek_estricto,
            dfs_cache=dfs,
            label=label,
            seed=42,
        )
        resultados.append(r)

        aprobacion = f" | DS aprobación: {r['apr_rate']:.1f}%" if r["apr_rate"] else ""
        print(f"    → Capital: ${r['capital']:.2f} | ROI: {r['roi']:+.1f}% "
              f"| WR: {r['wr']}% | DD: {r['dd']}% | Trades: {r['trades']}{aprobacion}")
        print()

    # ── Tabla comparativa final ───────────────────────────────────────────────
    print("=" * 70)
    print("  TABLA COMPARATIVA DEFINITIVA")
    print("=" * 70)
    print(f"  {'Variante':<32} {'Capital':>8} {'ROI':>8} {'WR':>6} {'DD':>7} {'Trades':>7} {'DS%':>5}")
    print(f"  {'-'*68}")
    for r in resultados:
        ds_str = f"{r['apr_rate']:.0f}%" if r["apr_rate"] else "  N/A"
        print(
            f"  {r['label']:<32} "
            f"${r['capital']:>7.2f} "
            f"{r['roi']:>+7.1f}% "
            f"{r['wr']:>5.1f}% "
            f"{r['dd']:>6.1f}% "
            f"{r['trades']:>7} "
            f"{ds_str:>5}"
        )

    print(f"\n  {'─'*68}")

    # Ganador
    mejor = max(resultados, key=lambda x: x["capital"])
    print(f"\n  ✅ GANADOR: {mejor['label'].strip()}")
    print(f"     Capital final: ${mejor['capital']:.2f} | ROI: {mejor['roi']:+.1f}%"
          f" | WR: {mejor['wr']}% | DD: {mejor['dd']}%")

    # Detalle por estrategia del ganador
    if mejor.get("by_estrat"):
        print(f"\n  Desglose por estrategia ({mejor['label'].strip()}):")
        print(f"  {'Estrategia':<22} {'Trades':>7} {'WR':>6} {'PnL':>9}")
        print(f"  {'-'*48}")
        for estrat, dat in mejor["by_estrat"].items():
            if dat["trades"] > 0:
                wr_e = round(dat["wins"] / dat["trades"] * 100, 1)
                print(f"  {estrat:<22} {dat['trades']:>7} {wr_e:>5.1f}% ${dat['pnl']:>+8.2f}")

    # Recomendación
    print(f"\n  {'═'*68}")
    print("  RECOMENDACIÓN:")
    mejor_roi  = max(resultados, key=lambda x: x["roi"])
    menor_dd   = min(resultados, key=lambda x: x["dd"])
    mejor_wr   = max(resultados, key=lambda x: x["wr"])

    # Calcular ratio ROI/DD (Calmar ratio simplificado)
    for r in resultados:
        r["calmar"] = round(abs(r["roi"]) / r["dd"], 2) if r["dd"] > 0 else 0

    mejor_calmar = max(resultados, key=lambda x: x["calmar"])

    print(f"  Mayor ROI:      {mejor_roi['label'].strip()} ({mejor_roi['roi']:+.1f}%)")
    print(f"  Menor DD:       {menor_dd['label'].strip()} ({menor_dd['dd']:.1f}%)")
    print(f"  Mayor WR:       {mejor_wr['label'].strip()} ({mejor_wr['wr']:.1f}%)")
    print(f"  Mejor ROI/DD:   {mejor_calmar['label'].strip()} (ratio {mejor_calmar['calmar']:.2f}x)")
    print(f"  Capital final:  {mejor['label'].strip()} (${mejor['capital']:.2f})")
    print()

    print(f"  Tiempo total: {time.time() - t0:.1f}s")
    print("=" * 70)

    # Guardar resultados
    out_path = ROOT / "data" / "backtesting" / "comparativo_3v.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp":   datetime.now().isoformat(),
        "descripcion": "Comparativo 3 variantes — mismo motor, mismos datos, misma semilla",
        "config_base": {
            "capital_ini":   CAPITAL_INI,
            "n_semanas":     N_SEMANAS,
            "sim_start":     SIM_START.isoformat(),
            "pares":         PARES,
            "sl_atr_mult":   BASE_CONFIG.sl_atr_mult,
            "rr_ratio":      BASE_CONFIG.rr_ratio,
            "sesiones":      list(BASE_CONFIG.sesiones_act),
            "riesgo_pct":    BASE_CONFIG.riesgo_pct,
        },
        "resultados": [
            {
                "label":    r["label"],
                "capital":  r["capital"],
                "roi":      r["roi"],
                "wr":       r["wr"],
                "dd":       r["dd"],
                "trades":   r["trades"],
                "calmar":   r["calmar"],
                "apr_rate": r["apr_rate"],
                "by_estrat": r.get("by_estrat", {}),
            }
            for r in resultados
        ],
    }, indent=2))
    print(f"\n  Resultados guardados en: {out_path}")


if __name__ == "__main__":
    main()
