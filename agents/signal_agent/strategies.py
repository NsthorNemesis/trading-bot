"""
agents/signal_agent/strategies.py — v2.0 (calibración independiente por estrategia)

Arquitectura:
  - PatronDetectado: resultado de la detección (nombre, dir_hint, descripción)
  - EstrategiaBase: clase base con detectar() + log_shadow()
  - 6 clases concretas, cada una con:
      · tipo:                  "trend" | "reversion" — clase de mercado ideal
      · min_confidence_default: umbral mínimo de confianza para esta estrategia
      · criterios_decision:    prompt específico para DeepSeek decisor
  - SHADOW_LOG: logs/shadow_signals.jsonl — señales de estrategias pausadas

Calibración independiente:
  Cada estrategia tiene su propio min_confidence y criterios de evaluación.
  El SignalAgent pasa estos criterios al briefing y al decisor para que
  cada señal se evalúe con las métricas correctas para su tipo de setup.
  Los parámetros pueden sobreescribirse en strategy_params.json bajo 'per_strategy'.

Tipos de estrategia:
  trend     → requieren ADX alto, momentum, alineación EMA. Mejor en tendencia fuerte.
  reversion → requieren ADX bajo/moderado, extremo de precio, señal de agotamiento.
              NO deben operar en tendencia fuerte (el precio puede seguir).

Bugs corregidos vs _detectar_patrones() original:
  EMA_Crossover   — ADX hardcoded 20 → ahora usa params['adx_min_operar']
  EMA_Crossover   — sin dir_hint → ahora retorna 'long' o 'short' según tipo de cruce
  Engulfing       — cuerpo >= prev*0.90 (90%) → ahora 100% (engullimiento real)
  Engulfing       — sin ADX → ahora requiere ADX >= adx_min_operar
  Engulfing       — sin dir_hint → ahora retorna 'long' o 'short'
  Hammer          — sombra_inf >= 2x (demasiado amplio) → ahora 2.5x
  Hammer          — sombra_sup <= 0.5x (demasiado amplio) → ahora 0.3x
  Hammer          — sin Shooting Star para SHORT → ahora implementado
  Doji            — sin contexto de tendencia → ahora requiere EMA20 vs EMA50 + ADX>20
  Doji            — sin dir_hint → ahora retorna 'long' o 'short' según contexto
  RSI_Bollinger   — sin dir_hint → ahora retorna 'long' o 'short' con descripción
  RSI_Bollinger   — ignoraba adx_max_rsi_bollinger → ahora rechaza si ADX >= max
  Hammer          — ignoraba adx_max_hammer → ahora rechaza si ADX >= max
  RSI_Divergence  — rsi_rec[-1] > min*1.02 (dispara casi siempre) →
                    ahora: RSI actual vs RSI en la misma vela del precio extremo, ≥8 pts
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("strategies")

SHADOW_LOG = Path("logs/shadow_signals.jsonl")


# ── Tipos ─────────────────────────────────────────────────────────────────────

@dataclass
class PatronDetectado:
    """Resultado de detectar una estrategia en una vela."""
    nombre:      str
    dir_hint:    Optional[str]   # 'long', 'short', o None
    descripcion: str


# ── Clase base ────────────────────────────────────────────────────────────────

class EstrategiaBase:
    """
    Contrato mínimo de una estrategia.
    Subclases implementan detectar(df, params) → PatronDetectado | None.

    Atributos de clase que cada sub-agente define:
      nombre:               identificador único de la estrategia
      tipo:                 "trend" | "reversion"
      min_confidence_default: umbral mínimo de confianza (0-1)
      criterios_decision:   texto inyectado al prompt del decisor
    """
    nombre:                str   = ""
    tipo:                  str   = "trend"
    min_confidence_default: float = 0.75
    criterios_decision:    str   = ""

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        raise NotImplementedError

    # Cooldown por (estrategia, par) — evita registrar la misma señal cada 10s
    _shadow_cooldown: dict = {}

    def log_shadow(
        self,
        par:           str,
        patron:        PatronDetectado,
        df,
        h4_tendencia:  str = "rango",
        params:        dict = None,
    ) -> None:
        """
        Registra una señal de estrategia pausada en logs/shadow_signals.jsonl.
        Solo registra señales alineadas con H4 y respeta cooldown de 15 minutos
        para evitar duplicados del loop de 10 segundos.
        """
        try:
            # ── Filtro H4: solo registrar señales alineadas con tendencia macro ──
            if h4_tendencia != "rango":
                alineado = (
                    (h4_tendencia == "up"   and patron.dir_hint == "long") or
                    (h4_tendencia == "down" and patron.dir_hint == "short")
                )
                if not alineado:
                    logger.debug(
                        f"[Shadow] {par} {self.nombre} {patron.dir_hint} "
                        f"bloqueada — H4={h4_tendencia} (contra tendencia)"
                    )
                    return

            # ── Cooldown: no registrar la misma señal más de 1 vez cada 15 min ──
            from datetime import timedelta
            key = (self.nombre, par, patron.dir_hint)
            ultimo = EstrategiaBase._shadow_cooldown.get(key)
            if ultimo and (datetime.now(timezone.utc) - ultimo).total_seconds() < 900:
                return
            EstrategiaBase._shadow_cooldown[key] = datetime.now(timezone.utc)

            SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
            u = df.iloc[-1]
            entry    = float(u.get("Close", 0))
            atr      = float(u.get("ATR_14", 0) or 0)
            sl_mult  = float((params or {}).get("sl_atr_mult", 1.5))
            rr       = float((params or {}).get("rr_ratio",   2.0))
            pip      = 0.01 if "JPY" in par else 0.0001
            min_sl_d = float((params or {}).get("min_sl_pips", 10)) * pip

            sl_nivel = getattr(patron, "sl_nivel", None)
            if sl_nivel is not None:
                # Estrategia experimental (bloque 3): el patrón define su SL
                # como nivel absoluto. Sin enforcement de min_sl_pips y TP
                # recalculado para mantener el RR (igual que el engine).
                sl = float(sl_nivel)
                dist = abs(entry - sl)
                if patron.dir_hint == "long":
                    tp = entry + dist * rr
                elif patron.dir_hint == "short":
                    tp = entry - dist * rr
                else:
                    sl = tp = None
            elif patron.dir_hint == "long":
                # Enforcement min_sl_pips (risk_execution_agent.py:341-352)
                dist = max(atr * sl_mult, min_sl_d)
                sl, tp = entry - dist, entry + dist * rr
            elif patron.dir_hint == "short":
                dist = max(atr * sl_mult, min_sl_d)
                sl, tp = entry + dist, entry - dist * rr
            else:
                sl = tp = None

            record = {
                "ts":          datetime.now(timezone.utc).isoformat(),
                "par":         par,
                "estrategia":  self.nombre,
                "dir_hint":    patron.dir_hint,
                "descripcion": patron.descripcion,
                "entry":       round(entry, 5),
                "atr":         round(atr, 5),
                "sl":          round(sl, 5) if sl is not None else None,
                "tp":          round(tp, 5) if tp is not None else None,
                "h4":          h4_tendencia,
                "resultado":   None,
                # Feature vector (2026-10-06): snapshot de indicadores al
                # momento de la señal — alimenta el futuro dataset del modelo
                # especializado. Todos los campos anteriores se mantienen.
                "features":    self._feature_vector(u, h4_tendencia),
            }
            with SHADOW_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            logger.debug(
                f"[Shadow] {par} {self.nombre} {patron.dir_hint} "
                f"@ {entry:.5f}  H4={h4_tendencia}"
            )
        except Exception as exc:
            logger.debug(f"[Shadow] error al registrar: {exc}")

    @staticmethod
    def _feature_vector(u, h4_tendencia: str = "rango") -> dict:
        """Vector de features al momento de la señal (2026-10-06).
        `u` es la última vela del DataFrame con indicadores del market agent.
        Pensado para el futuro dataset del modelo especializado."""
        def _f(col, nd=5):
            try:
                return round(float(u.get(col, 0) or 0), nd)
            except (TypeError, ValueError):
                return 0.0

        close   = _f("Close")
        bb_low  = _f("BB_LOW")
        bb_high = _f("BB_HIGH")
        bb_pos  = ((close - bb_low) / (bb_high - bb_low)
                   if bb_high > bb_low else 0.5)

        hora_utc = datetime.now(timezone.utc).hour
        if 7 <= hora_utc < 13:
            sesion = "london"
        elif 13 <= hora_utc < 17:
            sesion = "overlap"
        elif 17 <= hora_utc < 22:
            sesion = "new_york"
        else:
            sesion = "asia"

        return {
            "ema_9":     _f("EMA_9"),
            "ema_20":    _f("EMA_20"),
            "ema_50":    _f("EMA_50"),
            "adx_14":    _f("ADX_14", 2),
            "rsi_14":    _f("RSI_14", 2),
            "macd":      _f("MACD"),
            "macd_sig":  _f("MACD_SIG"),
            "macd_hist": _f("MACD_DIF"),
            "bb_pos":    round(bb_pos, 4),
            "atr_14":    _f("ATR_14"),
            "h4":        h4_tendencia,
            "sesion":    sesion,
        }


# ── 1. EMA Crossover ──────────────────────────────────────────────────────────

class EstrategiaEMACrossover(EstrategiaBase):
    """
    EMA9 cruza EMA50 en últimas 3 velas.
    Requiere ADX >= adx_min_operar (params, no hardcoded).
    Retorna dir_hint='long' para cruce alcista, 'short' para bajista.
    """
    nombre                 = "EMA_Crossover"
    tipo                   = "trend"
    min_confidence_default = 0.75
    criterios_decision     = """
EMPLEADO: EMA_Crossover (seguimiento de tendencia)
TRABAJO: Detectar cruces EMA9/EMA50 con momentum real.
CRITERIOS DE APROBACIÓN (necesita al menos 3 de 4):
  1. ¿El cruce ocurrió en las últimas 1-3 velas? (más antiguo = señal fría, rechazar)
  2. ¿El ATR de la vela del cruce es >= al ATR promedio de las últimas 10 velas? (momentum real)
  3. ¿El precio se está alejando de las EMAs, no volviendo hacia ellas? (alejarse = impulso activo)
  4. ¿El MACD_hist es positivo para LONG / negativo para SHORT? (confirmación de momentum)
RECHAZAR si: el precio ya regresó al nivel del cruce, o el ADX está bajando, o la vela del cruce fue pequeña."""

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        if "EMA_9" not in df.columns or "EMA_50" not in df.columns:
            return None
        if len(df) < 5:
            return None

        adx_min   = float(params.get("adx_min_operar", 27))
        u         = df.iloc[-1]
        adx       = float(u.get("ADX_14", 0) or 0)
        ema9_arr  = df["EMA_9"].values
        ema50_arr = df["EMA_50"].values

        if adx < adx_min:
            return None

        cruce_alcista = any(
            ema9_arr[i - 1] <= ema50_arr[i - 1] and ema9_arr[i] > ema50_arr[i]
            for i in range(-3, 0)
            if i - 1 >= -len(ema9_arr)
        )
        cruce_bajista = any(
            ema9_arr[i - 1] >= ema50_arr[i - 1] and ema9_arr[i] < ema50_arr[i]
            for i in range(-3, 0)
            if i - 1 >= -len(ema9_arr)
        )

        if cruce_alcista:
            return PatronDetectado(
                nombre      = "EMA_Crossover",
                dir_hint    = "long",
                descripcion = (
                    f"EMA9 cruzó SOBRE EMA50 en últimas 3 velas (ADX={adx:.1f} >= {adx_min}). "
                    f"Impulso alcista confirmado — LONG."
                ),
            )
        if cruce_bajista:
            return PatronDetectado(
                nombre      = "EMA_Crossover",
                dir_hint    = "short",
                descripcion = (
                    f"EMA9 cruzó BAJO EMA50 en últimas 3 velas (ADX={adx:.1f} >= {adx_min}). "
                    f"Impulso bajista confirmado — SHORT."
                ),
            )
        return None


# ── 1b. EMA Overlap + Kronos (shadow-only) ────────────────────────────────────
class EstrategiaEMAOverlapKronos(EstrategiaBase):
    """
    Shadow de la configuración ganadora Familia 4 (solo-Overlap, GBP_USD/USD_JPY)
    + filtro de volatilidad Kronos (K2-vol, Fase 0 pasada en 5y el 2026-10-10).

    - Replica la detección de EMA_Crossover pero SOLO en sesión overlap
      (13:00-17:00 UTC) y en los 2 pares, sin tocar sesiones_activas global
      (el live sigue con su configuración).
    - El veto de Kronos se aplica OFFLINE en la máquina de backtest
      (el VPS no corre torch): esta estrategia registra TODAS las señales
      EMA-overlap en shadow_signals.jsonl; el veto se evalúa fuera de línea
      con el umbral p75 calibrado en el backtest 5y.
    - adx_min_operar=22 vía per_strategy (valor del backtest campeón).
    """
    nombre                 = "EMA_OverlapKronos"
    tipo                   = "trend"
    min_confidence_default = 0.75
    PARES                  = ("GBP_USD", "USD_JPY")
    criterios_decision     = (
        "Shadow: configuración ganadora Familia 4 (EMA, overlap, 2 pares) "
        "con veto de volatilidad Kronos aplicado offline."
    )

    def detectar(self, df, params: dict):
        par = params.get("_par_actual", "")
        if par not in self.PARES:
            return None
        h = datetime.now(timezone.utc).hour
        if not (13 <= h < 17):
            return None
        patron = EstrategiaEMACrossover().detectar(df, params)
        if patron is None:
            return None
        return PatronDetectado(
            nombre      = self.nombre,
            dir_hint    = patron.dir_hint,
            descripcion = f"[Kronos-shadow] {patron.descripcion}",
        )


# ── 2. Engulfing ──────────────────────────────────────────────────────────────

class EstrategiaEngulfing(EstrategiaBase):
    """
    Vela actual engulló COMPLETAMENTE la vela anterior (100%, no 90%).
    Requiere ADX >= adx_min_operar para confirmar momentum.
    dir_hint basado en dirección de la vela engullente.
    """
    nombre                 = "Engulfing"
    tipo                   = "trend"
    min_confidence_default = 0.75
    criterios_decision     = """
EMPLEADO: Engulfing (cambio de control con fuerza)
TRABAJO: Confirmar que la vela engulfing representa un cambio real de control entre compradores y vendedores.
CRITERIOS DE APROBACIÓN (necesita al menos 3 de 4):
  1. ¿El tamaño de la vela engulfing es >= 1.5x el ATR promedio? (vela grande = fuerza real)
  2. ¿El cierre está en el 30% superior del rango para LONG, o 30% inferior para SHORT? (cierre fuerte)
  3. ¿Hay 2+ velas previas en la dirección contraria que se está revirtiendo? (agotamiento previo)
  4. ¿El RSI no está en zona de sobrecompra para LONG (>65) ni sobreventa para SHORT (<35)?
RECHAZAR si: la vela engulfing es pequeña (<1x ATR), o el cierre está en el medio de la vela."""

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        if len(df) < 3:
            return None

        adx_min    = float(params.get("adx_min_operar", 27))
        u          = df.iloc[-1]
        prev       = df.iloc[-2]
        adx        = float(u.get("ADX_14", 0) or 0)

        if adx < adx_min:
            return None

        close      = float(u.get("Close",    0))
        open_      = float(u.get("Open",     0))
        prev_close = float(prev.get("Close", 0))
        prev_open  = float(prev.get("Open",  0))
        atr        = float(u.get("ATR_14",   0) or 0)
        cuerpo     = abs(close - open_)
        prev_cuerpo = abs(prev_close - prev_open)

        if prev_cuerpo == 0 or cuerpo == 0:
            return None

        # Filtro de cuerpo mínimo: el engulfing debe tener al menos 25% del ATR
        # Evita señales con velas casi planas (Doji disfrazado de Engulfing)
        # Ejemplo: ATR=0.0010 → cuerpo mínimo=0.00025 (2.5 pips en EUR/USD)
        min_cuerpo = atr * 0.25 if atr > 0 else 0
        if cuerpo < min_cuerpo or prev_cuerpo < min_cuerpo:
            return None

        # Filtro de contexto: verificar que hay momentum previo en dirección contraria
        # (al menos 1 de las 2 velas anteriores confirma el movimiento que el engulfing revierte)
        if len(df) >= 4:
            prev2 = df.iloc[-3]
            prev2_close = float(prev2.get("Close", 0))
            prev2_open  = float(prev2.get("Open",  0))
            # Para bull engulfing: queremos que prev o prev2 sean bajistas (hay algo que revertir)
            # Para bear engulfing: queremos que prev o prev2 sean alcistas
            prev_es_bajista  = prev_close < prev_open
            prev2_es_bajista = prev2_close < prev2_open
            prev_es_alcista  = prev_close > prev_open
            prev2_es_alcista = prev2_close > prev2_open
        else:
            prev_es_bajista = prev_es_alcista = True  # sin datos suficientes, no filtrar
            prev2_es_bajista = prev2_es_alcista = True

        # Bullish engulfing: vela alcista envuelve vela bajista anterior
        # + contexto: al menos 1 de las 2 velas previas debe ser bajista
        bull = (
            close > open_            and   # vela alcista
            prev_close < prev_open   and   # anterior bajista
            close  >= prev_open      and   # cierre >= apertura anterior
            open_  <= prev_close     and   # apertura <= cierre anterior
            cuerpo >= prev_cuerpo    and   # engullimiento completo (100%)
            (prev_es_bajista or prev2_es_bajista)  # hay momentum bajista previo
        )
        # Bearish engulfing: vela bajista envuelve vela alcista anterior
        # + contexto: al menos 1 de las 2 velas previas debe ser alcista
        bear = (
            close < open_            and   # vela bajista
            prev_close > prev_open   and   # anterior alcista
            close  <= prev_open      and   # cierre <= apertura anterior
            open_  >= prev_close     and   # apertura >= cierre anterior
            cuerpo >= prev_cuerpo    and   # engullimiento completo (100%)
            (prev_es_alcista or prev2_es_alcista)  # hay momentum alcista previo
        )

        # Filtro de cierre fuerte: la vela debe cerrar en el rango correcto
        # JPY tiene velas más volátiles → umbral más flexible (50% vs configurable)
        # Bull: cierre en la mitad/tercio superior → compradores mantuvieron control
        # Bear: cierre en la mitad/tercio inferior → vendedores mantuvieron control
        high_u  = float(u.get("High", close))
        low_u   = float(u.get("Low",  close))
        rango_u = high_u - low_u if high_u > low_u else 1e-9
        cierre_pct = (close - low_u) / rango_u   # 0=fondo, 1=techo

        # Umbral configurable desde params (default 0.60), JPY siempre 0.50
        es_jpy = "JPY" in params.get("_par_actual", "")
        cierre_pct_min = float(params.get("engulfing_cierre_pct", 0.60))
        umbral_bull = 0.50 if es_jpy else cierre_pct_min
        umbral_bear = 0.50 if es_jpy else (1.0 - cierre_pct_min)

        if bull:
            if cierre_pct < umbral_bull:
                return None
            return PatronDetectado(
                nombre      = "Engulfing",
                dir_hint    = "long",
                descripcion = (
                    f"Bullish Engulfing fuerte: cuerpo={cuerpo:.5f} vs prev={prev_cuerpo:.5f}, "
                    f"cierre en {cierre_pct:.0%} del rango, ADX={adx:.1f}. LONG."
                ),
            )
        if bear:
            if cierre_pct > umbral_bear:
                return None
            return PatronDetectado(
                nombre      = "Engulfing",
                dir_hint    = "short",
                descripcion = (
                    f"Bearish Engulfing fuerte: cuerpo={cuerpo:.5f} vs prev={prev_cuerpo:.5f}, "
                    f"cierre en {cierre_pct:.0%} del rango, ADX={adx:.1f}. SHORT."
                ),
            )
        return None


# ── 3. Hammer / Shooting Star ─────────────────────────────────────────────────

class EstrategiaHammer(EstrategiaBase):
    """
    Hammer (LONG):  sombra inf >= 2.5x cuerpo + sombra sup <= 0.3x + EMA20<EMA50 + RSI<45
    Shooting Star (SHORT): sombra sup >= 2.5x cuerpo + sombra inf <= 0.3x + EMA20>EMA50 + RSI>55
    Ratios más estrictos que el original (2x / 0.5x).
    """
    nombre                 = "Hammer"
    tipo                   = "reversion"
    min_confidence_default = 0.78
    criterios_decision     = """
EMPLEADO: Hammer / Shooting Star (rechazo de nivel con mecha)
TRABAJO: Verificar que la sombra larga indica rechazo activo en un nivel técnico, no vela casual.
CRITERIOS DE APROBACIÓN (necesita al menos 3 de 4):
  1. ¿La sombra apunta hacia un nivel técnico identificable? (BB, EMA50, soporte/resistencia reciente)
  2. ¿Las últimas 3-5 velas previas muestran momentum decreciente en esa dirección? (agotamiento)
  3. ¿El cuerpo del Hammer cerró por encima del 50% de la sombra? (los compradores recuperaron control)
  4. ¿El ATR de esta vela es >= promedio? (rechazo con fuerza, no doji disfrazado)
RECHAZAR si: no hay nivel técnico cercano que explique el rechazo, o si el ADX es muy alto
             (en tendencia fuerte el precio puede romper la sombra y seguir)."""

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        if len(df) < 5:
            return None

        u     = df.iloc[-1]
        close = float(u.get("Close",  0))
        open_ = float(u.get("Open",   0))
        high  = float(u.get("High",   0))
        low   = float(u.get("Low",    0))
        rsi   = float(u.get("RSI_14", 50) or 50)
        adx   = float(u.get("ADX_14", 0)  or 0)
        ema20 = float(u.get("EMA_20", 0)  or 0)
        ema50 = float(u.get("EMA_50", 0)  or 0)

        cuerpo     = abs(close - open_)
        sombra_inf = min(open_, close) - low
        sombra_sup = high - max(open_, close)

        if cuerpo == 0:
            return None

        # Hammer es reversión — no operar si tendencia M15 muy fuerte
        adx_max_h = float(params.get("adx_max_hammer", 30))
        if adx >= adx_max_h:
            return None

        es_hammer = (
            sombra_inf >= 2.5 * cuerpo and
            sombra_sup <= cuerpo * 0.3 and
            ema20 < ema50 and              # downtrend
            rsi < 45
        )
        es_shooting = (
            sombra_sup >= 2.5 * cuerpo and
            sombra_inf <= cuerpo * 0.3 and
            ema20 > ema50 and              # uptrend
            rsi > 55
        )

        if es_hammer:
            return PatronDetectado(
                nombre      = "Hammer",
                dir_hint    = "long",
                descripcion = (
                    f"Hammer: sombra inf={sombra_inf/cuerpo:.1f}x cuerpo en downtrend "
                    f"(EMA20<EMA50, RSI={rsi:.0f}). Reversión alcista probable — LONG."
                ),
            )
        if es_shooting:
            return PatronDetectado(
                nombre      = "Hammer",
                dir_hint    = "short",
                descripcion = (
                    f"Shooting Star: sombra sup={sombra_sup/cuerpo:.1f}x cuerpo en uptrend "
                    f"(EMA20>EMA50, RSI={rsi:.0f}). Reversión bajista probable — SHORT."
                ),
            )
        return None


# ── 4. Doji ───────────────────────────────────────────────────────────────────

class EstrategiaDoji(EstrategiaBase):
    """
    Cuerpo < 10% del rango + contexto de tendencia (EMA20 vs EMA50) + ADX > 20.
    Ya no dispara en neutro — requiere que el Doji esté en un extremo de tendencia.
    """
    nombre                 = "Doji"
    tipo                   = "reversion"
    min_confidence_default = 0.82
    criterios_decision     = """
EMPLEADO: Doji (indecisión en extremo de tendencia)
TRABAJO: El Doji solo vale si está en el punto exacto de agotamiento de una tendencia.
CRITERIOS DE APROBACIÓN (necesita los 4):
  1. ¿El Doji está en un nivel técnico relevante? (sin nivel = indecisión sin contexto, rechazar)
  2. ¿Las 3-5 velas previas muestran momentum decreciente? (velas cada vez más pequeñas = agotamiento)
  3. ¿El RSI está en zona extrema genuina? (<30 para LONG, >70 para SHORT — no solo <33 o >67)
  4. ¿La vela SIGUIENTE al Doji (si disponible) cierra en la dirección esperada? (confirmación)
RECHAZAR si: el Doji aparece en mitad de tendencia sin nivel técnico, o si no hay señales
             de agotamiento en las velas previas. El Doji es la señal más débil — exigir máximo contexto."""

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        if len(df) < 5:
            return None

        u     = df.iloc[-1]
        close = float(u.get("Close",  0))
        open_ = float(u.get("Open",   0))
        high  = float(u.get("High",   0))
        low   = float(u.get("Low",    0))
        rsi   = float(u.get("RSI_14", 50) or 50)
        ema20 = float(u.get("EMA_20", 0)  or 0)
        ema50 = float(u.get("EMA_50", 0)  or 0)
        adx   = float(u.get("ADX_14", 0)  or 0)

        cuerpo = abs(close - open_)
        rango  = high - low or 1e-9

        if cuerpo / rango >= 0.10:      # no es Doji
            return None
        if adx < 20:                    # mercado sin dirección clara
            return None

        # Umbrales RSI configurables desde params (default estándar 30/70)
        doji_rsi_low  = float(params.get("doji_rsi_low",  30))
        doji_rsi_high = float(params.get("doji_rsi_high", 70))

        # Doji al fondo de downtrend → posible LONG
        if ema20 < ema50 and rsi < doji_rsi_low:
            return PatronDetectado(
                nombre      = "Doji",
                dir_hint    = "long",
                descripcion = (
                    f"Doji en fondo de downtrend: EMA20({ema20:.5f})<EMA50({ema50:.5f}), "
                    f"RSI={rsi:.0f} (<{doji_rsi_low:.0f}), ADX={adx:.1f}. Posible reversión LONG."
                ),
            )
        # Doji en techo de uptrend → posible SHORT
        if ema20 > ema50 and rsi > doji_rsi_high:
            return PatronDetectado(
                nombre      = "Doji",
                dir_hint    = "short",
                descripcion = (
                    f"Doji en techo de uptrend: EMA20({ema20:.5f})>EMA50({ema50:.5f}), "
                    f"RSI={rsi:.0f} (>{doji_rsi_high:.0f}), ADX={adx:.1f}. Posible reversión SHORT."
                ),
            )
        return None


# ── 5. RSI + Bollinger ────────────────────────────────────────────────────────

class EstrategiaRSIBollinger(EstrategiaBase):
    """
    RSI < 30 + precio ≤ BB inferior  → LONG (sobreventa + banda inferior)
    RSI > 70 + precio ≥ BB superior  → SHORT (sobrecompra + banda superior)
    Requiere ADX < adx_max_rsi_bollinger (mercado en rango, no tendencia fuerte).
    """
    nombre                 = "RSI_Bollinger"
    tipo                   = "reversion"
    min_confidence_default = 0.80
    criterios_decision     = """
EMPLEADO: RSI_Bollinger (reversión a la media desde extremo de banda)
TRABAJO: Confirmar que el precio está en un EXTREMO REAL con agotamiento, no solo tocando la banda en tendencia.
CRITERIOS DE APROBACIÓN (necesita al menos 3 de 4):
  1. ¿El mercado está en rango o tendencia débil? (ADX < 22 = ideal; si ADX > 25 = riesgo alto)
  2. ¿El precio tocó la banda con una vela de rechazo visible? (mecha larga fuera de la banda = rechazo)
  3. ¿El RSI está en zona genuinamente extrema? (<25 para LONG es mucho más fiable que <30)
  4. ¿Hay 2+ velas consecutivas tocando/fuera de la banda? (sobreextensión real, no puntual)
RECHAZAR si: el ADX es alto (el precio puede continuar rompiendo la banda en tendencia fuerte),
             o si el precio solo rozó la banda sin rechazo visible,
             o si es la primera vela tocando la banda (puede ser entrada de tendencia, no extremo)."""

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        if len(df) < 5:
            return None

        u       = df.iloc[-1]
        prev    = df.iloc[-2]
        close   = float(u.get("Close",  0))
        open_   = float(u.get("Open",   0))
        low     = float(u.get("Low",    0))
        high    = float(u.get("High",   0))
        rsi     = float(u.get("RSI_14", 50) or 50)
        adx     = float(u.get("ADX_14", 0)  or 0)
        atr     = float(u.get("ATR_14", 0)  or 0)
        bbl     = next((float(u[c] or 0) for c in df.columns if "BBL" in c), 0)
        bbu     = next((float(u[c] or 0) for c in df.columns if "BBU" in c), 0)
        bbl_prev = next((float(prev[c] or 0) for c in df.columns if "BBL" in c), 0)
        bbu_prev = next((float(prev[c] or 0) for c in df.columns if "BBU" in c), 0)

        if bbl == 0 or bbu == 0:
            return None

        # RSI_Bollinger es reversión — no operar si hay tendencia fuerte
        adx_max = float(params.get("adx_max_rsi_bollinger", 25))
        if adx >= adx_max:
            return None

        cuerpo     = abs(close - open_)
        min_mecha  = atr * 0.3   # mecha mínima para considerar rechazo real

        # LONG: RSI sobrevendido + precio en/bajo BB + 2 velas consecutivas + mecha inferior
        prev_close_v = float(prev.get("Close", 0))
        prev_bbl_ok  = bbl_prev > 0 and prev_close_v <= bbl_prev * 1.001
        mecha_inf    = (open_ - low) if open_ > close else (close - low)
        hay_rechazo_long = mecha_inf >= min_mecha and close > low

        if (rsi < 30 and close <= bbl * 1.001
                and prev_bbl_ok             # 2 velas consecutivas en banda
                and hay_rechazo_long):      # mecha inferior visible = rechazo
            return PatronDetectado(
                nombre      = "RSI_Bollinger",
                dir_hint    = "long",
                descripcion = (
                    f"RSI sobrevendido ({rsi:.1f}<30) + precio ({close:.5f}) "
                    f"en/bajo BB_bajo ({bbl:.5f}) 2 velas consecutivas + "
                    f"mecha rechazo ({mecha_inf/atr:.1f}x ATR). LONG."
                ),
            )

        # SHORT: RSI sobrecomprado + precio en/sobre BB + 2 velas consecutivas + mecha superior
        prev_bbu_ok  = bbu_prev > 0 and prev_close_v >= bbu_prev * 0.999
        mecha_sup    = (high - open_) if open_ < close else (high - close)
        hay_rechazo_short = mecha_sup >= min_mecha and close < high

        if (rsi > 70 and close >= bbu * 0.999
                and prev_bbu_ok             # 2 velas consecutivas en banda
                and hay_rechazo_short):     # mecha superior visible = rechazo
            return PatronDetectado(
                nombre      = "RSI_Bollinger",
                dir_hint    = "short",
                descripcion = (
                    f"RSI sobrecomprado ({rsi:.1f}>70) + precio ({close:.5f}) "
                    f"en/sobre BB_alto ({bbu:.5f}) 2 velas consecutivas + "
                    f"mecha rechazo ({mecha_sup/atr:.1f}x ATR). SHORT."
                ),
            )
        return None


# ── 6. RSI Divergencia ────────────────────────────────────────────────────────

class EstrategiaRSIDivergencia(EstrategiaBase):
    """
    Divergencia alcista:  precio nuevo mínimo Y RSI en ese mínimo era >= 8 pts más bajo
    Divergencia bajista:  precio nuevo máximo Y RSI en ese máximo era >= 8 pts más alto

    Bug corregido: antes se comparaba rsi_rec[-1] > rsi_rec[:-1].min() * 1.02
    (solo 2% por encima del mínimo de RSI, que dispara casi siempre).
    Ahora se busca el índice del precio extremo y se compara RSI en ESA misma vela,
    requiriendo ≥ 8 puntos de diferencia absoluta.
    """
    nombre                 = "Engulfing+RSI_Divergence"
    tipo                   = "reversion"
    min_confidence_default = 0.78
    criterios_decision     = """
EMPLEADO: RSI_Divergence (agotamiento de tendencia por divergencia)
TRABAJO: La divergencia precio/RSI debe indicar pérdida de momentum REAL, no ruido.
CRITERIOS DE APROBACIÓN (necesita al menos 3 de 4):
  1. ¿La diferencia RSI entre los dos extremos es significativa? (>12 pts = fuerte; 8-12 = moderada)
  2. ¿El segundo extremo de precio es notablemente más alto/bajo? (>0.5x ATR de diferencia)
  3. ¿El tiempo entre los dos extremos es razonable? (5-25 velas = válido; >30 = divergencia vieja)
  4. ¿El MACD también muestra divergencia o histograma decreciente en la misma dirección?
RECHAZAR si: la diferencia de precio entre los extremos es mínima (casi plano),
             o si el RSI ya rebotó desde la zona extrema antes de esta señal,
             o si hay evento económico pendiente que podría invalidar el setup."""

    @staticmethod
    def _es_engulfing(df, dir_hint: str) -> bool:
        """Verifica si la última vela es un engulfing en la dirección indicada."""
        if len(df) < 2:
            return False
        u    = df.iloc[-1]
        prev = df.iloc[-2]
        close      = float(u.get("Close",    0))
        open_      = float(u.get("Open",     0))
        prev_close = float(prev.get("Close", 0))
        prev_open  = float(prev.get("Open",  0))
        cuerpo      = abs(close - open_)
        prev_cuerpo = abs(prev_close - prev_open)
        if prev_cuerpo == 0 or cuerpo == 0:
            return False
        if dir_hint == "long":
            return (
                close > open_          and
                prev_close < prev_open and
                close  >= prev_open    and
                open_  <= prev_close   and
                cuerpo >= prev_cuerpo
            )
        else:
            return (
                close < open_          and
                prev_close > prev_open and
                close  <= prev_open    and
                open_  >= prev_close   and
                cuerpo >= prev_cuerpo
            )

    def detectar(self, df, params: dict) -> Optional[PatronDetectado]:
        if "RSI_14" not in df.columns or len(df) < 20:
            return None

        # Ventana configurable desde params (default 50, antes hardcodeado a 15)
        # Con 15 velas (3.5h en M15) la divergencia casi nunca ocurre
        # Con 50 velas (~12h) captura divergencias de sesión completa
        div_ventana = int(params.get("rsi_divergencia_ventana", 50))
        div_ventana = max(15, min(div_ventana, len(df) - 1))

        ventana    = df.iloc[-div_ventana:]
        closes     = ventana["Close"].values
        rsi_arr    = ventana["RSI_14"].values

        precio_actual = float(closes[-1])
        rsi_actual    = float(rsi_arr[-1] if rsi_arr[-1] is not None else 50)

        # Buscar extremos en las 14 velas anteriores (excluir la actual)
        hist_closes = closes[:-1]
        hist_rsi    = rsi_arr[:-1]

        if len(hist_closes) == 0:
            return None

        idx_min = int(hist_closes.argmin())
        idx_max = int(hist_closes.argmax())

        precio_min    = float(hist_closes[idx_min])
        precio_max    = float(hist_closes[idx_max])
        rsi_en_min    = float(hist_rsi[idx_min] if hist_rsi[idx_min] is not None else 50)
        rsi_en_max    = float(hist_rsi[idx_max] if hist_rsi[idx_max] is not None else 50)

        # Filtro de frescura: el extremo de precio no puede ser más antiguo que 10 velas
        # Una divergencia de hace 13 velas ya no es accionable — el mercado la olvidó
        n_hist = len(hist_closes)
        velas_desde_min = n_hist - 1 - idx_min   # 0 = vela más reciente del histórico
        velas_desde_max = n_hist - 1 - idx_max

        # Mínimo de puntos RSI para la divergencia (configurable, default 8)
        div_min_pts   = float(params.get("rsi_div_min_pts", 8.0))
        # Frescura configurable (default 10 velas, relativo a la ventana usada)
        div_frescura  = int(params.get("rsi_div_frescura", 10))

        # Divergencia alcista: precio bate mínimo pero RSI NO lo bate
        div_bull = (
            precio_actual <= precio_min * 1.001          and  # precio igual o menor al mínimo
            rsi_actual    >= rsi_en_min + div_min_pts    and  # RSI actual ≥N pts sobre RSI en ese mínimo
            rsi_actual    <  45                          and  # RSI en zona sobrevendida
            velas_desde_min <= div_frescura                   # divergencia reciente
        )
        # Divergencia bajista: precio bate máximo pero RSI NO lo bate
        div_bear = (
            precio_actual >= precio_max * 0.999          and  # precio igual o mayor al máximo
            rsi_actual    <= rsi_en_max - div_min_pts    and  # RSI actual ≥N pts bajo RSI en ese máximo
            rsi_actual    >  55                          and  # RSI en zona sobrecomprada
            velas_desde_max <= div_frescura                   # divergencia reciente
        )

        if div_bull:
            # Requiere confirmación engulfing alcista
            if not self._es_engulfing(df, "long"):
                return None
            return PatronDetectado(
                nombre      = "Engulfing+RSI_Divergence",
                dir_hint    = "long",
                descripcion = (
                    f"Engulfing alcista + Divergencia RSI: precio nuevo mínimo ({precio_actual:.5f} ≤ {precio_min:.5f}) "
                    f"pero RSI mejoró ({rsi_actual:.0f} vs {rsi_en_min:.0f} en ese mínimo, "
                    f"Δ={rsi_actual-rsi_en_min:.1f} pts). Confirmación doble — LONG."
                ),
            )
        if div_bear:
            # Requiere confirmación engulfing bajista
            if not self._es_engulfing(df, "short"):
                return None
            return PatronDetectado(
                nombre      = "Engulfing+RSI_Divergence",
                dir_hint    = "short",
                descripcion = (
                    f"Engulfing bajista + Divergencia RSI: precio nuevo máximo ({precio_actual:.5f} ≥ {precio_max:.5f}) "
                    f"pero RSI deterioró ({rsi_actual:.0f} vs {rsi_en_max:.0f} en ese máximo, "
                    f"Δ={rsi_en_max-rsi_actual:.1f} pts). Confirmación doble — SHORT."
                ),
            )
        return None


# ── Registro de estrategias ───────────────────────────────────────────────────
#
# TODAS_LAS_ESTRATEGIAS: cargadas siempre por el SignalAgent.
#   Activas  → operan en vivo (definido en estrategias_activas de params)
#   Pausadas → van a shadow log (monitoreo sin operar)
#
# Estado por estrategia (basado en backtest 52 semanas + análisis literatura):
#   Engulfing              ✅ ACTIVA   — ~32% WR con ADX≥27. Edge marginal pero positivo.
#   Engulfing+RSI_Div      ✅ ACTIVA   — lógica sólida, pendiente de validar con ventana=40.
#   RSI_Bollinger          ⏸ PAUSADA  — -37R en 52w. Inviable con SL corto post-fricción.
#   EMA_Crossover          ⏸ PAUSADA  — ventana 3 velas insuficiente en M15.
#   Hammer                 ⏸ PAUSADA  — falta verificación de nivel técnico cercano.
#   Doji                   ⏸ PAUSADA  — WR~50% sin confirmación de nivel (ruido).

TODAS_LAS_ESTRATEGIAS: list[EstrategiaBase] = [
    EstrategiaEngulfing(),          # ✅ activa
    EstrategiaRSIDivergencia(),     # ✅ activa (Engulfing+RSI_Divergence)
    EstrategiaRSIBollinger(),       # ⏸ shadow
    EstrategiaEMACrossover(),       # ⏸ shadow
    EstrategiaHammer(),             # ⏸ shadow
    EstrategiaDoji(),               # ⏸ shadow
    EstrategiaEMAOverlapKronos(),   # 🆕 shadow: ganadora F4 + veto Kronos offline
]
