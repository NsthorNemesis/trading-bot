"""
agents/signal_agent/signal_agent.py  — v14_reasoner
Arquitectura: 2 agentes paralelos → 1 decisor con razonamiento profundo

  AgenteBriefing      (deepseek-chat)   — análisis técnico + régimen unificado
  AgenteContextoRiesgo (local, 0ms)     — exposición del portfolio sin API
         │                │
         └────────────────┘
                  ↓ paquete consolidado único
        AgenteDecision (deepseek-reasoner)
        Razonamiento profundo sobre el briefing completo.
        Decide: ¿operar? ¿dirección? ¿confianza?

Cambios vs v13:
  - Técnico + Régimen fusionados en un solo AgenteBriefing (1 llamada API en vez de 2)
  - Decisor usa deepseek-reasoner (MODEL_DEEP) para razonamiento profundo
  - Decisor recibe un único paquete consolidado estructurado (no 3 bloques separados)
  - Reducción: 3 llamadas API → 2 llamadas API por evaluación
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_FAST, MODEL_DEEP, PARAMS

try:
    from openai import OpenAI
    _OPENAI_OK = True
except ImportError:
    _OPENAI_OK = False

try:
    from utils.regime_detector import RegimeDetector
    _REGIME_OK = True
except ImportError:
    _REGIME_OK = False

try:
    from utils.economic_calendar import EconomicCalendar
    _CALENDAR_OK = True
except ImportError:
    _CALENDAR_OK = False

try:
    from utils.oanda_sentiment import OandaSentiment
    _SENTIMENT_OK = True
except ImportError:
    _SENTIMENT_OK = False

logger = logging.getLogger("signal_agent")
_REGIME_HIST: dict = {}

# ── Prompts del sistema por agente ─────────────────────────────────────────

_SYS_BRIEFING = """Eres un analista cuantitativo de forex especializado en M15.
Tu tarea: producir un briefing técnico completo del par en 6-8 oraciones que cubra:
1. SETUP TÉCNICO: patrones de velas visibles, dirección dominante del precio,
   confluencia de indicadores (EMA, MACD, RSI, Bollinger). Sé específico y objetivo.
2. RÉGIMEN DE MERCADO: determina si el mercado está en TENDENCIA, RANGO o TRANSICIÓN.
   Basa tu diagnóstico en ADX, alineación de EMAs y amplitud de las velas recientes.
3. CALIDAD DEL SETUP: indica si los factores técnicos y el régimen son compatibles
   para una entrada ahora. Si hay señales contradictorias, menciónalas explícitamente.
No inventes patrones. Si el mercado es ambiguo, dilo claramente."""

_SYS_DECISION = """Eres un gestor de riesgo cuantitativo especializado en forex M15.
Tu tarea: evaluar el briefing de mercado y decidir si hay una oportunidad de ALTA PROBABILIDAD.

Proceso de evaluación:
1. El régimen debe ser TENDENCIA (no rango ni transición) — si no, rechaza.
2. Al menos 2 indicadores técnicos deben estar alineados con la dirección propuesta.
3. El contexto de riesgo no debe mostrar alertas activas que impidan el trade.
4. Asigna confianza: 0.65-0.75 setup normal, 0.75-0.90 setup sólido, >0.90 excepcional.

Principio fundamental: la inacción preserva capital. En caso de duda, conf=0.0 y trade=false.

Al finalizar tu análisis responde EXCLUSIVAMENTE con este JSON válido, sin texto adicional:
{"trade": true/false, "dir": "long"/"short"/null, "conf": 0.0-1.0,
 "razon": "máximo 25 palabras explicando la decisión"}"""


class SignalAgent:

    def __init__(self, market_agent, params: dict = None):
        self._market    = market_agent
        self._params    = params or PARAMS
        self._cooldown: dict = {}
        self._running   = False
        self._regime    = RegimeDetector()   if _REGIME_OK   else None
        self._calendar  = EconomicCalendar() if _CALENDAR_OK else None
        self._sentiment = OandaSentiment(oanda_api=None, modo_backtest=True) if _SENTIMENT_OK else None

        if DEEPSEEK_KEY and _OPENAI_OK:
            self._ds = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)
            logger.info(f"SignalAgent v14: briefing={MODEL_FAST} | decisor={MODEL_DEEP}")
        else:
            self._ds = None
            logger.warning("SignalAgent v14: sin DeepSeek — señales desactivadas")

        self._suscriptores_senal: list = []

    def suscribir_señal(self, cb):
        """Registra un callback que se llama cada vez que hay una señal válida."""
        self._suscriptores_senal.append(cb)

    # ── Loop principal ──────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("SignalAgent: esperando 5 min para datos frescos...")
        await asyncio.sleep(300)
        logger.info("SignalAgent: iniciando evaluacion multi-agente")
        while self._running:
            for par in self._params.get("pares_activos", []):
                try:
                    senal = await self.evaluar(par)
                    if senal:
                        logger.info(
                            f"SENAL | {par} {senal['dir'].upper()} | "
                            f"conf={senal['conf']:.0%} | {senal['razon']}"
                        )
                        for cb in self._suscriptores_senal:
                            try:
                                await cb(senal)
                            except Exception as e:
                                logger.warning(f"Error en suscriptor de señal: {e}")
                except Exception as e:
                    logger.debug(f"Error evaluando {par}: {e}")
            await asyncio.sleep(30)

    # ── Punto de entrada principal ──────────────────────────────────────────

    async def evaluar(self, par: str) -> Optional[dict]:
        """
        Flujo v14:
        1. Filtros rápidos locales (cooldown, sesión, datos frescos, calendario)
        2. Pre-filtro ADX mínimo (evitar mercados muertos)
        3. Lanzar 2 tareas en paralelo:
             - AgenteBriefing (deepseek-chat): análisis técnico + régimen unificado
             - AgenteContextoRiesgo (local):    portfolio + límites sin API
        4. Ensamblar paquete consolidado único
        5. AgenteDecision (deepseek-reasoner): razonamiento profundo → JSON
        6. Ajuste de sentimiento OANDA (opcional)
        """
        # ── Filtros locales (sin coste API) ──────────────────────────────────
        if not self._filtro_cooldown(par):
            return None
        if not self._filtro_sesion():
            return None
        if not self._market.datos_frescos(par):
            return None

        df = self._market.get_df(par, n=80)
        if df is None or len(df) < 30:
            return None

        # ── Calendario económico ─────────────────────────────────────────────
        if self._calendar:
            ts_actual = self._get_timestamp_actual()
            if self._calendar.en_blackout(ts_actual, par):
                logger.debug(f"[Calendario] {par} en blackout")
                return None

        # ── Pre-filtro ADX: no operar en mercados planos ─────────────────────
        adx_val = 0.0
        if "ADX_14" in df.columns:
            adx_val = float(df["ADX_14"].iloc[-1] or 0)
        adx_min = float(self._params.get("adx_min_operar", 14))
        if adx_val < adx_min:
            logger.debug(f"[ADX] {par} ADX={adx_val:.1f} < {adx_min} — mercado plano, skip")
            return None

        # ── Sin DeepSeek: no operar ──────────────────────────────────────────
        if not self._ds:
            return None

        # ── Detectar estrategias activas ─────────────────────────────────────
        patrones_detectados = self._detectar_patrones(df)

        # Pre-gate: solo llama a DeepSeek si hay al menos una estrategia activa
        if not patrones_detectados:
            logger.debug(f"[Estrategias] {par}: sin setup activo — skip DeepSeek")
            return None

        logger.debug(f"[Estrategias] {par}: detectadas={patrones_detectados}")

        # ── Lanzar 2 tareas en paralelo ──────────────────────────────────────
        # BriefIng (API) + Riesgo (local) corren simultáneamente
        try:
            briefing_txt, riesgo_txt = await asyncio.wait_for(
                asyncio.gather(
                    self._agente_briefing(par, df, patrones_detectados),
                    self._agente_riesgo(par),
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[v14] {par}: timeout en briefing")
            return None
        except Exception as e:
            logger.warning(f"[v14] {par}: error en briefing — {e}")
            return None

        # ── Agente decisor (deepseek-reasoner) ──────────────────────────────
        senal = await self._agente_decision(par, df, briefing_txt, riesgo_txt, patrones_detectados)
        if not senal:
            return None

        # ── Ajuste de sentimiento OANDA ──────────────────────────────────────
        if self._sentiment:
            senal["conf"] = self._sentiment.ajustar_confianza(
                senal["conf"], par, senal["dir"]
            )
            if senal["conf"] < float(self._params.get("min_confidence", 0.55)):
                return None

        self._cooldown[par] = datetime.utcnow()
        return senal

    # ── Agente 1: Briefing unificado (técnico + régimen) ───────────────────────

    async def _agente_briefing(self, par: str, df, patrones_detectados: list = None) -> str:
        """
        Fusión de análisis técnico y de régimen en una sola llamada API.
        Produce un briefing estructurado de 6-8 oraciones que el decisor
        puede consumir como paquete completo.
        """
        u = df.iloc[-1]

        # Últimas 20 velas
        velas_data = []
        for _, row in df.tail(20).iterrows():
            ts = str(row.get("Timestamp", row.name))[:16]
            op = float(row.get("Open",  0))
            hi = float(row.get("High",  0))
            lo = float(row.get("Low",   0))
            cl = float(row.get("Close", 0))
            velas_data.append(f"{ts}  O:{op:.5f}  H:{hi:.5f}  L:{lo:.5f}  C:{cl:.5f}")
        velas_str = "\n".join(velas_data)

        # Indicadores
        rsi   = float(u.get("RSI_14",   50) or 50)
        atr   = float(u.get("ATR_14",    0) or 0)
        ema20 = float(u.get("EMA_20",    0) or 0)
        ema50 = float(u.get("EMA_50",    0) or 0)
        adx   = float(u.get("ADX_14",    0) or 0)
        macd  = float(u.get("MACD_DIF",  0) or 0)
        bbl   = next((float(u[c] or 0) for c in df.columns if "BBL" in c), 0)
        bbu   = next((float(u[c] or 0) for c in df.columns if "BBU" in c), 0)
        precio = float(u.get("Close", 0))
        cierres = [f"{x:.5f}" for x in df["Close"].tail(10).tolist()]
        atrs    = [f"{x:.5f}" for x in df["ATR_14"].dropna().tail(5).tolist()] \
                  if "ATR_14" in df.columns else []

        # Score de régimen local (cross-check sin LLM)
        regime_score = 0.5
        if self._regime:
            try:
                regime_score = self._regime.calcular_regime_score(df)
            except Exception:
                pass

        # Estrategias detectadas por pre-filtro técnico
        estrategias_str = ", ".join(patrones_detectados) if patrones_detectados else "ninguna"

        prompt = (
            f"Par: {par} | Timeframe: M15\n\n"
            f"── SETUP DETECTADO POR PRE-FILTRO ──\n"
            f"  Estrategias activas en esta vela: {estrategias_str}\n\n"
            f"── VELAS (últimas 15) ──\n{velas_str}\n\n"
            f"── INDICADORES ACTUALES ──\n"
            f"  Precio={precio:.5f}  ATR={atr:.5f}  ADX={adx:.1f}\n"
            f"  EMA20={ema20:.5f}  EMA50={ema50:.5f}  "
            f"  (EMA20 {'SOBRE' if ema20>ema50 else 'BAJO'} EMA50, spread={abs(ema20-ema50):.5f})\n"
            f"  RSI={rsi:.1f}  MACD_hist={macd:.5f}\n"
            f"  BB_low={bbl:.5f}  BB_high={bbu:.5f}\n"
            f"  Últimos 10 cierres: {', '.join(cierres)}\n"
            f"  Últimos 5 ATR: {', '.join(atrs) if atrs else 'N/A'}\n"
            f"  Score régimen (0=rango, 1=tendencia): {regime_score:.2f}\n\n"
            f"Valida si el setup detectado ({estrategias_str}) es sólido. "
            f"Produce el briefing técnico completo: setup, régimen y calidad."
        )

        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self._ds.chat.completions.create(
                    model=MODEL_FAST,
                    messages=[
                        {"role": "system", "content": _SYS_BRIEFING},
                        {"role": "user",   "content": prompt},
                    ],
                    max_tokens=320,
                    temperature=0.2,
                )
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"[Error briefing: {e}]"

    # ── Agente 2: Contexto de Riesgo (local, sin LLM) ───────────────────────

    async def _agente_riesgo(self, par: str) -> str:
        """
        Recopila estado del portfolio: posiciones abiertas, P&L del día,
        drawdown, cooldowns. Devuelve resumen de texto estructurado.
        """
        try:
            from config.settings import TRADES_LOG
            import json as _json

            lineas = []
            # Posición actual del par
            cooldown_activo = par in self._cooldown
            mins_desde_ult  = None
            if cooldown_activo:
                mins_desde_ult = int(
                    (datetime.utcnow() - self._cooldown[par]).total_seconds() / 60
                )

            # Intentar leer últimos trades del log
            trades_hoy = []
            try:
                raw = _json.loads(TRADES_LOG.read_text(encoding="utf-8"))
                hoy = datetime.utcnow().date().isoformat()
                trades_hoy = [
                    t for t in (raw if isinstance(raw, list) else [])
                    if str(t.get("timestamp", ""))[:10] == hoy
                ]
            except Exception:
                pass

            wins_hoy  = sum(1 for t in trades_hoy if t.get("resultado") == "win")
            loss_hoy  = sum(1 for t in trades_hoy if t.get("resultado") == "loss")
            pnl_hoy   = sum(float(t.get("pnl", 0)) for t in trades_hoy)
            total_hoy = len(trades_hoy)

            max_dd_dia = float(self._params.get("max_drawdown_dia", 0.05))
            max_trades = int(self._params.get("max_posiciones", 5))

            lineas.append(f"Trades hoy: {total_hoy} ({wins_hoy}W/{loss_hoy}L) | P&L={pnl_hoy:+.2f}$")
            lineas.append(f"Límites: max_posiciones={max_trades} | max_DD_dia={max_dd_dia:.0%}")
            if mins_desde_ult is not None:
                lineas.append(f"Último trade {par}: hace {mins_desde_ult} minutos")
            else:
                lineas.append(f"Sin trades recientes en {par}")

            # Advertencias
            if loss_hoy >= int(self._params.get("max_consecutive_losses", 3)):
                lineas.append("⚠ ALERTA: Racha de pérdidas — operar con extrema cautela")
            if total_hoy >= max_trades:
                lineas.append("⚠ ALERTA: Límite de posiciones diarias alcanzado")

            return "\n".join(lineas)

        except Exception as e:
            return f"Contexto de riesgo no disponible ({e})"

    # ── Agente Decisor (deepseek-reasoner) ─────────────────────────────────

    async def _agente_decision(
        self, par: str, df,
        briefing_txt: str, riesgo_txt: str,
        patrones_detectados: list = None
    ) -> Optional[dict]:
        """
        Recibe el paquete consolidado (briefing + riesgo) y toma la decisión final.
        Usa deepseek-reasoner (MODEL_DEEP) para razonamiento profundo.
        El reasoner piensa internamente antes de producir el JSON final.
        """
        u        = df.iloc[-1]
        precio   = float(u.get("Close", 0))
        atr_val  = float(u.get("ATR_14", 0) or 0)
        sesion   = self._market.sesion_actual()
        adx_val  = float(u.get("ADX_14", 0) or 0)

        min_conf = float(self._params.get("min_confidence", 0.55))
        rr       = float(self._params.get("rr_ratio", 2.0))

        # Paquete consolidado único — el reasoner recibe todo en un solo bloque
        prompt = (
            f"╔═══ SOLICITUD DE DECISIÓN — {par} ═══╗\n\n"
            f"METADATA\n"
            f"  Par       : {par}\n"
            f"  Sesión    : {sesion.upper()}\n"
            f"  Precio    : {precio:.5f}\n"
            f"  ATR       : {atr_val:.5f}\n"
            f"  ADX       : {adx_val:.1f}\n"
            f"  RR target : {rr}\n"
            f"  Conf min  : {min_conf:.0%}\n\n"
            f"BRIEFING DE MERCADO (técnico + régimen)\n"
            f"{'─'*45}\n"
            f"{briefing_txt}\n\n"
            f"CONTEXTO DE RIESGO (portfolio actual)\n"
            f"{'─'*45}\n"
            f"{riesgo_txt}\n\n"
            f"╚═══════════════════════════════════════╝\n\n"
            f"Analiza el briefing completo y decide si operar {par} ahora.\n"
            f"Responde con JSON válido al final."
        )

        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self._ds.chat.completions.create(
                    model=MODEL_DEEP,          # deepseek-reasoner — razonamiento profundo
                    messages=[
                        {"role": "system", "content": _SYS_DECISION},
                        {"role": "user",   "content": prompt},
                    ],
                    max_tokens=1200,           # espacio para thinking + JSON final
                    temperature=0.0,           # máxima determinismo en decisiones de riesgo
                )
            )
        except Exception as e:
            logger.warning(f"[Decisor-R1] {par} error API: {e}")
            return None

        content = (resp.choices[0].message.content or "").strip()

        # El reasoner puede incluir texto de razonamiento antes del JSON
        # Extraer el último bloque JSON del contenido
        import re as _re
        json_matches = _re.findall(r'\{[^{}]*"trade"[^{}]*\}', content, _re.DOTALL)
        if not json_matches:
            logger.debug(f"[Decisor-R1] {par} JSON no encontrado: {content[:120]}")
            return None
        try:
            resultado = json.loads(json_matches[-1])  # usar el último (la decisión final)
        except Exception:
            logger.debug(f"[Decisor-R1] {par} JSON inválido: {json_matches[-1][:80]}")
            return None

        if not resultado.get("trade", False):
            logger.debug(
                f"[Decisor] {par} PASS — {resultado.get('razon','sin razón')}"
            )
            return None

        conf = float(resultado.get("conf", 0))
        if conf < min_conf:
            logger.debug(f"[Decisor] {par} conf={conf:.0%} < {min_conf:.0%} — descartado")
            return None

        direction = resultado.get("dir", "")
        if direction not in ("long", "short"):
            return None

        logger.info(
            f"[Decisor] {par} TRADE {direction.upper()} conf={conf:.0%} — "
            f"{resultado.get('razon','')}"
        )
        # Etiquetar estrategia con patrones detectados localmente
        patrones = patrones_detectados if patrones_detectados else []
        if patrones:
            estrategia_tag = "+".join(patrones)
        else:
            estrategia_tag = "MultiAgente_v14_reasoner"

        return {
            "par":         par,
            "dir":         direction,
            "conf":        round(conf, 3),
            "entry":       precio,
            "razon":       resultado.get("razon", ""),
            "estrategia":  estrategia_tag,
            "patrones":    patrones,
            "atr":         atr_val,
            "regime_score": 0.5,
        }

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _detectar_patrones(self, df) -> list:
        """
        Detecta qué estrategias activas tienen sus condiciones técnicas cumplidas.
        Cada estrategia tiene reglas propias bien definidas.
        Solo retorna estrategias que están en 'estrategias_activas'.
        """
        activas  = self._params.get("estrategias_activas", [])
        patrones = []
        try:
            if len(df) < 10:
                return patrones

            u     = df.iloc[-1]
            prev  = df.iloc[-2]
            close = float(u.get("Close", 0))
            open_ = float(u.get("Open",  0))
            high  = float(u.get("High",  0))
            low   = float(u.get("Low",   0))
            rsi   = float(u.get("RSI_14", 50) or 50)
            adx   = float(u.get("ADX_14",  0) or 0)
            ema20 = float(u.get("EMA_20",  0) or 0)
            ema50 = float(u.get("EMA_50",  0) or 0)
            ema9  = float(u.get("EMA_9",   0) or 0)
            bbl   = next((float(u[c] or 0) for c in df.columns if "BBL" in c), 0)
            bbu   = next((float(u[c] or 0) for c in df.columns if "BBU" in c), 0)
            cuerpo     = abs(close - open_)
            rango      = high - low or 1e-9
            sombra_inf = min(open_, close) - low
            sombra_sup = high - max(open_, close)

            # ── EMA_Crossover ─────────────────────────────────────────────────
            # Regla: EMA9 cruza EMA50 en últimas 3 velas + ADX > 20
            if "EMA_Crossover" in activas and "EMA_9" in df.columns and "EMA_50" in df.columns:
                ema9_arr  = df["EMA_9"].values
                ema50_arr = df["EMA_50"].values
                cruce = any(
                    (ema9_arr[i-1] <= ema50_arr[i-1] and ema9_arr[i] > ema50_arr[i]) or
                    (ema9_arr[i-1] >= ema50_arr[i-1] and ema9_arr[i] < ema50_arr[i])
                    for i in range(-3, 0) if i-1 >= -len(ema9_arr)
                )
                if cruce and adx >= 20:
                    patrones.append("EMA_Crossover")

            # ── Engulfing ─────────────────────────────────────────────────────
            # Regla: vela actual engulle completamente la anterior + alineada con tendencia
            if "Engulfing" in activas:
                prev_close = float(prev.get("Close", 0))
                prev_open  = float(prev.get("Open",  0))
                prev_cuerpo = abs(prev_close - prev_open)
                bull_engulf = (
                    close > open_ and           # vela alcista
                    prev_close < prev_open and  # anterior bajista
                    close > prev_open and       # cierre > apertura anterior
                    open_ < prev_close and      # apertura < cierre anterior
                    cuerpo >= prev_cuerpo * 0.9
                )
                bear_engulf = (
                    close < open_ and           # vela bajista
                    prev_close > prev_open and  # anterior alcista
                    close < prev_open and       # cierre < apertura anterior
                    open_ > prev_close and      # apertura > cierre anterior
                    cuerpo >= prev_cuerpo * 0.9
                )
                if bull_engulf or bear_engulf:
                    patrones.append("Engulfing")

            # ── Hammer ────────────────────────────────────────────────────────
            # Regla: sombra inf >= 2x cuerpo + sombra sup <= 0.5x cuerpo
            #        + en downtrend (EMA20 < EMA50) + RSI < 45
            if "Hammer" in activas:
                es_hammer = (
                    cuerpo > 0 and
                    sombra_inf >= 2 * cuerpo and
                    sombra_sup <= cuerpo * 0.5 and
                    ema20 < ema50 and            # downtrend
                    rsi < 45                     # no sobrecomprado
                )
                if es_hammer:
                    patrones.append("Hammer")

            # ── Doji ──────────────────────────────────────────────────────────
            # Regla: cuerpo < 10% del rango + RSI extremo (< 33 o > 67)
            if "Doji" in activas:
                es_doji = (
                    cuerpo / rango < 0.10 and
                    (rsi < 33 or rsi > 67)
                )
                if es_doji:
                    patrones.append("Doji")

            # ── RSI_Bollinger ─────────────────────────────────────────────────
            # Regla: RSI < 30 + precio ≤ BB inferior  ó  RSI > 70 + precio ≥ BB superior
            if "RSI_Bollinger" in activas and bbl > 0 and bbu > 0:
                rsi_boll = (
                    (rsi < 30 and close <= bbl * 1.001) or
                    (rsi > 70 and close >= bbu * 0.999)
                )
                if rsi_boll:
                    patrones.append("RSI_Bollinger")

            # ── RSI_Divergence ────────────────────────────────────────────────
            # Regla: precio hace nuevo mínimo/máximo pero RSI NO confirma (divergencia)
            if "RSI_Divergence" in activas and "RSI_14" in df.columns and len(df) >= 15:
                closes_rec = df["Close"].iloc[-8:].values
                rsi_rec    = df["RSI_14"].iloc[-8:].values
                # Divergencia alcista: precio mínimo más bajo, RSI mínimo más alto
                div_bull = (
                    closes_rec[-1] < closes_rec[:-1].min() * 1.001 and
                    rsi_rec[-1]    > rsi_rec[:-1].min()    * 1.02  and
                    rsi < 45
                )
                # Divergencia bajista: precio máximo más alto, RSI máximo más bajo
                div_bear = (
                    closes_rec[-1] > closes_rec[:-1].max() * 0.999 and
                    rsi_rec[-1]    < rsi_rec[:-1].max()    * 0.98  and
                    rsi > 55
                )
                if div_bull or div_bear:
                    patrones.append("RSI_Divergence")

        except Exception as e:
            logger.debug(f"[Patrones] error detectando: {e}")

        return patrones

    def _filtro_cooldown(self, par: str) -> bool:
        ultimo = self._cooldown.get(par)
        if not ultimo:
            return True
        mins = self._params.get("cooldown_minutes", 15)
        return (datetime.utcnow() - ultimo) > timedelta(minutes=mins)

    def _filtro_sesion(self) -> bool:
        sesion  = self._market.sesion_actual()
        activas = self._params.get("sesiones_activas", ["london", "overlap", "new_york"])
        return sesion in activas

    def _get_timestamp_actual(self):
        try:
            import backtest_harness as _bh
            ts = _bh.get_fake_time()
            if ts:
                return ts
        except Exception:
            pass
        return datetime.utcnow()

    def stop(self):
        self._running = False
