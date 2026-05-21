"""
agents/signal_agent/signal_agent.py
Flujo de 5 filtros: cooldown/sesion → calendario → prefiltro → régimen ADX → DeepSeek+sentimiento
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
from config.settings import DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_FAST, PARAMS

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


class SignalAgent:

    def __init__(self, market_agent, params: dict = None):
        self._market   = market_agent
        self._params   = params or PARAMS
        self._cooldown: dict = {}
        self._running  = False
        self._regime    = RegimeDetector()   if _REGIME_OK   else None
        self._calendar  = EconomicCalendar() if _CALENDAR_OK else None
        self._sentiment = OandaSentiment(oanda_api=None, modo_backtest=True) if _SENTIMENT_OK else None
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

    async def run(self):
        self._running = True
        logger.info("SignalAgent: esperando 5 min para datos frescos...")
        await asyncio.sleep(300)
        logger.info("SignalAgent: iniciando evaluacion de senales")
        while self._running:
            for par in self._params.get("pares_activos", []):
                try:
                    senal = await self.evaluar(par)
                    if senal:
                        logger.info(
                            f"SENAL DETECTADA | {par} {senal['dir'].upper()} | "
                            f"conf={senal['conf']:.0%} | {senal['razon']}"
                        )
                except Exception as e:
                    logger.debug(f"Error evaluando {par}: {e}")
            await asyncio.sleep(30)

    async def evaluar(self, par: str) -> Optional[dict]:
        # Filtro 1: cooldown + sesion + datos frescos
        if not self._filtro_cooldown(par):
            return None
        if not self._filtro_sesion():
            return None
        if not self._market.datos_frescos(par):
            return None

        df = self._market.get_df(par, n=60)
        if df is None or len(df) < 30:
            return None

        # Filtro 2: calendario economico
        if self._calendar:
            ts_actual = self._get_timestamp_actual()
            if self._calendar.en_blackout(ts_actual, par):
                logger.debug(f"[Calendario] {par} en blackout")
                return None

        # Filtro 3: prefiltro tecnico
        presenal = self._prefiltro_tecnico(df, par)
        if not presenal:
            return None

        # Filtro 4: regimen ADX
        regime_score, transicion = self._calcular_regime(df, par)
        presenal = self._aplicar_peso_regime(presenal, regime_score, transicion, par)
        if not presenal:
            return None

        # Filtro 5: DeepSeek
        if self._ds:
            senal = await self._consultar_deepseek(par, df, presenal, regime_score, transicion)
        else:
            conf_base = presenal.get("conf", 0)
            senal = presenal if conf_base >= self._params.get("min_confidence", 0.30) else None

        # Ajuste sentimiento OANDA
        if senal and self._sentiment:
            senal["conf"] = self._sentiment.ajustar_confianza(senal["conf"], par, senal["dir"])
            if senal["conf"] < self._params.get("min_confidence", 0.30):
                return None

        return senal

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
        peso = self._regime.peso_estrategia(estrategia, regime_score, transicion)
        conf_nueva = presenal["conf"] * peso
        umbral = self._params.get("min_confidence", 0.30)
        if conf_nueva < umbral * 0.7:
            return None
        presenal["conf"] = round(conf_nueva, 3)
        presenal["regime_score"] = round(regime_score, 3)
        return presenal

    def _prefiltro_tecnico(self, df, par: str) -> Optional[dict]:
        """
        Prefiltro técnico v2 — prioridad por fiabilidad histórica:
          1. EMA_Crossover  (WR ~44%, trend-following) — AÑADIDO
          2. Engulfing      (WR ~40%, ambas direcciones) — CORREGIDO
          3. Hammer         (WR ~24%, reversión bajista)
          4. RSI_Bollinger  (WR ~26%, mean-reversion)
          5. Doji           (WR ~26%, indecisión)
        """
        u         = df.iloc[-1]
        rsi_val   = u.get("RSI_14", 50) if "RSI_14" in df.columns else 50
        atr_val   = u.get("ATR_14",  0) if "ATR_14" in df.columns else 0
        ema20_v   = u.get("EMA_20",  0) if "EMA_20" in df.columns else 0
        ema50_v   = u.get("EMA_50",  0) if "EMA_50" in df.columns else 0
        macd_dif  = u.get("MACD_DIF",0) if "MACD_DIF" in df.columns else 0
        precio    = float(u.get("Close", 0))
        tendencia = "up" if ema20_v > ema50_v else "down"

        bbl  = [c for c in df.columns if c.startswith("BBL_")]
        bbu  = [c for c in df.columns if c.startswith("BBU_")]

        # Patrones de vela
        hammer_cols      = [c for c in df.columns if "HAMMER" in c.upper()]
        doji_cols        = [c for c in df.columns if "DOJI" in c.upper() or c == "es_doji"]
        engulf_bull_cols = [c for c in df.columns if "ENGULF_BULL" in c.upper() or c == "CDL_ENGULF_BULL"]
        engulf_bear_cols = [c for c in df.columns if "ENGULF_BEAR" in c.upper() or c == "CDL_ENGULF_BEAR"]
        # fallback: columna genérica ENGULF (solo bull)
        engulf_gen_cols  = [c for c in df.columns if "ENGULF" in c.upper()
                            and "BULL" not in c.upper() and "BEAR" not in c.upper()]

        hay_hammer      = bool(hammer_cols      and u.get(hammer_cols[0],      0) != 0)
        hay_doji        = bool(doji_cols        and u.get(doji_cols[0],        0) != 0)
        hay_engulf_bull = bool(engulf_bull_cols and u.get(engulf_bull_cols[0], 0) != 0)
        hay_engulf_bear = bool(engulf_bear_cols and u.get(engulf_bear_cols[0], 0) != 0)
        # Si solo hay columna genérica, asumirla como bull
        if not hay_engulf_bull and not hay_engulf_bear and engulf_gen_cols:
            hay_engulf_bull = bool(u.get(engulf_gen_cols[0], 0) != 0)

        # Cruce EMA_20 / EMA_50 en las últimas 3 velas
        cruce_up = cruce_down = False
        if "EMA_20" in df.columns and "EMA_50" in df.columns and len(df) >= 4:
            ema20s = df["EMA_20"].values
            ema50s = df["EMA_50"].values
            for idx in range(-3, 0):
                if ema20s[idx-1] <= ema50s[idx-1] and ema20s[idx] > ema50s[idx]:
                    cruce_up = True
                if ema20s[idx-1] >= ema50s[idx-1] and ema20s[idx] < ema50s[idx]:
                    cruce_down = True

        activas = self._params.get("estrategias_activas", [])

        # ── Prioridad 1: EMA_Crossover (trend-following, mayor fiabilidad) ────
        if "EMA_Crossover" in activas:
            if cruce_up and macd_dif > 0:
                return {
                    "par": par, "dir": "long", "conf": 0.50,
                    "entry": precio, "estrategia": "EMA_Crossover",
                    "razon": f"EMA_Cross alcista | MACD={macd_dif:.5f} | tend=up",
                    "atr": atr_val,
                }
            if cruce_down and macd_dif < 0:
                return {
                    "par": par, "dir": "short", "conf": 0.50,
                    "entry": precio, "estrategia": "EMA_Crossover",
                    "razon": f"EMA_Cross bajista | MACD={macd_dif:.5f} | tend=down",
                    "atr": atr_val,
                }

        # ── Prioridad 2: Engulfing (ambas direcciones, con tendencia) ─────────
        if "Engulfing" in activas:
            if hay_engulf_bull and tendencia == "up":
                return {
                    "par": par, "dir": "long", "conf": 0.45,
                    "entry": precio, "estrategia": "Engulfing",
                    "razon": f"Engulfing alcista | tend=up | RSI={rsi_val:.1f}",
                    "atr": atr_val,
                }
            if hay_engulf_bear and tendencia == "down":
                return {
                    "par": par, "dir": "short", "conf": 0.45,
                    "entry": precio, "estrategia": "Engulfing",
                    "razon": f"Engulfing bajista | tend=down | RSI={rsi_val:.1f}",
                    "atr": atr_val,
                }

        # ── Prioridad 3: Hammer (reversión en downtrend) ──────────────────────
        if "Hammer" in activas and hay_hammer and tendencia == "down" and atr_val > 0:
            rsi_max       = float(self._params.get("rsi_hammer_max",  50))
            prev_bear_min = int(  self._params.get("prev_bearish_min",  1))
            adx_max       = float(self._params.get("adx_max_hammer",  99))
            if rsi_val < rsi_max:
                adx_ok = True
                if adx_max < 99.0 and "ADX_14" in df.columns:
                    adx_ok = float(u.get("ADX_14", 0)) <= adx_max
                if adx_ok:
                    try:
                        prev_bear = sum(
                            1 for i in [-3, -2]
                            if df["Close"].iloc[i] < df["Open"].iloc[i]
                        )
                    except Exception:
                        prev_bear = 1
                    if prev_bear >= prev_bear_min:
                        return {
                            "par": par, "dir": "long", "conf": 0.45,
                            "entry": precio, "estrategia": "Hammer",
                            "razon": (
                                f"Hammer | RSI={rsi_val:.1f}<{rsi_max:.0f} | "
                                f"EMA20<EMA50 | {prev_bear}/2 prev bajistas"
                            ),
                            "atr": atr_val,
                        }

        # ── Prioridad 4: RSI_Bollinger (mean-reversion, mejor en rango) ───────
        if "RSI_Bollinger" in activas:
            adx_max_rsib = float(self._params.get("adx_max_rsi_bollinger", 25))
            adx_val      = float(u.get("ADX_14", 0)) if "ADX_14" in df.columns else 0
            if adx_val <= adx_max_rsib:
                precio_bbl = precio < float(u.get(bbl[0], 0)) * 1.002 if bbl else False
                precio_bbu = precio > float(u.get(bbu[0], 0)) * 0.998 if bbu else False
                if rsi_val < 32 and precio_bbl:
                    return {
                        "par": par, "dir": "long", "conf": 0.45,
                        "entry": precio, "estrategia": "RSI_Bollinger",
                        "razon": f"RSI_Bollinger | RSI={rsi_val:.1f} | BB_low",
                        "atr": atr_val,
                    }
                if rsi_val > 68 and precio_bbu:
                    return {
                        "par": par, "dir": "short", "conf": 0.45,
                        "entry": precio, "estrategia": "RSI_Bollinger",
                        "razon": f"RSI_Bollinger | RSI={rsi_val:.1f} | BB_high",
                        "atr": atr_val,
                    }

        # ── Prioridad 5: Doji (indecisión en extremos RSI) ────────────
        if "Doji" in activas and hay_doji:
            if rsi_val < 32:
                return {
                    "par": par, "dir": "long", "conf": 0.45,
                    "entry": precio, "estrategia": "Doji",
                    "razon": f"Doji+oversold | RSI={rsi_val:.1f}",
                    "atr": atr_val,
                }
            if rsi_val > 68:
                return {
                    "par": par, "dir": "short", "conf": 0.45,
                    "entry": precio, "estrategia": "Doji",
                    "razon": f"Doji+overbought | RSI={rsi_val:.1f}",
                    "atr": atr_val,
                }

        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    async def _consultar_deepseek(self, par, df, presenal,
                                   regime_score=0.5, transicion="estable"):
        u        = df.iloc[-1]
        rsi_val  = u.get("RSI_14", 50)
        atr_val  = u.get("ATR_14", 0)
        ema20    = u.get("EMA_20", 0)
        ema50    = u.get("EMA_50", 0)
        precio   = float(u.get("Close", 0))
        sesion   = self._market.sesion_actual()
        cierres_str = ",".join(f"{x:.5f}" for x in df["Close"].tail(10).tolist())

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
            f"RSI:{rsi_val:.1f} ATR:{atr_val:.5f} EMA20:{ema20:.5f} EMA50:{ema50:.5f}\n"
            f"Precio:{precio:.5f}\nPatron:{presenal['estrategia']}\n"
            f"Regimen:{regime_txt}\nCierres10:{cierres_str}\n"
            f"WR_min:{self._params.get('min_win_rate', 0.35):.0%} "
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
            )
        )

        _content = (response.choices[0].message.content or "").strip()
        if not _content:
            _conf_fb = float(presenal.get("conf", 0.40))
            if _conf_fb >= self._params.get("min_confidence", 0.25):
                self._cooldown[par] = datetime.utcnow()
                return {
                    "par": par, "dir": presenal["dir"],
                    "conf": _conf_fb, "entry": precio,
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
        if confirmado and resultado.get("conf", 0) >= self._params.get("min_confidence", 0.25):
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
