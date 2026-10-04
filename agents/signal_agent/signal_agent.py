"""
agents/signal_agent/signal_agent.py — v16_patron_puro
=======================================================
Arquitectura simplificada: sin DeepSeek, solo detección matemática de patrones.

Flujo por par (cada 15 min via detection cooldown):
  1. Filtros rápidos: cooldown, sesión, datos frescos M15, ADX mínimo
  2. Filtro H4: tendencia macro (opcional, configurable)
  3. Detección de patrones: cada estrategia activa evalúa su lógica
  4. Filtro de dirección H4: señal debe estar alineada con tendencia macro
  5. Señal → RiskExecutionAgent

Estrategias activas/pausadas se controlan desde strategy_params.json.
Hot-reload sin reinicio via ParamsWatcher.

Eliminado vs v15:
  - AgenteBriefing (DeepSeek API)
  - AgenteContextoRiesgo
  - AgenteDecision (DeepSeek reasoner)
  - RegimeDetector, EconomicCalendar, OandaSentiment (deps opcionales inutilizados)
  - M1 entry refinement (desactivado permanentemente)
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

from config.settings import PARAMS
from agents.signal_agent.strategies import TODAS_LAS_ESTRATEGIAS, PatronDetectado

logger = logging.getLogger("signal_agent")


class SignalAgent:

    def __init__(self, market_agent, params: dict = None):
        self._market  = market_agent
        self._params  = params or PARAMS
        self._running = False

        # Cooldown de ejecución: evita operar el mismo par dos veces seguidas
        self._cooldown: dict = {}
        # Cooldown de detección: evita reevaluar la misma vela 90 veces en el loop de 10s
        self._detection_cooldown: dict = {}

        self._suscriptores_senal: list = []
        self._estrategias = TODAS_LAS_ESTRATEGIAS

        logger.info(
            f"SignalAgent v16 (patrón puro) | "
            f"{len(self._estrategias)} estrategias cargadas: "
            f"{[e.nombre for e in self._estrategias]}"
        )

    def suscribir_señal(self, cb):
        self._suscriptores_senal.append(cb)

    # ── Loop principal ────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("SignalAgent: esperando 5 min para datos frescos...")
        await asyncio.sleep(300)
        logger.info("SignalAgent: iniciando evaluación de patrones")

        while self._running:
            for par in self._params.get("pares_activos", []):
                try:
                    senal = await self.evaluar(par)
                    if senal:
                        logger.info(
                            f"SEÑAL | {par} {senal['dir'].upper()} | "
                            f"{senal['estrategia']} | {senal['razon']}"
                        )
                        for cb in self._suscriptores_senal:
                            try:
                                await cb(senal)
                            except Exception as e:
                                logger.warning(f"Error en suscriptor de señal: {e}")
                except Exception as e:
                    logger.warning(f"Error evaluando {par}: {e}")

            # Pares en observación: detectar pero no operar
            for par in self._params.get("pares_observacion", []):
                try:
                    await self._evaluar_observacion(par)
                except Exception as e:
                    logger.debug(f"Error observando {par}: {e}")

            await asyncio.sleep(10)

    # ── Evaluación principal ──────────────────────────────────────────────────

    async def evaluar(self, par: str) -> Optional[dict]:
        """
        Evalúa un par y retorna una señal si hay patrón válido.
        Flujo: cooldowns → sesión → datos frescos → medir régimen (ADX/H4) →
               patrones → filtro de régimen POR TIPO (trend/reversion) → señal
        """
        # ── Pausa manual (/pausar en Telegram) ────────────────────────────────
        if self._params.get("pausado"):
            return None

        # ── Cooldown de ejecución ─────────────────────────────────────────────
        if not self._filtro_cooldown(par):
            return None

        # ── Cooldown de detección: una evaluación por vela del timeframe activo
        tf = self._params.get("signal_timeframe", "M15")
        det_mins = 55 if tf == "H1" else 14   # H1=55min, M15=14min
        ultimo_det = self._detection_cooldown.get(par)
        if ultimo_det and (datetime.utcnow() - ultimo_det) < timedelta(minutes=det_mins):
            return None

        # ── Filtros rápidos ───────────────────────────────────────────────────
        if not self._filtro_sesion():
            return None
        if not self._market.datos_frescos(par):
            return None

        # Usar H1 o M15 según signal_timeframe configurado
        # NOTA: get_df() retorna velas M1 (monitoreo); para señales M15 usar get_df_m15()
        tf = self._params.get("signal_timeframe", "M15")
        if tf == "H1":
            df = self._market.get_df_h1(par, n=80)
        else:
            df = self._market.get_df_m15(par, n=80)
        if df is None or len(df) < 20:
            return None

        # ── Régimen de mercado (se mide, no se filtra aún) ──────────────────────
        # Fase 2h: los filtros de régimen se aplican POR TIPO de estrategia
        # después de detectar patrones. El pre-filtro global anterior
        # (ADX-min + H4-sin-rango) mataba al 100% las estrategias de reversión
        # (RSI_Bollinger, Hammer, Doji, RSI_Divergence), que necesitan rango.
        adx_val = float(df["ADX_14"].iloc[-1] or 0) if "ADX_14" in df.columns else 0.0
        per_strat = self._params.get("per_strategy", {})

        # ── Filtro H4: solo se mide aquí ───────────────────────────────────────
        h4_tendencia = "rango"
        if self._params.get("filtro_h4_activo", False):
            h4_tendencia = self._market.tendencia_h4(par)
            logger.debug(f"[H4] {par} tendencia={h4_tendencia}")

        # ── Detección de patrones ─────────────────────────────────────────────
        patrones = self._detectar_patrones(df, h4_tendencia, par=par)
        self._detection_cooldown[par] = datetime.utcnow()  # marcar vela evaluada

        if not patrones:
            logger.debug(f"[Patrones] {par}: sin setup")
            return None

        patron = patrones[0]
        nombres = [p.nombre for p in patrones]
        logger.debug(f"[Patrones] {par}: {nombres}")

        # ── Filtro de régimen por tipo de estrategia (fase 2h) ─────────────────
        # trend: ADX >= min + H4 con tendencia + dirección alineada (lógica previa)
        # reversion: ADX <= max (necesita rango; el pre-filtro global anterior
        #            las mataba al 100%)
        # NOTA: `tipo` vive en la CLASE estrategia, no en PatronDetectado
        strat_cfg = per_strat.get(patron.nombre, {})
        tipo = next((e.tipo for e in self._estrategias if e.nombre == patron.nombre),
                    "trend") or "trend"
        if tipo == "reversion":
            adx_max = float(strat_cfg.get("adx_max",
                            self._params.get("adx_max_reversion", 25)))
            if adx_val >= adx_max:
                logger.info(
                    f"[ADX] {par} {patron.nombre} ADX={adx_val:.1f} >= {adx_max:.0f} "
                    f"— skip (reversión necesita rango)"
                )
                return None
        else:  # trend (default, comportamiento previo)
            adx_min = float(strat_cfg.get("adx_min_operar",
                            self._params.get("adx_min_operar", 14)))
            if adx_val < adx_min:
                logger.info(f"[ADX] {par} ADX={adx_val:.1f} < {adx_min:.0f} — skip")
                return None
            if self._params.get("filtro_h4_activo", False):
                if h4_tendencia == "rango":
                    return None  # sin tendencia H4 → no operar (lógica previa)
                alineado = (
                    (h4_tendencia == "up"   and patron.dir_hint == "long") or
                    (h4_tendencia == "down" and patron.dir_hint == "short")
                )
                if not alineado:
                    logger.info(
                        f"[H4] {par} {patron.nombre} {patron.dir_hint.upper()} "
                        f"rechazada — H4={h4_tendencia.upper()}"
                    )
                    return None

        # ── Construir señal ───────────────────────────────────────────────────
        u       = df.iloc[-1]
        precio  = float(u.get("Close", 0))
        atr     = float(u.get("ATR_14", 0) or 0)
        # strat_cfg ya definido arriba (filtro de régimen por tipo)
        rr      = float(strat_cfg.get("rr_ratio",     self._params.get("rr_ratio", 2.0)))
        sl_mult = float(strat_cfg.get("sl_atr_mult",  self._params.get("sl_atr_mult", 1.5)))
        min_conf = float(strat_cfg.get("min_confidence", self._params.get("min_confidence", 0.72)))

        if patron.dir_hint == "long":
            sl = precio - atr * sl_mult
            tp = precio + atr * sl_mult * rr
        else:
            sl = precio + atr * sl_mult
            tp = precio - atr * sl_mult * rr

        senal = {
            "par":        par,
            "dir":        patron.dir_hint,
            "estrategia": patron.nombre,
            "conf":       min_conf,
            "entry":      round(precio, 5),
            "sl":         round(sl, 5),
            "tp":         round(tp, 5),
            "atr":        atr,
            "rr":         rr,
            "h4":         h4_tendencia,
            "adx":        round(adx_val, 1),
            "razon":      patron.descripcion[:80],
        }

        self._cooldown[par] = datetime.utcnow()
        return senal

    # ── Pares en observación ──────────────────────────────────────────────────

    async def _evaluar_observacion(self, par: str) -> None:
        """Evalúa pares en observación: detecta pero no opera. Solo loggea."""
        ultimo_det = self._detection_cooldown.get(f"obs_{par}")
        if ultimo_det and (datetime.utcnow() - ultimo_det) < timedelta(minutes=14):
            return

        if not self._filtro_sesion():
            return
        if not self._market.datos_frescos(par):
            return

        tf = self._params.get("signal_timeframe", "M15")
        df = self._market.get_df_h1(par, n=80) if tf == "H1" else self._market.get_df_m15(par, n=80)
        if df is None or len(df) < 20:
            return

        h4_tendencia = "rango"
        if self._params.get("filtro_h4_activo", False):
            h4_tendencia = self._market.tendencia_h4(par)

        patrones = self._detectar_patrones(df, h4_tendencia, par=par)
        self._detection_cooldown[f"obs_{par}"] = datetime.utcnow()

        if patrones:
            patron = patrones[0]
            logger.info(
                f"[OBS] {par} {patron.nombre} {patron.dir_hint.upper()} "
                f"H4={h4_tendencia} ADX={df['ADX_14'].iloc[-1]:.1f}"
            )
            self._log_observacion(par, patron, df, h4_tendencia)

    # ── Detección de patrones ─────────────────────────────────────────────────

    def _detectar_patrones(self, df, h4_tendencia: str, par: str = "") -> list:
        """
        Corre cada estrategia activa sobre el DataFrame.
        Estrategias pausadas van a shadow log (sin operar).
        """
        activas  = self._params.get("estrategias_activas", [])
        pausadas = self._params.get("estrategias_pausadas", [])
        per_strat_cfg = self._params.get("per_strategy", {})
        patrones_activos: list = []

        if len(df) < 10:
            return patrones_activos

        for estrategia in self._estrategias:
            params_est = {**self._params, "_par_actual": par}
            if estrategia.nombre in per_strat_cfg:
                params_est.update(per_strat_cfg[estrategia.nombre])

            try:
                patron = estrategia.detectar(df, params_est)
            except Exception as exc:
                logger.debug(f"[{estrategia.nombre}] error en detectar: {exc}")
                continue

            if patron is None:
                continue

            if estrategia.nombre in activas:
                patrones_activos.append(patron)
                logger.debug(
                    f"[{estrategia.nombre}] ✓ {par}: "
                    f"{patron.dir_hint} — {patron.descripcion[:60]}"
                )
            elif estrategia.nombre in pausadas:
                estrategia.log_shadow(
                    par=par, patron=patron, df=df,
                    h4_tendencia=h4_tendencia, params=self._params,
                )

        return patrones_activos

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _filtro_cooldown(self, par: str) -> bool:
        ultimo = self._cooldown.get(par)
        if not ultimo:
            return True
        mins = self._params.get("cooldown_minutes", 15)
        return (datetime.utcnow() - ultimo) > timedelta(minutes=mins)

    def _filtro_sesion(self) -> bool:
        sesion  = self._market.sesion_actual()
        activas = self._params.get("sesiones_activas", ["london", "overlap"])
        return sesion in activas

    def _log_observacion(self, par: str, patron, df, h4: str) -> None:
        """Registra señal de par en observación en logs/observacion_signals.jsonl."""
        try:
            OBS_LOG = Path("logs/observacion_signals.jsonl")
            OBS_LOG.parent.mkdir(parents=True, exist_ok=True)
            u     = df.iloc[-1]
            entry = float(u.get("Close", 0))
            atr   = float(u.get("ATR_14", 0) or 0)
            record = {
                "ts":         datetime.now(timezone.utc).isoformat(),
                "par":        par,
                "dir":        patron.dir_hint,
                "estrategia": patron.nombre,
                "entry":      round(entry, 5),
                "atr":        round(atr, 5),
                "h4":         h4,
                "descripcion": patron.descripcion[:80],
                "modo":       "observacion",
            }
            with OBS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.debug(f"[Obs] error al registrar: {exc}")

    def reload_params(self, new_params: dict) -> None:
        """Hot-reload: aplica nuevos parámetros sin reinicio."""
        self._params = new_params
        logger.info(
            f"SignalAgent reload_params | "
            f"activas={new_params.get('estrategias_activas', [])} | "
            f"pares={new_params.get('pares_activos', [])}"
        )

    def stop(self):
        self._running = False
