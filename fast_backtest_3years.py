"""
fast_backtest_3years.py — Trading Bot v11
==========================================
Simulación 3 años completos (156 semanas) con parámetros optimizados:
  - riesgo_pct    : 0.015  (1.5% por trade — half-Kelly conservador)
  - max_posiciones: 3      (más capital trabajando simultáneo)
  - circuit_breaker_pct: 0.15  (umbral rolling 4 semanas)

Período: 2023-05-08 → 2026-05-08  (datos reales M15 OANDA)
Estrategias: RSI_Bollinger + RSI_Divergence  (Doji pausado)

Uso:
  python fast_backtest_3years.py

Duración estimada: ~30–45 segundos (vectorizado, sin DeepSeek)
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

# ── Configuración ────────────────────────────────────────────────────────────
PARES       = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
DATA_DIR    = Path("data/historical")
PARAMS_FILE = Path("data/calibration/strategy_params.json")
OUT_FILE    = Path("data/backtesting/backtest_3years_optimized.json")
LOG_FILE    = Path("logs/fast_backtest_3years.log")

CAPITAL_INI = 200.0
N_SEMANAS   = 156                                    # 3 años completos
SIM_START   = datetime(2023, 5, 8, tzinfo=timezone.utc)

# Cargar parámetros desde calibration (los nuevos valores ya aplicados)
with open(PARAMS_FILE) as f:
    PARAMS = json.load(f)

ESTRATEGIAS_ACTIVAS = set(PARAMS["estrategias_activas"])
MIN_CONF      = PARAMS["min_confidence"]
SL_ATR_MULT   = PARAMS["sl_atr_mult"]
MIN_SL_PIPS   = PARAMS["min_sl_pips"]
MAX_SL_PIPS   = PARAMS["max_sl_pips"]
RR_RATIO      = PARAMS["rr_ratio"]
COOLDOWN_MINS = PARAMS["cooldown_minutes"]
MAX_POS       = PARAMS["max_posiciones"]        # ahora = 3
RIESGO_PCT    = PARAMS["riesgo_pct"]            # ahora = 0.015
CB_PCT        = PARAMS["circuit_breaker_pct"]   # ahora = 0.15
SESIONES_ACT  = set(PARAMS.get("sesiones_activas", ["london", "overlap", "new_york"]))
PIP_SIZE      = {"USD_JPY": 0.01}

CB_VENTANA_DIAS = 28    # ventana rolling del circuit breaker (4 semanas)
CB_PAUSA_DIAS   = 7     # días de pausa cuando se dispara

# ── Logging ──────────────────────────────────────────────────────────────────
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
log = logging.getLogger("fast_bt_3y")


# ── ADX vectorizado ──────────────────────────────────────────────────────────
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
    av = adx_series.values
    sv = adx_series.diff(3).fillna(0).values
    base = np.where(av < 15, 0.0, np.where(av > 35, 1.0, (av - 15.0) / 20.0))
    adj  = np.clip(sv / 25.0, -0.20, 0.20)
    return np.clip(base + adj, 0.0, 1.0).astype(np.float32)


# ── Carga de datos ───────────────────────────────────────────────────────────
def load_pair(par: str) -> pd.DataFrame:
    path = DATA_DIR / f"{par}_M15.json"
    raw  = json.load(open(path))
    if isinstance(raw, list):
        data = raw
    elif "candles" in raw:
        data = [{"timestamp": c["time"],
                 "open":  float(c["mid"]["o"]),
                 "high":  float(c["mid"]["h"]),
                 "low":   float(c["mid"]["l"]),
                 "close": float(c["mid"]["c"])}
                for c in raw["candles"] if c.get("complete", True)]
    else:
        data = raw

    df = pd.DataFrame(data)
    df.rename(columns={"open": "Open", "high": "High",
                        "low": "Low",  "close": "Close"}, inplace=True)
    if "volume" in df.columns:
        df.rename(columns={"volume": "Volume"}, inplace=True)

    for col in ["Open", "High", "Low", "Close"]:
        df[col] = df[col].astype(float)

    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    df["ATR_14"] = AverageTrueRange(df["High"], df["Low"], df["Close"],
                                    window=14, fillna=True).average_true_range()
    df["RSI_14"] = RSIIndicator(df["Close"], window=14, fillna=True).rsi()
    df["EMA_20"] = EMAIndicator(df["Close"], window=20, fillna=True).ema_indicator()
    df["EMA_50"] = EMAIndicator(df["Close"], window=50, fillna=True).ema_indicator()
    bb = BollingerBands(df["Close"], window=20, window_dev=2, fillna=True)
    df["BBL_20"] = bb.bollinger_lband()
    df["BBU_20"] = bb.bollinger_hband()

    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    cu = abs(c - o); rg = h - l
    si = o.combine(c, min) - l; ss = h - o.combine(c, max)
    df["HAMMER"] = ((cu > 0) & (si >= 2 * cu) & (ss <= cu * 0.5))
    df["DOJI"]   = ((rg > 0) & (cu / rg.replace(0, 1) < 0.10))
    df["EBULL"]  = ((c > o) & (c.shift(1) < o.shift(1)) &
                    (c > o.shift(1)) & (o < c.shift(1)) & (cu > cu.shift(1) * 1.05))
    df["EBEAR"]  = ((c < o) & (c.shift(1) > o.shift(1)) &
                    (c < o.shift(1)) & (o > c.shift(1)) & (cu > cu.shift(1) * 1.05))

    adx = _adx_vectorized(df, 14)
    df["REGIME"] = _make_regime_array(adx)
    df["ADX"]    = adx.values
    return df


# ── Position Tracker con Circuit Breaker Rolling ─────────────────────────────
class Tracker:
    def __init__(self, capital: float):
        self.capital     = capital
        self.capital_ini = capital
        self.peak        = capital
        self.open        = {}
        self.closed      = []
        self._nid        = 1
        # Circuit breaker rolling
        self._cap_history: deque = deque()   # (datetime, capital)
        self._cap_history.append((SIM_START, capital))
        self._cb_activo  = False
        self._cb_hasta   = None
        self._cb_fires   = 0    # veces que se disparó
        self._cb_log     = []

    # ── Circuit breaker ──────────────────────────────────────────────────────
    def cb_check(self, ts: datetime) -> bool:
        """Retorna True si se puede operar, False si el CB está activo."""
        # Auto-recovery
        if self._cb_activo:
            if ts >= self._cb_hasta:
                self._cb_activo = False
                log.info(f"  [CB] ✅ Circuit breaker desactivado — reanudando operaciones")
            else:
                return False

        # Calcular DD rolling desde el pico de las últimas 4 semanas
        cutoff = ts - timedelta(days=CB_VENTANA_DIAS)
        ventana = [(t, c) for t, c in self._cap_history if t >= cutoff]
        if not ventana:
            return True
        peak_rolling = max(c for _, c in ventana)
        if peak_rolling <= 0:
            return True
        dd_rolling = (peak_rolling - self.capital) / peak_rolling

        if dd_rolling >= CB_PCT:
            self._cb_activo  = True
            self._cb_hasta   = ts + timedelta(days=CB_PAUSA_DIAS)
            self._cb_fires  += 1
            self._cb_log.append({
                "ts": ts.isoformat(), "dd_pct": round(dd_rolling * 100, 2),
                "peak": round(peak_rolling, 2), "capital": round(self.capital, 2),
                "hasta": self._cb_hasta.isoformat(),
            })
            log.warning(
                f"  [CB] 🚨 CIRCUIT BREAKER disparado #{self._cb_fires} | "
                f"DD rolling={dd_rolling*100:.1f}% | "
                f"Pico=${peak_rolling:.2f} | Capital=${self.capital:.2f} | "
                f"Pausa hasta {self._cb_hasta.date()}"
            )
            return False
        return True

    def registrar_capital(self, ts: datetime):
        self._cap_history.append((ts, self.capital))
        # Purgar entradas > 35 días
        cutoff = ts - timedelta(days=35)
        while self._cap_history and self._cap_history[0][0] < cutoff:
            self._cap_history.popleft()

    # ── Trades ───────────────────────────────────────────────────────────────
    def open_trade(self, par, dir_, entry, sl, tp, estrat, ts):
        sl_dist  = abs(entry - sl)
        risk_usd = self.capital * RIESGO_PCT
        units    = max(1, int(risk_usd / sl_dist)) if sl_dist > 1e-9 else 1
        tid = self._nid; self._nid += 1
        self.open[tid] = dict(par=par, dir=dir_, entry=entry,
                              sl=sl, tp=tp, units=units,
                              estrat=estrat, opened_at=ts)
        return tid

    def check_fills(self, par: str, high: float, low: float, ts: datetime):
        out = []
        for tid, t in list(self.open.items()):
            if t["par"] != par:
                continue
            if t["dir"] == "long":
                if   low  <= t["sl"]: pnl, res = (t["sl"] - t["entry"]) * t["units"], "SL"
                elif high >= t["tp"]: pnl, res = (t["tp"] - t["entry"]) * t["units"], "TP"
                else: continue
            else:
                if   high >= t["sl"]: pnl, res = (t["entry"] - t["sl"]) * t["units"], "SL"
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


# ── MAIN ─────────────────────────────────────────────────────────────────────
def run():
    t_total = time.time()
    log.info("=" * 65)
    log.info("FAST BACKTEST 3 AÑOS — Trading Bot v11 — Parámetros Optimizados")
    log.info("=" * 65)
    log.info(f"  Capital inicial   : ${CAPITAL_INI:.2f}")
    log.info(f"  Período           : {SIM_START.date()} → {(SIM_START + timedelta(weeks=N_SEMANAS)).date()}")
    log.info(f"  Semanas           : {N_SEMANAS} ({N_SEMANAS//52} años)")
    log.info(f"  riesgo_pct        : {RIESGO_PCT:.1%}  (antes: 1.0%)")
    log.info(f"  max_posiciones    : {MAX_POS}         (antes: 2)")
    log.info(f"  circuit_breaker   : {CB_PCT:.0%} rolling  (antes: 10%)")
    log.info(f"  Estrategias       : {', '.join(sorted(ESTRATEGIAS_ACTIVAS))}")
    log.info(f"  min_confidence    : {MIN_CONF} | RR={RR_RATIO} | SL_ATR={SL_ATR_MULT}")
    log.info("=" * 65)

    # ── Carga ──────────────────────────────────────────────────────────────
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
    log.info(f"  [carga+indicadores] {time.time()-t_load:.2f}s")

    # ── Arrays NumPy por par ───────────────────────────────────────────────
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
            "regime": df["REGIME"].values,
            "adx":    df["ADX"].values,
        }

    rd      = RegimeDetector()
    tracker = Tracker(CAPITAL_INI)
    metricas: list[dict] = []
    cooldown: dict[str, int] = {}   # par → ts_ns del último trade

    # Proyección con aportes de $200/mes (día 15 de cada mes)
    aporte_mensual   = 200.0
    aporte_siguiente = SIM_START.replace(day=15) if SIM_START.day < 15 else \
                       (SIM_START + timedelta(days=32)).replace(day=15)
    aportes_realizados = []

    for semana in range(1, N_SEMANAS + 1):
        w_start = SIM_START + timedelta(weeks=semana - 1)
        w_end   = w_start   + timedelta(weeks=1)
        n_antes = len(tracker.closed)

        w_start_ns = pd.Timestamp(w_start).value
        w_end_ns   = pd.Timestamp(w_end).value

        # Aportes mensuales durante la semana
        while aporte_siguiente < w_end:
            if aporte_siguiente >= w_start:
                tracker.capital += aporte_mensual
                tracker.peak     = max(tracker.peak, tracker.capital)
                aportes_realizados.append({
                    "fecha": aporte_siguiente.date().isoformat(),
                    "capital_tras_aporte": round(tracker.capital, 2),
                })
                log.info(f"  💰 Aporte ${aporte_mensual:.0f} → Capital=${tracker.capital:.2f}")
            # Siguiente aporte: día 15 del mes siguiente
            mes = aporte_siguiente.month + 1
            ano = aporte_siguiente.year + (1 if mes > 12 else 0)
            mes = mes if mes <= 12 else 1
            aporte_siguiente = aporte_siguiente.replace(year=ano, month=mes, day=15)

        log.info(f"\n📅 SEMANA {semana:3d}/{N_SEMANAS} | {w_start.date()} → {w_end.date()} "
                 f"| Capital=${tracker.capital:.2f}")

        # Construir lista cronológica de candles
        ticks = []
        for par, a in arrs.items():
            ts_arr = a["ts"].astype("int64")
            idxs   = np.where((ts_arr >= w_start_ns) & (ts_arr < w_end_ns))[0]
            for gi in idxs:
                ticks.append((ts_arr[gi], par, int(gi)))
        ticks.sort(key=lambda x: x[0])

        regime_hist: dict[str, deque] = {p: deque(maxlen=20) for p in dfs}

        for (ts_ns, par, gi) in ticks:
            a    = arrs[par]
            ts_dt = a["ts"][gi].astype("datetime64[ms]").astype(datetime).replace(tzinfo=timezone.utc)

            # Check SL/TP
            cerrados = tracker.check_fills(par, float(a["high"][gi]), float(a["low"][gi]), ts_dt)
            for t in cerrados:
                emoji = "✅" if t["resultado"] == "TP" else "🔴"
                log.info(f"  {emoji} {par} [{t['resultado']}] "
                         f"est={t['estrat']} PnL=${t['pnl']:+.4f} Capital=${tracker.capital:.2f}")
                tracker.registrar_capital(ts_dt)

            # Filtros de entrada
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

            # Circuit breaker
            if not tracker.cb_check(ts_dt):
                continue

            # Cooldown por par
            last_cd = cooldown.get(par)
            if last_cd is not None and (ts_ns - last_cd) < COOLDOWN_MINS * 60 * 1_000_000_000:
                continue

            # Pre-filtro técnico
            rv = float(a["rsi"][gi]); cl = float(a["close"][gi])
            rsi_s = rv > 68;  rsi_b = rv < 32
            pbbl  = cl < float(a["bbl"][gi]) * 1.002
            pbbu  = cl > float(a["bbu"][gi]) * 0.998
            ham   = bool(a["hammer"][gi]); doj = bool(a["doji"][gi])
            ebull = bool(a["ebull"][gi]); ebear = bool(a["ebear"][gi])

            sl_bool = ((ham and a["ema20"][gi] < a["ema50"][gi]) or
                       (doj and rsi_b) or (rsi_b and pbbl) or
                       (ebull and a["ema20"][gi] > a["ema50"][gi]))
            ss_bool = ((doj and rsi_s) or (rsi_s and pbbu) or
                       (ebear and a["ema20"][gi] < a["ema50"][gi]))
            if not sl_bool and not ss_bool:
                continue

            if sl_bool:
                dir_ = "long"
                if ham:   estrat = "Hammer"
                elif doj: estrat = "Doji"
                elif ebull: estrat = "Engulfing"
                else:     estrat = "RSI_Bollinger" if pbbl else "RSI_Divergence"
            else:
                dir_ = "short"
                if doj:    estrat = "Doji"
                elif ebear: estrat = "Engulfing"
                else:      estrat = "RSI_Bollinger"

            if estrat not in ESTRATEGIAS_ACTIVAS:
                continue

            # Régimen ADX
            rs = float(a["regime"][gi])
            regime_hist[par].append(rs)
            trans = rd.detectar_transicion(list(regime_hist[par]))
            peso  = rd.peso_estrategia(estrat, rs, trans)
            conf  = round(0.45 * peso, 3)
            if conf < MIN_CONF:
                continue

            # Abrir trade
            entry  = cl
            atr_v  = float(a["atr"][gi])
            ps     = PIP_SIZE.get(par, 0.0001)
            sl_d   = float(np.clip(atr_v * SL_ATR_MULT, MIN_SL_PIPS * ps, MAX_SL_PIPS * ps))

            if dir_ == "long":
                sl_p = entry - sl_d; tp_p = entry + sl_d * RR_RATIO
            else:
                sl_p = entry + sl_d; tp_p = entry - sl_d * RR_RATIO

            tracker.open_trade(par, dir_, entry, sl_p, tp_p, estrat, ts_dt)
            cooldown[par] = ts_ns

            log.info(f"  [OPEN] {par} {dir_.upper()} est={estrat} "
                     f"conf={conf:.0%} rs={rs:.2f} entry={entry:.5f}")

        # ── Métricas semanales ─────────────────────────────────────────────
        nuevos  = tracker.closed[n_antes:]
        n       = len(nuevos)
        ganadas = sum(1 for t in nuevos if t["resultado"] == "TP")
        wr      = ganadas / n if n > 0 else 0.0
        pnl_tot = sum(t["pnl"] for t in nuevos)
        pos_p   = sum(t["pnl"] for t in nuevos if t["pnl"] > 0)
        neg_p   = abs(sum(t["pnl"] for t in nuevos if t["pnl"] < 0))
        pf      = pos_p / neg_p if neg_p > 0 else (99.0 if pos_p > 0 else 0.0)

        flag = "✅" if (wr >= 0.55 and pf >= 1.4) else ("⚠️" if n == 0 else "❌")
        log.info(f"  {flag}  Trades:{n:3d} | WR:{wr*100:5.1f}% | "
                 f"PF:{pf:5.2f} | PnL:${pnl_tot:+.2f} | CB fires:{tracker._cb_fires}")
        log.info(f"     Capital: ${tracker.capital:.2f}")
        log.info("─" * 65)

        metricas.append({
            "semana":      semana,
            "inicio":      str(w_start.date()),
            "fin":         str(w_end.date()),
            "trades":      n,
            "ganadas":     ganadas,
            "win_rate":    round(wr, 3),
            "profit_factor": round(pf, 3),
            "pnl_total":   round(pnl_tot, 4),
            "capital_fin": round(tracker.capital, 2),
            "cb_activo":   tracker._cb_activo,
        })

    # ── Resumen final ──────────────────────────────────────────────────────
    tot_trades = sum(m["trades"] for m in metricas)
    tot_won    = sum(m["ganadas"] for m in metricas)
    tot_pnl    = tracker.capital - tracker.capital_ini - (aporte_mensual * len(aportes_realizados))
    total_inv  = tracker.capital_ini + aporte_mensual * len(aportes_realizados)
    roi_total  = ((tracker.capital - total_inv) / total_inv) * 100
    wr_global  = tot_won / tot_trades if tot_trades > 0 else 0
    max_dd     = tracker.max_dd_pct()
    sem_pos    = sum(1 for m in metricas if m["pnl_total"] > 0)

    by_estrat: dict = {}
    for t in tracker.closed:
        e = t.get("estrat", "?")
        s = by_estrat.setdefault(e, {"n": 0, "won": 0, "pnl": 0.0})
        s["n"] += 1
        s["won"] += 1 if t["resultado"] == "TP" else 0
        s["pnl"] += t["pnl"]

    log.info("\n" + "═" * 65)
    log.info("RESUMEN FINAL — 3 AÑOS — PARÁMETROS OPTIMIZADOS")
    log.info("═" * 65)
    log.info(f"  Capital inicial   : ${tracker.capital_ini:.2f}")
    log.info(f"  Aportes realizados: {len(aportes_realizados)} × ${aporte_mensual:.0f} = ${aporte_mensual*len(aportes_realizados):.0f}")
    log.info(f"  Total invertido   : ${total_inv:.2f}")
    log.info(f"  Capital final     : ${tracker.capital:.2f}")
    log.info(f"  Ganancia neta     : ${tracker.capital - total_inv:+.2f}")
    log.info(f"  ROI total         : {roi_total:+.1f}%")
    log.info(f"  Total trades      : {tot_trades}")
    log.info(f"  WR global         : {wr_global*100:.1f}%")
    log.info(f"  Max drawdown      : {max_dd*100:.1f}%")
    log.info(f"  Semanas positivas : {sem_pos}/{N_SEMANAS} ({sem_pos/N_SEMANAS*100:.0f}%)")
    log.info(f"  CB disparado      : {tracker._cb_fires} veces")
    log.info(f"  Tiempo total      : {time.time()-t_total:.1f}s")
    log.info("\n  Por estrategia:")
    for e, s in sorted(by_estrat.items()):
        wr_e = s["won"] / s["n"] if s["n"] > 0 else 0
        log.info(f"    {e:22s}  N={s['n']:4d}  WR={wr_e*100:5.1f}%  PnL=${s['pnl']:+.2f}")

    # Comparativa vs parámetros anteriores (1% riesgo, 2 pos)
    log.info("\n" + "─" * 65)
    log.info("  COMPARATIVA: Anterior (1% riesgo / 2 pos) vs Optimizado")
    log.info(f"  Anterior  1 año  : $200 → $441  (+120.6%)")
    log.info(f"  Optimizado 3 años: ${tracker.capital_ini:.0f}+aportes → ${tracker.capital:.0f} ({roi_total:+.1f}% ROI)")
    log.info("═" * 65)

    resultado = {
        "config": {
            "capital_ini":    CAPITAL_INI,
            "n_semanas":      N_SEMANAS,
            "sim_start":      str(SIM_START.date()),
            "sim_end":        str((SIM_START + timedelta(weeks=N_SEMANAS)).date()),
            "estrategias_activas": sorted(ESTRATEGIAS_ACTIVAS),
            "min_confidence": MIN_CONF,
            "rr_ratio":       RR_RATIO,
            "sl_atr_mult":    SL_ATR_MULT,
            "riesgo_pct":     RIESGO_PCT,
            "max_posiciones": MAX_POS,
            "circuit_breaker_pct": CB_PCT,
            "aporte_mensual": aporte_mensual,
        },
        "resumen": {
            "capital_final":          round(tracker.capital, 2),
            "total_invertido":        round(total_inv, 2),
            "ganancia_neta":          round(tracker.capital - total_inv, 2),
            "roi_total_pct":          round(roi_total, 2),
            "pnl_sin_aportes":        round(tot_pnl, 2),
            "total_trades":           tot_trades,
            "win_rate_global":        round(wr_global, 3),
            "max_drawdown_pct":       round(max_dd * 100, 2),
            "semanas_positivas":      sem_pos,
            "semanas_totales":        N_SEMANAS,
            "circuit_breaker_fires":  tracker._cb_fires,
            "aportes_realizados":     len(aportes_realizados),
        },
        "por_estrategia": {
            e: {
                "trades":   s["n"],
                "win_rate": round(s["won"] / s["n"], 3) if s["n"] > 0 else 0,
                "pnl":      round(s["pnl"], 2),
            }
            for e, s in by_estrat.items()
        },
        "circuit_breaker_log": tracker._cb_log,
        "aportes_log":         aportes_realizados,
        "semanas":             metricas,
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"\n  Resultados guardados en: {OUT_FILE}")
    return resultado


if __name__ == "__main__":
    run()
