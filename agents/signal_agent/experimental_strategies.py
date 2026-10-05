"""
experimental_strategies.py — Versión LIVE (shadow-only) de las estrategias
experimentales del bloque 3 (aprobado por Néstor 2026-10-05).

REGLA DURA: estas 4 clases son SHADOW-ONLY. Van en `estrategias_pausadas`
de strategy_params.json → detectan y registran en logs/shadow_signals.jsonl
vía EstrategiaBase.log_shadow(), NUNCA generan señales operables.
JAMÁS agregarlas a `estrategias_activas`.

Derivada de harness/experimental_strategies.py (la lógica de DETECCIÓN es
idéntica; verificar con diff ante cualquier cambio). Adaptaciones live
(documentadas, no cambian la detección):
  1. `preparar(full)` del harness → `_rangos_desde_df(df)`: los rangos
     diarios se computan perezosamente del df que recibe detectar()
     (el live no tiene dataset completo; el df trae 500 velas ≈ 5.2 días,
     suficiente para el fallback de 3 días de PrevDayBreakout).
     Sin lookahead: solo usa velas <= la actual (todas las del df lo son).
  2. Columna de tiempo defensiva: el df live trae "timestamp" (minúscula,
     ISO "YYYY-MM-DDTHH:MM:SSZ"); el harness usa "Timestamp" (datetime).
     `_ts_utc()` normaliza ambos casos.
  3. LondonBreakout: guarda de sesión london-only (hora UTC 7-13). Replica
     la regla propia del screening del bloque 3 (sesiones_activas=["london"]
     forzada en el harness aunque el filtro global live sea london+overlap).
     Las otras 3 usan el filtro global live (london+overlap), igual que en
     el screening.

Definiciones de sesión (MarketAgent._sesion, idénticas en harness/engine.py):
  london 7-13 UTC, overlap 13-17, new_york 17-22, asia resto.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

from agents.signal_agent.strategies import (  # noqa: E402
    EstrategiaBase,
    PatronDetectado,
)


@dataclass
class PatronExperimental(PatronDetectado):
    """PatronDetectado + SL absoluto definido por el patrón (opcional).

    log_shadow() lo honra como nivel de stop absoluto y recalcula el TP
    desde el entry para mantener el RR (igual que el engine del harness).
    """
    sl_nivel: Optional[float] = None


# ── Estado diario (1 señal por día) para las rupturas ─────────────────────────
_ULTIMO_DIA_SENAL: dict = {}


def reset_estado_experimental() -> None:
    """Limpia el estado (paridad con el harness; en live se usa tras reinicios)."""
    _ULTIMO_DIA_SENAL.clear()


def _marca_dia(nombre: str, par: str, fecha) -> bool:
    """True si ya hubo señal hoy (no emitir otra); False si es la primera."""
    key = (nombre, par)
    if _ULTIMO_DIA_SENAL.get(key) == fecha:
        return True
    _ULTIMO_DIA_SENAL[key] = fecha
    return False


def _ts_utc(df) -> pd.Series:
    """Serie de timestamps UTC del df (acepta 'Timestamp' o 'timestamp')."""
    col = "Timestamp" if "Timestamp" in df.columns else "timestamp"
    return pd.to_datetime(df[col], utc=True)


def _rangos_por_dia(df, col_fecha="fecha"):
    """{date: (lo, hi, n)} desde el df (solo velas pasadas → sin lookahead)."""
    t = _ts_utc(df)
    tmp = pd.DataFrame({"fecha": t.dt.date,
                        "High": df["High"].to_numpy(dtype=float),
                        "Low": df["Low"].to_numpy(dtype=float)})
    g = tmp.groupby("fecha").agg(lo=("Low", "min"), hi=("High", "max"),
                                 n=("Low", "size"))
    return {d: (float(r.lo), float(r.hi), int(r.n)) for d, r in g.iterrows()}


# ── 1. DoubleBottom (doble suelo / doble techo) ───────────────────────────────
class EstrategiaDoubleBottom(EstrategiaBase):
    """
    EXPERIMENTAL shadow-only — reversión.

    Ventana de 50 velas (excluye la actual). Dos mínimos separados >= 10 velas,
    el segundo dentro del 0.15% del primero. Entrada LONG al cierre que rompe
    al alza el neckline (máximo entre ambos mínimos). Espejo SHORT con doble
    techo. SL absoluto: bajo el segundo mínimo - 0.1×ATR (sobre el segundo
    máximo + 0.1×ATR en short). TP: RR 2.0 (lo calcula el resolutor shadow).

    NOTA de diseño: la ventana y la separación se miden en VELAS (no en tiempo),
    por decisión explícita del alcance: el patrón es geométrico, no temporal.
    """
    nombre = "DoubleBottom"
    tipo = "reversion"
    min_confidence_default = 0.75
    criterios_decision = "EXPERIMENTAL-bloque3-shadow"

    VENTANA = 50
    SEP_MIN = 10
    TOL = 0.0015  # 0.15%

    def _buscar(self, w, columna: str, buscar_min: bool):
        """Devuelve (a, b, neckline, va, vb) del par válido con b más reciente.

        Vectorizado con numpy: O(n) por evaluación en vez de O(n²) en Python.
        """
        vals = w[columna].to_numpy(dtype=float)
        n = len(vals)
        for b in range(n - 1, self.SEP_MIN - 1, -1):
            a_vals = vals[:b - self.SEP_MIN + 1]
            if len(a_vals) == 0 or vals[b] == 0:
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                rel = np.abs(a_vals - vals[b]) / np.abs(a_vals)
            ok = np.flatnonzero(rel <= self.TOL)
            if len(ok):
                a = int(ok[np.argmin(rel[ok])])  # el extremo más parecido
                entre = w.iloc[a + 1:b]
                if buscar_min:
                    neck = float(entre["High"].max())
                else:
                    neck = float(entre["Low"].min())
                return a, b, neck, float(vals[a]), float(vals[b])
        return None

    def detectar(self, df, params: dict):
        if len(df) < self.VENTANA + 2:
            return None
        u = df.iloc[-1]
        atr = float(u.get("ATR_14", 0) or 0)
        if atr <= 0:
            return None
        w = df.iloc[-(self.VENTANA + 1):-1]  # 50 velas, sin la actual
        close = float(u.get("Close", 0))

        setup_l = self._buscar(w, "Low", buscar_min=True)    # doble suelo
        setup_s = self._buscar(w, "High", buscar_min=False)  # doble techo

        candidatos = []
        if setup_l:
            a, b, neck, la, lb = setup_l
            if close > neck:
                sl = float(w["Low"].iloc[b]) - 0.1 * atr
                if sl < close:
                    candidatos.append((b, "long", neck, sl,
                                       f"DoubleBottom: doble suelo, cierre {close:.5f} "
                                       f"rompe neckline {neck:.5f}"))
        if setup_s:
            a, b, neck, ha, hb = setup_s
            if close < neck:
                sl = float(w["High"].iloc[b]) + 0.1 * atr
                if sl > close:
                    candidatos.append((b, "short", neck, sl,
                                       f"DoubleBottom: doble techo, cierre {close:.5f} "
                                       f"rompe neckline {neck:.5f}"))
        if not candidatos:
            return None
        # Si ambos, el patrón más reciente manda.
        candidatos.sort(key=lambda c: c[0], reverse=True)
        _, direccion, neck, sl, desc = candidatos[0]
        return PatronExperimental(self.nombre, direccion, desc, sl_nivel=sl)


# ── 2. LondonBreakout (ruptura del rango asiático) ────────────────────────────
class EstrategiaLondonBreakout(EstrategiaBase):
    """
    EXPERIMENTAL shadow-only — trend.

    Rango asiático: máximo/mínimo de las velas entre 00:00 y 07:00 GMT del
    mismo día (reloj de pared: igual en M1/M5/M15). Entrada en el primer
    cierre fuera del rango después de las 07:00 GMT, en dirección de la
    ruptura. Solo 1 señal por día (la primera). SL/TP: fórmula genérica
    (1.5× ATR, mín 20 pips, RR 2.0).

    Guarda london-only (07:00-13:00 UTC): replica la regla propia del
    screening del bloque 3 aunque el filtro global live sea london+overlap.
    """
    nombre = "LondonBreakout"
    tipo = "trend"
    min_confidence_default = 0.75
    criterios_decision = "EXPERIMENTAL-bloque3-shadow"

    def detectar(self, df, params: dict):
        par = params.get("_par_actual")
        ts = _ts_utc(df).iloc[-1]
        # Guarda london-only: replica sesiones_activas=["london"] del screening.
        if not (7 <= ts.hour < 13):
            return None
        hoy = ts.date()
        t = _ts_utc(df)
        asia = df[(t.dt.date == hoy) & (t.dt.hour >= 0) & (t.dt.hour < 7)]
        if len(asia) < 8:
            return None
        lo, hi = float(asia["Low"].min()), float(asia["High"].max())
        close = float(df["Close"].iloc[-1])
        if close > hi:
            if _marca_dia(self.nombre, par, hoy):
                return None
            return PatronDetectado(
                self.nombre, "long",
                f"LondonBreakout: cierre {close:.5f} rompe rango asiático "
                f"{lo:.5f}-{hi:.5f}")
        if close < lo:
            if _marca_dia(self.nombre, par, hoy):
                return None
            return PatronDetectado(
                self.nombre, "short",
                f"LondonBreakout: cierre {close:.5f} rompe rango asiático "
                f"{lo:.5f}-{hi:.5f}")
        return None


# ── 3. PrevDayBreakout (ruptura del máximo/mínimo del día anterior) ───────────
class EstrategiaPrevDayBreakout(EstrategiaBase):
    """
    EXPERIMENTAL shadow-only — trend.

    Máximo/mínimo del día UTC anterior (si no tiene >= 8 velas —p.ej. domingo—,
    se retrocede hasta 3 días buscando el día previo con datos; en la práctica
    el lunes usa el rango del viernes). Entrada en el cierre que rompe el
    nivel, en dirección de la ruptura. Solo 1 señal por día. SL/TP: fórmula
    genérica (1.5× ATR, mín 20 pips, RR 2.0).
    """
    nombre = "PrevDayBreakout"
    tipo = "trend"
    min_confidence_default = 0.75
    criterios_decision = "EXPERIMENTAL-bloque3-shadow"

    def detectar(self, df, params: dict):
        par = params.get("_par_actual")
        ts = _ts_utc(df).iloc[-1]
        hoy = ts.date()
        rangos = _rangos_por_dia(df)
        prev = None
        for d in range(1, 4):
            cand = rangos.get(hoy - timedelta(days=d))
            if cand is not None and cand[2] >= 8:
                prev = cand
                break
        if prev is None:
            return None
        lo, hi, _ = prev
        close = float(df["Close"].iloc[-1])
        if close > hi:
            if _marca_dia(self.nombre, par, hoy):
                return None
            return PatronDetectado(
                self.nombre, "long",
                f"PrevDayBreakout: cierre {close:.5f} rompe máximo previo {hi:.5f}")
        if close < lo:
            if _marca_dia(self.nombre, par, hoy):
                return None
            return PatronDetectado(
                self.nombre, "short",
                f"PrevDayBreakout: cierre {close:.5f} rompe mínimo previo {lo:.5f}")
        return None


# ── 4. InsideBar ──────────────────────────────────────────────────────────────
class EstrategiaInsideBar(EstrategiaBase):
    """
    EXPERIMENTAL shadow-only — trend.

    Vela inside: high[-2] < high[-3] y low[-2] > low[-3] (la vela madre es la
    de hace 2). Entrada al cierre de la vela actual que rompe el high/low de
    la madre, en dirección de la ruptura. SL absoluto: más allá del extremo
    opuesto de la madre (low de la madre en long, high en short). TP: RR 2.0.
    """
    nombre = "InsideBar"
    tipo = "trend"
    min_confidence_default = 0.75
    criterios_decision = "EXPERIMENTAL-bloque3-shadow"

    def detectar(self, df, params: dict):
        if len(df) < 3:
            return None
        madre = df.iloc[-3]
        inside = df.iloc[-2]
        u = df.iloc[-1]
        mh, ml = float(madre["High"]), float(madre["Low"])
        ih, il = float(inside["High"]), float(inside["Low"])
        if not (ih < mh and il > ml):
            return None
        close = float(u.get("Close", 0))
        if close > mh:
            return PatronExperimental(
                self.nombre, "long",
                f"InsideBar: cierre {close:.5f} rompe madre {ml:.5f}-{mh:.5f}",
                sl_nivel=ml)
        if close < ml:
            return PatronExperimental(
                self.nombre, "short",
                f"InsideBar: cierre {close:.5f} rompe madre {ml:.5f}-{mh:.5f}",
                sl_nivel=mh)
        return None


REGISTRO_EXPERIMENTAL = {
    "DoubleBottom": EstrategiaDoubleBottom,
    "LondonBreakout": EstrategiaLondonBreakout,
    "PrevDayBreakout": EstrategiaPrevDayBreakout,
    "InsideBar": EstrategiaInsideBar,
}
