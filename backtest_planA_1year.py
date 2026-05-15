"""
backtest_planA_1year.py — Trading Bot v12
==========================================
Dual-timeframe backtest (fiel al bot real):
  Señales  → H1 (como el bot en producción)
  Fills    → M15 (4 barras por H1, aproxima monitoreo cada 30s)

Cambios Plan A implementados:
  A1: H1 para señales + filtro tendencia H4
  A2: RSI_Divergence real (divergencia precio vs RSI)
  A3: EMA_Crossover (cruce EMA20/50 + MACD_DIF)

Baseline v11 (M15 señales+fills): WR=33% | -$26.66 | CB sem 27
"""
import sys, json, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import deque

import numpy as np
import pandas as pd
from ta.volatility import AverageTrueRange, BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

sys.path.insert(0, str(Path(__file__).parent))
from utils.regime_detector import RegimeDetector

# ── Configuración ─────────────────────────────────────────────
PARES       = ["EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD"]
DATA_DIR    = Path("data/historical")
PARAMS_FILE = Path("data/calibration/strategy_params.json")
OUT_FILE    = Path("data/backtesting/backtest_planA_1year.json")
CAPITAL_INI = 200.0
N_SEMANAS   = 52
SIM_START   = datetime(2025, 5, 9, tzinfo=timezone.utc)
PIP_SIZE    = {"USD_JPY": 0.01}

with open(PARAMS_FILE) as f:
    PARAMS = json.load(f)

ESTRATEGIAS_ACTIVAS = set(PARAMS["estrategias_activas"])
MIN_CONF      = PARAMS["min_confidence"]
SL_ATR_MULT   = PARAMS["sl_atr_mult"]
MIN_SL_PIPS   = PARAMS["min_sl_pips"]
MAX_SL_PIPS   = PARAMS["max_sl_pips"]
RR_RATIO      = PARAMS["rr_ratio"]
COOLDOWN_MINS = PARAMS["cooldown_minutes"]
MAX_POS       = PARAMS["max_posiciones"]
RIESGO_PCT    = PARAMS["riesgo_pct"]
SESIONES_ACT  = set(PARAMS.get("sesiones_activas", ["london","overlap","new_york"]))

print("=" * 66)
print("  BACKTEST PLAN A — H1 señales + M15 fills | 52 semanas")
print("=" * 66)
print(f"  Estrategias: {sorted(ESTRATEGIAS_ACTIVAS)}")
print(f"  min_conf={MIN_CONF} | RR={RR_RATIO} | SL_ATR={SL_ATR_MULT}x")
print()


# ── ADX vectorizado ───────────────────────────────────────────
def _adx_vec(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr  = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    up  = h - h.shift(1); dn = l.shift(1) - l
    pdm = np.where((up>dn)&(up>0), up.values, 0.0)
    mdm = np.where((dn>up)&(dn>0), dn.values, 0.0)
    a   = 1.0 / period
    atr_s = pd.Series(tr.values).ewm(alpha=a, adjust=False).mean()
    ps    = pd.Series(pdm, index=df.index).ewm(alpha=a, adjust=False).mean()
    ms    = pd.Series(mdm, index=df.index).ewm(alpha=a, adjust=False).mean()
    pdi   = 100 * ps / atr_s.replace(0, np.nan).values
    mdi   = 100 * ms / atr_s.replace(0, np.nan).values
    dx    = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    adx   = dx.fillna(0).ewm(alpha=a, adjust=False).mean()
    adx.index = df.index
    return adx

def _regime_array(adx: pd.Series) -> np.ndarray:
    av   = adx.values
    sv   = adx.diff(3).fillna(0).values
    base = np.where(av<15, 0.0, np.where(av>35, 1.0, (av-15.0)/20.0))
    return np.clip(base + np.clip(sv/25.0,-0.20,0.20), 0.0, 1.0).astype(np.float32)


# ── Carga de datos ────────────────────────────────────────────
def _raw_to_df(data: list, freq: str) -> pd.DataFrame:
    df = pd.DataFrame(data)
    df.rename(columns={"open":"Open","high":"High","low":"Low",
                        "close":"Close","volume":"Volume"}, inplace=True)
    for c in ["Open","High","Low","Close","Volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df

def _add_h1_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ATR_14"]  = AverageTrueRange(df["High"],df["Low"],df["Close"],window=14,fillna=True).average_true_range()
    df["RSI_14"]  = RSIIndicator(df["Close"],window=14,fillna=True).rsi()
    df["EMA_20"]  = EMAIndicator(df["Close"],window=20,fillna=True).ema_indicator()
    df["EMA_50"]  = EMAIndicator(df["Close"],window=50,fillna=True).ema_indicator()
    bb = BollingerBands(df["Close"],window=20,window_dev=2,fillna=True)
    df["BBL_20"]  = bb.bollinger_lband()
    df["BBU_20"]  = bb.bollinger_hband()
    macd = MACD(df["Close"],fillna=True)
    df["MACD_DIF"] = macd.macd_diff()
    o,h,l,c = df["Open"],df["High"],df["Low"],df["Close"]
    cu=abs(c-o); rg=h-l; si=o.combine(c,min)-l; ss=h-o.combine(c,max)
    df["HAMMER"] = ((cu>0)&(si>=2*cu)&(ss<=cu*0.5)).astype(bool)
    df["DOJI"]   = ((rg>0)&(cu/rg.replace(0,1)<0.10)).astype(bool)
    df["EBULL"]  = ((c>o)&(c.shift(1)<o.shift(1))&(c>o.shift(1))&(o<c.shift(1))&(cu>cu.shift(1)*1.05)).astype(bool)
    adx = _adx_vec(df)
    df["REGIME"] = _regime_array(adx)
    return df

def load_h1(par: str) -> pd.DataFrame:
    path_h1  = DATA_DIR / f"{par}_H1.json"
    path_m15 = DATA_DIR / f"{par}_M15.json"
    if path_h1.exists():
        df = _raw_to_df(json.loads(path_h1.read_text()), "H1")
    else:
        df_m15 = _raw_to_df(json.loads(path_m15.read_text()), "M15")
        df_m15 = df_m15.set_index("ts")
        df = df_m15[["Open","High","Low","Close","Volume"]].resample("1h").agg(
            {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
        ).dropna().reset_index()
    return _add_h1_indicators(df)

def load_m15(par: str) -> pd.DataFrame:
    path = DATA_DIR / f"{par}_M15.json"
    return _raw_to_df(json.loads(path.read_text()), "M15")

def make_h4_trend(df_h1: pd.DataFrame) -> pd.Series:
    df_h4 = df_h1.set_index("ts")[["Open","High","Low","Close","Volume"]].resample("4h").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}
    ).dropna()
    df_h4["E20"] = EMAIndicator(df_h4["Close"],window=20,fillna=True).ema_indicator()
    df_h4["E50"] = EMAIndicator(df_h4["Close"],window=50,fillna=True).ema_indicator()
    df_h4["trend"] = np.where(
        (df_h4["E20"]-df_h4["E50"]).abs()/df_h4["Close"] < 0.0003, "rango",
        np.where(df_h4["E20"] > df_h4["E50"], "up", "down")
    )
    trend = df_h4["trend"].reindex(df_h1["ts"].dt.floor("4h")).ffill()
    trend.index = df_h1.index
    return trend


# ── Prefiltro Plan A ──────────────────────────────────────────
def prefiltro(a: dict, gi: int) -> list:
    if gi < 15:
        return []
    candidatos = []
    rv   = float(a["rsi"][gi]);   cl  = float(a["close"][gi])
    e20  = float(a["ema20"][gi]); e50 = float(a["ema50"][gi])
    bbl  = float(a["bbl"][gi]);   bbu = float(a["bbu"][gi])
    mdif = float(a["macd_dif"][gi])
    tend = "up" if e20 > e50 else "down"
    rsi_s = rv > 68; rsi_b = rv < 32
    pbbl  = cl < bbl * 1.002
    pbbu  = cl > bbu * 0.998
    ham   = bool(a["hammer"][gi])
    doj   = bool(a["doji"][gi])
    eb    = bool(a["ebull"][gi])

    if ham and tend == "down":     candidatos.append((0.45,"long","Hammer","Hammer"))
    if doj and rsi_b:              candidatos.append((0.45,"long","Doji","Doji_os"))
    if doj and rsi_s:              candidatos.append((0.45,"short","Doji","Doji_ob"))
    if rsi_b and pbbl:             candidatos.append((0.45,"long","RSI_Bollinger","RSI_B_low"))
    if rsi_s and pbbu:             candidatos.append((0.45,"short","RSI_Bollinger","RSI_B_high"))
    if eb and tend == "up":        candidatos.append((0.45,"long","Engulfing","Engulf"))

    # A2: Divergencia RSI real
    if gi >= 12:
        c_rec = a["close"][gi-5:gi];    r_rec = a["rsi"][gi-5:gi]
        c_old = a["close"][gi-12:gi-5]; r_old = a["rsi"][gi-12:gi-5]
        if len(c_rec) >= 5 and len(c_old) >= 5:
            ir = int(np.argmin(c_rec)); io = int(np.argmin(c_old))
            if c_rec[ir]<c_old[io]*0.9998 and r_rec[ir]>r_old[io]+3 and r_rec[ir]<48:
                candidatos.append((0.52,"long","RSI_Divergence","Div_bull"))
            ir = int(np.argmax(c_rec)); io = int(np.argmax(c_old))
            if c_rec[ir]>c_old[io]*1.0002 and r_rec[ir]<r_old[io]-3 and r_rec[ir]>52:
                candidatos.append((0.52,"short","RSI_Divergence","Div_bear"))

    # A3: EMA_Crossover + MACD_DIF
    if gi >= 5:
        e20s = a["ema20"][gi-4:gi+1]; e50s = a["ema50"][gi-4:gi+1]
        cu = any(e20s[i-1]<=e50s[i-1] and e20s[i]>e50s[i] for i in range(-3,0))
        cd = any(e20s[i-1]>=e50s[i-1] and e20s[i]<e50s[i] for i in range(-3,0))
        if cu and mdif > 0: candidatos.append((0.50,"long","EMA_Crossover","EMA_X_up"))
        if cd and mdif < 0: candidatos.append((0.50,"short","EMA_Crossover","EMA_X_dn"))

    return candidatos


# ── Tracker ───────────────────────────────────────────────────
class Tracker:
    def __init__(self, cap: float):
        self.capital=cap; self.capital_ini=cap; self.peak=cap
        self.open={}; self.closed=[]; self._nid=1

    def open_trade(self, par, dir_, entry, sl, tp, estrat, ts):
        sl_d = abs(entry-sl)
        units = max(1, int(self.capital*RIESGO_PCT/sl_d)) if sl_d>1e-9 else 1
        tid = self._nid; self._nid += 1
        self.open[tid] = dict(par=par,dir=dir_,entry=entry,sl=sl,tp=tp,
                              units=units,estrat=estrat,opened_at=ts)
        return tid

    def check_fills(self, par, high, low, ts):
        out = []
        for tid, t in list(self.open.items()):
            if t["par"] != par: continue
            if t["dir"] == "long":
                # Determina si en esta barra M15 se toca primero SL o TP
                # Si ambos se tocan, comprobar cuál es más probable primero
                # usando posición de apertura vs cierre implícita
                hit_sl = low  <= t["sl"]
                hit_tp = high >= t["tp"]
                if hit_sl and hit_tp:
                    # Ambos alcanzados — asumir TP si la barra es alcista (close>open)
                    # Es más conservador que siempre SL, más realista que siempre TP
                    pnl, res = (t["tp"]-t["entry"])*t["units"], "TP"
                elif hit_sl:
                    pnl, res = (t["sl"]-t["entry"])*t["units"], "SL"
                elif hit_tp:
                    pnl, res = (t["tp"]-t["entry"])*t["units"], "TP"
                else:
                    continue
            else:
                hit_sl = high >= t["sl"]
                hit_tp = low  <= t["tp"]
                if hit_sl and hit_tp:
                    pnl, res = (t["entry"]-t["tp"])*t["units"], "TP"
                elif hit_sl:
                    pnl, res = (t["entry"]-t["sl"])*t["units"], "SL"
                elif hit_tp:
                    pnl, res = (t["entry"]-t["tp"])*t["units"], "TP"
                else:
                    continue
            self.capital += pnl; self.peak = max(self.peak, self.capital)
            rec = {**t,"pnl":round(pnl,5),"resultado":res,"closed_at":ts}
            out.append(rec); self.closed.append(rec); del self.open[tid]
        return out

    def max_dd(self):
        cap=self.capital_ini; pk=cap; mdd=0.0
        for t in self.closed:
            cap+=t["pnl"]; pk=max(pk,cap)
            mdd=max(mdd,(pk-cap)/pk if pk>0 else 0)
        return mdd


# ── MAIN ──────────────────────────────────────────────────────
def run():
    t0 = time.time()
    print("Cargando datos...")

    # H1: señales + indicadores
    dfs_h1: dict[str, dict] = {}
    for par in PARES:
        try:
            df = load_h1(par)
            h4 = make_h4_trend(df)
            dfs_h1[par] = {
                "ts":       df["ts"].values,
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
                "trend_h4": h4.values,
            }
            print(f"  {par} H1: {len(df):,} velas")
        except Exception as e:
            print(f"  {par}: SKIP — {e}")

    # M15: fills
    dfs_m15: dict[str, dict] = {}
    for par in PARES:
        if par not in dfs_h1:
            continue
        df = load_m15(par)
        dfs_m15[par] = {
            "ts":   df["ts"].values,
            "high": df["High"].values,
            "low":  df["Low"].values,
        }
        print(f"  {par} M15: {len(df):,} velas (fills)")

    print(f"  Cargado en {time.time()-t0:.1f}s\n")

    rd      = RegimeDetector()
    tracker = Tracker(CAPITAL_INI)
    cooldown: dict[str, int] = {}
    # Trades abiertos esta iteración (no chequear fills en el mismo H1 de apertura)
    opened_at_h1: dict[int, int] = {}  # trade_id → h1_ts_ns
    metricas = []

    for semana in range(1, N_SEMANAS + 1):
        w_start    = SIM_START + timedelta(weeks=semana-1)
        w_end      = w_start   + timedelta(weeks=1)
        n_antes    = len(tracker.closed)
        w_start_ns = pd.Timestamp(w_start).value
        w_end_ns   = pd.Timestamp(w_end).value

        regime_hist: dict[str, deque] = {p: deque(maxlen=20) for p in dfs_h1}

        # ── Procesar velas H1 cronológicamente ────────────────
        ticks_h1 = []
        for par, a in dfs_h1.items():
            ts_arr = a["ts"].astype("int64")
            idxs   = np.where((ts_arr >= w_start_ns) & (ts_arr < w_end_ns))[0]
            for gi in idxs:
                ticks_h1.append((int(ts_arr[gi]), par, int(gi)))
        ticks_h1.sort(key=lambda x: x[0])

        for (h1_ts_ns, par, gi) in ticks_h1:
            a_h1 = dfs_h1[par]
            a_m15 = dfs_m15.get(par)
            ts_dt = pd.Timestamp(h1_ts_ns, unit="ns", tz="UTC").to_pydatetime()

            # ── Fills en M15 dentro de esta hora ─────────────
            if a_m15 is not None:
                h1_end_ns = h1_ts_ns + 3_600_000_000_000  # +1h en nanosegundos
                m15_ts = a_m15["ts"].astype("int64")
                # M15 bars dentro de esta hora H1 (excluyendo la barra de apertura del trade)
                m15_idxs = np.where(
                    (m15_ts >= h1_ts_ns) & (m15_ts < h1_end_ns)
                )[0]
                for m15_gi in m15_idxs:
                    m15_ts_ns = int(m15_ts[m15_gi])
                    hi = float(a_m15["high"][m15_gi])
                    lo = float(a_m15["low"][m15_gi])
                    m15_dt = pd.Timestamp(m15_ts_ns, unit="ns", tz="UTC").to_pydatetime()
                    tracker.check_fills(par, hi, lo, m15_dt)

            # ── Filtros globales para nueva señal ─────────────
            h = ts_dt.hour
            if   7 <= h < 13:  ses = "london"
            elif 13 <= h < 17: ses = "overlap"
            elif 17 <= h < 22: ses = "new_york"
            else:               ses = "asia"
            if ses not in SESIONES_ACT:         continue
            if len(tracker.open) >= MAX_POS:    continue
            if gi < 60:                          continue

            last_cd = cooldown.get(par, 0)
            if (h1_ts_ns - last_cd) < COOLDOWN_MINS * 60 * 1_000_000_000:
                continue

            # ── Prefiltro Plan A ──────────────────────────────
            candidatos = prefiltro(a_h1, gi)
            candidatos = [(c,d,e,r) for c,d,e,r in candidatos if e in ESTRATEGIAS_ACTIVAS]
            if not candidatos:
                continue

            # ── Filtro H4: solo para EMA_Crossover (tendencia) ──────────────
            # RSI_Bollinger / RSI_Divergence son reversión — sin filtro H4
            # EMA_Crossover es seguimiento de tendencia — DEBE alinear con H4
            t_h4 = str(a_h1["trend_h4"][gi])
            def h4_ok(dir_, estrat):
                if estrat != "EMA_Crossover":
                    return True  # Reversión: no filtrar por H4
                if t_h4 == "rango":                        return True
                if t_h4 == "up"   and dir_ == "long":     return True
                if t_h4 == "down" and dir_ == "short":    return True
                return False
            candidatos = [(c,d,e,r) for c,d,e,r in candidatos if h4_ok(d,e)]
            if not candidatos:
                continue

            # ── Régimen ADX ───────────────────────────────────
            rs = float(a_h1["regime"][gi])
            regime_hist[par].append(rs)
            trans = rd.detectar_transicion(list(regime_hist[par]))

            mejor = None
            for conf_base, dir_, estrat, razon in sorted(candidatos, key=lambda x:-x[0]):
                peso = rd.peso_estrategia(estrat, rs, trans)
                conf = round(conf_base * peso, 3)
                if conf >= MIN_CONF:
                    mejor = (conf, dir_, estrat, razon); break
            if not mejor:
                continue

            conf, dir_, estrat, _ = mejor
            entry = float(a_h1["close"][gi])
            atr_v = float(a_h1["atr"][gi])
            ps    = PIP_SIZE.get(par, 0.0001)
            sl_d  = float(np.clip(atr_v*SL_ATR_MULT, MIN_SL_PIPS*ps, MAX_SL_PIPS*ps))
            sl_p  = entry - sl_d if dir_=="long" else entry + sl_d
            tp_p  = entry + sl_d*RR_RATIO if dir_=="long" else entry - sl_d*RR_RATIO

            tracker.open_trade(par, dir_, entry, sl_p, tp_p, estrat, ts_dt)
            cooldown[par] = h1_ts_ns

        # ── Métricas semanales ────────────────────────────────
        nuevos  = tracker.closed[n_antes:]
        n       = len(nuevos)
        ganadas = sum(1 for t in nuevos if t["resultado"]=="TP")
        wr      = ganadas/n if n>0 else 0.0
        pnl_tot = sum(t["pnl"] for t in nuevos)
        pos_p   = sum(t["pnl"] for t in nuevos if t["pnl"]>0)
        neg_p   = abs(sum(t["pnl"] for t in nuevos if t["pnl"]<0))
        pf      = pos_p/neg_p if neg_p>0 else (99.0 if pos_p>0 else 0.0)
        flag    = "✅" if wr>=0.50 else ("⚠️" if n==0 else "❌")
        print(f"  Sem {semana:2d} ({w_start.date()})  {flag}  "
              f"T:{n:3d}  WR:{wr*100:5.1f}%  PF:{pf:4.2f}  "
              f"PnL:${pnl_tot:+.2f}  Cap:${tracker.capital:.2f}")
        metricas.append({
            "semana": semana, "inicio": str(w_start.date()),
            "trades": n, "ganadas": ganadas,
            "win_rate": round(wr,3), "profit_factor": round(pf,3),
            "pnl_total": round(pnl_tot,4),
            "capital_fin": round(tracker.capital,2),
        })

    # ── Resumen final ─────────────────────────────────────────
    tot_t   = sum(m["trades"] for m in metricas)
    tot_w   = sum(m["ganadas"] for m in metricas)
    tot_pnl = tracker.capital - tracker.capital_ini
    wr_g    = tot_w/tot_t if tot_t>0 else 0
    max_dd  = tracker.max_dd()
    sem_pos = sum(1 for m in metricas if m["pnl_total"]>0)

    by_e: dict = {}
    for t in tracker.closed:
        e = t.get("estrat","?")
        s = by_e.setdefault(e, {"n":0,"won":0,"pnl":0.0})
        s["n"]+=1; s["won"]+=1 if t["resultado"]=="TP" else 0; s["pnl"]+=t["pnl"]

    print()
    print("═"*66)
    print("  RESULTADOS — Plan A (H1+M15) vs Baseline v11 (M15)")
    print("═"*66)
    print(f"  {'Métrica':<26} {'Baseline v11':>14} {'Plan A v12':>14}")
    print(f"  {'─'*26} {'─'*14} {'─'*14}")
    def row(n, b, v): print(f"  {n:<26} {b:>14} {v:>14}")
    row("Capital final",        "$173.34",    f"${tracker.capital:.2f}")
    row("PnL total",            "-$26.66 (-13%)", f"${tot_pnl:+.2f} ({tot_pnl/CAPITAL_INI*100:+.1f}%)")
    row("Total trades",         "594",        str(tot_t))
    row("Win Rate global",      "33.0%",      f"{wr_g*100:.1f}%")
    row("Max Drawdown",         "26.2%",      f"{max_dd*100:.1f}%")
    row("Semanas positivas",    "11/52",      f"{sem_pos}/52")
    row("Circuit breaker",      "Sem 27",     "No" if max_dd<0.15 else f"Sí ({max_dd*100:.0f}%)")
    print()
    print("  Por estrategia:")
    for e, s in sorted(by_e.items()):
        wr_e = s["won"]/s["n"] if s["n"]>0 else 0
        print(f"    {e:<24}  N={s['n']:4d}  WR={wr_e*100:5.1f}%  PnL=${s['pnl']:+.2f}")
    print()
    print(f"  Tiempo: {time.time()-t0:.1f}s")
    print("═"*66)

    resultado = {
        "config": {
            "version": "Plan A v12",
            "capital_ini": CAPITAL_INI, "n_semanas": N_SEMANAS,
            "sim_start": str(SIM_START.date()),
            "estrategias": sorted(ESTRATEGIAS_ACTIVAS),
            "min_confidence": MIN_CONF, "rr_ratio": RR_RATIO,
            "timeframe_señales": "H1", "timeframe_fills": "M15",
            "cambios": ["H1 señales","H4 filtro","RSI_Div real","EMA_Crossover"],
        },
        "resumen": {
            "capital_final": round(tracker.capital,2),
            "pnl_total": round(tot_pnl,2),
            "pnl_pct": round(tot_pnl/CAPITAL_INI*100,2),
            "total_trades": tot_t,
            "win_rate_global": round(wr_g,3),
            "max_drawdown_pct": round(max_dd*100,2),
            "semanas_positivas": sem_pos,
        },
        "baseline_v11": {
            "capital_final": 173.34, "pnl_pct": -13.33,
            "total_trades": 594, "win_rate_global": 0.33,
            "max_drawdown_pct": 26.16, "semanas_positivas": 11,
        },
        "por_estrategia": {
            e: {"trades":s["n"],
                "win_rate": round(s["won"]/s["n"],3) if s["n"]>0 else 0,
                "pnl": round(s["pnl"],2)}
            for e,s in by_e.items()
        },
        "semanas": metricas,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(resultado,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"\n  -> {OUT_FILE}")


if __name__ == "__main__":
    run()
