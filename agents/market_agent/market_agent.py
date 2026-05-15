"""
agents/market_agent/market_agent.py
Multi-timeframe: M1 (monitoreo stream) + M15 (señales) + H4 (tendencia).

Cambios v11.1:
  - Pares activos leídos de strategy_params.json (dinámicos)
  - Timeframe de señales leído de strategy_params.json (signal_timeframe)
  - Staleness check en archivos M15 cacheados (refresca si > 1 hora)
  - Agregar un par = editar JSON + restart (sin cambios de código)
"""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

import pandas as pd
from ta.volatility import AverageTrueRange, BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from tenacity import retry, stop_after_attempt, wait_exponential
import oandapyV20
import oandapyV20.endpoints.pricing as pricing
from oandapyV20.contrib.factories import InstrumentsCandlesFactory
from oandapyV20.exceptions import V20Error
import sys
from pathlib import Path as P

sys.path.insert(0, str(P(__file__).parent.parent.parent))
from config.settings import (
    OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV,
    PARES, PARES_DISPLAY, PARAMS,
)

logger = logging.getLogger("market_agent")

# ── Tamaños de buffer por timeframe ───────────────────────────────────────────
BUFFER_SIZE   = 500   # M1  → ~8 horas de velas de 1 minuto
BUFFER_M15    = 700   # M15 → ~7 días de velas de 15 minutos
BUFFER_H4     = 200   # H4  → ~33 días de velas de 4 horas
VELAS_MIN     = 60    # mínimo M1 para operar
VELAS_MIN_M15 = 30    # mínimo M15 para señales
MAX_EDAD      = 180   # segundos máximos de antigüedad del último tick
M15_REFRESH   = 900   # refrescar M15/H4 cada 15 minutos
MAX_CACHE_AGE = 3600  # segundos: si el archivo M15 tiene más de 1h, re-descarga


class MarketAgent:

    def __init__(self, params: dict = None):
        # Parámetros operacionales (dinámicos desde JSON)
        self._params = params or PARAMS

        # Pares activos leídos del JSON — agregar un par = editar JSON + restart
        self._pares = list(self._params.get("pares_activos", PARES))

        # Timeframe de señales configurable
        self._signal_tf = self._params.get("signal_timeframe", "M15")

        # Buffers por timeframe, inicializados para cada par activo
        self._buffer     = {p: deque(maxlen=BUFFER_SIZE) for p in self._pares}
        self._buffer_m15 = {p: deque(maxlen=BUFFER_M15)  for p in self._pares}
        self._buffer_h4  = {p: deque(maxlen=BUFFER_H4)   for p in self._pares}

        self._vela_actual   = {}
        self._ultimo_precio = {}
        self._spread_prom   = {p: 0.0 for p in self._pares}
        self._suscriptores  = []
        self._running       = False
        self._client        = oandapyV20.API(
            access_token=OANDA_TOKEN, environment=OANDA_ENV
        )
        logger.info(
            f"MarketAgent iniciado | {len(self._pares)} pares | "
            f"OANDA {OANDA_ENV} | MTF: M1+{self._signal_tf}+H4"
        )
        logger.info(f"Pares activos: {self._pares}")

    # ── Recarga dinámica de parámetros (hot-reload) ───────────────────────────

    def reload_params(self, new_params: dict) -> None:
        """
        Actualiza parámetros operacionales sin reiniciar.
        Nota: cambios en pares_activos o signal_timeframe requieren restart
        porque implican cambios de infraestructura (buffers, streaming).
        """
        old_pares = set(self._pares)
        new_pares = set(new_params.get("pares_activos", self._pares))

        if old_pares != new_pares:
            logger.warning(
                "⚠️  pares_activos cambió — requiere reinicio para aplicarse. "
                f"Actual: {sorted(old_pares)} | Nuevo: {sorted(new_pares)}"
            )

        self._params = new_params
        logger.info("MarketAgent: parámetros recargados")

    # ── Getters de DataFrame ──────────────────────────────────────────────────

    def get_df(self, par: str, n: int = 100) -> Optional[pd.DataFrame]:
        """DataFrame M1 con indicadores — para monitoreo de posiciones."""
        if par not in self._buffer:
            return None
        buf = list(self._buffer[par])
        if len(buf) < VELAS_MIN:
            return None
        df = self._buf_to_df(buf[-min(n, len(buf)):])
        return self._calcular_indicadores(df)

    def get_df_m15(self, par: str, n: int = 80) -> Optional[pd.DataFrame]:
        """DataFrame M15 con indicadores — para detección de señales."""
        if par not in self._buffer_m15:
            return None
        buf = list(self._buffer_m15[par])
        if len(buf) < VELAS_MIN_M15:
            return None
        df = self._buf_to_df(buf[-min(n, len(buf)):])
        return self._calcular_indicadores(df)

    # Alias de compatibilidad — algunos módulos aún llaman get_df_h1
    def get_df_h1(self, par: str, n: int = 80) -> Optional[pd.DataFrame]:
        return self.get_df_m15(par, n)

    def get_df_h4(self, par: str, n: int = 50) -> Optional[pd.DataFrame]:
        """DataFrame H4 con indicadores — para confirmación de tendencia."""
        if par not in self._buffer_h4:
            return None
        buf = list(self._buffer_h4[par])
        if len(buf) < 10:
            return None
        df = self._buf_to_df(buf[-min(n, len(buf)):])
        return self._calcular_indicadores(df)

    def tendencia_h4(self, par: str) -> str:
        """Tendencia del H4: 'up', 'down' o 'rango'."""
        df = self.get_df_h4(par, n=50)
        if df is None or len(df) < 10:
            return "rango"
        ema20 = df["EMA_20"].iloc[-1]
        ema50 = df["EMA_50"].iloc[-1]
        diff_pct = abs(ema20 - ema50) / ema50 if ema50 > 0 else 0
        if diff_pct < 0.0003:
            return "rango"
        return "up" if ema20 > ema50 else "down"

    # ── Utilidades ────────────────────────────────────────────────────────────

    def get_precio(self, par: str) -> float:
        return self._ultimo_precio.get(par, 0.0)

    def datos_frescos(self, par: str) -> bool:
        return self.get_edad_ultima_vela(par) < MAX_EDAD

    def sesion_actual(self) -> str:
        return self._sesion(datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))

    def get_edad_ultima_vela(self, par: str) -> float:
        buf = list(self._buffer.get(par, []))
        if not buf:
            return 9999
        try:
            ts = datetime.fromisoformat(
                buf[-1]["timestamp"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            return (datetime.utcnow() - ts).total_seconds()
        except Exception:
            return 9999

    def m15_disponible(self, par: str) -> bool:
        """True si hay suficientes velas M15 para generar señales."""
        return len(self._buffer_m15.get(par, [])) >= VELAS_MIN_M15

    # Alias de compatibilidad
    def h1_disponible(self, par: str) -> bool:
        return self.m15_disponible(par)

    def suscribir(self, cb: Callable):
        self._suscriptores.append(cb)

    # ── Loop principal ────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        await self._precargar_historico()
        await self._precargar_m15_h4()
        asyncio.create_task(self._refrescar_m15_h4_loop())
        while self._running:
            try:
                await self._streaming_loop()
            except Exception as e:
                logger.error(f"Stream error: {e} — reintentando en 5s")
                await asyncio.sleep(5)

    async def _streaming_loop(self):
        req = pricing.PricingStream(
            accountID=OANDA_ACCOUNT,
            params={"instruments": ",".join(self._pares)},
        )
        loop = asyncio.get_event_loop()
        logger.info("MarketAgent: streaming activo")
        await loop.run_in_executor(None, self._procesar_stream, req)

    async def _refrescar_m15_h4_loop(self):
        """Refresca los buffers M15 y H4 cada 15 minutos en background."""
        await asyncio.sleep(M15_REFRESH)
        while self._running:
            try:
                await self._precargar_m15_h4(silencioso=True)
            except Exception as e:
                logger.debug(f"Refresco M15/H4 error: {e}")
            await asyncio.sleep(M15_REFRESH)

    # ── Procesamiento de stream M1 ────────────────────────────────────────────

    def _procesar_stream(self, req):
        try:
            for tick in self._client.request(req):
                if not self._running:
                    req.terminate()
                    break
                if tick.get("type") != "PRICE":
                    continue
                par = tick.get("instrument", "")
                if par not in self._pares:
                    continue
                bids = tick.get("bids", [{}])
                asks = tick.get("asks", [{}])
                bid  = float(bids[0].get("price", 0)) if bids else 0
                ask  = float(asks[0].get("price", 0)) if asks else 0
                mid  = (bid + ask) / 2
                if mid <= 0:
                    continue
                self._ultimo_precio[par] = mid
                spread = ask - bid
                prev   = self._spread_prom[par]
                self._spread_prom[par] = (
                    spread if prev == 0 else 0.9 * prev + 0.1 * spread
                )
                self._actualizar_vela(par, mid, tick.get("time", "")[:19])
        except V20Error as e:
            logger.error(f"V20Error: {e}")
            raise

    def _actualizar_vela(self, par: str, precio: float, ts_str: str):
        minuto = ts_str[:16]
        if par not in self._vela_actual:
            self._vela_actual[par] = {
                "timestamp": ts_str + "Z", "minuto": minuto,
                "open": precio, "high": precio,
                "low": precio, "close": precio, "volume": 1,
            }
            return
        v = self._vela_actual[par]
        if v["minuto"] == minuto:
            v["high"]   = max(v["high"], precio)
            v["low"]    = min(v["low"],  precio)
            v["close"]  = precio
            v["volume"] += 1
        else:
            cerrada = {
                "timestamp": v["timestamp"],
                "open":   v["open"],  "high": v["high"],
                "low":    v["low"],   "close": v["close"],
                "volume": v["volume"],
            }
            self._buffer[par].append(cerrada)
            for cb in self._suscriptores:
                try:
                    asyncio.run_coroutine_threadsafe(
                        cb(par, cerrada), asyncio.get_event_loop()
                    )
                except Exception:
                    pass
            self._vela_actual[par] = {
                "timestamp": ts_str + "Z", "minuto": minuto,
                "open": precio, "high": precio,
                "low": precio, "close": precio, "volume": 1,
            }

    # ── Precarga histórica ────────────────────────────────────────────────────

    async def _precargar_historico(self):
        """Precarga buffer M1 desde archivo o API."""
        from config.settings import HIST_DIR
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        loop  = asyncio.get_event_loop()
        desde = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).strftime("%Y-%m-%dT00:00:00Z")
        logger.info("Precargando historial M1...")
        for par in self._pares:
            arch = HIST_DIR / f"{par}_M1.json"
            if arch.exists():
                try:
                    velas = json.loads(arch.read_text())
                    for v in velas[-BUFFER_SIZE:]:
                        self._buffer[par].append(v)
                    nombre = PARES_DISPLAY.get(par, par)
                    logger.info(f"  {nombre}: {len(self._buffer[par])} velas M1 cargadas")
                    continue
                except Exception:
                    pass
            try:
                velas = await loop.run_in_executor(
                    None, self._descargar_velas, par, "M1", desde
                )
                for v in velas[-BUFFER_SIZE:]:
                    self._buffer[par].append(v)
                arch.write_text(json.dumps(velas))
                nombre = PARES_DISPLAY.get(par, par)
                logger.info(f"  {nombre}: {len(velas)} velas M1 descargadas")
            except Exception as e:
                logger.error(f"  Error M1 {par}: {e}")

    async def _precargar_m15_h4(self, silencioso: bool = False):
        """Precarga / refresca buffers M15 y H4 desde archivo o API."""
        from config.settings import HIST_DIR
        HIST_DIR.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()

        # Usar days=3 — OANDA practice API devuelve últimos ~3 días para M15
        desde_m15 = (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).strftime("%Y-%m-%dT00:00:00Z")

        desde_h4 = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).strftime("%Y-%m-%dT00:00:00Z")

        if not silencioso:
            logger.info("Precargando historial M15 y H4...")

        for par in self._pares:
            nombre = PARES_DISPLAY.get(par, par)

            # ── M15 ──────────────────────────────────────────────────────────
            arch_m15 = HIST_DIR / f"{par}_M15.json"
            try:
                usar_cache = self._cache_valida(arch_m15, max_age=MAX_CACHE_AGE)
                if usar_cache and not silencioso:
                    velas = json.loads(arch_m15.read_text())
                else:
                    velas = await loop.run_in_executor(
                        None, self._descargar_velas, par, "M15", desde_m15
                    )
                    arch_m15.write_text(json.dumps(velas))

                self._buffer_m15[par].clear()
                for v in velas[-BUFFER_M15:]:
                    self._buffer_m15[par].append(v)

                if not silencioso:
                    fuente = "cache" if usar_cache else "API"
                    logger.info(
                        f"  {nombre}: {len(self._buffer_m15[par])} velas M15 ({fuente})"
                    )
            except Exception as e:
                logger.error(f"  Error M15 {par}: {e}")

            # ── H4 ───────────────────────────────────────────────────────────
            arch_h4 = HIST_DIR / f"{par}_H4.json"
            try:
                usar_cache = self._cache_valida(arch_h4, max_age=MAX_CACHE_AGE)
                if usar_cache and not silencioso:
                    velas = json.loads(arch_h4.read_text())
                else:
                    velas = await loop.run_in_executor(
                        None, self._descargar_velas, par, "H4", desde_h4
                    )
                    arch_h4.write_text(json.dumps(velas))

                self._buffer_h4[par].clear()
                for v in velas[-BUFFER_H4:]:
                    self._buffer_h4[par].append(v)

                if not silencioso:
                    logger.info(f"  {nombre}: {len(self._buffer_h4[par])} velas H4")
            except Exception as e:
                logger.error(f"  Error H4 {par}: {e}")

        if silencioso:
            logger.debug("M15/H4 refrescados silenciosamente")

    @staticmethod
    def _cache_valida(path: P, max_age: int = MAX_CACHE_AGE) -> bool:
        """
        True si el archivo existe, no está vacío, y tiene menos de max_age segundos.
        Evita usar archivos stale o corruptos (2 bytes = []).
        """
        if not path.exists():
            return False
        stat = path.stat()
        if stat.st_size < 10:   # menos de 10 bytes → probablemente vacío/corrupto
            return False
        age = time.time() - stat.st_mtime
        return age < max_age

    # ── Descarga de velas ─────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _descargar_velas(self, par: str, tf: str, desde: str) -> list:
        params = {
            "from": desde, "granularity": tf,
            "price": "M", "count": 5000,
        }
        velas = []
        for req in InstrumentsCandlesFactory(instrument=par, params=params):
            self._client.request(req)
            for c in req.response.get("candles", []):
                if c.get("complete", True):
                    mid = c.get("mid", {})
                    velas.append({
                        "timestamp": c["time"][:19] + "Z",
                        "open":   float(mid.get("o", 0)),
                        "high":   float(mid.get("h", 0)),
                        "low":    float(mid.get("l", 0)),
                        "close":  float(mid.get("c", 0)),
                        "volume": int(c.get("volume", 0)),
                    })
            time.sleep(0.2)
        return velas

    # ── Cálculo de indicadores ────────────────────────────────────────────────

    @staticmethod
    def _buf_to_df(buf: list) -> pd.DataFrame:
        df = pd.DataFrame(buf)
        df = df.rename(columns={
            "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        })
        return df.astype({
            "Open": float, "High": float,
            "Low": float, "Close": float, "Volume": float,
        })

    @staticmethod
    def _calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ATR_14"] = AverageTrueRange(
            df["High"], df["Low"], df["Close"], window=14, fillna=True
        ).average_true_range()
        df["RSI_14"] = RSIIndicator(
            df["Close"], window=14, fillna=True
        ).rsi()
        df["EMA_9"]  = EMAIndicator(df["Close"], window=9,  fillna=True).ema_indicator()
        df["EMA_20"] = EMAIndicator(df["Close"], window=20, fillna=True).ema_indicator()
        df["EMA_50"] = EMAIndicator(df["Close"], window=50, fillna=True).ema_indicator()
        bb = BollingerBands(df["Close"], window=20, window_dev=2, fillna=True)
        df["BBL_20"]  = bb.bollinger_lband()
        df["BBU_20"]  = bb.bollinger_hband()
        df["BB_LOW"]  = bb.bollinger_lband_indicator()
        df["BB_HIGH"] = bb.bollinger_hband_indicator()
        macd_ind       = MACD(df["Close"], fillna=True)
        df["MACD"]     = macd_ind.macd()
        df["MACD_SIG"] = macd_ind.macd_signal()
        df["MACD_DIF"] = macd_ind.macd_diff()

        o = df["Open"];  h = df["High"]
        l = df["Low"];   c = df["Close"]
        cuerpo     = abs(c - o)
        rango      = h - l
        sombra_inf = o.combine(c, min) - l
        sombra_sup = h - o.combine(c, max)

        df["CDL_HAMMER"] = (
            (cuerpo > 0) &
            (sombra_inf >= 2 * cuerpo) &
            (sombra_sup <= cuerpo * 0.5)
        ).astype(int)
        df["es_doji"] = (
            (rango > 0) &
            (cuerpo / rango.replace(0, 1) < 0.10)
        ).astype(int)
        df["CDL_ENGULF_BULL"] = (
            (c > o) &
            (c.shift(1) < o.shift(1)) &
            (c > o.shift(1)) &
            (o < c.shift(1)) &
            (cuerpo > cuerpo.shift(1) * 1.05)
        ).astype(int)
        df["CDL_ENGULF_BEAR"] = (
            (c < o) &
            (c.shift(1) > o.shift(1)) &
            (c < o.shift(1)) &
            (o > c.shift(1)) &
            (cuerpo > cuerpo.shift(1) * 1.05)
        ).astype(int)
        df["tendencia"] = (df["EMA_20"] > df["EMA_50"]).map(
            {True: "up", False: "down"}
        )
        return df

    @staticmethod
    def _sesion(ts) -> str:
        try:
            h = int(ts[11:13]) if isinstance(ts, str) else ts.hour
            if 7  <= h < 13: return "london"
            if 13 <= h < 17: return "overlap"
            if 17 <= h < 22: return "new_york"
            return "asia"
        except Exception:
            return "unknown"

    def stop(self):
        self._running = False
        logger.info("MarketAgent detenido")
