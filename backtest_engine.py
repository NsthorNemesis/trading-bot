"""
backtest_engine.py — Motor compartido de backtesting  v1.0
===========================================================
Consolida el código duplicado de todos los scripts de backtest.

Uso típico:
    from backtest_engine import BacktestConfig, cargar_datos, run_backtest

    cfg  = BacktestConfig.desde_json()
    dfs  = cargar_datos(cfg.pares, cfg.data_dir)
    r    = run_backtest(cfg, estrategias={"RSI_Bollinger"}, dfs_cache=dfs)

Expone:
  - BacktestConfig   — dataclass con todos los parámetros
  - Tracker          — gestión de capital y trades
  - cargar_datos()   — carga pares M15 a dicts de numpy
  - run_backtest()   — loop principal, devuelve dict de resultados
  - imprimir_resumen() — tabla de resultados estándar
"""
from __future__ import annotations

import json
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

sys.path.insert(0, str(Path(__file__).parent))
from utils.regime_detector import RegimeDetector

# ── Constantes por defecto ────────────────────────────────────────────────────

_DEFAULT_PARES    = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
_DEFAULT_DATA_DIR = Path("data/historical")
_PARAMS_FILE      = Path("data/calibration/strategy_params.json")
_PIP_SIZE         = {"USD_JPY": 0.01}   # resto → 0.0001
_SESIONES_HORAS   = {
    "london":   (7,  13),
    "overlap":  (13, 17),
    "new_york": (17, 22),
}


# ── Configuración ─────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """Parámetros completos para un backtest. Cargados desde strategy_params.json."""

    capital_ini:   float      = 200.0
    n_semanas:     int        = 52
    sim_start:     datetime   = field(default_factory=lambda: datetime(2025, 5, 9, tzinfo=timezone.utc))
    pares:         List[str]  = field(default_factory=lambda: list(_DEFAULT_PARES))
    data_dir:      Path       = field(default_factory=lambda: _DEFAULT_DATA_DIR)

    # Parámetros de riesgo (leídos del JSON)
    min_conf:      float = 0.40
    sl_atr_mult:   float = 1.5
    min_sl_pips:   float = 10.0
    max_sl_pips:   float = 40.0
    rr_ratio:      float = 2.0
    cooldown_mins: int   = 15
    max_pos:       int   = 3
    riesgo_pct:    float = 0.015
    sesiones_act:  Set[str] = field(default_factory=lambda: {"london", "overlap", "new_york"})
    pip_size:      Dict[str, float] = field(default_factory=lambda: dict(_PIP_SIZE))

    @classmethod
    def desde_json(
        cls,
        params_file: Path = _PARAMS_FILE,
        data_dir: Path = _DEFAULT_DATA_DIR,
        capital_ini: float = 200.0,
        n_semanas: int = 52,
        sim_start: Optional[datetime] = None,
    ) -> "BacktestConfig":
        """Crea una config cargando parámetros de riesgo desde strategy_params.json."""
        with open(params_file, encoding="utf-8") as f:
            p = json.load(f)
        return cls(
            capital_ini   = capital_ini,
            n_semanas     = n_semanas,
            sim_start     = sim_start or datetime(2025, 5, 9, tzinfo=timezone.utc),
            pares         = p.get("pares_activos", list(_DEFAULT_PARES)),
            data_dir      = data_dir,
            min_conf      = p["min_confidence"],
            sl_atr_mult   = p["sl_atr_mult"],
            min_sl_pips   = p.get("min_sl_pips", 10.0),
            max_sl_pips   = p.get("max_sl_pips", 40.0),
            rr_ratio      = p["rr_ratio"],
            cooldown_mins = p.get("cooldown_minutes", 15),
            max_pos       = p.get("max_posiciones", 3),
            riesgo_pct    = p["riesgo_pct"],
            sesiones_act  = set(p.get("sesiones_activas", ["london", "overlap", "new_york"])),
        )


# ── Tracker ───────────────────────────────────────────────────────────────────

class Tracker:
    """Gestiona capital, posiciones abiertas y trades cerrados."""

    def __init__(self, cap: float, riesgo_pct: float):
        self.capital     = cap
        self.capital_ini = cap
        self.peak        = cap
        self.riesgo_pct  = riesgo_pct
        self.open: Dict[int, dict]  = {}
        self.closed: List[dict]     = []
        self._nid = 1

    def open_trade(
        self, par: str, dir_: str, entry: float,
        sl: float, tp: float, estrat: str, ts: datetime
    ) -> None:
        sl_d  = abs(entry - sl)
        units = max(1, int(self.capital * self.riesgo_pct / sl_d)) if sl_d > 1e-9 else 1
        tid   = self._nid; self._nid += 1
        self.open[tid] = dict(
            par=par, dir=dir_, entry=entry, sl=sl, tp=tp,
            units=units, estrat=estrat, opened_at=ts,
        )

    def check_fills(self, par: str, high: float, low: float, ts: datetime) -> None:
        for tid, t in list(self.open.items()):
            if t["par"] != par:
                continue
            if t["dir"] == "long":
                hs = low  <= t["sl"]; ht = high >= t["tp"]
                if   hs and ht: pnl, res = (t["tp"] - t["entry"]) * t["units"], "TP"
                elif hs:        pnl, res = (t["sl"] - t["entry"]) * t["units"], "SL"
                elif ht:        pnl, res = (t["tp"] - t["entry"]) * t["units"], "TP"
                else: continue
            else:
                hs = high >= t["sl"]; ht = low <= t["tp"]
                if   hs and ht: pnl, res = (t["entry"] - t["tp"]) * t["units"], "TP"
                elif hs:        pnl, res = (t["entry"] - t["sl"]) * t["units"], "SL"
                elif ht:        pnl, res = (t["entry"] - t["tp"]) * t["units"], "TP"
                else: continue
            self.capital += pnl
            self.peak     = max(self.peak, self.capital)
            self.closed.append({**t, "pnl": round(pnl, 5), "resultado": res, "closed_at": ts})
            del self.open[tid]

    def max_dd(self) -> float:
        cap = self.capital_ini; pk = cap; mdd = 0.0
        for t in self.closed:
            cap += t["pnl"]; pk = max(pk, cap)
            mdd  = max(mdd, (pk - cap) / pk if pk > 0 else 0.0)
        return mdd


# ── Helpers de indicadores ────────────────────────────────────────────────────

def _adx_vec(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr  = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    up  = h - h.shift(1); dn = l.shift(1) - l
    pdm = np.where((up > dn) & (up > 0), up.values, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn.values, 0.0)
    a   = 1.0 / period
    atr_s = pd.Series(tr.values).ewm(alpha=a, adjust=False).mean()
    ps    = pd.Series(pdm, index=df.index).ewm(alpha=a, adjust=False).mean()
    ms    = pd.Series(mdm, index=df.index).ewm(alpha=a, adjust=False).mean()
    pdi   = 100 * ps / atr_s.replace(0, np.nan).values
    mdi   = 100 * ms / atr_s.replace(0, np.nan).values
    dx    = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx   = dx.fillna(0).ewm(alpha=a, adjust=False).mean()
    adx.index = df.index
    return adx


def _regime_array(adx: pd.Series) -> np.ndarray:
    av   = adx.values
    sv   = adx.diff(3).fillna(0).values
    base = np.where(av < 15, 0.0, np.where(av > 35, 1.0, (av - 15.0) / 20.0))
    return np.clip(base + np.clip(sv / 25.0, -0.20, 0.20), 0.0, 1.0).astype(np.float32)


def _raw_to_df(data: list) -> pd.DataFrame:
    df = pd.DataFrame(data)
    df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "volume": "Volume"}, inplace=True)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ATR_14"]  = AverageTrueRange(df["High"], df["Low"], df["Close"], window=14, fillna=True).average_true_range()
    df["RSI_14"]  = RSIIndicator(df["Close"], window=14, fillna=True).rsi()
    df["EMA_20"]  = EMAIndicator(df["Close"], window=20, fillna=True).ema_indicator()
    df["EMA_50"]  = EMAIndicator(df["Close"], window=50, fillna=True).ema_indicator()
    bb            = BollingerBands(df["Close"], window=20, window_dev=2, fillna=True)
    df["BBL_20"]  = bb.bollinger_lband(); df["BBU_20"] = bb.bollinger_hband()
    macd          = MACD(df["Close"], fillna=True); df["MACD_DIF"] = macd.macd_diff()
    o, h, l, c    = df["Open"], df["High"], df["Low"], df["Close"]
    cu = abs(c - o); rg = h - l
    si = o.combine(c, min) - l; ss = h - o.combine(c, max)
    df["HAMMER"]  = ((cu > 0) & (si >= 2 * cu) & (ss <= cu * 0.5)).astype(bool)
    df["DOJI"]    = ((rg > 0) & (cu / rg.replace(0, 1) < 0.10)).astype(bool)
    df["EBULL"]   = (
        (c > o) & (c.shift(1) < o.shift(1)) & (c > o.shift(1))
        & (o < c.shift(1)) & (cu > cu.shift(1) * 1.05)
    ).astype(bool)
    df["REGIME"]  = _regime_array(_adx_vec(df))
    return df


def load_m15(par: str, data_dir: Path = _DEFAULT_DATA_DIR) -> pd.DataFrame:
    """Carga M15, calcula indicadores y agrega trend_h4."""
    raw = json.loads((data_dir / f"{par}_M15.json").read_text())
    df  = _add_indicators(_raw_to_df(raw))
    h4  = (
        df.set_index("ts")[["Open", "High", "Low", "Close", "Volume"]]
        .resample("4h")
        .agg({"Open": "first", "High": "max", "Low": "min",
              "Close": "last", "Volume": "sum"})
        .dropna()
    )
    h4["E20"]   = EMAIndicator(h4["Close"], window=20, fillna=True).ema_indicator()
    h4["E50"]   = EMAIndicator(h4["Close"], window=50, fillna=True).ema_indicator()
    h4["trend"] = np.where(
        (h4["E20"] - h4["E50"]).abs() / h4["Close"] < 0.0003, "rango",
        np.where(h4["E20"] > h4["E50"], "up", "down"),
    )
    t = h4["trend"].reindex(df["ts"].dt.floor("4h")).ffill()
    t.index = df.index
    df["trend_h4"] = t
    return df


# ── Carga de datos ────────────────────────────────────────────────────────────

def cargar_datos(
    pares: List[str],
    data_dir: Path = _DEFAULT_DATA_DIR,
    verbose: bool = True,
) -> Dict[str, dict]:
    """
    Carga todos los pares M15 y devuelve dicts de numpy arrays indexados por par.

    Estructura de cada dict:
      ts, open, high, low, close, atr, rsi, ema20, ema50,
      bbl, bbu, macd_dif, hammer, doji, ebull, regime, trend_h4
    """
    dfs: Dict[str, dict] = {}
    for par in pares:
        try:
            df = load_m15(par, data_dir)
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
            if verbose:
                print(f"  {par}: {len(df):,} velas")
        except Exception as e:
            if verbose:
                print(f"  {par}: ERROR — {e}")
    return dfs


# ── Prefiltro técnico completo ────────────────────────────────────────────────

def prefiltro_completo(a: dict, gi: int) -> List[tuple]:
    """
    Evalúa TODAS las estrategias posibles en el índice gi.
    Devuelve lista de (conf, dir_, estrategia).
    El caller filtra por el set de estrategias activas.
    """
    if gi < 60:
        return []
    cands: List[tuple] = []
    rv   = float(a["rsi"][gi]);    cl  = float(a["close"][gi])
    e20  = float(a["ema20"][gi]);  e50 = float(a["ema50"][gi])
    bbl  = float(a["bbl"][gi]);    bbu = float(a["bbu"][gi])
    mdif = float(a["macd_dif"][gi])
    tend = "up" if e20 > e50 else "down"

    # RSI_Bollinger
    if rv < 32 and cl < bbl * 1.002:
        cands.append((0.45, "long",  "RSI_Bollinger"))
    if rv > 68 and cl > bbu * 0.998:
        cands.append((0.45, "short", "RSI_Bollinger"))

    # Hammer / Doji / Engulfing
    if bool(a["hammer"][gi]) and tend == "down":
        cands.append((0.45, "long",  "Hammer"))
    if bool(a["doji"][gi])   and rv < 32:
        cands.append((0.45, "long",  "Doji"))
    if bool(a["doji"][gi])   and rv > 68:
        cands.append((0.45, "short", "Doji"))
    if bool(a["ebull"][gi])  and tend == "up":
        cands.append((0.45, "long",  "Engulfing"))

    # RSI_Divergence
    if gi >= 12:
        c_r = a["close"][gi - 5:gi]; r_r = a["rsi"][gi - 5:gi]
        c_o = a["close"][gi - 12:gi - 5]; r_o = a["rsi"][gi - 12:gi - 5]
        if len(c_r) >= 5 and len(c_o) >= 5:
            ir = int(np.argmin(c_r)); io = int(np.argmin(c_o))
            if c_r[ir] < c_o[io] * 0.9998 and r_r[ir] > r_o[io] + 3 and r_r[ir] < 48:
                cands.append((0.52, "long",  "RSI_Divergence"))
            ir = int(np.argmax(c_r)); io = int(np.argmax(c_o))
            if c_r[ir] > c_o[io] * 1.0002 and r_r[ir] < r_o[io] - 3 and r_r[ir] > 52:
                cands.append((0.52, "short", "RSI_Divergence"))

    # EMA_Crossover
    if gi >= 5:
        e20s = a["ema20"][gi - 4:gi + 1]; e50s = a["ema50"][gi - 4:gi + 1]
        cu = any(e20s[i-1] <= e50s[i-1] and e20s[i] > e50s[i] for i in range(-3, 0))
        cd = any(e20s[i-1] >= e50s[i-1] and e20s[i] < e50s[i] for i in range(-3, 0))
        if cu and mdif > 0: cands.append((0.50, "long",  "EMA_Crossover"))
        if cd and mdif < 0: cands.append((0.50, "short", "EMA_Crossover"))

    return cands


# ── Detección de sesión ───────────────────────────────────────────────────────

def _sesion(hora: int) -> str:
    if   7  <= hora < 13: return "london"
    elif 13 <= hora < 17: return "overlap"
    elif 17 <= hora < 22: return "new_york"
    return "asia"


# ── Motor principal de backtest ───────────────────────────────────────────────

def run_backtest(
    config:     BacktestConfig,
    estrategias: Set[str],
    ds_func:    Optional[Callable] = None,
    dfs_cache:  Optional[Dict[str, dict]] = None,
    label:      str  = "",
    seed:       int  = 42,
    verbose:    bool = False,
    on_semana:  Optional[Callable] = None,
) -> dict:
    """
    Corre un backtest completo.

    Parámetros
    ----------
    config       : BacktestConfig con parámetros de riesgo y simulación
    estrategias  : set de nombres de estrategia a usar, p.ej. {"RSI_Bollinger"}
    ds_func      : función de aprobación DeepSeek(par, dir_, estrat, rsi, ema20,
                   ema50, macd_dif, bbl, bbu, precio, trend_h4, regime, sesion) -> bool
                   Si None, no se aplica filtro DS
    dfs_cache    : dicts de numpy (de cargar_datos). Si None, se cargan en el momento
    label        : nombre de esta variante para el resumen
    seed         : semilla aleatoria (para reproducibilidad con DS simulado)
    verbose      : imprime resumen semanal por pantalla
    on_semana    : callback(semana, metricas_semana, tracker) — opcional, para output custom

    Retorna
    -------
    dict con claves: label, capital, pnl, roi, wr, dd, trades, sem_pos,
                     apr_rate, by_estrat, ds_por_estrat, metricas
    """
    np.random.seed(seed)

    if dfs_cache is None:
        dfs_cache = cargar_datos(config.pares, config.data_dir, verbose=verbose)

    rd          = RegimeDetector()
    tracker     = Tracker(config.capital_ini, config.riesgo_pct)
    cooldown:   Dict[str, int]      = {}
    regime_hist = {p: deque(maxlen=80) for p in dfs_cache}
    cooldown_ns = config.cooldown_mins * 60 * 1_000_000_000

    metricas:     List[dict] = []
    ds_total      = 0;  ds_aprobadas = 0
    ds_por_estrat: Dict[str, dict] = {}
    by_e:          Dict[str, dict] = {}

    for semana in range(1, config.n_semanas + 1):
        w_start = config.sim_start + timedelta(weeks=semana - 1)
        w_end   = w_start + timedelta(weeks=1)
        n_antes = len(tracker.closed)
        w_sn    = pd.Timestamp(w_start).value
        w_en    = pd.Timestamp(w_end).value

        # Reunir ticks del período ordenados cronológicamente
        ticks: List[tuple] = []
        for par, a in dfs_cache.items():
            ts_arr = a["ts"].astype("int64")
            idxs   = np.where((ts_arr >= w_sn) & (ts_arr < w_en))[0]
            for gi in idxs:
                ticks.append((int(ts_arr[gi]), par, int(gi)))
        ticks.sort(key=lambda x: x[0])

        for (ts_ns, par, gi) in ticks:
            a    = dfs_cache[par]
            ts_dt = pd.Timestamp(ts_ns, unit="ns", tz="UTC").to_pydatetime()

            # Verificar fills antes de evaluar nueva señal
            tracker.check_fills(par, float(a["high"][gi]), float(a["low"][gi]), ts_dt)

            # Filtros previos
            ses = _sesion(ts_dt.hour)
            if ses not in config.sesiones_act:          continue
            if len(tracker.open) >= config.max_pos:     continue
            if (ts_ns - cooldown.get(par, 0)) < cooldown_ns: continue

            # Régimen
            rs = float(a["regime"][gi])
            regime_hist[par].append(rs)
            trans = rd.detectar_transicion(list(regime_hist[par]))

            # Prefiltro + filtro por estrategias activas
            cands = [(c, d, e) for c, d, e in prefiltro_completo(a, gi)
                     if e in estrategias]
            if not cands: continue

            # Filtro H4 (solo EMA_Crossover)
            t_h4 = str(a["trend_h4"][gi])
            def _h4_ok(dir_: str, estrat: str) -> bool:
                if estrat != "EMA_Crossover": return True
                if t_h4 == "rango":           return True
                return (t_h4 == "up" and dir_ == "long") or (t_h4 == "down" and dir_ == "short")
            cands = [(c, d, e) for c, d, e in cands if _h4_ok(d, e)]
            if not cands: continue

            # Peso régimen → elegir mejor candidato
            mejor = None
            for conf_base, dir_, estrat in sorted(cands, key=lambda x: -x[0]):
                peso = rd.peso_estrategia(estrat, rs, trans)
                conf = round(conf_base * peso, 3)
                if conf >= config.min_conf:
                    mejor = (conf, dir_, estrat); break
            if not mejor: continue
            conf, dir_, estrat = mejor

            # Filtro DeepSeek (opcional)
            if ds_func is not None:
                ds_total += 1
                es = ds_por_estrat.setdefault(estrat, {"total": 0, "aprobadas": 0})
                es["total"] += 1
                aprobado = ds_func(
                    par, dir_, estrat,
                    float(a["rsi"][gi]),     float(a["ema20"][gi]),
                    float(a["ema50"][gi]),   float(a["macd_dif"][gi]),
                    float(a["bbl"][gi]),     float(a["bbu"][gi]),
                    float(a["close"][gi]),   t_h4, rs, ses,
                )
                if not aprobado: continue
                ds_aprobadas += 1; es["aprobadas"] += 1

            # Sizing y apertura
            entry = float(a["close"][gi]); atr_v = float(a["atr"][gi])
            ps    = config.pip_size.get(par, 0.0001)
            sl_d  = float(np.clip(atr_v * config.sl_atr_mult,
                                  config.min_sl_pips * ps, config.max_sl_pips * ps))
            sl_p  = entry - sl_d if dir_ == "long" else entry + sl_d
            tp_p  = entry + sl_d * config.rr_ratio if dir_ == "long" else entry - sl_d * config.rr_ratio
            tracker.open_trade(par, dir_, entry, sl_p, tp_p, estrat, ts_dt)
            cooldown[par] = ts_ns

        # Métricas semanales
        nuevos  = tracker.closed[n_antes:]
        n       = len(nuevos)
        ganadas = sum(1 for t in nuevos if t["resultado"] == "TP")
        wr      = ganadas / n if n > 0 else 0.0
        pnl_sem = sum(t["pnl"] for t in nuevos)
        pos_p   = sum(t["pnl"] for t in nuevos if t["pnl"] > 0)
        neg_p   = abs(sum(t["pnl"] for t in nuevos if t["pnl"] < 0))
        pf      = pos_p / neg_p if neg_p > 0 else (99.0 if pos_p > 0 else 0.0)

        m = {
            "semana":     semana,
            "inicio":     str(w_start.date()),
            "trades":     n,
            "ganadas":    ganadas,
            "win_rate":   round(wr, 3),
            "profit_factor": round(pf, 3),
            "pnl_total":  round(pnl_sem, 4),
            "capital_fin": round(tracker.capital, 2),
        }
        metricas.append(m)

        # Actualizar by_estrat
        for t in nuevos:
            e = t.get("estrat", "?")
            s = by_e.setdefault(e, {"n": 0, "won": 0, "pnl": 0.0})
            s["n"]   += 1
            s["won"] += 1 if t["resultado"] == "TP" else 0
            s["pnl"] += t["pnl"]

        if verbose:
            flag = "✅" if wr >= 0.50 else ("⚠️" if n == 0 else "❌")
            print(f"  Sem {semana:2d} ({w_start.date()})  {flag}  "
                  f"T:{n:3d}  WR:{wr*100:5.1f}%  PF:{pf:4.2f}  "
                  f"PnL:${pnl_sem:+.2f}  Cap:${tracker.capital:.2f}")

        if on_semana:
            on_semana(semana, m, tracker)

    # Resumen global
    tot_t   = sum(m["trades"]  for m in metricas)
    tot_w   = sum(m["ganadas"] for m in metricas)
    tot_pnl = tracker.capital - config.capital_ini
    wr_g    = tot_w / tot_t if tot_t > 0 else 0.0
    max_dd  = tracker.max_dd()
    sem_pos = sum(1 for m in metricas if m["pnl_total"] > 0)
    apr_rate = (ds_aprobadas / ds_total * 100) if ds_total > 0 else None

    return {
        "label":        label,
        "capital":      round(tracker.capital, 2),
        "pnl":          round(tot_pnl, 2),
        "roi":          round(tot_pnl / config.capital_ini * 100, 1),
        "wr":           round(wr_g * 100, 1),
        "dd":           round(max_dd * 100, 1),
        "trades":       tot_t,
        "sem_pos":      sem_pos,
        "apr_rate":     apr_rate,
        "by_estrat":    by_e,
        "ds_por_estrat": ds_por_estrat,
        "metricas":     metricas,
    }


# ── Resumen estándar ──────────────────────────────────────────────────────────

def imprimir_resumen(
    resultados: List[dict],
    config:     BacktestConfig,
    historico:  Optional[List[dict]] = None,
    titulo:     str = "RESULTADOS BACKTEST",
) -> None:
    """
    Imprime tabla comparativa de resultados.

    historico : lista opcional de runs previos con las mismas claves
                (label, capital, roi, wr, dd, trades, sem_pos, apr_rate)
    """
    W = [28, 8, 7, 7, 6, 8, 8, 7]
    print()
    print("═" * sum(W))
    print(f"  {titulo}")
    print(f"  Capital inicial: ${config.capital_ini:.2f} | Semanas: {config.n_semanas} | RR: {config.rr_ratio}")
    print("═" * sum(W))

    header = (f"{'Versión':<{W[0]}} {'Capital':>{W[1]}} {'ROI':>{W[2]}} "
              f"{'WR':>{W[3]}} {'DD':>{W[4]}} {'Trades':>{W[5]}} "
              f"{'Sem+':>{W[6]}} {'DS%':>{W[7]}}")
    print(f"  {header}")
    print(f"  {'─' * sum(W)}")

    def _fila(d: dict) -> None:
        cap = f"${d['capital']:.2f}"
        roi = f"{d['roi']:+.1f}%"
        wr  = f"{d['wr']:.1f}%"
        dd  = f"{d['dd']:.1f}%"
        trd = str(d["trades"])
        smp = d.get("sem_pos", "—")
        if isinstance(smp, int): smp = f"{smp}/{config.n_semanas}"
        apr = f"{d['apr_rate']:.0f}%" if d.get("apr_rate") else "—"
        print(f"  {d['label']:<{W[0]}} {cap:>{W[1]}} {roi:>{W[2]}} "
              f"{wr:>{W[3]}} {dd:>{W[4]}} {trd:>{W[5]}} "
              f"{smp:>{W[6]}} {apr:>{W[7]}}")

    if historico:
        for d in historico:
            _fila(d)
        print(f"  {'─' * sum(W)}")

    for r in resultados:
        r2 = dict(r); r2["sem_pos"] = f"{r['sem_pos']}/{config.n_semanas}"
        _fila(r2)

    print(f"\n  {'─' * sum(W)}")
    print("  DESGLOSE POR ESTRATEGIA:")
    for r in resultados:
        if not r["by_estrat"]: continue
        print(f"\n  {r['label']}:")
        for e, s in sorted(r["by_estrat"].items()):
            wr_e  = s["won"] / s["n"] * 100 if s["n"] > 0 else 0.0
            ds_e  = r["ds_por_estrat"].get(e, {})
            apr_e = (ds_e.get("aprobadas", 0) / ds_e["total"] * 100
                     if ds_e.get("total", 0) > 0 else None)
            apr_s = f"  DS:{apr_e:.0f}%" if apr_e else ""
            print(f"    {e:<22}  N={s['n']:4d}  WR={wr_e:5.1f}%  PnL=${s['pnl']:+.2f}{apr_s}")
    print("═" * sum(W))
