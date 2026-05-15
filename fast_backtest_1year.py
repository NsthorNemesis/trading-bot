"""
fast_backtest_26w.py — Trading Bot v11
======================================
Backtest rápido single-thread, sin asyncio — M15, 52 semanas (1 año).
Lógica idéntica a SignalAgent con 3 cambios aplicados:
  1. RSI_Bollinger PESOS_TENDENCIA=0.40 → bloqueado si regime_score > 0.62
  2. Hammer pausado (fuera de estrategias_activas)
  3. conf < min_conf correcto (no *0.7)

Optimizaciones de velocidad:
  - ADX vectorizado (EWM pandas) → 680x vs. loop Python
  - Regime scores pre-computados como array NumPy → O(1) lookup
  - Acceso a columnas vía arrays NumPy → 168x vs. pandas iloc
  Resultado: ~10s para carga + 26 semanas completas.
"""
import sys, json, logging, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import deque

import numpy as np
import pandas as pd
from ta.volatility import AverageTrueRange, BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

sys.path.insert(0, str(Path(__file__).parent))
from utils.regime_detector import RegimeDetector

# ── Configuración ────────────────────────────────────────────
PARES = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
DATA_DIR    = Path("data/historical")
PARAMS_FILE = Path("data/calibration/strategy_params.json")
OUT_FILE    = Path("data/backtesting/backtest_1year_nodoji.json")
LOG_FILE    = Path("logs/fast_backtest_1year_nodoji.log")
CAPITAL_INI = 200.0
N_SEMANAS   = 52
SIM_START   = datetime(2025, 5, 9, tzinfo=timezone.utc)
WINDOW      = 60    # velas para ventana de señales

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
SESIONES_ACT  = set(PARAMS.get("sesiones_activas", ["london", "overlap", "new_york"]))
PIP_SIZE      = {"USD_JPY": 0.01}

# ── Logging ──────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), mode="w", encoding="utf-8"),
    ],
    force=True,
)
log = logging.getLogger("fast_bt")


# ── ADX vectorizado (Wilder = EWM, 680x más rápido que loop) ─
def _adx_vectorized(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    up = h - h.shift(1); dn = l.shift(1) - l
    pdm = np.where((up > dn) & (up > 0), up.values, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn.values, 0.0)
    alpha = 1.0 / period
    atr_s = pd.Series(tr.values).ewm(alpha=alpha, adjust=False).mean()
    ps    = pd.Series(pdm, index=df.index).ewm(alpha=alpha, adjust=False).mean()
    ms    = pd.Series(mdm, index=df.index).ewm(alpha=alpha, adjust=False).mean()
    pdi   = 100 * ps / atr_s.replace(0, np.nan).values
    mdi   = 100 * ms / atr_s.replace(0, np.nan).values
    dx    = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx   = dx.fillna(0).ewm(alpha=alpha, adjust=False).mean()
    adx.index = df.index
    return adx


def _make_regime_array(adx_series: pd.Series) -> np.ndarray:
    """Convierte la serie ADX en array de regime_scores (0.0-1.0). Vectorizado."""
    av = adx_series.values
    sv = adx_series.diff(3).fillna(0).values
    base = np.where(av < 15, 0.0, np.where(av > 35, 1.0, (av - 15.0) / 20.0))
    adj  = np.clip(sv / 25.0, -0.20, 0.20)
    return np.clip(base + adj, 0.0, 1.0).astype(np.float32)


# ── Carga de datos con indicadores pre-computados ────────────
def load_pair(par: str) -> pd.DataFrame:
    path = DATA_DIR / f"{par}_M15.json"
    data = json.load(open(path))
    df = pd.DataFrame(data)
    df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "volume": "Volume"}, inplace=True)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    # Indicadores técnicos
    df["ATR_14"] = AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14, fillna=True
    ).average_true_range()
    df["RSI_14"] = RSIIndicator(df["Close"], window=14, fillna=True).rsi()
    df["EMA_20"] = EMAIndicator(df["Close"], window=20, fillna=True).ema_indicator()
    df["EMA_50"] = EMAIndicator(df["Close"], window=50, fillna=True).ema_indicator()
    bb = BollingerBands(df["Close"], window=20, window_dev=2, fillna=True)
    df["BBL_20"] = bb.bollinger_lband()
    df["BBU_20"] = bb.bollinger_hband()

    # Patrones de velas
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    cu = abs(c - o); rg = h - l
    si = o.combine(c, min) - l; ss = h - o.combine(c, max)
    df["HAMMER"]  = ((cu > 0) & (si >= 2 * cu) & (ss <= cu * 0.5))
    df["DOJI"]    = ((rg > 0) & (cu / rg.replace(0, 1) < 0.10))
    df["EBULL"]   = ((c > o) & (c.shift(1) < o.shift(1)) &
                     (c > o.shift(1)) & (o < c.shift(1)) & (cu > cu.shift(1) * 1.05))
    df["EBEAR"]   = ((c < o) & (c.shift(1) > o.shift(1)) &
                     (c < o.shift(1)) & (o > c.shift(1)) & (cu > cu.shift(1) * 1.05))

    # Régimen ADX pre-computado (vectorizado)
    adx = _adx_vectorized(df, 14)
    df["REGIME"]  = _make_regime_array(adx)
    df["ADX"]     = adx.values

    return df


# ── Sesión ───────────────────────────────────────────────────
def sesion(ts: datetime) -> str:
    h = ts.hour
    if  7 <= h < 13: return "london"
    if 13 <= h < 17: return "overlap"
    if 17 <= h < 22: return "new_york"
    return "asia"


# ── Position Tracker ─────────────────────────────────────────
class Tracker:
    def __init__(self, capital: float):
        self.capital     = capital
        self.capital_ini = capital
        self.peak        = capital
        self.open        = {}   # id → dict
        self.closed      = []
        self._nid        = 1

    def open_trade(self, par, dir_, entry, sl, tp, estrat, ts):
        sl_dist  = abs(entry - sl)
        risk_usd = self.capital * RIESGO_PCT
        units    = max(1, int(risk_usd / sl_dist)) if sl_dist > 1e-9 else 1
        tid = self._nid; self._nid += 1
        self.open[tid] = dict(par=par, dir=dir_, entry=entry,
                              sl=sl, tp=tp, units=units,
                              estrat=estrat, opened_at=ts)
        return tid

    def check_fills(self, par: str, high: float, low: float, ts):
        out = []
        for tid, t in list(self.open.items()):
            if t["par"] != par:
                continue
            if t["dir"] == "long":
                if low  <= t["sl"]: pnl, res = (t["sl"] - t["entry"]) * t["units"], "SL"
                elif high >= t["tp"]: pnl, res = (t["tp"] - t["entry"]) * t["units"], "TP"
                else: continue
            else:
                if high >= t["sl"]: pnl, res = (t["entry"] - t["sl"]) * t["units"], "SL"
                elif low  <= t["tp"]: pnl, res = (t["entry"] - t["tp"]) * t["units"], "TP"
                else: continue
            self.capital += pnl
            self.peak = max(self.peak, self.capital)
            rec = {**t, "pnl": round(pnl, 5), "resultado": res, "closed_at": ts}
            out.append(rec); self.closed.append(rec)
            del self.open[tid]
        return out

    def max_dd_pct(self) -> float:
        cap = self.capital_ini; peak = cap; mdd = 0.0
        for t in self.closed:
            cap += t["pnl"]; peak = max(peak, cap)
            dd = (peak - cap) / peak if peak > 0 else 0
            mdd = max(mdd, dd)
        return mdd


# ── MAIN ─────────────────────────────────────────────────────
def run():
    t_total = time.time()
    log.info("=" * 62)
    log.info("FAST BACKTEST 26 SEMANAS — Trading Bot v11")
    log.info(f"Capital: ${CAPITAL_INI:.2f} | Timeframe: M15 | Semanas: {N_SEMANAS}")
    log.info(f"Inicio simulación: {SIM_START.date()}")
    log.info(f"Estrategias activas: {', '.join(sorted(ESTRATEGIAS_ACTIVAS))}")
    log.info(f"min_confidence={MIN_CONF} | RR={RR_RATIO} | SL_ATR={SL_ATR_MULT}")
    log.info(f"RSI_Bollinger PESOS_TENDENCIA=0.40 (bloqueado si ADX>~27)")
    log.info("=" * 62)

    # ── Carga + indicadores ───────────────────────────────────
    t_load = time.time()
    dfs: dict[str, pd.DataFrame] = {}
    for par in PARES:
        try:
            df = load_pair(par)
            dfs[par] = df
            log.info(f"  {par}: {len(df):,} velas "
                     f"({df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()})")
        except Exception as e:
            log.warning(f"  {par}: SKIP — {e}")
    log.info(f"  [carga] {time.time()-t_load:.2f}s")

    # ── Extraer arrays NumPy por par (acceso O(1)) ────────────
    arrs: dict[str, dict] = {}
    for par, df in dfs.items():
        arrs[par] = {
            "ts":     df["ts"].values,
            "high":   df["High"].values,
            "low":    df["Low"].values,
            "close":  df["Close"].values,
            "atr":    df["ATR_14"].values,
            "rsi":    df["RSI_14"].values,
            "ema20":  df["EMA_20"].values,
            "ema50":  df["EMA_50"].values,
            "bbl":    df["BBL_20"].values,
            "bbu":    df["BBU_20"].values,
            "hammer": df["HAMMER"].values.astype(bool),
            "doji":   df["DOJI"].values.astype(bool),
            "ebull":  df["EBULL"].values.astype(bool),
            "ebear":  df["EBEAR"].values.astype(bool),
            "regime": df["REGIME"].values,   # pre-computed → O(1)
            "adx":    df["ADX"].values,
        }

    rd      = RegimeDetector()
    tracker = Tracker(CAPITAL_INI)
    metricas: list[dict] = []
    cooldown: dict[str, pd.Timestamp] = {}

    for semana in range(1, N_SEMANAS + 1):
        w_start = SIM_START + timedelta(weeks=semana - 1)
        w_end   = w_start   + timedelta(weeks=1)
        n_antes = len(tracker.closed)
        w_start_ns = pd.Timestamp(w_start).value
        w_end_ns   = pd.Timestamp(w_end).value

        log.info(f"\n📅 SEMANA {semana:2d} | {w_start.date()} → {w_end.date()}")

        # Construir lista cronológica de candles de la semana
        # (ts_ns, par, global_idx) — ordenado por tiempo
        ticks = []
        for par, a in arrs.items():
            ts_arr = a["ts"].astype("int64")
            idxs   = np.where((ts_arr >= w_start_ns) & (ts_arr < w_end_ns))[0]
            for gi in idxs:
                ticks.append((ts_arr[gi], par, int(gi)))
        ticks.sort(key=lambda x: x[0])

        regime_hist: dict[str, deque] = {p: deque(maxlen=20) for p in dfs}

        for (ts_ns, par, gi) in ticks:
            a = arrs[par]

            # Check SL/TP para este par
            ts_dt = a["ts"][gi].astype("datetime64[ms]").astype(datetime).replace(tzinfo=timezone.utc)
            cerrados = tracker.check_fills(par, float(a["high"][gi]), float(a["low"][gi]), ts_dt)
            for t in cerrados:
                emoji = "✅" if t["resultado"] == "TP" else "🔴"
                log.info(f"  {emoji} {par} [{t['resultado']}] "
                         f"est={t['estrat']} PnL=${t['pnl']:+.4f} "
                         f"Capital=${tracker.capital:.2f}")

            # Filtros globales
            ts_hour = (ts_ns // 3_600_000_000_000) % 24
            if   7 <= ts_hour < 13: ses = "london"
            elif 13 <= ts_hour < 17: ses = "overlap"
            elif 17 <= ts_hour < 22: ses = "new_york"
            else:                    ses = "asia"
            if ses not in SESIONES_ACT:
                continue
            if len(tracker.open) >= MAX_POS:
                continue
            if gi < 30:
                continue

            # Cooldown
            last_cd = cooldown.get(par)
            if last_cd is not None:
                diff_ns = ts_ns - last_cd
                if diff_ns < COOLDOWN_MINS * 60 * 1_000_000_000:
                    continue

            # ── Pre-filtro técnico (idéntico a SignalAgent) ──
            rv = float(a["rsi"][gi]); cl = float(a["close"][gi])
            tend     = "up" if a["ema20"][gi] > a["ema50"][gi] else "down"
            rsi_s    = rv > 68;  rsi_b = rv < 32
            pbbl     = cl < float(a["bbl"][gi]) * 1.002
            pbbu     = cl > float(a["bbu"][gi]) * 0.998
            ham      = bool(a["hammer"][gi])
            doj      = bool(a["doji"][gi])
            ebull    = bool(a["ebull"][gi])
            ebear    = bool(a["ebear"][gi])

            sl_bool  = ((ham and tend == "down") or (doj and rsi_b) or
                        (rsi_b and pbbl) or (ebull and tend == "up"))
            ss_bool  = ((doj and rsi_s) or (rsi_s and pbbu) or
                        (ebear and tend == "down"))
            if not sl_bool and not ss_bool:
                continue

            if sl_bool:
                dir_ = "long"
                if ham:        estrat = "Hammer"
                elif doj:      estrat = "Doji"
                elif ebull:    estrat = "Engulfing"
                else:          estrat = "RSI_Bollinger" if pbbl else "RSI_Divergence"
            else:
                dir_ = "short"
                if doj:        estrat = "Doji"
                elif ebear:    estrat = "Engulfing"
                else:          estrat = "RSI_Bollinger"

            if estrat not in ESTRATEGIAS_ACTIVAS:
                continue

            # ── Régimen ADX (O(1) lookup) ─────────────────────
            rs = float(a["regime"][gi])
            regime_hist[par].append(rs)
            trans = rd.detectar_transicion(list(regime_hist[par]))
            peso  = rd.peso_estrategia(estrat, rs, trans)
            conf  = round(0.45 * peso, 3)
            if conf < MIN_CONF:
                continue

            # ── Abrir trade ───────────────────────────────────
            entry = cl
            atr_v = float(a["atr"][gi])
            ps    = PIP_SIZE.get(par, 0.0001)
            sl_d  = float(np.clip(atr_v * SL_ATR_MULT, MIN_SL_PIPS * ps, MAX_SL_PIPS * ps))

            if dir_ == "long":
                sl_p = entry - sl_d; tp_p = entry + sl_d * RR_RATIO
            else:
                sl_p = entry + sl_d; tp_p = entry - sl_d * RR_RATIO

            tracker.open_trade(par, dir_, entry, sl_p, tp_p, estrat, ts_dt)
            cooldown[par] = ts_ns

            log.info(f"  [OPEN] {par} {dir_.upper()} est={estrat} "
                     f"conf={conf:.0%} rs={rs:.2f} entry={entry:.5f} "
                     f"adx={a['adx'][gi]:.1f}")

        # ── Métricas semanales ────────────────────────────────
        nuevos  = tracker.closed[n_antes:]
        n       = len(nuevos)
        ganadas = sum(1 for t in nuevos if t["resultado"] == "TP")
        wr      = ganadas / n if n > 0 else 0.0
        pnl_tot = sum(t["pnl"] for t in nuevos)
        pos_p   = sum(t["pnl"] for t in nuevos if t["pnl"] > 0)
        neg_p   = abs(sum(t["pnl"] for t in nuevos if t["pnl"] < 0))
        pf      = pos_p / neg_p if neg_p > 0 else (99.0 if pos_p > 0 else 0.0)

        flag = "✅" if (wr >= 0.55 and pf >= 1.4) else ("⚠️" if n == 0 else "❌")
        log.info(f"  {flag} Trades:{n:3d} | WR:{wr*100:5.1f}% | "
                 f"PF:{pf:5.2f} | PnL:${pnl_tot:+.2f}")
        log.info(f"     Capital: ${tracker.capital:.2f}")
        log.info("─" * 55)

        metricas.append({
            "semana": semana, "inicio": str(w_start.date()),
            "fin": str(w_end.date()), "trades": n,
            "ganadas": ganadas, "win_rate": round(wr, 3),
            "profit_factor": round(pf, 3),
            "pnl_total": round(pnl_tot, 4),
            "capital_fin": round(tracker.capital, 2),
        })

    # ── Resumen final ─────────────────────────────────────────
    tot_trades = sum(m["trades"] for m in metricas)
    tot_won    = sum(m["ganadas"] for m in metricas)
    tot_pnl    = tracker.capital - tracker.capital_ini
    wr_global  = tot_won / tot_trades if tot_trades > 0 else 0
    max_dd     = tracker.max_dd_pct()

    by_estrat: dict = {}
    for t in tracker.closed:
        e = t.get("estrat", "?")
        s = by_estrat.setdefault(e, {"n": 0, "won": 0, "pnl": 0.0})
        s["n"] += 1
        s["won"] += 1 if t["resultado"] == "TP" else 0
        s["pnl"] += t["pnl"]

    log.info("\n" + "═" * 62)
    log.info("RESUMEN FINAL — 26 SEMANAS")
    log.info("═" * 62)
    log.info(f"Capital inicial : ${tracker.capital_ini:.2f}")
    log.info(f"Capital final   : ${tracker.capital:.2f}")
    log.info(f"PnL total       : ${tot_pnl:+.2f}  ({tot_pnl/tracker.capital_ini*100:+.1f}%)")
    log.info(f"Total trades    : {tot_trades}")
    log.info(f"WR global       : {wr_global*100:.1f}%")
    log.info(f"Max drawdown    : {max_dd*100:.1f}%")
    log.info(f"Tiempo total    : {time.time()-t_total:.1f}s")
    log.info("\nPor estrategia:")
    for e, s in sorted(by_estrat.items()):
        wr_e = s["won"] / s["n"] if s["n"] > 0 else 0
        log.info(f"  {e:22s}  N={s['n']:4d}  WR={wr_e*100:5.1f}%  PnL=${s['pnl']:+.2f}")
    log.info("═" * 62)

    # ── Guardar JSON ──────────────────────────────────────────
    resultado = {
        "config": {
            "capital_ini": CAPITAL_INI, "n_semanas": N_SEMANAS,
            "sim_start": str(SIM_START.date()),
            "estrategias_activas": sorted(ESTRATEGIAS_ACTIVAS),
            "min_confidence": MIN_CONF, "rr_ratio": RR_RATIO,
            "sl_atr_mult": SL_ATR_MULT, "timeframe": "M15",
            "cambios_v11": [
                "RSI_Bollinger PESOS_TENDENCIA=0.40 (bloqueado ADX>~27)",
                "Hammer pausado",
                "conf < min_conf correcto",
            ],
        },
        "resumen": {
            "capital_final":    round(tracker.capital, 2),
            "pnl_total":        round(tot_pnl, 2),
            "pnl_pct":          round(tot_pnl / tracker.capital_ini * 100, 2),
            "total_trades":     tot_trades,
            "win_rate_global":  round(wr_global, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
        },
        "por_estrategia": {
            e: {
                "trades":   s["n"],
                "win_rate": round(s["won"] / s["n"], 3) if s["n"] > 0 else 0,
                "pnl":      round(s["pnl"], 2),
            }
            for e, s in by_estrat.items()
        },
        "semanas": metricas,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"\nResultados: {OUT_FILE}")


if __name__ == "__main__":
    run()
