"""
engine.py — Motor de backtesting
=================================
Lógica determinista, sin asyncio.
Estrategias: RSI_Bollinger, EMA_Crossover, RSI_Divergence, Hammer, Doji
Filtro DeepSeek: simulado con score calibrado.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands


# ── Configuración de backtest ─────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    capital_ini:   float     = 200.0
    riesgo_pct:    float     = 0.015
    sl_atr_mult:   float     = 2.0
    min_sl_pips:   float     = 10.0
    max_sl_pips:   float     = 50.0
    rr_ratio:      float     = 2.0
    cooldown_mins: int       = 240
    max_pos:       int       = 6
    max_pos_par:   int       = 2
    min_conf:      float     = 0.40
    sesiones:      Set[str]  = field(default_factory=lambda: {"london", "new_york"})
    estrategias:   Set[str]  = field(default_factory=lambda: {"RSI_Bollinger"})
    usar_ds:       bool      = True
    seed:          int       = 42


# ── Tracker de capital y trades ───────────────────────────────────────────────

class Tracker:
    def __init__(self, cap: float, riesgo_pct: float):
        self.capital     = cap
        self.capital_ini = cap
        self.peak        = cap
        self.riesgo_pct  = riesgo_pct
        self.open: Dict[int, dict] = {}
        self.closed: List[dict]    = []
        self._nid = 1
        self.equity_curve: List[dict] = [{"ts": None, "capital": cap}]

    def open_trade(self, par, dir_, entry, sl, tp, estrat, ts):
        sl_d  = abs(entry - sl)
        units = max(1, int(self.capital * self.riesgo_pct / sl_d)) if sl_d > 1e-9 else 1
        tid   = self._nid; self._nid += 1
        self.open[tid] = dict(
            par=par, dir=dir_, entry=entry, sl=sl, tp=tp,
            units=units, estrat=estrat, opened_at=ts,
        )

    def check_fills(self, par, high, low, ts):
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
            self.closed.append({
                **t,
                "pnl":       round(pnl, 5),
                "resultado": res,
                "closed_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "opened_at": t["opened_at"].isoformat() if hasattr(t["opened_at"], "isoformat") else str(t["opened_at"]),
            })
            self.equity_curve.append({"ts": str(ts)[:16], "capital": round(self.capital, 4)})
            del self.open[tid]

    def max_dd(self) -> float:
        cap = self.capital_ini; pk = cap; mdd = 0.0
        for t in self.closed:
            cap += t["pnl"]; pk = max(pk, cap)
            mdd  = max(mdd, (pk - cap) / pk if pk > 0 else 0.0)
        return mdd

    def profit_factor(self) -> float:
        ganancias = sum(t["pnl"] for t in self.closed if t["pnl"] > 0)
        perdidas  = abs(sum(t["pnl"] for t in self.closed if t["pnl"] < 0))
        return round(ganancias / perdidas, 3) if perdidas > 0 else 999.0

    def by_strategy(self) -> Dict[str, dict]:
        result: Dict[str, dict] = {}
        for t in self.closed:
            e = t["estrat"]
            s = result.setdefault(e, {"trades": 0, "wins": 0, "pnl": 0.0})
            s["trades"] += 1
            s["pnl"]    += t["pnl"]
            if t["pnl"] > 0:
                s["wins"] += 1
        for s in result.values():
            s["wr"]  = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 4)
        return result

    def by_pair(self) -> Dict[str, dict]:
        result: Dict[str, dict] = {}
        for t in self.closed:
            p = t["par"]
            s = result.setdefault(p, {"trades": 0, "wins": 0, "pnl": 0.0})
            s["trades"] += 1
            s["pnl"]    += t["pnl"]
            if t["pnl"] > 0:
                s["wins"] += 1
        for s in result.values():
            s["wr"]  = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 4)
        return result


# ── Indicadores ───────────────────────────────────────────────────────────────

def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ATR"]     = AverageTrueRange(df["high"], df["low"], df["close"], window=14, fillna=True).average_true_range()
    df["RSI"]     = RSIIndicator(df["close"], window=14, fillna=True).rsi()
    df["EMA20"]   = EMAIndicator(df["close"], window=20, fillna=True).ema_indicator()
    df["EMA50"]   = EMAIndicator(df["close"], window=50, fillna=True).ema_indicator()
    bb            = BollingerBands(df["close"], window=20, window_dev=2, fillna=True)
    df["BBL"]     = bb.bollinger_lband()
    df["BBU"]     = bb.bollinger_hband()
    macd          = MACD(df["close"], fillna=True)
    df["MACD"]    = macd.macd_diff()

    # Patrones de velas
    o, h, l, c   = df["open"], df["high"], df["low"], df["close"]
    body          = (c - o).abs()
    rng           = h - l
    shadow_low    = o.combine(c, min) - l
    shadow_high   = h - o.combine(c, max)
    df["HAMMER"]  = ((body > 0) & (shadow_low >= 2 * body) & (shadow_high <= body * 0.5))
    df["DOJI"]    = ((rng > 0) & (body / rng.replace(0, 1) < 0.10))

    # Régimen ADX simplificado
    tr  = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    a   = 1.0 / 14
    atr_s = tr.ewm(alpha=a, adjust=False).mean()
    up    = (h - h.shift(1)).clip(lower=0)
    dn    = (l.shift(1) - l).clip(lower=0)
    pdi   = 100 * up.ewm(alpha=a, adjust=False).mean() / atr_s.replace(0, np.nan)
    mdi   = 100 * dn.ewm(alpha=a, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx    = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx   = dx.fillna(0).ewm(alpha=a, adjust=False).mean()
    df["REGIME"] = np.clip((adx.values - 15) / 20.0, 0.0, 1.0)

    # Tendencia H4 desde M15
    h4 = df.set_index("ts")[["open", "high", "low", "close"]].resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    h4["E20"] = EMAIndicator(h4["close"], window=20, fillna=True).ema_indicator()
    h4["E50"] = EMAIndicator(h4["close"], window=50, fillna=True).ema_indicator()
    h4["trend"] = np.where(
        (h4["E20"] - h4["E50"]).abs() / h4["close"] < 0.0003, "rango",
        np.where(h4["E20"] > h4["E50"], "up", "down"),
    )
    trend_map = h4["trend"].reindex(df["ts"].dt.floor("4h")).ffill()
    trend_map.index = df.index
    df["TREND_H4"] = trend_map.fillna("rango")
    return df


def _sesion(hora: int) -> str:
    if   7  <= hora < 13: return "london"
    elif 13 <= hora < 17: return "overlap"
    elif 17 <= hora < 22: return "new_york"
    return "asia"


# ── Señales ───────────────────────────────────────────────────────────────────

def generar_candidatos(row: pd.Series, estrategias: Set[str]) -> List[tuple]:
    """Evalúa todas las condiciones y retorna (conf, dir, estrategia)."""
    cands = []
    rsi   = row["RSI"];  cl = row["close"]
    bbl   = row["BBL"];  bbu = row["BBU"]
    e20   = row["EMA20"]; e50 = row["EMA50"]
    macd  = row["MACD"]
    tend  = "up" if e20 > e50 else "down"

    if "RSI_Bollinger" in estrategias:
        if rsi < 32 and cl < bbl * 1.002:
            cands.append((0.45, "long",  "RSI_Bollinger"))
        if rsi > 68 and cl > bbu * 0.998:
            cands.append((0.45, "short", "RSI_Bollinger"))

    if "Hammer" in estrategias and row.get("HAMMER", False) and tend == "down":
        cands.append((0.45, "long", "Hammer"))

    if "Doji" in estrategias and row.get("DOJI", False):
        if rsi < 32: cands.append((0.45, "long",  "Doji"))
        if rsi > 68: cands.append((0.45, "short", "Doji"))

    if "EMA_Crossover" in estrategias:
        # Nota: cruce detectado en bucle principal por look-back
        pass  # manejado en run_backtest

    if "RSI_Divergence" in estrategias:
        pass  # manejado en run_backtest con ventana deslizante

    return cands


# ── Filtro DeepSeek simulado ──────────────────────────────────────────────────

def deepseek_filter(
    par, dir_, estrat, rsi, ema20, ema50, macd,
    bbl, bbu, precio, trend_h4, regime, sesion,
    seed_noise: float = 0.0,
) -> tuple[bool, float]:
    """
    Retorna (aprobado: bool, score: float).
    Calibrado a ~55% de aprobación con noise aleatorio.
    """
    score = 0.50

    if estrat == "RSI_Bollinger":
        if dir_ == "long":
            if   rsi < 24: score += 0.35
            elif rsi < 27: score += 0.20
            elif rsi < 30: score += 0.08
            else:          score -= 0.18
            dist = (bbl - precio) / bbl if bbl > 0 else 0
            if dist > 0.002:  score += 0.12
            elif dist > 0.001: score += 0.05
            if trend_h4 == "up": score += 0.06
        else:
            if   rsi > 76: score += 0.35
            elif rsi > 73: score += 0.20
            elif rsi > 70: score += 0.08
            else:          score -= 0.18
            dist = (precio - bbu) / bbu if bbu > 0 else 0
            if dist > 0.002:  score += 0.12
            elif dist > 0.001: score += 0.05
            if trend_h4 == "down": score += 0.06

    elif estrat == "EMA_Crossover":
        macd_abs = abs(macd)
        if   macd_abs > 0.0010: score += 0.30
        elif macd_abs > 0.0006: score += 0.15
        elif macd_abs > 0.0003: score += 0.03
        else:                   score -= 0.30
        if dir_ == "long"  and trend_h4 == "up":    score += 0.22
        if dir_ == "short" and trend_h4 == "down":  score += 0.22
        if dir_ == "long"  and trend_h4 == "down":  score -= 0.40
        if dir_ == "short" and trend_h4 == "up":    score -= 0.40
        if trend_h4 == "rango":                      score -= 0.20

    elif estrat == "RSI_Divergence":
        if dir_ == "long"  and rsi < 38: score += 0.25
        if dir_ == "short" and rsi > 62: score += 0.25
        if dir_ == "long"  and rsi < 30: score += 0.15
        if dir_ == "short" and rsi > 70: score += 0.15

    elif estrat in ("Hammer", "Doji"):
        if estrat == "Hammer" and rsi < 35: score += 0.20
        if estrat == "Doji":
            if dir_ == "long"  and rsi < 32: score += 0.25
            if dir_ == "short" and rsi > 68: score += 0.25

    if sesion == "london": score += 0.03
    if regime < 0.35 and estrat == "RSI_Bollinger": score += 0.07
    if regime > 0.65 and estrat == "EMA_Crossover": score += 0.07

    score += seed_noise
    return score > 0.62, round(score, 3)


# ── Motor principal ───────────────────────────────────────────────────────────

def run_backtest(
    dfs: Dict[str, pd.DataFrame],
    cfg: BacktestConfig,
    start: datetime,
    end: datetime,
) -> dict:
    """
    Corre el backtest sobre los DataFrames precargados.

    dfs: {par: DataFrame con columnas ts, open, high, low, close, volume}
    Retorna dict completo con todas las métricas.
    """
    np.random.seed(cfg.seed)
    pip_size = {"USD_JPY": 0.01}

    # Preprocesar indicadores
    processed: Dict[str, pd.DataFrame] = {}
    for par, df in dfs.items():
        if df.empty or len(df) < 60:
            continue
        df2 = df.copy()
        df2["ts"] = pd.to_datetime(df2["ts"], utc=True)
        df2 = df2.sort_values("ts").reset_index(drop=True)
        df2 = _add_indicators(df2)
        # Filtrar al período
        mask = (df2["ts"] >= start) & (df2["ts"] < end)
        processed[par] = df2[mask].reset_index(drop=True)

    if not processed:
        return _empty_result(cfg)

    tracker  = Tracker(cfg.capital_ini, cfg.riesgo_pct)
    cooldown: Dict[str, pd.Timestamp] = {}
    cd_delta = pd.Timedelta(minutes=cfg.cooldown_mins)

    ds_total = 0; ds_aprobadas = 0

    # Reunir y ordenar todos los ticks cronológicamente
    ticks = []
    for par, df in processed.items():
        for i, row in df.iterrows():
            ticks.append((row["ts"], par, i, df))
    ticks.sort(key=lambda x: x[0])

    # Buffers para EMA_Crossover y RSI_Divergence
    ema_hist:  Dict[str, deque] = {p: deque(maxlen=6) for p in processed}
    close_hist: Dict[str, deque] = {p: deque(maxlen=15) for p in processed}
    rsi_hist:  Dict[str, deque] = {p: deque(maxlen=15) for p in processed}

    for ts, par, i, df in ticks:
        row = df.iloc[i]

        # Verificar fills
        tracker.check_fills(par, float(row["high"]), float(row["low"]), ts)

        # Filtros rápidos
        ses = _sesion(ts.hour)
        if ses not in cfg.sesiones:                             continue
        if len(tracker.open) >= cfg.max_pos:                   continue
        if par in cooldown and (ts - cooldown[par]) < cd_delta: continue
        # Límite de posiciones simultáneas por par
        open_en_par = sum(1 for t in tracker.open.values() if t["par"] == par)
        if open_en_par >= cfg.max_pos_par:                      continue

        # Actualizar buffers históricos
        ema_hist[par].append((float(row["EMA20"]), float(row["EMA50"])))
        close_hist[par].append(float(row["close"]))
        rsi_hist[par].append(float(row["RSI"]))

        rsi    = float(row["RSI"])
        regime = float(row["REGIME"])
        th4    = str(row["TREND_H4"])

        # Candidatos básicos
        cands = generar_candidatos(row, cfg.estrategias)

        # EMA_Crossover con ventana
        if "EMA_Crossover" in cfg.estrategias and len(ema_hist[par]) >= 4:
            buf = list(ema_hist[par])
            cruce_up   = any(buf[j-1][0] <= buf[j-1][1] and buf[j][0] > buf[j][1] for j in range(-3, 0))
            cruce_down = any(buf[j-1][0] >= buf[j-1][1] and buf[j][0] < buf[j][1] for j in range(-3, 0))
            macd_v = float(row["MACD"])
            h4_ok_l = th4 in ("up", "rango")
            h4_ok_s = th4 in ("down", "rango")
            if cruce_up   and macd_v > 0 and h4_ok_l: cands.append((0.50, "long",  "EMA_Crossover"))
            if cruce_down and macd_v < 0 and h4_ok_s: cands.append((0.50, "short", "EMA_Crossover"))

        # RSI_Divergence con ventana
        if "RSI_Divergence" in cfg.estrategias and len(close_hist[par]) >= 12:
            c_r = list(close_hist[par])[-6:]
            c_o = list(close_hist[par])[-12:-6]
            r_r = list(rsi_hist[par])[-6:]
            r_o = list(rsi_hist[par])[-12:-6]
            ir  = int(np.argmin(c_r)); io = int(np.argmin(c_o))
            if c_r[ir] < c_o[io] * 0.9998 and r_r[ir] > r_o[io] + 3 and r_r[ir] < 48:
                cands.append((0.52, "long",  "RSI_Divergence"))
            ir = int(np.argmax(c_r)); io = int(np.argmax(c_o))
            if c_r[ir] > c_o[io] * 1.0002 and r_r[ir] < r_o[io] - 3 and r_r[ir] > 52:
                cands.append((0.52, "short", "RSI_Divergence"))

        # Filtrar por estrategias activas
        cands = [(c, d, e) for c, d, e in cands if e in cfg.estrategias]
        if not cands: continue

        # Elegir mejor candidato
        mejor = None
        for conf, dir_, estrat in sorted(cands, key=lambda x: -x[0]):
            if conf >= cfg.min_conf:
                mejor = (conf, dir_, estrat); break
        if not mejor: continue
        conf, dir_, estrat = mejor

        # Filtro DeepSeek
        if cfg.usar_ds:
            ds_total += 1
            noise = float(np.random.uniform(-0.08, 0.08))
            aprobado, _ = deepseek_filter(
                par, dir_, estrat, rsi,
                float(row["EMA20"]), float(row["EMA50"]),
                float(row["MACD"]), float(row["BBL"]), float(row["BBU"]),
                float(row["close"]), th4, regime, ses, noise,
            )
            if not aprobado: continue
            ds_aprobadas += 1

        # Abrir trade
        entry = float(row["close"])
        atr_v = float(row["ATR"])
        ps    = pip_size.get(par, 0.0001)
        sl_d  = float(np.clip(atr_v * cfg.sl_atr_mult,
                              cfg.min_sl_pips * ps, cfg.max_sl_pips * ps))
        tp_d  = sl_d * cfg.rr_ratio
        sl    = entry - sl_d if dir_ == "long" else entry + sl_d
        tp    = entry + tp_d if dir_ == "long" else entry - tp_d

        tracker.open_trade(par, dir_, entry, sl, tp, estrat, ts)
        cooldown[par] = ts

    # Métricas finales
    closed = tracker.closed
    total  = len(closed)
    wins   = sum(1 for t in closed if t["pnl"] > 0)
    pnl    = sum(t["pnl"] for t in closed)
    wr     = round(wins / total * 100, 1) if total else 0
    roi    = round((tracker.capital - cfg.capital_ini) / cfg.capital_ini * 100, 2)
    dd     = round(tracker.max_dd() * 100, 1)
    pf     = tracker.profit_factor()
    calmar = round(abs(roi) / dd, 2) if dd > 0 else 0
    apr    = round(ds_aprobadas / ds_total * 100, 1) if ds_total > 0 else None

    return {
        "capital":      round(tracker.capital, 4),
        "roi":          roi,
        "pnl":          round(pnl, 4),
        "wr":           wr,
        "dd":           dd,
        "trades":       total,
        "wins":         wins,
        "losses":       total - wins,
        "profit_factor": pf,
        "calmar":       calmar,
        "apr_rate":     apr,
        "by_strategy":  tracker.by_strategy(),
        "by_pair":      tracker.by_pair(),
        "trades_log":   closed,
        "equity_curve": tracker.equity_curve,
        "config":       {
            "capital_ini":   cfg.capital_ini,
            "riesgo_pct":    cfg.riesgo_pct,
            "sl_atr_mult":   cfg.sl_atr_mult,
            "rr_ratio":      cfg.rr_ratio,
            "sesiones":      list(cfg.sesiones),
            "estrategias":   list(cfg.estrategias),
            "usar_ds":       cfg.usar_ds,
        },
    }


def _empty_result(cfg: BacktestConfig) -> dict:
    return {
        "capital": cfg.capital_ini, "roi": 0.0, "pnl": 0.0,
        "wr": 0.0, "dd": 0.0, "trades": 0, "wins": 0, "losses": 0,
        "profit_factor": 0.0, "calmar": 0.0, "apr_rate": None,
        "by_strategy": {}, "by_pair": {}, "trades_log": [],
        "equity_curve": [], "config": {},
    }
