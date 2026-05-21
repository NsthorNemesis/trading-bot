"""
agents/signal_agent/signal_agent.py
════════════════════════════════════════════════════════════════
SIGNAL AGENT — Restaurado desde backup_20260509_2240

Flujo de 3 filtros (del más barato al más caro):
  1. Python puro:    cooldown, sesión, parámetros calibrados
  2. Pre-filtro:     indicadores técnicos sin LLM (_prefiltro_tecnico)
  3. DeepSeek Flash: razonamiento final con contexto completo

Solo llama a DeepSeek si pasó los 2 primeros filtros.
Usa formato TONL compacto para reducir tokens del prompt.

DIFERENCIAS vs v13 (signal_agent_v13_aifirst.py):
  - _prefiltro_tecnico() en lugar de plugins de estrategia + ensemble voting
  - Timeframe M1 por defecto (get_df, no get_df_m15)
  - Prompt DeepSeek más compacto (sin régimen, sin H4, sin TF)
  - min_confidence=0.35 (más señales llegan a DeepSeek)
  - Sin filtro de calendario, sin filtro de régimen ADX, sin H4
════════════════════════════════════════════════════════════════
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import (
    DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_FAST, PARAMS,
)

logger = logging.getLogger("signal_agent")


class SignalAgent:
    """
    Detecta señales de trading usando 3 filtros en cascada.
    Arquitectura fiel al backup_20260509_2240.
    """

    def __init__(self, market_agent, params: dict = None):
        self._market   = market_agent
        self._params   = params or PARAMS
        self._cooldown: dict[str, datetime] = {}
        self._running  = False

        # Cliente DeepSeek
        if DEEPSEEK_KEY:
            self._ds = OpenAI(
                api_key  = DEEPSEEK_KEY,
                base_url = DEEPSEEK_BASE_URL,
            )
            logger.info(f"SignalAgent: DeepSeek {MODEL_FAST} activo")
        else:
            self._ds = None
            logger.warning("SignalAgent: sin DeepSeek — modo solo indicadores")

    # ── LOOP PRINCIPAL ────────────────────────────────────────────────────────

    async def run(self):
        """Evalúa todos los pares cada 30 segundos."""
        self._running = True
        logger.info("SignalAgent: iniciando loop de evaluación")
        logger.info("SignalAgent: esperando 5 min para datos frescos...")
        await asyncio.sleep(300)
        logger.info("SignalAgent: iniciando evaluación de señales")

        while self._running:
            for par in self._params.get("pares_activos", []):
                try:
                    senal = await self.evaluar(par)
                    if senal:
                        logger.info(
                            f"SEÑAL DETECTADA | {par} {senal['dir'].upper()} | "
                            f"conf={senal['conf']:.0%} | {senal['razon']}"
                        )
                except Exception as e:
                    logger.debug(f"Error evaluando {par}: {e}")
            await asyncio.sleep(30)

    # ── EVALUACIÓN ────────────────────────────────────────────────────────────

    async def evaluar(self, par: str) -> Optional[dict]:
        """
        Evalúa un par y retorna señal o None.
        Retorna: {"par", "dir", "conf", "entry", "razon", "estrategia", "atr"}
        """
        # ── FILTRO 1: Python puro (sin LLM) ──────────────────────────────────
        if not self._filtro_cooldown(par):
            return None
        if not self._filtro_sesion():
            return None
        if not self._market.datos_frescos(par):
            return None

        # ── OBTENER DataFrame con indicadores (M1) ────────────────────────────
        df = self._market.get_df(par, n=50)
        if df is None or len(df) < 30:
            return None

        # ── FILTRO 2: Pre-filtro técnico (sin LLM) ────────────────────────────
        presenal = self._prefiltro_tecnico(df, par)
        if not presenal:
            return None

        # ── FILTRO 3: DeepSeek Flash (solo si pasó los 2 anteriores) ──────────
        if self._ds:
            return await self._consultar_deepseek(par, df, presenal)
        else:
            # Fallback sin DeepSeek — usar solo indicadores técnicos
            conf = presenal.get("conf", 0)
            if conf >= self._params.get("min_confidence", 0.35):
                self._cooldown[par] = datetime.utcnow()
                return presenal
            return None

    # ── FILTROS PYTHON PURO ───────────────────────────────────────────────────

    def _filtro_cooldown(self, par: str) -> bool:
        """Evita señales repetidas en el mismo par."""
        ultimo = self._cooldown.get(par)
        if not ultimo:
            return True
        mins = self._params.get("cooldown_minutes", 15)
        return (datetime.utcnow() - ultimo) > timedelta(minutes=mins)

    def _filtro_sesion(self) -> bool:
        """Solo opera en sesiones activas."""
        sesion  = self._market.sesion_actual()
        activas = self._params.get("sesiones_activas", ["london", "overlap", "new_york"])
        return sesion in activas

    def _prefiltro_tecnico(self, df, par: str) -> Optional[dict]:
        """
        Pre-filtro matemático rápido sin LLM.
        Detecta condiciones básicas para una señal candidata.
        Retorna dict con datos o None si no hay señal.

        Prioridad: Hammer > Doji > Engulfing > RSI_Bollinger > RSI_Divergence
        Solo una señal por evaluación — DeepSeek hace el juicio final.
        """
        u = df.iloc[-1]

        rsi_col = "RSI_14"
        atr_col = "ATR_14"
        ema20   = "EMA_20"
        ema50   = "EMA_50"
        bbl     = [c for c in df.columns if c.startswith("BBL_")]
        bbu     = [c for c in df.columns if c.startswith("BBU_")]
        hammer  = [c for c in df.columns if "HAMMER" in c.upper()]
        doji    = [c for c in df.columns if "DOJI" in c.upper()]
        engulf  = [c for c in df.columns if "ENGULF" in c.upper()]

        rsi_val  = u.get(rsi_col, 50) if rsi_col in df.columns else 50
        atr_val  = u.get(atr_col, 0)  if atr_col in df.columns else 0
        ema20_v  = u.get(ema20, 0)    if ema20 in df.columns else 0
        ema50_v  = u.get(ema50, 0)    if ema50 in df.columns else 0
        precio   = float(u.get("Close", 0))
        tendencia = "up" if ema20_v > ema50_v else "down"

        hay_hammer = bool(hammer and u.get(hammer[0], 0) != 0)
        hay_doji   = bool(doji   and u.get(doji[0],   0) != 0)
        hay_engulf = bool(engulf and u.get(engulf[0],  0) != 0)

        rsi_sobre = rsi_val > 68
        rsi_bajo  = rsi_val < 32

        precio_bbl = precio < float(u.get(bbl[0], 0)) * 1.002 if bbl else False
        precio_bbu = precio > float(u.get(bbu[0], 0)) * 0.998 if bbu else False

        # Condiciones de señal
        senal_long = (
            (hay_hammer and tendencia == "down") or
            (hay_doji   and rsi_bajo) or
            (rsi_bajo   and precio_bbl) or
            (hay_engulf and tendencia == "up")
        )
        senal_short = (
            (hay_doji  and rsi_sobre) or
            (rsi_sobre and precio_bbu)
        )

        if not senal_long and not senal_short:
            return None

        # Determinar estrategia y dirección (prioridad fija)
        if senal_long:
            dir_ = "long"
            if hay_hammer:
                estrat = "Hammer"
            elif hay_doji:
                estrat = "Doji"
            elif hay_engulf:
                estrat = "Engulfing"
            elif rsi_bajo:
                estrat = "RSI_Bollinger" if precio_bbl else "RSI_Divergence"
            else:
                estrat = "RSI_Divergence"
        else:
            dir_  = "short"
            estrat = "Doji" if hay_doji else "RSI_Bollinger"

        # Verificar que la estrategia está activa
        if estrat not in self._params.get("estrategias_activas", []):
            return None

        return {
            "par":        par,
            "dir":        dir_,
            "conf":       0.45,   # confianza base — DeepSeek la ajustará
            "entry":      precio,
            "razon":      f"{estrat} | RSI={rsi_val:.1f} | tendencia={tendencia}",
            "estrategia": estrat,
            "atr":        atr_val,
        }

    # ── DEEPSEEK FLASH ────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    async def _consultar_deepseek(self, par: str, df, presenal: dict) -> Optional[dict]:
        """
        Consulta DeepSeek Flash con contexto TONL optimizado.
        Solo se llama si el pre-filtro técnico ya aprobó la señal.
        """
        u       = df.iloc[-1]
        rsi_val = u.get("RSI_14", 50)
        atr_val = u.get("ATR_14", 0)
        ema20   = u.get("EMA_20", 0)
        ema50   = u.get("EMA_50", 0)
        precio  = float(u.get("Close", 0))
        sesion  = self._market.sesion_actual()

        cierres_str = ",".join(
            f"{x:.5f}" for x in df["Close"].tail(10).tolist()
        )

        wr_min = self._params.get("min_win_rate", 0.38)
        rr     = self._params.get("rr_ratio", 2.0)

        prompt = f"""
Par:{par} Dir:{presenal['dir']} Sesion:{sesion}
RSI:{rsi_val:.1f} ATR:{atr_val:.5f} EMA20:{ema20:.5f} EMA50:{ema50:.5f}
Precio:{precio:.5f}
Patron:{presenal['estrategia']}
Cierres10:{cierres_str}
WR_min:{wr_min:.0%} RR:{rr}

¿Confirmas señal? JSON:
{{"señal":bool,"dir":"long/short","conf":0-1,"razon":"max 15 palabras"}}
"""

        loop     = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._ds.chat.completions.create(
                model           = MODEL_FAST,
                messages        = [{"role": "user", "content": prompt}],
                response_format = {"type": "json_object"},
                max_tokens      = 80,
            )
        )

        _content = (response.choices[0].message.content or "").strip()
        if not _content:
            # Fallback: DeepSeek devolvió vacío — usar conf del pre-filtro
            _conf_fb = float(presenal.get("conf", 0.45))
            if _conf_fb >= self._params.get("min_confidence", 0.35):
                self._cooldown[par] = datetime.utcnow()
                return {
                    "par":        par,
                    "dir":        presenal["dir"],
                    "conf":       _conf_fb,
                    "entry":      precio,
                    "razon":      "tecnico (DS no disponible)",
                    "estrategia": presenal["estrategia"],
                    "atr":        atr_val,
                }
            return None

        try:
            resultado = json.loads(_content)
        except Exception:
            return None

        if (resultado.get("señal") and
                resultado.get("conf", 0) >= self._params.get("min_confidence", 0.35)):
            self._cooldown[par] = datetime.utcnow()
            return {
                "par":        par,
                "dir":        resultado["dir"],
                "conf":       float(resultado["conf"]),
                "entry":      precio,
                "razon":      resultado.get("razon", ""),
                "estrategia": presenal["estrategia"],
                "atr":        atr_val,
            }

        return None

    def stop(self):
        self._running = False
        logger.info("SignalAgent detenido")
