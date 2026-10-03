"""
backtest_2dias.py — Simulación de los últimos 2 días
======================================================
Usa los datos rolling del VPS (últimas 1000 velas M15).
Incluye ambas versiones del filtro H4:
  - strict: original (3/5 velas, 0.05%)
  - relaxed: nuevo (2/5 velas, 0.03%)
Sirve para verificar si hubiera habido trades con la config actual.
"""
import json, sys, logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from agents.signal_agent.strategies import TODAS_LAS_ESTRATEGIAS

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("bt2d")

PARAMS_FILE = ROOT / "data" / "calibration" / "strategy_params.json"
DATA_DIR    = ROOT / "data" / "historical"

# ─── Indicadores ──────────────────────────────────────────────────────────────

def calc_indicators(df):
    c = df["Close"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)
    n = len(c)

    def ema_arr(arr, p):
        out = np.full(n, np.nan)
        if n < p: return out
        k = 2.0 / (p + 1)
        out[p-1] = arr[:p].mean()
        for i in range(p, n):
            out[i] = arr[i]*k + out[i-1]*(1-k)
        return out

    def rsi_arr(arr, p=14):
        out = np.full(n, np.nan)
        if n < p+1: return out
        d = np.diff(arr)
        g = np.where(d > 0, d, 0.0); ls = np.where(d < 0, -d, 0.0)
        ag = g[:p].mean(); al = ls[:p].mean()
        for i in range(p, len(d)):
            ag = (ag*(p-1) + g[i]) / p
            al = (al*(p-1) + ls[i]) / p
            rs = ag/al if al != 0 else 100
            out[i+1] = 100 - 100/(1+rs)
        return out

    def atr_arr(p=14):
        tr = np.maximum(h[1:]-l[1:],
             np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
        tr = np.concatenate([[h[0]-l[0]], tr])
        out = np.full(n, np.nan)
        if n < p: return out
        out[p-1] = tr[:p].mean()
        for i in range(p, n):
            out[i] = (out[i-1]*(p-1) + tr[i]) / p
        return out

    def adx_arr(p=14):
        out = np.full(n, np.nan)
        if n < p*2+2: return out
        dmp = np.where((h[1:]-h[:-1]) > (l[:-1]-l[1:]), np.maximum(h[1:]-h[:-1], 0), 0.0)
        dmm = np.where((l[:-1]-l[1:]) > (h[1:]-h[:-1]), np.maximum(l[:-1]-l[1:], 0), 0.0)
        tr  = np.maximum(h[1:]-l[1:],
              np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
        def wilder(arr, p):
            w = np.full(len(arr), np.nan)
            w[p-1] = arr[:p].sum()
            for i in range(p, len(arr)):
                w[i] = w[i-1] - w[i-1]/p + arr[i]
            return w
        atr14 = wilder(tr,p); dmp14 = wilder(dmp,p); dmm14 = wilder(dmm,p)
        dx = np.full(len(tr), np.nan)
        for i in range(len(tr)):
            a = atr14[i]; pm = dmp14[i]; mm = dmm14[i]
            if a and a > 0:
                di_p=100*pm/a; di_m=100*mm/a; s=di_p+di_m
                dx[i] = 100*abs(di_p-di_m)/s if s > 0 else 0
        adx_val = np.nanmean(dx[:p])
        for i in range(p, len(dx)):
            if not np.isnan(dx[i]):
                adx_val = (adx_val*(p-1) + dx[i]) / p
            j = i + p + 1
            if j < n: out[j] = adx_val
        return out

    def bollinger(p=20, mult=2.0):
        upper=np.full(n,np.nan); mid=np.full(n,np.nan); lower=np.full(n,np.nan)
        for i in range(p-1, n):
            sl=c[i-p+1:i+1]; m=sl.mean(); std=sl.std()
            mid[i]=m; upper[i]=m+mult*std; lower[i]=m-mult*std
        return upper, mid, lower

    df = df.copy()
    df["EMA_9"]    = ema_arr(c, 9)
    df["EMA_20"]   = ema_arr(c, 20)
    df["EMA_50"]   = ema_arr(c, 50)
    df["RSI_14"]   = rsi_arr(c, 14)
    df["ATR_14"]   = atr_arr(14)
    df["ADX_14"]   = adx_arr(14)
    bbu, bbm, bbl  = bollinger(20, 2.0)
    df["BBU_20_2"]=bbu; df["BBM_20_2"]=bbm; df["BBL_20_2"]=bbl
    return df


def tendencia_h4(df_h4, modo="relaxed"):
    """
    modo='strict'  → original: 0.05%, 3/5 velas
    modo='relaxed' → nuevo:    0.03%, 2/5 velas
    """
    if df_h4 is None or len(df_h4) < 10: return "rango"
    ema20  = float(df_h4["EMA_20"].iloc[-1] or 0)
    ema50  = float(df_h4["EMA_50"].iloc[-1] or 0)
    precio = float(df_h4["Close"].iloc[-1] or 0)
    if ema20 == 0 or ema50 == 0 or precio == 0: return "rango"

    umbral  = 0.0003 if modo == "relaxed" else 0.0005
    min_vel = 2      if modo == "relaxed" else 3

    diff_pct = abs(ema20 - ema50) / precio
    if diff_pct < umbral: return "rango"

    direccion = "up" if ema20 > ema50 else "down"
    if direccion == "up"   and precio < ema20: return "rango"
    if direccion == "down" and precio > ema20: return "rango"

    ultimas = df_h4.tail(5)
    cf = (ultimas["Close"] > ultimas["Open"]).sum() if direccion == "up" else (ultimas["Close"] < ultimas["Open"]).sum()
    return "rango" if cf < min_vel else direccion


def sesion(ts):
    h = ts.hour
    if  8 <= h < 12: return "london"
    if 12 <= h < 17: return "overlap"
    if 17 <= h < 22: return "new_york"
    return "asia"


def cargar_m15(par):
    path = DATA_DIR / f"{par}_M15.json"
    if not path.exists():
        raise FileNotFoundError(f"Sin datos: {path}")
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df.rename(columns={"timestamp":"Timestamp","open":"Open","high":"High",
                        "low":"Low","close":"Close","volume":"Volume"}, inplace=True)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)
    return df.sort_values("Timestamp").reset_index(drop=True)


def run(modo_h4="relaxed"):
    params = json.loads(PARAMS_FILE.read_text())
    pares           = params.get("pares_activos", ["EUR_USD","USD_CAD","AUD_USD"])
    activas         = set(params.get("estrategias_activas", []))
    sesiones_ok     = set(params.get("sesiones_activas", ["london","overlap"]))
    adx_min         = float(params.get("adx_min_operar", 27))
    per_strategy    = params.get("per_strategy", {})
    cooldown_min    = int(params.get("cooldown_minutes", 15))

    estrategias = [e for e in TODAS_LAS_ESTRATEGIAS if e.nombre in activas]
    log.info(f"\n{'='*60}")
    log.info(f"Backtest 2 días — H4 modo={modo_h4.upper()}")
    log.info(f"Estrategias: {[e.nombre for e in estrategias]}")
    log.info(f"Pares: {pares} | Sesiones: {sesiones_ok}")
    log.info(f"ADX_min={adx_min} | H4_activo=True")
    log.info(f"{'='*60}\n")

    # Fechas: últimos 2 días completos
    ahora   = datetime.now(timezone.utc)
    fin     = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio  = fin - timedelta(days=2)
    log.info(f"Periodo: {inicio.date()} → {fin.date()} ({(fin-inicio).total_seconds()/3600:.0f}h)\n")

    señales_encontradas = []
    rechazos = defaultdict(int)

    for par in pares:
        try:
            df_full = cargar_m15(par)
        except FileNotFoundError as e:
            log.warning(f"  {par}: {e}")
            continue

        df_full = calc_indicators(df_full)

        # H4 desde datos completos
        df_h4 = df_full.copy().set_index("Timestamp")
        df_h4_raw = df_h4[["Open","High","Low","Close"]].resample("4h",
            closed="left", label="left").agg(
            {"Open":"first","High":"max","Low":"min","Close":"last"}
        ).dropna(subset=["Close"]).reset_index()
        df_h4_ind = calc_indicators(df_h4_raw)

        # Filtrar al periodo de 2 días
        mask   = (df_full["Timestamp"] >= inicio) & (df_full["Timestamp"] < fin)
        idx_periodo = df_full[mask].index.tolist()

        if not idx_periodo:
            log.warning(f"  {par}: sin datos en el periodo {inicio.date()}→{fin.date()}")
            continue

        log.info(f"  {par}: {len(idx_periodo)} velas en el periodo")
        cooldown = {}

        for i in idx_periodo:
            if i < 50:  # warmup mínimo para indicadores
                continue
            row = df_full.iloc[i]
            ts  = row["Timestamp"]

            # Filtro sesión
            ses = sesion(ts)
            if ses not in sesiones_ok:
                rechazos["sesion"] += 1
                continue

            # Filtro cooldown
            ultimo = cooldown.get(par)
            if ultimo and (ts - ultimo).total_seconds() < cooldown_min * 60:
                rechazos["cooldown"] += 1
                continue

            # Filtro ADX
            adx_val = float(row.get("ADX_14", 0) or 0)
            adx_min_ef = float(per_strategy.get("Engulfing", {}).get("adx_min_operar", adx_min))
            if adx_val < adx_min_ef:
                rechazos["adx"] += 1
                continue

            # Filtro H4 — usar velas H4 disponibles hasta este momento
            h4_mask = df_h4_ind["Timestamp"] <= ts
            df_h4_slice = df_h4_ind[h4_mask]
            h4 = tendencia_h4(df_h4_slice, modo=modo_h4)
            if h4 == "rango":
                rechazos["h4_rango"] += 1
                continue

            # Detectar patrones en ventana de 80 velas
            df_window = df_full.iloc[max(0, i-80):i+1].copy().reset_index(drop=True)
            params_strat = {**params, "_par_actual": par}

            for est in estrategias:
                ps_cfg = per_strategy.get(est.nombre, {})
                p_eff  = {**params_strat, **ps_cfg}
                patron = est.detectar(df_window, p_eff)
                if patron is None:
                    continue

                # Filtro H4 dirección
                if h4 != "rango":
                    alineado = (h4 == "up" and patron.dir_hint == "long") or \
                               (h4 == "down" and patron.dir_hint == "short")
                    if not alineado:
                        rechazos["h4_dir"] += 1
                        continue

                precio = float(row["Close"])
                atr    = float(row.get("ATR_14", 0) or 0)
                rr     = float(ps_cfg.get("rr_ratio", params.get("rr_ratio", 2.0)))
                sl_m   = float(params.get("sl_atr_mult", 1.5))
                sl = precio - atr*sl_m if patron.dir_hint == "long" else precio + atr*sl_m
                tp = precio + atr*sl_m*rr if patron.dir_hint == "long" else precio - atr*sl_m*rr

                señal = {
                    "ts":         ts.isoformat(),
                    "par":        par,
                    "estrategia": patron.nombre,
                    "dir":        patron.dir_hint,
                    "h4":         h4,
                    "adx":        round(adx_val, 1),
                    "entry":      round(precio, 5),
                    "sl":         round(sl, 5),
                    "tp":         round(tp, 5),
                    "atr":        round(atr, 5),
                    "descripcion": patron.descripcion[:80],
                }
                señales_encontradas.append(señal)
                cooldown[par] = ts
                log.info(f"    ✓ SEÑAL {ts.strftime('%m-%d %H:%M')} {par} {patron.nombre} "
                         f"{patron.dir_hint.upper()} @ {precio:.5f} H4={h4} ADX={adx_val:.1f}")

    # ── Resumen ────────────────────────────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info(f"RESUMEN — H4 modo={modo_h4.upper()}")
    log.info(f"  Total señales detectadas: {len(señales_encontradas)}")
    log.info(f"  Rechazos por filtro:")
    for k, v in sorted(rechazos.items(), key=lambda x: -x[1]):
        log.info(f"    {k:15s}: {v}")

    if señales_encontradas:
        log.info(f"\n  Detalle señales:")
        for s in señales_encontradas:
            log.info(f"    {s['ts'][:16]} | {s['par']} | {s['estrategia']} | "
                     f"{s['dir'].upper()} | H4={s['h4']} | ADX={s['adx']} | entry={s['entry']}")
    else:
        log.info("\n  → Sin señales en el periodo con este modo de filtro H4")

    log.info(f"{'='*60}\n")
    return señales_encontradas, rechazos


if __name__ == "__main__":
    log.info("=== BACKTEST 2 DÍAS ===\n")
    log.info("--- Modo STRICT (filtro H4 original: 3/5 velas, 0.05%) ---")
    s_strict, r_strict = run(modo_h4="strict")

    log.info("\n--- Modo RELAXED (filtro H4 nuevo: 2/5 velas, 0.03%) ---")
    s_relaxed, r_relaxed = run(modo_h4="relaxed")

    log.info(f"\nDIFERENCIA: strict={len(s_strict)} señales | relaxed={len(s_relaxed)} señales")
    extra = [s for s in s_relaxed if s not in s_strict]
    if extra:
        log.info(f"Señales adicionales con filtro relaxed ({len(extra)}):")
        for s in extra:
            log.info(f"  {s['ts'][:16]} {s['par']} {s['estrategia']} {s['dir'].upper()}")
