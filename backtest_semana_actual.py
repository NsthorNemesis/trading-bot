"""
backtest_semana_actual.py — Backtest semana en curso vs trades reales
=====================================================================
Descarga velas M15 de esta semana, corre RSI_Bollinger + DS estricto
y compara resultado teórico con los trades reales del sistema.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backtest_engine import (
    BacktestConfig, _add_indicators, _raw_to_df,
    run_backtest, prefiltro_completo, _sesion, Tracker
)

# ── Configuración ─────────────────────────────────────────────────────────────
PARES     = ["EUR_USD", "AUD_USD", "USD_CHF", "USD_CAD"]
DATA_DIR  = ROOT / "data" / "semana_actual"
TRADES_LOG = ROOT / "logs" / "trades.json"

# Semana: lunes al viernes actual
HOY       = datetime.now(timezone.utc)
LUNES     = HOY - timedelta(days=HOY.weekday())
LUNES     = LUNES.replace(hour=0, minute=0, second=0, microsecond=0)
FIN       = HOY

CAPITAL_INI = 200.0
RIESGO_PCT  = 0.015
SL_ATR_MULT = 2.0
RR_RATIO    = 2.0
MIN_SL_PIPS = 10.0
MAX_SL_PIPS = 50.0
SESIONES    = {"london", "new_york"}
COOLDOWN_M  = 15

# ── Leer .env ────────────────────────────────────────────────────────────────
def _load_env():
    env_path = ROOT / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── Descarga datos de la semana ───────────────────────────────────────────────
def descargar_semana():
    _load_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    import oandapyV20
    import oandapyV20.endpoints.instruments as instruments
    client = oandapyV20.API(
        access_token=os.environ["OANDA_ACCESS_TOKEN"],
        environment="practice"
    )

    print(f"  Descargando {LUNES.strftime('%Y-%m-%d')} → {HOY.strftime('%Y-%m-%d %H:%M')} UTC")

    for par in PARES:
        params = {
            "granularity": "M15",
            "from":  LUNES.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":    HOY.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": "M",
        }
        try:
            ep  = instruments.InstrumentsCandles(instrument=par, params=params)
            rv  = client.request(ep)
            raw = rv.get("candles", [])
            velas = [{
                "timestamp": c["time"],
                "open":   float(c["mid"]["o"]),
                "high":   float(c["mid"]["h"]),
                "low":    float(c["mid"]["l"]),
                "close":  float(c["mid"]["c"]),
                "volume": int(c.get("volume", 0)),
            } for c in raw if c.get("complete", True)]
            (DATA_DIR / f"{par}_M15.json").write_text(json.dumps(velas))
            print(f"    {par}: {len(velas)} velas ✅")
        except Exception as e:
            print(f"    {par}: ERROR — {e}")
        time.sleep(0.3)

# ── DeepSeek simulado ─────────────────────────────────────────────────────────
def deepseek_estricto(par, dir_, estrat, rsi, ema20, ema50, macd_dif,
                      bbl, bbu, precio, trend_h4, regime, sesion) -> bool:
    score = 0.50
    if estrat == "RSI_Bollinger":
        if dir_ == "long":
            if   rsi < 24: score += 0.35
            elif rsi < 27: score += 0.20
            elif rsi < 30: score += 0.08
            else:          score -= 0.18
            dist = (bbl - precio) / bbl if bbl > 0 else 0
            if dist > 0.002: score += 0.12
            elif dist > 0.001: score += 0.05
            if trend_h4 == "up": score += 0.06
        else:
            if   rsi > 76: score += 0.35
            elif rsi > 73: score += 0.20
            elif rsi > 70: score += 0.08
            else:          score -= 0.18
            dist = (precio - bbu) / bbu if bbu > 0 else 0
            if dist > 0.002: score += 0.12
            elif dist > 0.001: score += 0.05
            if trend_h4 == "down": score += 0.06
    if sesion == "london": score += 0.03
    if regime < 0.35:     score += 0.07
    score += np.random.uniform(-0.08, 0.08)
    return score > 0.62

# ── Cargar datos de la semana ─────────────────────────────────────────────────
def cargar_semana():
    import pandas as pd
    from ta.trend import EMAIndicator
    dfs = {}
    for par in PARES:
        f = DATA_DIR / f"{par}_M15.json"
        if not f.exists():
            continue
        raw = json.loads(f.read_text())
        if len(raw) < 60:
            print(f"  {par}: solo {len(raw)} velas — insuficiente para indicadores")
            continue
        df = _add_indicators(_raw_to_df(raw))
        # Agregar trend_h4 simplificado
        df["trend_h4"] = "rango"
        dfs[par] = {
            "ts":       df["ts"].values,
            "open":     df["Open"].values,
            "high":     df["High"].values,
            "low":      df["Low"].values,
            "close":    df["Close"].values,
            "atr":      df["ATR_14"].values,
            "rsi":      df["RSI_14"].values,
            "ema20":    df["EMA_20"].values,
            "ema50":    df["EMA_50"].values,
            "bbl":      df["BBL_20"].values,
            "bbu":      df["BBU_20"].values,
            "macd_dif": df["MACD_DIF"].values,
            "hammer":   df["HAMMER"].values.astype(bool),
            "doji":     df["DOJI"].values.astype(bool),
            "ebull":    df["EBULL"].values.astype(bool),
            "regime":   df["REGIME"].values,
            "trend_h4": df["trend_h4"].values,
        }
        print(f"  {par}: {len(df)} velas cargadas")
    return dfs

# ── Mini backtest semana ──────────────────────────────────────────────────────
def correr_backtest_semana(dfs):
    import pandas as pd
    from collections import deque
    from utils.regime_detector import RegimeDetector

    tracker     = Tracker(CAPITAL_INI, RIESGO_PCT)
    cooldown    = {}
    regime_hist = {p: deque(maxlen=80) for p in dfs}
    rd          = RegimeDetector()
    cooldown_ns = COOLDOWN_M * 60 * 1_000_000_000
    pip_size    = {"USD_JPY": 0.01}
    estrats     = {"RSI_Bollinger"}
    ds_total    = 0; ds_aprobadas = 0
    np.random.seed(42)

    # Reunir todos los ticks de la semana ordenados
    ticks = []
    for par, a in dfs.items():
        for gi in range(len(a["ts"])):
            ticks.append((int(a["ts"][gi].astype("int64")), par, gi))
    ticks.sort(key=lambda x: x[0])

    for ts_ns, par, gi in ticks:
        a     = dfs[par]
        ts_dt = pd.Timestamp(ts_ns, unit="ns", tz="UTC").to_pydatetime()

        tracker.check_fills(par, float(a["high"][gi]), float(a["low"][gi]), ts_dt)

        ses = _sesion(ts_dt.hour)
        if ses not in SESIONES:                            continue
        if len(tracker.open) >= 3:                        continue
        if (ts_ns - cooldown.get(par, 0)) < cooldown_ns:  continue

        rs    = float(a["regime"][gi])
        regime_hist[par].append(rs)
        trans = rd.detectar_transicion(list(regime_hist[par]))

        cands = [(c, d, e) for c, d, e in prefiltro_completo(a, gi) if e in estrats]
        if not cands: continue

        mejor = None
        for conf_base, dir_, estrat in sorted(cands, key=lambda x: -x[0]):
            peso = rd.peso_estrategia(estrat, rs, trans)
            conf = round(conf_base * peso, 3)
            if conf >= 0.40:
                mejor = (conf, dir_, estrat); break
        if not mejor: continue

        ds_total += 1
        aprobado = deepseek_estricto(
            par, mejor[1], mejor[2],
            float(a["rsi"][gi]),     float(a["ema20"][gi]),
            float(a["ema50"][gi]),   float(a["macd_dif"][gi]),
            float(a["bbl"][gi]),     float(a["bbu"][gi]),
            float(a["close"][gi]),   str(a["trend_h4"][gi]),
            rs, ses,
        )
        if not aprobado: continue
        ds_aprobadas += 1

        entry = float(a["close"][gi])
        atr_v = float(a["atr"][gi])
        ps    = pip_size.get(par, 0.0001)
        sl_d  = float(np.clip(atr_v * SL_ATR_MULT, MIN_SL_PIPS * ps, MAX_SL_PIPS * ps))
        tp_d  = sl_d * RR_RATIO
        dir_  = mejor[1]
        sl    = entry - sl_d if dir_ == "long" else entry + sl_d
        tp    = entry + tp_d if dir_ == "long" else entry - tp_d

        tracker.open_trade(par, dir_, entry, sl, tp, mejor[2], ts_dt)
        cooldown[par] = ts_ns

    apr = round(ds_aprobadas / ds_total * 100, 1) if ds_total > 0 else 0
    return tracker, apr

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  BACKTEST SEMANA ACTUAL vs TRADES REALES")
    print(f"  Período: {LUNES.strftime('%Y-%m-%d')} → {HOY.strftime('%Y-%m-%d %H:%M')} UTC")
    print("  Estrategia: RSI_Bollinger | Sesiones: London + NY")
    print("=" * 65)

    # Descargar datos
    print("\n[1] Descargando velas M15 de la semana...")
    descargar_semana()

    # Cargar y procesar
    print("\n[2] Cargando datos y calculando indicadores...")
    dfs = cargar_semana()
    if not dfs:
        print("  ERROR: No hay datos suficientes.")
        return

    # Correr backtest
    print("\n[3] Corriendo backtest RSI_Bollinger + DS estricto...")
    tracker, apr = correr_backtest_semana(dfs)

    wins_bt   = [t for t in tracker.closed if t["pnl"] > 0]
    losses_bt = [t for t in tracker.closed if t["pnl"] <= 0]
    pnl_bt    = sum(t["pnl"] for t in tracker.closed)
    wr_bt     = len(wins_bt) / len(tracker.closed) * 100 if tracker.closed else 0

    # Leer trades reales
    print("\n[4] Leyendo trades reales del sistema...")
    trades_real = []
    if TRADES_LOG.exists():
        todos = json.loads(TRADES_LOG.read_text())
        # Solo trades de esta semana con pnl
        for t in todos:
            if "pnl" not in t or t.get("estado") == "zombie":
                continue
            try:
                import pandas as pd
                fecha = pd.Timestamp(t.get("opened_at", "")).to_pydatetime()
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
                if fecha >= LUNES:
                    trades_real.append(t)
            except Exception:
                continue

    wins_real   = [t for t in trades_real if t.get("pnl", 0) > 0]
    losses_real = [t for t in trades_real if t.get("pnl", 0) <= 0]
    pnl_real    = sum(t.get("pnl", 0) for t in trades_real)
    wr_real     = len(wins_real) / len(trades_real) * 100 if trades_real else 0

    # ── Comparativa ──────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("  COMPARATIVA: BACKTEST vs SISTEMA REAL")
    print("=" * 65)
    print(f"  {'Métrica':<28} {'Backtest':>12} {'Real':>12}")
    print(f"  {'-'*54}")
    print(f"  {'Trades totales':<28} {len(tracker.closed):>12} {len(trades_real):>12}")
    print(f"  {'Wins':<28} {len(wins_bt):>12} {len(wins_real):>12}")
    print(f"  {'Losses':<28} {len(losses_bt):>12} {len(losses_real):>12}")
    print(f"  {'Win Rate':<28} {wr_bt:>11.1f}% {wr_real:>11.1f}%")
    print(f"  {'PnL total':<28} ${pnl_bt:>+10.2f} ${pnl_real:>+10.2f}")
    print(f"  {'Capital final':<28} ${tracker.capital:>10.2f} {'N/A':>12}")
    print(f"  {'DS aprobación':<28} {apr:>11.1f}% {'real':>12}")
    print(f"  {'-'*54}")

    # Diferencia de filtrado
    if len(tracker.closed) > 0 and len(trades_real) > 0:
        ratio = len(trades_real) / len(tracker.closed) * 100
        print(f"\n  El sistema real ejecutó {ratio:.0f}% de los trades que el")
        print(f"  backtest habría ejecutado ({len(trades_real)} de {len(tracker.closed)}).")
        print(f"  → DeepSeek real filtró más agresivamente que el simulado.")

    # Detalle backtest
    if tracker.closed:
        print(f"\n  TRADES DEL BACKTEST ({len(tracker.closed)}):")
        print(f"  {'Par':<10} {'Dir':<6} {'Resultado':<10} {'PnL':>9}")
        print(f"  {'-'*38}")
        for t in tracker.closed:
            emoji = "✅" if t["pnl"] > 0 else "❌"
            print(f"  {t['par']:<10} {t['dir']:<6} {t['resultado']:<10} ${t['pnl']:>+8.4f} {emoji}")

    # Detalle real
    if trades_real:
        print(f"\n  TRADES REALES ({len(trades_real)}):")
        print(f"  {'Par':<10} {'Dir':<6} {'PnL':>9}")
        print(f"  {'-'*28}")
        for t in trades_real:
            emoji = "✅" if t.get("pnl", 0) > 0 else "❌"
            print(f"  {t.get('par','?'):<10} {t.get('dir','?'):<6} ${t.get('pnl',0):>+8.4f} {emoji}")
    else:
        print("\n  Sin trades reales registrados esta semana aún.")

    print("=" * 65)


if __name__ == "__main__":
    main()
