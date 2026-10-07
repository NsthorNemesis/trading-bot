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
from ta.trend import EMAIndicator, MACD, ADXIndicator
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
BUFFER_M15    = 900   # M15 → ~9 días (~56 velas H4 para EMA50 del filtro tendencia)
BUFFER_H4     = 200   # H4  → ~33 días de velas de 4 horas
VELAS_MIN     = 60    # mínimo M1 para operar
VELAS_MIN_M15 = 30    # mínimo M15 para señales
MAX_EDAD      = 180   # segundos máximos de antigüedad del último tick (M1)
MAX_EDAD_M15  = 1200  # 20 min: las velas M15 se timbran a la APERTURA (edad mínima 900 s)
M15_REFRESH   = 900   # refrescar M15/H4 cada 15 minutos
MAX_CACHE_AGE = 3600  # segundos: si el archivo M15 tiene más de 1h, re-descarga


class MarketAgent:

    def __init__(self, params: dict = None):
        # Parámetros operacionales (dinámicos desde JSON)
        self._params = params or PARAMS

        # Pares activos + observacion leídos del JSON — agregar un par = editar JSON + restart
        self._pares = list(self._params.get("pares_activos", PARES))
        pares_obs   = list(self._params.get("pares_observacion", []))
        self._todos_pares = list(dict.fromkeys(self._pares + pares_obs))  # sin duplicados

        # Timeframe de señales configurable
        self._signal_tf = self._params.get("signal_timeframe", "M15")

        # Buffers por timeframe, inicializados para pares activos + observacion
        self._buffer     = {p: deque(maxlen=BUFFER_SIZE) for p in self._todos_pares}
        self._buffer_m15 = {p: deque(maxlen=BUFFER_M15)  for p in self._todos_pares}
        self._buffer_h4  = {p: deque(maxlen=BUFFER_H4)   for p in self._todos_pares}

        self._vela_actual   = {}
        self._ultimo_precio = {}
        self._spread_prom   = {p: 0.0 for p in self._todos_pares}
        self._suscriptores  = []
        self._running       = False
        self._client        = oandapyV20.API(
            access_token=OANDA_TOKEN, environment=OANDA_ENV
        )
        logger.info(
            f"MarketAgent iniciado | {len(self._pares)} pares activos + "
            f"{len(self._todos_pares)-len(self._pares)} observacion | "
            f"OANDA {OANDA_ENV} | MTF: M1+{self._signal_tf}+H4"
        )
        logger.info(f"Pares activos: {self._pares}")
        obs = [p for p in self._todos_pares if p not in self._pares]
        if obs: logger.info(f"Pares observacion: {obs}")

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

    def get_df_h1(self, par: str, n: int = 80) -> Optional[pd.DataFrame]:
        """DataFrame H1 resampleado desde M15 con indicadores."""
        if par not in self._buffer_m15:
            return None
        buf_m15 = list(self._buffer_m15[par])
        if len(buf_m15) < 4:   # mínimo 1 vela H1
            return None
        try:
            df_m15 = self._buf_to_df(buf_m15)
            df_m15["Timestamp"] = pd.to_datetime(df_m15["Timestamp"], utc=True)
            df_m15 = df_m15.set_index("Timestamp")
            df_h1 = df_m15[["Open","High","Low","Close"]].resample("1h",
                closed="left", label="left").agg(
                {"Open":"first","High":"max","Low":"min","Close":"last"}
            ).dropna(subset=["Close"]).reset_index()
            if len(df_h1) < 5:
                return None
            df_h1 = self._calcular_indicadores(df_h1)
            return df_h1.tail(n).reset_index(drop=True) if df_h1 is not None else None
        except Exception as e:
            logger.debug(f"[H1] {par} error resampleando M15→H1: {e}")
            return None


    def tendencia_h4(self, par: str) -> str:
        """
        Tendencia del H4: 'up', 'down' o 'rango'.

        Calcula H4 resampleando desde el buffer M15 (igual que el backtest),
        eliminando la discrepancia entre datos reales de OANDA y el backtest.

        Criterios (todos deben cumplirse para declarar tendencia):
          1. EMA20 y EMA50 separadas > 0.03% del precio  (antes 0.05% — muy estricto en consolidación)
          2. Precio actual del lado correcto de EMA20
          3. Al menos 2 de las últimas 5 velas H4 cierran en la dirección esperada  (antes 3/5)

        Si alguno falla → "rango" (el filtro bloquea la señal).
        """
        # ── Resamplear M15 → H4 (idéntico al backtest) ───────────────────────
        if par not in self._buffer_m15:
            return "rango"
        buf_m15 = list(self._buffer_m15[par])
        if len(buf_m15) < 64:   # mínimo ~4 velas H4
            return "rango"
        try:
            df_m15 = self._buf_to_df(buf_m15)
            df_m15["Timestamp"] = pd.to_datetime(df_m15["Timestamp"], utc=True)
            df_m15 = df_m15.set_index("Timestamp")
            df_h4_raw = df_m15[["Open","High","Low","Close"]].resample("4h",
                closed="left", label="left").agg(
                {"Open":"first","High":"max","Low":"min","Close":"last"}
            ).dropna(subset=["Close"]).reset_index()
            if len(df_h4_raw) < 10:
                return "rango"
            df = self._calcular_indicadores(df_h4_raw)
        except Exception as e:
            logger.debug(f"[H4] {par} error resampleando M15→H4: {e}")
            return "rango"

        if df is None or len(df) < 10:
            return "rango"

        ema20  = float(df["EMA_20"].iloc[-1] or 0)
        ema50  = float(df["EMA_50"].iloc[-1] or 0)
        precio = float(df["Close"].iloc[-1] or 0)

        if ema20 == 0 or ema50 == 0 or precio == 0:
            return "rango"

        # 1. Separación mínima entre EMAs (relativa al precio, no a EMA50)
        #    0.0003 = 0.03% del precio → ~3 pips en EUR/USD, ~4 pips en USD/JPY
        #    (reducido de 0.05% — tras consolidación de 1-2 días las EMAs convergen)
        diff_pct = abs(ema20 - ema50) / precio
        if diff_pct < 0.0003:
            logger.info(f"[H4] {par} rango — EMAs muy juntas: diff={diff_pct:.4%} < 0.03%")
            return "rango"

        direccion = "up" if ema20 > ema50 else "down"

        # 2. Precio actual debe estar del lado correcto de EMA20
        #    Si el precio está bajo EMA20 en tendencia "up", no es tendencia real
        if direccion == "up"   and precio < ema20:
            logger.info(f"[H4] {par} rango — precio bajo EMA20 en tendencia up: precio={precio:.5f} EMA20={ema20:.5f}")
            return "rango"
        if direccion == "down" and precio > ema20:
            logger.info(f"[H4] {par} rango — precio sobre EMA20 en tendencia down: precio={precio:.5f} EMA20={ema20:.5f}")
            return "rango"

        # 3. Momentum de velas: al menos 2 de las últimas 5 H4 confirman dirección
        #    (reducido de 3/5 — en consolidaciones post-tendencia fácilmente hay 3 velas en contra)
        ultimas = df.tail(5)
        if direccion == "up":
            cierres_favor = (ultimas["Close"] > ultimas["Open"]).sum()
        else:
            cierres_favor = (ultimas["Close"] < ultimas["Open"]).sum()
        if cierres_favor < 2:
            logger.info(f"[H4] {par} rango — pocas velas en dirección: {cierres_favor}/5 (min=2) tendencia={direccion}")
            return "rango"

        logger.info(
            f"[H4] {par} tendencia={direccion} | EMA20={ema20:.5f} EMA50={ema50:.5f} "
            f"precio={precio:.5f} diff={diff_pct:.4%} velas_favor={cierres_favor}/5"
        )
        return direccion

    # ── Utilidades ────────────────────────────────────────────────────────────


    def datos_frescos(self, par: str) -> bool:
        """Verifica que los datos M15 (no M1) sean recientes.

        Las velas M15 se timbran a la hora de APERTURA, por lo que la edad
        mínima posible es 900 s (15 min). MAX_EDAD_M15=1200 (20 min) deja
        margen para el refresco cada 15 min + latencia de red.
        """
        return self.get_edad_ultima_vela_m15(par) < MAX_EDAD_M15

    def sesion_actual(self) -> str:
        return self._sesion(datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))


    def get_edad_ultima_vela_m15(self, par: str) -> float:
        """Edad de la última vela M15 — la que usan las estrategias."""
        buf = list(self._buffer_m15.get(par, []))
        if not buf:
            return 9999
        try:
            ts_str = buf[-1].get("timestamp") or buf[-1].get("Timestamp", "")
            if not ts_str:
                return 9999
            ts = datetime.fromisoformat(
                str(ts_str).replace("Z", "+00:00")
            ).replace(tzinfo=None)
            # Las velas M15 se generan cada 15 min — toleramos hasta 20 min de edad
            return (datetime.utcnow() - ts).total_seconds()
        except Exception:
            return 9999

    def m15_disponible(self, par: str) -> bool:
        """True si hay suficientes velas M15 para generar señales."""
        return len(self._buffer_m15.get(par, [])) >= VELAS_MIN_M15



    # ── Loop principal ────────────────────────────────────────────────────────
    async def run(self):
        self._running = True
        await self._precargar_historico()
        await self._precargar_m15_h4()
        asyncio.create_task(self._refrescar_m15_h4_loop())
        while self._running:
            try:
                await self._streaming_loop()
            except RuntimeError as e:
                if "cannot schedule" in str(e) or "shutdown" in str(e).lower():
                    break
                logger.error(f"Stream error: {e} — reintentando en 5s")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Stream error: {e} — reintentando en 5s")
                await asyncio.sleep(5)

    async def _streaming_loop(self):
        req = pricing.PricingStream(
            accountID=OANDA_ACCOUNT,
            params={"instruments": ",".join(self._todos_pares)},
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
        for par in self._todos_pares:
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
        if not self._running:
            return
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

        for par in self._todos_pares:
            if not self._running:
                return
            nombre = PARES_DISPLAY.get(par, par)

            # ── M15 ──────────────────────────────────────────────────────────
            arch_m15 = HIST_DIR / f"{par}_M15.json"
            try:
                usar_cache = self._cache_valida(arch_m15, max_age=MAX_CACHE_AGE)
                velas_archivo = None

                # Intentar leer del archivo (cache fresco O como fallback)
                if arch_m15.exists() and arch_m15.stat().st_size > 10:
                    try:
                        velas_archivo = json.loads(arch_m15.read_text())
                        if not isinstance(velas_archivo, list) or len(velas_archivo) < 5:
                            velas_archivo = None
                    except Exception:
                        velas_archivo = None

                if usar_cache and not silencioso and velas_archivo:
                    # Cache fresco y válido → usar directamente
                    velas = velas_archivo
                    fuente = "cache"
                else:
                    # Cache vencido o primer run → intentar API
                    velas = await loop.run_in_executor(
                        None, self._descargar_velas, par, "M15", desde_m15
                    )
                    fuente = "API"
                    if velas:
                        # Solo sobreescribir si la API devolvió datos reales
                        arch_m15.write_text(json.dumps(velas))
                    elif velas_archivo:
                        # API vacía → usar archivo aunque esté vencido
                        velas = velas_archivo
                        fuente = "cache-fallback"

                self._buffer_m15[par].clear()
                for v in velas[-BUFFER_M15:]:
                    self._buffer_m15[par].append(v)

                if not silencioso:
                    logger.info(
                        f"  {nombre}: {len(self._buffer_m15[par])} velas M15 ({fuente})"
                    )
            except RuntimeError as e:
                # Silently ignore shutdown errors (harness teardown)
                if "cannot schedule" in str(e) or "shutdown" in str(e).lower():
                    return
                logger.error(f"  Error M15 {par}: {e}")
            except Exception as e:
                logger.error(f"  Error M15 {par}: {e}")

            # ── H4 ───────────────────────────────────────────────────────────
            if not self._running:
                return
            arch_h4 = HIST_DIR / f"{par}_H4.json"
            try:
                usar_cache = self._cache_valida(arch_h4, max_age=MAX_CACHE_AGE)
                velas_archivo = None

                # Intentar leer del archivo (cache fresco O como fallback)
                if arch_h4.exists() and arch_h4.stat().st_size > 10:
                    try:
                        velas_archivo = json.loads(arch_h4.read_text())
                        if not isinstance(velas_archivo, list) or len(velas_archivo) < 5:
                            velas_archivo = None
                    except Exception:
                        velas_archivo = None

                if usar_cache and not silencioso and velas_archivo:
                    velas = velas_archivo
                    fuente = "cache"
                else:
                    velas = await loop.run_in_executor(
                        None, self._descargar_velas, par, "H4", desde_h4
                    )
                    fuente = "API"
                    if velas:
                        arch_h4.write_text(json.dumps(velas))
                    elif velas_archivo:
                        velas = velas_archivo
                        fuente = "cache-fallback"

                self._buffer_h4[par].clear()
                for v in velas[-BUFFER_H4:]:
                    self._buffer_h4[par].append(v)

                if not silencioso:
                    logger.info(f"  {nombre}: {len(self._buffer_h4[par])} velas H4 ({fuente})")
            except RuntimeError as e:
                # Silently ignore shutdown errors (harness teardown)
                if "cannot schedule" in str(e) or "shutdown" in str(e).lower():
                    return
                logger.error(f"  Error H4 {par}: {e}")
            except Exception as e:
                logger.error(f"  Error H4 {par}: {e}")

        if silencioso:
            logger.debug("M15/H4 refrescados silenciosamente")

        # ── Persistir rolling window M15 en disco (últimas 1000 velas) ──────────
        # Garantiza que el próximo reinicio arranque con historial completo
        # sin necesidad de descarga. El archivo nunca supera ~120 KB por par.
        try:
            from config.settings import HIST_DIR as _HIST_DIR
            ROLLING_N = 1000
            for par in self._todos_pares:
                try:
                    buf = list(self._buffer_m15.get(par, []))
                    if len(buf) < 64:
                        continue
                    ventana = buf[-ROLLING_N:]
                    arch = _HIST_DIR / f"{par}_M15.json"
                    arch.write_text(json.dumps(ventana))
                except Exception as e:
                    logger.debug(f"[RollingM15] {par}: error guardando — {e}")
        except Exception:
            pass

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
        # ADXIndicator necesita ≥ window+1 filas — fallback a 0 si hay pocas velas.
        # Con ADX_14=0, el filtro adx_max_hammer no bloquea (0 < cualquier umbral).
        try:
            df["ADX_14"] = ADXIndicator(
                df["High"], df["Low"], df["Close"], window=14, fillna=True
            ).adx()
        except Exception:
            df["ADX_14"] = 0.0

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
