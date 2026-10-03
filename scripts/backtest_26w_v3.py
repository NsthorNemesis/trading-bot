"""
backtest_26w_v3.py — Motor de backtest 26 semanas
===================================================
Mejoras sobre v2:
  1. Spread + slippage por par (cierra brecha vs sistema real)
  2. Break-even automático (igual que el bot en vivo)
  3. Límite de posiciones simultáneas entre pares
  4. Walk-forward: separación train/test configurable
"""
import argparse, json, logging, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from agents.signal_agent.strategies import TODAS_LAS_ESTRATEGIAS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest_26w")

PARAMS_FILE = ROOT / "data" / "calibration" / "strategy_params.json"
DATA_DIR    = ROOT / "data" / "historical"
DEFAULT_OUT = ROOT / "data" / "backtesting" / "backtest_26w.json"

CAPITAL_INI = 200.0
N_SEMANAS   = 26
WARMUP      = 200
COOLDOWN_MIN= 15
RR_RATIO    = 2.0
SL_ATR_MULT = 1.5
MIN_SL_PIPS = 10
MAX_SL_PIPS = 30
RIESGO_PCT  = 0.01
MAX_POS     = 6

# ── Spread por par (pips) — promedio real medido ──────────────────────────────
SPREAD_PIPS = {
    "EUR_USD": 1.5,
    "USD_CAD": 2.0,
    "AUD_USD": 2.0,
    "USD_CHF": 2.0,
    "GBP_USD": 1.8,
    "USD_JPY": 1.5,
}
SLIPPAGE_PIPS = 1.0   # promedio medido en vivo (0.7-1.5 pips)


def pip(par):
    return 0.01 if "JPY" in par else 0.0001

def spread_total(par):
    """Costo total de fricción: spread + slippage, en precio."""
    ps = pip(par)
    return (SPREAD_PIPS.get(par, 2.0) + SLIPPAGE_PIPS) * ps


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
        g = np.where(d > 0, d, 0.0)
        ls = np.where(d < 0, -d, 0.0)
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
        dmp = np.where((h[1:]-h[:-1]) > (l[:-1]-l[1:]),
                        np.maximum(h[1:]-h[:-1], 0), 0.0)
        dmm = np.where((l[:-1]-l[1:]) > (h[1:]-h[:-1]),
                        np.maximum(l[:-1]-l[1:], 0), 0.0)
        tr  = np.maximum(h[1:]-l[1:],
              np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
        def wilder(arr, p):
            w = np.full(len(arr), np.nan)
            w[p-1] = arr[:p].sum()
            for i in range(p, len(arr)):
                w[i] = w[i-1] - w[i-1]/p + arr[i]
            return w
        atr14 = wilder(tr, p); dmp14 = wilder(dmp, p); dmm14 = wilder(dmm, p)
        dx = np.full(len(tr), np.nan)
        for i in range(len(tr)):
            a = atr14[i]; pm = dmp14[i]; mm = dmm14[i]
            if a and a > 0:
                di_p = 100*pm/a; di_m = 100*mm/a
                s = di_p + di_m
                dx[i] = 100*abs(di_p-di_m)/s if s > 0 else 0
        adx_val = np.nanmean(dx[:p])
        for i in range(p, len(dx)):
            if not np.isnan(dx[i]):
                adx_val = (adx_val*(p-1) + dx[i]) / p
            j = i + p + 1
            if j < n: out[j] = adx_val
        return out

    def bollinger(p=20, mult=2.0):
        upper = np.full(n, np.nan); mid = np.full(n, np.nan); lower = np.full(n, np.nan)
        for i in range(p-1, n):
            sl = c[i-p+1:i+1]
            m = sl.mean(); std = sl.std()
            mid[i] = m; upper[i] = m+mult*std; lower[i] = m-mult*std
        return upper, mid, lower

    df = df.copy()
    df["EMA_9"]    = ema_arr(c, 9)
    df["EMA_20"]   = ema_arr(c, 20)
    df["EMA_50"]   = ema_arr(c, 50)
    df["RSI_14"]   = rsi_arr(c, 14)
    df["ATR_14"]   = atr_arr(14)
    df["ADX_14"]   = adx_arr(14)
    bbu, bbm, bbl  = bollinger(20, 2.0)
    df["BBU_20_2"] = bbu; df["BBM_20_2"] = bbm; df["BBL_20_2"] = bbl
    return df


def tendencia_h4(df_h4, params=None):
    """
    Mismos umbrales que el bot en vivo (market_agent.tendencia_h4):
      - EMA separation: params.get('h4_ema_diff', 0.0003)   [live: 0.0003]
      - Velas mínimas:  params.get('h4_min_velas', 2)        [live: 2/5]
    """
    if df_h4 is None or len(df_h4) < 10: return "rango"
    ema20  = float(df_h4["EMA_20"].iloc[-1] or 0)
    ema50  = float(df_h4["EMA_50"].iloc[-1] or 0)
    precio = float(df_h4["Close"].iloc[-1] or 0)
    if ema20 == 0 or ema50 == 0 or precio == 0: return "rango"

    umbral   = float((params or {}).get("h4_ema_diff",   0.0003))
    min_velas = int((params or {}).get("h4_min_velas",   2))

    if abs(ema20 - ema50) / precio < umbral: return "rango"
    direccion = "up" if ema20 > ema50 else "down"
    if direccion == "up"   and precio < ema20: return "rango"
    if direccion == "down" and precio > ema20: return "rango"
    ultimas = df_h4.tail(5)
    cierres = (ultimas["Close"] > ultimas["Open"]).sum() if direccion == "up" else (ultimas["Close"] < ultimas["Open"]).sum()
    return "rango" if cierres < min_velas else direccion


def sesion(ts):
    h = ts.hour
    if  8 <= h < 12: return "london"
    if 12 <= h < 17: return "overlap"
    if 17 <= h < 22: return "new_york"
    return "asia"


def cargar_m15(par):
    # Prioridad: _jun2.json > _full.json > _M15.json (rolling ~10 días)
    candidatos = [
        DATA_DIR / f"{par}_M15_jun2.json",
        DATA_DIR / f"{par}_M15_full.json",
        DATA_DIR / f"{par}_M15.json",
    ]
    path = next((p for p in candidatos if p.exists()), None)
    if path is None:
        raise FileNotFoundError(f"Sin datos M15 para {par}")
    log.info(f"  {par}: usando {path.name}")
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df.rename(columns={"timestamp":"Timestamp","open":"Open","high":"High",
                        "low":"Low","close":"Close","volume":"Volume"}, inplace=True)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)
    return df.sort_values("Timestamp").reset_index(drop=True)


def resamplear_h4(df_m15):
    df = df_m15.copy().set_index("Timestamp")
    h4 = df[["Open","High","Low","Close","Volume"]].resample("4h", closed="left", label="left").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    ).dropna(subset=["Close"]).reset_index()
    return calc_indicators(h4)


def resamplear_h1(df_m15):
    df = df_m15.copy().set_index("Timestamp")
    h1 = df[["Open","High","Low","Close","Volume"]].resample("1h", closed="left", label="left").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    ).dropna(subset=["Close"]).reset_index()
    return calc_indicators(h1)


def run_backtest(pares, n_semanas, params, output_path, semanas_validacion=4, timeframe="M15"):
    log.info(f"Backtest v3 | pares={pares} | {n_semanas}w (validacion: {semanas_validacion}w) | TF={timeframe}")

    estrategias_activas = set(params.get("estrategias_activas", []))
    sesiones_activas    = set(params.get("sesiones_activas", ["london","overlap"]))
    adx_min             = float(params.get("adx_min_operar", 27))
    min_conf_global     = float(params.get("min_confidence", 0.72))
    cooldown_min        = int(params.get("cooldown_minutes", COOLDOWN_MIN))
    riesgo_pct          = float(params.get("riesgo_pct", RIESGO_PCT))
    sl_atr_mult         = float(params.get("sl_atr_mult", SL_ATR_MULT))
    rr_ratio            = float(params.get("rr_ratio", RR_RATIO))
    min_sl_pips         = float(params.get("min_sl_pips", MIN_SL_PIPS))
    max_sl_pips         = float(params.get("max_sl_pips", MAX_SL_PIPS))
    max_posiciones      = int(params.get("max_posiciones", MAX_POS))
    per_strategy        = params.get("per_strategy", {})

    estrategias = [e for e in TODAS_LAS_ESTRATEGIAS if e.nombre in estrategias_activas]
    if "Engulfing+RSI_Divergence" in estrategias_activas:
        from agents.signal_agent.strategies import EstrategiaRSIDivergencia
        if not any(isinstance(e, EstrategiaRSIDivergencia) for e in estrategias):
            estrategias.append(EstrategiaRSIDivergencia())
    log.info(f"Estrategias: {[e.nombre for e in estrategias]}")

    dfs_m15 = {}; dfs_sig = {}; dfs_h4 = {}
    for par in pares:
        try:
            df_m15 = cargar_m15(par)
            dfs_m15[par] = calc_indicators(df_m15)
            dfs_h4[par]  = resamplear_h4(df_m15)
            # DataFrame de señales: H1 o M15 según timeframe
            if timeframe == "H1":
                dfs_sig[par] = resamplear_h1(df_m15)
                log.info(f"  {par}: {len(df_m15)} velas M15 → {len(dfs_sig[par])} velas H1 | "
                         f"{dfs_sig[par]['Timestamp'].iloc[0].date()} -> {dfs_sig[par]['Timestamp'].iloc[-1].date()}")
            else:
                dfs_sig[par] = dfs_m15[par]
                log.info(f"  {par}: {len(df_m15)} velas | {df_m15['Timestamp'].iloc[0].date()} -> {df_m15['Timestamp'].iloc[-1].date()}")
        except Exception as e:
            log.error(f"  {par}: {e}")

    pares_ok = [p for p in pares if p in dfs_sig]
    if not pares_ok: return

    sim_end   = min(dfs_sig[p]["Timestamp"].iloc[-1] for p in pares_ok)
    sim_start = sim_end - timedelta(weeks=n_semanas)
    val_start = sim_end - timedelta(weeks=semanas_validacion)
    log.info(f"Periodo total: {sim_start.date()} -> {sim_end.date()}")
    log.info(f"Train: {sim_start.date()} -> {val_start.date()} | Validacion: {val_start.date()} -> {sim_end.date()}")

    def semana_key(ts):
        d = ts.isocalendar()
        return f"{d.year}-W{d.week:02d}"

    avg_risk = CAPITAL_INI * riesgo_pct

    # Estado global compartido entre pares
    capital       = CAPITAL_INI
    peak          = CAPITAL_INI
    max_dd        = 0.0
    trades_all    = []
    equity_curve  = [{"ts": sim_start.isoformat(), "capital": capital}]
    cooldown      = {}
    pos_abiertas  = 0   # contador global de posiciones abiertas

    stats_strat   = defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0})
    stats_par     = defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0})
    stats_semana  = defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0,"capital":capital})
    # Separar train vs validacion
    stats_train   = defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0})
    stats_val     = defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0})

    for par in pares_ok:
        df_sig = dfs_sig[par]   # velas de señal (H1 o M15)
        df_h4  = dfs_h4[par]

        mask = df_sig["Timestamp"] >= sim_start
        idx_start = df_sig[mask].index[0] if mask.any() else None
        if idx_start is None: continue
        idx_start = max(idx_start, WARMUP)
        log.info(f"Procesando {par}: {len(df_sig) - idx_start} velas {timeframe}")

        trade_abierto = None

        for i in range(idx_start, len(df_sig)):
            row = df_sig.iloc[i]
            ts  = row["Timestamp"]
            if ts > sim_end: break

            if trade_abierto:
                hi = float(row["High"]); lo = float(row["Low"])
                dir_    = trade_abierto["dir"]
                sl_p    = trade_abierto["sl"]
                tp_p    = trade_abierto["tp"]
                entry   = trade_abierto["entry"]
                sl_dist = trade_abierto["sl_dist"]
                rr      = trade_abierto["rr"]
                be_act  = trade_abierto.get("be_activado", False)

                # ── Break-even: solo para estrategias de TENDENCIA ──────────────
                # RSI_Bollinger (reversión) no usa BE — en mercado de rango
                # el precio oscila y el BE-SL se activa prematuramente.
                es_tendencia = trade_abierto.get("estrategia") in ("Engulfing", "EMA_Crossover", "Engulfing+RSI_Divergence")
                if not be_act and es_tendencia:
                    ps = pip(par)
                    if dir_ == "long"  and hi >= entry + sl_dist:
                        trade_abierto["sl"] = round(entry + ps, 5)
                        trade_abierto["be_activado"] = True
                    elif dir_ == "short" and lo <= entry - sl_dist:
                        trade_abierto["sl"] = round(entry - ps, 5)
                        trade_abierto["be_activado"] = True
                    sl_p = trade_abierto["sl"]

                resultado = None; pnl = 0.0
                if dir_ == "long":
                    if lo <= sl_p:   resultado = "SL"; pnl = (sl_p - trade_abierto["entry"]) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"]) if dir_ == "long" else (trade_abierto["entry"] - sl_p) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"])
                    elif hi >= tp_p: resultado = "TP"; pnl = (tp_p - trade_abierto["entry"]) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"]) if dir_ == "long" else (trade_abierto["entry"] - tp_p) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"])
                else:
                    if hi >= sl_p:   resultado = "SL"; pnl = (sl_p - trade_abierto["entry"]) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"]) if dir_ == "long" else (trade_abierto["entry"] - sl_p) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"])
                    elif lo <= tp_p: resultado = "TP"; pnl = (tp_p - trade_abierto["entry"]) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"]) if dir_ == "long" else (trade_abierto["entry"] - tp_p) * (trade_abierto["risk_usd"] / trade_abierto["sl_dist"])

                if resultado:
                    # Descontar friccion (spread + slippage) del PnL
                    pnl -= trade_abierto.get("friccion_usd", 0)
                    capital += pnl
                    peak = max(peak, capital)
                    dd = (peak - capital) / peak if peak > 0 else 0
                    max_dd = max(max_dd, dd)
                    pos_abiertas -= 1
                    trade_abierto.update({
                        "resultado": resultado,
                        "pnl": round(pnl, 4),
                        "capital_tras": round(capital, 4),
                        "close_ts": ts.isoformat(),
                    })
                    trades_all.append(trade_abierto)
                    sw = semana_key(ts)
                    est = trade_abierto["estrategia"]
                    is_val = ts >= val_start
                    for d in [stats_strat[est], stats_par[par], stats_semana[sw],
                              stats_val[est] if is_val else stats_train[est]]:
                        d["trades"] += 1; d["pnl"] += pnl
                        if resultado == "TP": d["wins"] += 1
                    stats_semana[sw]["capital"] = round(capital, 4)
                    equity_curve.append({"ts": ts.isoformat(), "capital": round(capital, 4)})
                    trade_abierto = None
                continue

            if sesion(ts) not in sesiones_activas: continue
            if pos_abiertas >= max_posiciones: continue

            adx_val = float(row.get("ADX_14") or 0)
            last = cooldown.get(par)
            if last and (ts - last).total_seconds() < cooldown_min * 60: continue

            # ── Filtro H4 — solo si filtro_h4_activo=True en params ──────────
            filtro_h4 = params.get("filtro_h4_activo", True)
            if filtro_h4:
                h4_window = df_h4[df_h4["Timestamp"] <= ts].tail(50)
                h4_tend   = tendencia_h4(h4_window, params) if len(h4_window) >= 10 else "rango"
            else:
                h4_tend = "any"   # valor especial: sin filtro direccional

            ventana = df_sig.iloc[max(0, i-WARMUP+1):i+1].copy()
            if len(ventana) < 30: continue

            params_est = {**params, "_par_actual": par}
            senal = None

            for estrategia in estrategias:
                est_params = {**params_est}
                if estrategia.nombre in per_strategy:
                    est_params.update(per_strategy[estrategia.nombre])

                if estrategia.nombre != "RSI_Bollinger" and adx_val < adx_min:
                    continue

                patron = estrategia.detectar(ventana, est_params)
                if patron is None: continue

                # Filtro H4 de dirección (solo si filtro_h4 activo y no RSI_Bollinger)
                if filtro_h4 and estrategia.nombre != "RSI_Bollinger":
                    if h4_tend == "rango": continue
                    if not ((h4_tend == "up" and patron.dir_hint == "long") or
                            (h4_tend == "down" and patron.dir_hint == "short")):
                        continue

                precio  = float(row["Close"])
                atr_val = float(row.get("ATR_14") or 0)
                if atr_val == 0: continue

                ps = pip(par)
                # Aplicar sl_atr_mult y min/max_sl_pips por estrategia si están definidos
                est_cfg      = per_strategy.get(estrategia.nombre, {})
                sl_mult_est  = float(est_cfg.get("sl_atr_mult",  sl_atr_mult))
                min_sl_est   = float(est_cfg.get("min_sl_pips",  min_sl_pips))
                max_sl_est   = float(est_cfg.get("max_sl_pips",  max_sl_pips))
                rr           = float(est_cfg.get("rr_ratio",     rr_ratio))
                sl_dist      = float(np.clip(atr_val * sl_mult_est, min_sl_est * ps, max_sl_est * ps))
                risk_usd     = capital * riesgo_pct

                # ── SL/TP desde precio mid (sin distorsionar por friccion) ───────
                # La friccion se descuenta del PnL al cerrar, no distorsiona
                # las distancias de SL/TP (preserva el WR real del sistema)
                friccion_usd = spread_total(par) * (risk_usd / sl_dist)
                if patron.dir_hint == "long":
                    sl_p = precio - sl_dist
                    tp_p = precio + sl_dist * rr
                else:
                    sl_p = precio + sl_dist
                    tp_p = precio - sl_dist * rr

                senal = {
                    "par":        par,
                    "dir":        patron.dir_hint,
                    "estrategia": estrategia.nombre,
                    "entry":      round(precio, 5),
                    "sl":         round(sl_p, 5),
                    "tp":         round(tp_p, 5),
                    "friccion_usd": round(friccion_usd, 4),
                    "sl_dist":    sl_dist,
                    "rr":         rr,
                    "risk_usd":   round(risk_usd, 4),
                    "open_ts":    ts.isoformat(),
                    "h4":         h4_tend,
                    "adx":        round(adx_val, 1),
                    "be_activado":False,
                    "es_validacion": ts >= val_start,
                }
                break

            if senal is None: continue
            trade_abierto = senal
            pos_abiertas += 1
            cooldown[par] = ts

        if trade_abierto:
            trade_abierto.update({"resultado":"OPEN","pnl":0.0,"capital_tras":round(capital,4)})
            trades_all.append(trade_abierto)
            pos_abiertas = max(0, pos_abiertas - 1)

    # ── Métricas finales ─────────────────────────────────────────────────────
    cerrados = [t for t in trades_all if t.get("resultado") in ("TP","SL")]
    n_total  = len(cerrados)
    n_wins   = sum(1 for t in cerrados if t["resultado"] == "TP")
    pnl_tot  = sum(t["pnl"] for t in cerrados)
    wr       = n_wins / n_total if n_total > 0 else 0.0
    pnl_pct  = (capital - CAPITAL_INI) / CAPITAL_INI * 100
    r_total  = pnl_tot / avg_risk if avg_risk > 0 else 0.0
    ganancias = sum(t["pnl"] for t in cerrados if t["pnl"] > 0)
    perdidas  = abs(sum(t["pnl"] for t in cerrados if t["pnl"] < 0))
    pf        = ganancias / perdidas if perdidas > 0 else 0.0

    # Walk-forward separado
    val_trades = [t for t in cerrados if t.get("es_validacion")]
    trn_trades = [t for t in cerrados if not t.get("es_validacion")]
    def metricas(ts_list):
        n = len(ts_list); w = sum(1 for t in ts_list if t["resultado"]=="TP")
        p = sum(t["pnl"] for t in ts_list)
        return {"trades":n,"wins":w,"win_rate":round(w/n,3) if n>0 else 0,
                "pnl":round(p,4),"r_total":round(p/avg_risk,2) if n>0 else 0}

    log.info(f"\n{'='*50}")
    log.info(f"RESULTADO {n_semanas}W (con spread+slippage+BE)")
    log.info(f"Capital: ${CAPITAL_INI} -> ${capital:.2f} ({pnl_pct:+.1f}%)")
    log.info(f"Trades: {n_total} | WR: {wr:.0%} | R: {r_total:+.1f}R | PF: {pf:.2f} | MaxDD: {max_dd:.1%}")
    log.info(f"\n[TRAIN  {n_semanas-semanas_validacion}w] {metricas(trn_trades)}")
    log.info(f"[VALIDA {semanas_validacion}w] {metricas(val_trades)}")
    log.info("\nPor estrategia:")
    for est, s in sorted(stats_strat.items(), key=lambda x: -x[1]["pnl"]):
        n=s["trades"]; w=s["wins"]; p=s["pnl"]
        log.info(f"  {est}: {n}t . {w/n:.0%} WR . {p/avg_risk:+.1f}R" if n>0 else f"  {est}: 0t")
    log.info("\nPor par:")
    for par2, s in sorted(stats_par.items(), key=lambda x: -x[1]["pnl"]):
        n=s["trades"]; w=s["wins"]; p=s["pnl"]
        log.info(f"  {par2}: {n}t . {w/n:.0%} WR . {p/avg_risk:+.1f}R" if n>0 else f"  {par2}: 0t")

    # ── Salida JSON compatible con mini app ──────────────────────────────────
    def est_out(stats):
        out = {}
        for est, s in stats.items():
            n=s["trades"]; w=s["wins"]; p=s["pnl"]
            out[est] = {"trades":n,"wins":w,"win_rate":round(w/n,3) if n>0 else 0,
                        "pnl":round(p,4),"pnl_r":round(p/avg_risk,2),"r_total":round(p/avg_risk,2)}
        return out

    def par_out(stats):
        out = {}
        for par2, s in stats.items():
            n=s["trades"]; w=s["wins"]; p=s["pnl"]
            out[par2] = {"trades":n,"wins":w,"win_rate":round(w/n,3) if n>0 else 0,
                         "pnl":round(p,4),"pnl_r":round(p/avg_risk,2),"r_total":round(p/avg_risk,2)}
        return out

    semanas_out = [{"semana":sw,"trades":s["trades"],
                    "win_rate":round(s["wins"]/s["trades"],3) if s["trades"]>0 else 0,
                    "pnl":round(s["pnl"],4),"capital":s["capital"]}
                   for sw in sorted(stats_semana.keys()) for s in [stats_semana[sw]]]

    now_str = datetime.now(timezone.utc).isoformat()
    resultado = {
        "modo": f"patron_puro_{n_semanas}w_v3",
        "fecha_backtest": now_str,
        "capital_inicial": CAPITAL_INI,
        "config": {
            "capital_ini": CAPITAL_INI, "n_semanas": n_semanas,
            "timeframe": timeframe,
            "pares": pares_ok, "estrategias_activas": list(estrategias_activas),
            "riesgo_pct": riesgo_pct, "rr_ratio": rr_ratio,
            "sl_atr_mult": sl_atr_mult, "min_confidence": min_conf_global,
            "adx_min_operar": adx_min, "filtro_h4_activo": params.get("filtro_h4_activo", True),
            "spread_pips": SPREAD_PIPS, "slippage_pips": SLIPPAGE_PIPS,
            "break_even": True, "max_posiciones": max_posiciones,
            "semanas_validacion": semanas_validacion,
            "sim_start": sim_start.date().isoformat(), "sim_end": sim_end.date().isoformat(),
        },
        "resumen": {
            "capital_final": round(capital,4), "pnl_total": round(pnl_tot,4),
            "pnl_usd": round(pnl_tot,4), "pnl_pct": round(pnl_pct,2),
            "pnl_r": round(r_total,2), "r_total": round(r_total,2),
            "trades": n_total, "total_trades": n_total, "wins": n_wins,
            "win_rate": round(wr,3), "win_rate_global": round(wr,3),
            "profit_factor": round(pf,3), "max_drawdown_pct": round(max_dd*100,2),
        },
        "walk_forward": {
            "train": metricas(trn_trades),
            "validacion": metricas(val_trades),
            "ratio_wr": round(metricas(val_trades)["win_rate"] /
                              metricas(trn_trades)["win_rate"],2) if metricas(trn_trades)["win_rate"]>0 else 0,
        },
        "por_estrategia": est_out(stats_strat),
        "por_par": par_out(stats_par),
        "semanas": semanas_out,
        "equity_curve": equity_curve,
        "trades": trades_all,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(resultado, indent=2, default=str))
    log.info(f"\nGuardado en {output_path}")
    return resultado


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--semanas",        type=int,   default=N_SEMANAS)
    parser.add_argument("--validacion",     type=int,   default=4)
    parser.add_argument("--capital",        type=float, default=CAPITAL_INI)
    parser.add_argument("--pares",          type=str,   default="EUR_USD,USD_CAD,USD_CHF,AUD_USD")
    parser.add_argument("--timeframe",      type=str,   default="M15", choices=["M15","H1","H4"])
    parser.add_argument("--output",         type=str,   default=str(DEFAULT_OUT))
    parser.add_argument("--no-deepseek",    action="store_true")
    parser.add_argument("--params-override",type=str,   default=None,
                        help="JSON string con overrides de parámetros (desde webapp Lab)")
    args = parser.parse_args()

    params = json.loads(PARAMS_FILE.read_text())

    # Aplicar overrides del Lab (ADX, RR, confianza, H4, riesgo_pct, etc.)
    if args.params_override:
        try:
            overrides = json.loads(args.params_override)
            params.update(overrides)
            log.info(f"Params override aplicado: {list(overrides.keys())}")
        except Exception as e:
            log.warning(f"params-override inválido, ignorando: {e}")

    # Capital desde arg (override del Lab)
    capital_run = args.capital if args.capital != CAPITAL_INI else CAPITAL_INI

    pares = [p.strip() for p in args.pares.split(",")]

    # Parchear CAPITAL_INI globalmente para esta ejecución
    import sys as _sys
    _this = _sys.modules[__name__]
    _this.CAPITAL_INI = capital_run

    run_backtest(pares, args.semanas, params, Path(args.output), args.validacion, args.timeframe)
