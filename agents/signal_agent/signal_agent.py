"""
agents/signal_agent/signal_agent.py
Pipeline de 5 filtros: cooldown/sesion -> calendario -> plugin strategies
-> regimen ADX -> DeepSeek + sentimiento.

Cambios v11.2 (paquete fin de semana):
  - Ensemble ponderado: multiples estrategias se combinan por consenso de
    direccion + bonus de confianza (+5% por cada estrategia adicional que confirma)
  - OandaSentiment en modo live cuando hay OANDA_TOKEN disponible
  - _evaluar_estrategias ahora detecta conflictos de direccion y los descarta
"""
import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import (
    DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_FAST, PARAMS,
    OANDA_TOKEN, OANDA_ENV,
)

try:
    from strategies import load_strategies
    _STRATEGIES_OK = True
except ImportError as _se:
    _STRATEGIES_OK = False
    logging.getLogger("signal_agent").warning(f"strategies/ no disponible: {_se}")

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


def _es_modo_backtest() -> bool:
    """Detecta si estamos corriendo en backtest (fake time activo)."""
    try:
        import backtest_harness as _bh
        return bool(_bh.get_fake_time())
    except Exception:
        return False


class SignalAgent:

    def __init__(self, market_agent, params: dict = None):
        self._market    = market_agent
        self._params    = params or PARAMS
        self._cooldown: dict = {}
        self._running   = False
        self._regime    = RegimeDetector()   if _REGIME_OK   else None
        self._calendar  = EconomicCalendar() if _CALENDAR_OK else None

        # Sentimiento: modo live si hay token OANDA y no estamos en backtest
        self._sentiment = None
        if _SENTIMENT_OK:
            modo_backtest = _es_modo_backtest() or not OANDA_TOKEN
            if not modo_backtest:
                try:
                    import oandapyV20
                    _oanda_api = oandapyV20.API(
                        access_token=OANDA_TOKEN,
                        environment=OANDA_ENV,
                    )
                    self._sentiment = OandaSentiment(
                        oanda_api=_oanda_api,
                        modo_backtest=False,
                    )
                    logger.info("SignalAgent: OandaSentiment en modo LIVE")
                except Exception as _se:
                    logger.warning(f"SignalAgent: OandaSentiment live error ({_se}) — modo backtest")
                    self._sentiment = OandaSentiment(oanda_api=None, modo_backtest=True)
            else:
                self._sentiment = OandaSentiment(oanda_api=None, modo_backtest=True)
                logger.info("SignalAgent: OandaSentiment en modo BACKTEST")

        # Cargar plugins de estrategias
        self._strategies = {}
        self._reload_strategies()

        # Callback para RiskExecutionAgent
        self._on_senal_ext = None

        if DEEPSEEK_KEY:
            self._ds = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)
            logger.info(f"SignalAgent: DeepSeek {MODEL_FAST} activo")
        else:
            self._ds = None
            logger.warning("SignalAgent: sin DeepSeek — modo solo indicadores")

        logger.info(
            f"SignalAgent: regimen={'ON' if self._regime else 'OFF'} | "
            f"calendario={'ON' if self._calendar else 'OFF'} | "
            f"sentimiento={'ON' if self._sentiment else 'OFF'}"
        )

    # ── Suscripcion de callbacks ──────────────────────────────────────────────

    def suscribir_senal(self, callback) -> None:
        """
        Registra un callback asincrono que se invoca cada vez que
        se detecta una senal valida. Usado por RiskExecutionAgent.
        """
        self._on_senal_ext = callback
        logger.info("SignalAgent: callback de senal registrado")

    # mantener compatibilidad con nombre anterior
    def suscribir_señal(self, callback) -> None:
        return self.suscribir_senal(callback)

    # ── Recarga dinamica ───────────────────────────────────────────────────────

    def reload_params(self, new_params: dict) -> None:
        """
        Actualiza parametros y recarga estrategias activas sin reiniciar el bot.
        Llamado automaticamente por ParamsWatcher cuando strategy_params.json cambia.
        """
        self._params = new_params
        self._reload_strategies()
        logger.info(
            f"SignalAgent: parametros recargados | "
            f"estrategias activas: {sorted(self._strategies.keys())}"
        )

    def _reload_strategies(self) -> None:
        """Carga (o recarga) las estrategias activas desde el paquete strategies/."""
        if not _STRATEGIES_OK:
            logger.warning("SignalAgent: usando prefiltro monolitico (strategies/ no disponible)")
            self._strategies = {}
            return
        activas = self._params.get("estrategias_activas", [])
        new_strategies = load_strategies(active_only=activas)

        # Salvaguarda: si el reload devuelve vacío pero hay estrategias esperadas,
        # mantener las anteriores para que el bot no quede sordo silenciosamente.
        if not new_strategies and activas:
            logger.warning(
                f"ALERTA: reload devolvió estrategias vacías con activas={activas} "
                f"— manteniendo anteriores: {sorted(self._strategies.keys())} "
                f"— reiniciar el bot para resolver"
            )
            return  # Conservar self._strategies sin cambiar

        self._strategies = new_strategies
        logger.info(f"Estrategias cargadas: {sorted(self._strategies.keys())}")

    # ── Loop principal ────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("SignalAgent: esperando 5 min para datos frescos...")
        await asyncio.sleep(300)
        logger.info("SignalAgent: iniciando evaluacion de senales")
        while self._running:
            pares = self._params.get("pares_activos", [])
            for par in pares:
                try:
                    senal = await self.evaluar(par)
                    if senal:
                        logger.info(
                            f"SENAL DETECTADA | {par} {senal['dir'].upper()} | "
                            f"conf={senal['conf']:.0%} | {senal['razon']}"
                        )
                        if self._on_senal_ext is not None:
                            try:
                                await self._on_senal_ext(senal)
                            except Exception as cb_err:
                                logger.error(f"Error en callback de senal: {cb_err}")
                except Exception as e:
                    logger.debug(f"Error evaluando {par}: {e}")
            await asyncio.sleep(30)

    # ── Pipeline de evaluacion ────────────────────────────────────────────────

    async def evaluar(self, par: str) -> Optional[dict]:
        # Filtro 1: cooldown + sesion + datos frescos
        if not self._filtro_cooldown(par):
            return None
        if not self._filtro_sesion():
            return None
        if not self._market.datos_frescos(par):
            return None

        # Obtener DataFrames multi-timeframe
        df_m15 = self._market.get_df_m15(par, n=80)
        if df_m15 is None or len(df_m15) < 20:
            logger.debug(f"[MTF] {par}: M15 sin suficientes velas aun")
            return None
        df_h4 = self._market.get_df_h4(par, n=50)

        # Filtro 2: calendario economico
        if self._calendar:
            ts_actual = self._get_timestamp_actual()
            if self._calendar.en_blackout(ts_actual, par):
                logger.debug(f"[Calendario] {par} en blackout")
                return None

        # Filtro 3: plugins de estrategias (ensemble)
        presenal = self._evaluar_estrategias(df_m15, par)
        if not presenal:
            return None

        # Filtro 3.5: confirmacion de tendencia H4 (solo EMA_Crossover)
        tendencia_h4 = self._market.tendencia_h4(par)
        if not self._filtro_tendencia_h4(
            presenal["dir"], tendencia_h4, par, presenal.get("estrategia", "")
        ):
            return None

        # Filtro 4: regimen ADX
        regime_score, transicion = self._calcular_regime(df_m15, par)
        presenal = self._aplicar_peso_regime(presenal, regime_score, transicion, par)
        if not presenal:
            return None

        presenal["tendencia_h4"] = tendencia_h4

        # Filtro 5: DeepSeek
        if self._ds:
            try:
                senal = await self._consultar_deepseek(
                    par, df_m15, presenal, regime_score, transicion, df_h4
                )
            except Exception as _e:
                logger.debug(f"[DS fallback] {par}: {_e} — usando indicadores")
                conf_base = presenal.get("conf", 0)
                senal = (
                    presenal
                    if conf_base >= self._params.get("min_confidence", 0.30)
                    else None
                )
        else:
            conf_base = presenal.get("conf", 0)
            senal = (
                presenal
                if conf_base >= self._params.get("min_confidence", 0.30)
                else None
            )

        # Ajuste sentimiento OANDA
        if senal and self._sentiment:
            conf_antes = senal["conf"]
            senal["conf"] = self._sentiment.ajustar_confianza(
                senal["conf"], par, senal["dir"]
            )
            if senal["conf"] < self._params.get("min_confidence", 0.30):
                logger.debug(
                    f"[Sentiment] {par} descartado: conf {conf_antes:.2f} -> {senal['conf']:.2f}"
                )
                return None

        return senal

    # ── Evaluacion de estrategias con ensemble ────────────────────────────────

    def _evaluar_estrategias(self, df, par: str) -> Optional[dict]:
        """
        Evalua todas las estrategias activas con logica de ensemble:

        1. Corre todas las estrategias activas
        2. Si solo hay una senal: la usa directamente
        3. Si hay varias:
           - Verifica consenso de direccion
           - Si hay conflicto (long vs short en igual proporcion): descarta
           - Si hay consenso: promedia confianzas + bonus de +5% por cada
             estrategia adicional que confirma (max 15% bonus)
           - Nombra la senal como Ensemble(A+B+...)
        """
        if not self._strategies:
            logger.debug(f"[{par}] Sin estrategias cargadas")
            return None

        candidatos = []
        for nombre, estrategia in self._strategies.items():
            try:
                resultado = estrategia.generate_signal(df, par, self._params)
                if resultado:
                    resultado.setdefault("par", par)
                    candidatos.append(resultado)
            except Exception as exc:
                logger.debug(f"[{par}] Error en estrategia {nombre}: {exc}")

        if not candidatos:
            return None

        # Una sola estrategia: retorno directo
        if len(candidatos) == 1:
            return candidatos[0]

        # --- Ensemble con multiples candidatos ---
        longs  = [c for c in candidatos if c["dir"] == "long"]
        shorts = [c for c in candidatos if c["dir"] == "short"]

        # Conflicto total: igual numero de long y short -> descartar
        if len(longs) == len(shorts):
            logger.debug(
                f"[{par}] Ensemble: conflicto directo {len(longs)}L vs {len(shorts)}S — descartado"
            )
            return None

        # Consenso: tomar el grupo mayoritario
        consenso = longs if len(longs) > len(shorts) else shorts
        dir_ganadora = "long" if len(longs) > len(shorts) else "short"

        # Confianza: promedio del grupo ganador + bonus por cada confirmacion extra
        conf_promedio = sum(c["conf"] for c in consenso) / len(consenso)
        bonus         = min(0.05 * (len(consenso) - 1), 0.15)  # +5% por extra, max +15%
        conf_final    = min(round(conf_promedio + bonus, 3), 0.95)

        # Estrategia representante: la de mayor confianza individual
        mejor = max(consenso, key=lambda x: x["conf"]).copy()
        mejor["conf"] = conf_final
        mejor["dir"]  = dir_ganadora
        mejor["par"]  = par

        # Nombre del ensemble para trazabilidad
        nombres = sorted(set(c.get("estrategia", "?") for c in consenso))
        if len(nombres) > 1:
            mejor["estrategia"] = f"Ensemble({'+'.join(nombres)})"
            mejor["razon"]      = (
                f"Ensemble {'+'.join(nombres)} | "
                f"conf_prom={conf_promedio:.2f} bonus={bonus:.2f} | "
                f"{mejor.get('razon', '')}"
            )
            logger.debug(
                f"[{par}] Ensemble: {mejor['estrategia']} {dir_ganadora.upper()} "
                f"conf={conf_final:.2f} ({len(consenso)}/{len(candidatos)} acuerdan)"
            )

        return mejor

    # ── Filtros ───────────────────────────────────────────────────────────────

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

    def _filtro_tendencia_h4(
        self,
        dir_senal: str,
        tendencia_h4: str,
        par: str,
        estrategia: str = "",
    ) -> bool:
        """
        Filtro 3.5: alineacion H4 SOLO para EMA_Crossover (trend-following).
        RSI_Bollinger y otras mean-reversion no se filtran por H4.
        Ensemble que incluya EMA_Crossover aplica el filtro.
        """
        aplica = ("EMA_Crossover" in estrategia)
        if not aplica:
            return True
        if tendencia_h4 == "rango":
            return True
        if tendencia_h4 == "up" and dir_senal == "long":
            return True
        if tendencia_h4 == "down" and dir_senal == "short":
            return True
        logger.debug(
            f"[H4 filtro] {par} {estrategia} {dir_senal.upper()} "
            f"descartada — tendencia H4 es {tendencia_h4.upper()}"
        )
        return False

    def _calcular_regime(self, df, par: str):
        if not self._regime:
            return 0.5, "estable"
        score = self._regime.calcular_regime_score(df)
        if par not in _REGIME_HIST:
            _REGIME_HIST[par] = deque(maxlen=20)
        _REGIME_HIST[par].append(score)
        transicion = self._regime.detectar_transicion(list(_REGIME_HIST[par]))
        return score, transicion

    def _aplicar_peso_regime(self, presenal, regime_score, transicion, par):
        if not self._regime:
            return presenal
        estrategia = presenal.get("estrategia", "")

        # Para ensembles, calcular peso promedio de las estrategias involucradas
        if "Ensemble(" in estrategia:
            nombres = estrategia.replace("Ensemble(", "").replace(")", "").split("+")
            pesos = [self._regime.peso_estrategia(n.strip(), regime_score, transicion)
                     for n in nombres]
            peso = sum(pesos) / len(pesos)
        else:
            peso = self._regime.peso_estrategia(estrategia, regime_score, transicion)

        conf_nueva = presenal["conf"] * peso
        umbral = self._params.get("min_confidence", 0.30)
        if conf_nueva < umbral:
            logger.debug(
                f"[Regime] {par} {estrategia} descartado: "
                f"conf {presenal['conf']:.2f} * peso {peso:.2f} = {conf_nueva:.2f} < {umbral:.2f}"
            )
            return None
        presenal["conf"]         = round(conf_nueva, 3)
        presenal["regime_score"] = round(regime_score, 3)
        return presenal

    # ── DeepSeek ──────────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    async def _consultar_deepseek(
        self, par, df, presenal, regime_score=0.5, transicion="estable", df_h4=None
    ):
        u        = df.iloc[-1]
        rsi_val  = u.get("RSI_14", 50)
        atr_val  = u.get("ATR_14", 0)
        ema20    = u.get("EMA_20", 0)
        ema50    = u.get("EMA_50", 0)
        precio   = float(u.get("Close", 0))
        sesion   = self._market.sesion_actual()
        tf       = self._params.get("signal_timeframe", "M15")
        cierres  = ",".join(f"{x:.5f}" for x in df["Close"].tail(8).tolist())

        if df_h4 is not None and len(df_h4) >= 5:
            u4 = df_h4.iloc[-1]
            rsi_h4   = u4.get("RSI_14", 50)
            ema20_h4 = u4.get("EMA_20", 0)
            ema50_h4 = u4.get("EMA_50", 0)
            cierres_h4 = ",".join(f"{x:.5f}" for x in df_h4["Close"].tail(5).tolist())
            h4_txt = (
                f"H4: RSI={rsi_h4:.1f} EMA20={ema20_h4:.5f} "
                f"EMA50={ema50_h4:.5f} Tend={presenal.get('tendencia_h4','?')}\n"
                f"CierresH4:{cierres_h4}"
            )
        else:
            h4_txt = f"H4: Tend={presenal.get('tendencia_h4','rango')}"

        if regime_score < 0.30:
            regime_txt = f"RANGO({regime_score:.2f})"
        elif regime_score > 0.70:
            regime_txt = f"TENDENCIA({regime_score:.2f})"
        else:
            regime_txt = f"TRANSICION({regime_score:.2f})"
        if transicion != "estable":
            regime_txt += f"->{transicion}"

        prompt = (
            f"Par:{par} Dir:{presenal['dir']} Sesion:{sesion}\n"
            f"{tf}: RSI:{rsi_val:.1f} ATR:{atr_val:.5f} "
            f"EMA20:{ema20:.5f} EMA50:{ema50:.5f}\n"
            f"{h4_txt}\n"
            f"Precio:{precio:.5f} Patron:{presenal['estrategia']}\n"
            f"Regimen:{regime_txt} Cierres{tf}:{cierres}\n"
            f"WR_min:{self._params.get('min_win_rate', 0.34):.0%} "
            f"RR:{self._params.get('rr_ratio', 2.0)}\n\n"
            f'Confirmas senal? JSON: {{"senal":bool,"dir":"long/short","conf":0-1,"razon":"max 15 palabras"}}'
        )

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._ds.chat.completions.create(
                model=MODEL_FAST,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=80,
            ),
        )

        _content = (response.choices[0].message.content or "").strip()
        if not _content:
            conf_fb = float(presenal.get("conf", 0.40))
            if conf_fb >= self._params.get("min_confidence", 0.30):
                self._cooldown[par] = datetime.utcnow()
                return {
                    "par": par, "dir": presenal["dir"],
                    "conf": conf_fb, "entry": precio,
                    "razon": "tecnico+regimen (DS no disponible)",
                    "estrategia": presenal["estrategia"],
                    "atr": atr_val, "regime_score": regime_score,
                }
            return None

        try:
            resultado = json.loads(_content)
        except Exception:
            return None

        confirmado = resultado.get("senal", resultado.get("signal", False))
        if confirmado and resultado.get("conf", 0) >= self._params.get("min_confidence", 0.30):
            self._cooldown[par] = datetime.utcnow()
            return {
                "par": par, "dir": resultado["dir"],
                "conf": float(resultado["conf"]), "entry": precio,
                "razon": resultado.get("razon", ""),
                "estrategia": presenal["estrategia"],
                "atr": atr_val, "regime_score": regime_score,
            }
        return None

    def _get_timestamp_actual(self):
        try:
            import backtest_harness as _bh
            ts = _bh.get_fake_time()
            if ts:
                return ts
        except Exception:
            pass
        return datetime.now(timezone.utc)

    def stop(self):
        self._running = False
        logger.info("SignalAgent detenido")