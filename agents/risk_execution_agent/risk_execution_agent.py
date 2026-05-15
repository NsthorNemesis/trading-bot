"""
agents/risk_execution_agent/risk_execution_agent.py
════════════════════════════════════════════════════════════════
RISK + EXECUTION AGENT — Responsabilidad única: gestión y ejecución

- Valida señales del SignalAgent (capital, drawdown, correlación)
- DeepSeek V4-Pro con thinking: SL/TP matemáticamente precisos
- Ejecuta órdenes en OANDA con retry automático (tenacity)
- Monitorea posiciones abiertas cada 30 segundos
- Registra cierres y actualiza capital tracker
════════════════════════════════════════════════════════════════
"""
import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import uuid

from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI

import oandapyV20
import oandapyV20.endpoints.orders as oanda_orders
import oandapyV20.endpoints.trades as oanda_trades
from oandapyV20.contrib.requests import (
    MarketOrderRequest, TakeProfitDetails, StopLossDetails,
)
from oandapyV20.exceptions import V20Error

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import (
    OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV,
    DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_DEEP,
    PARAMS, TRADES_LOG, PARES_DISPLAY,
)

logger = logging.getLogger("risk_agent")


class RiskExecutionAgent:
    """
    Valida, calcula y ejecuta órdenes.
    También monitorea cierres y notifica al AuditAgent.
    """

    def __init__(self, signal_agent, audit_agent=None,
                 params: dict = None):
        self._signal     = signal_agent
        self._audit      = audit_agent   # para notificaciones Telegram
        self._params     = params or PARAMS

        # ── Capital inicial: leer de capital_state.json si existe ─────────────
        _capital_default = 200.0
        try:
            _state_path = Path(__file__).parent.parent.parent / "data" / "capital_state.json"
            if _state_path.exists():
                _state = json.loads(_state_path.read_text())
                _capital_default = float(_state.get("capital", 200.0))
        except Exception:
            pass
        self._capital    = _capital_default
        self._peak       = self._capital
        self._pos_abiertas: dict[str, dict] = {}  # trade_id → info
        self._dd_dia     = 0.0
        self._running    = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # capturado en run()

        # ── Circuit breaker rolling (ventana 4 semanas) ───────────────────────
        # Historial de capital: deque de (datetime_utc, capital)
        self._capital_history: deque = deque()
        self._capital_history.append((datetime.now(timezone.utc), self._capital))
        self._cb_activo  = False          # True cuando el breaker está disparado
        self._cb_hasta:  Optional[datetime] = None  # Pausa hasta esta fecha
        self._cb_dd_max  = 0.0            # Máximo drawdown rolling observado

        # Clientes
        self._oanda = oandapyV20.API(
            access_token = OANDA_TOKEN,
            environment  = OANDA_ENV,
        )
        if DEEPSEEK_KEY:
            self._ds = OpenAI(
                api_key  = DEEPSEEK_KEY,
                base_url = DEEPSEEK_BASE_URL,
            )
            logger.info(f"RiskAgent: DeepSeek {MODEL_DEEP} activo")
        else:
            self._ds = None
            logger.warning("RiskAgent: sin DeepSeek — SL/TP por fórmulas")

        # Asegurar que exista el log de trades
        TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not TRADES_LOG.exists():
            TRADES_LOG.write_text("[]")

        logger.info(
            f"RiskExecutionAgent iniciado | Capital: ${self._capital:.2f} | "
            f"OANDA {OANDA_ENV}"
        )

        # Sincronizar trades abiertos en OANDA al arrancar (evita huérfanos)
        self._sincronizar_trades_oanda()

    # ── SINCRONIZACIÓN AL ARRANCAR ────────────────────────────────────────────

    def _sincronizar_trades_oanda(self):
        """
        Al arrancar, lee las posiciones abiertas en OANDA y añade a
        _pos_abiertas cualquiera que NO esté ya en trades.json.
        Esto evita que trades huérfanos (abiertos antes del reinicio)
        queden sin monitoreo.
        """
        try:
            r = oanda_trades.OpenTrades(OANDA_ACCOUNT)
            self._oanda.request(r)
            trades_oanda = r.response.get("trades", [])

            if not trades_oanda:
                logger.info("Sincronización startup: sin trades abiertos en OANDA")
                return

            # Leer IDs OANDA ya registrados en trades.json
            try:
                log_data = json.loads(TRADES_LOG.read_text())
            except Exception:
                log_data = []

            # Construir set de oanda_ids ya en el log
            ids_en_log = {
                str(t.get("oanda_id", ""))
                for t in log_data
                if t.get("oanda_id")
            }

            huerfanos = 0
            for t in trades_oanda:
                oanda_id = str(t["id"])
                if oanda_id in ids_en_log:
                    continue  # ya está rastreado

                # Trade en OANDA que no está en trades.json → sincronizar
                par       = t["instrument"]
                units_raw = int(t["currentUnits"])
                dir_      = "long" if units_raw > 0 else "short"
                entry     = float(t["price"])
                trade_id  = f"sync_{oanda_id}"

                # Leer SL/TP del trade si existen
                sl = float(t.get("stopLossOrder", {}).get("price", 0)) or None
                tp = float(t.get("takeProfitOrder", {}).get("price", 0)) or None

                pip      = 0.01 if "JPY" in par else 0.0001
                min_sl   = self._params.get("min_sl_pips", 8) * pip
                rr       = self._params.get("rr_ratio", 2.0)
                sl_mult  = self._params.get("sl_atr_mult", 1.5)
                atr_est  = min_sl * 2   # estimación conservadora si no hay df

                if sl is None:
                    sl = (entry - atr_est * sl_mult) if dir_ == "long" else (entry + atr_est * sl_mult)
                if tp is None:
                    sl_dist = abs(entry - sl)
                    tp = (entry + sl_dist * rr) if dir_ == "long" else (entry - sl_dist * rr)

                risk_usd = self._capital * self._params.get("riesgo_pct", 0.01)

                info = {
                    "trade_id":   trade_id,
                    "oanda_id":   oanda_id,
                    "par":        par,
                    "dir":        dir_,
                    "estrategia": "sync",
                    "entry":      entry,
                    "sl":         sl,
                    "sl_original": sl,
                    "tp":         tp,
                    "units":      units_raw,
                    "risk_usd":   risk_usd,
                    "be_activado": False,
                    "opened_at":  t.get("openTime", datetime.now(timezone.utc).isoformat()),
                    "synced_startup": True,
                }

                self._pos_abiertas[trade_id] = info
                self._guardar_trade(info)
                huerfanos += 1

                logger.warning(
                    f"SINCRONIZACIÓN: trade huérfano adoptado | "
                    f"{par} {dir_.upper()} {abs(units_raw)} @ {entry:.5f} | "
                    f"OANDA_ID={oanda_id} | SL={sl:.5f} TP={tp:.5f}"
                )

            if huerfanos > 0:
                logger.info(
                    f"Sincronización startup: {huerfanos} trade(s) huérfano(s) adoptados. "
                    f"Posiciones activas: {len(self._pos_abiertas)}"
                )
            else:
                logger.info(
                    f"Sincronización startup: {len(trades_oanda)} trade(s) en OANDA, "
                    f"todos ya registrados en trades.json"
                )

        except Exception as e:
            logger.error(f"Error sincronizando trades al arrancar: {e}")

    # ── LOOP PRINCIPAL ────────────────────────────────────────────────────────

    async def run(self):
        """Loop principal: procesa señales y monitorea posiciones."""
        self._running = True
        self._loop    = asyncio.get_running_loop()  # Bug 3: guardado para threads

        # Suscribirse a señales del SignalAgent
        self._signal.suscribir_señal(self._on_senal)

        # Monitorear posiciones cada 30s en paralelo
        asyncio.create_task(self._monitor_posiciones())

        logger.info("RiskExecutionAgent: escuchando señales")
        while self._running:
            await asyncio.sleep(10)

    async def _on_senal(self, senal: dict):
        """Callback cuando el SignalAgent detecta una señal."""
        await self.procesar_senal(senal)

    # ── PROCESAMIENTO DE SEÑAL ────────────────────────────────────────────────

    async def procesar_senal(self, senal: dict) -> Optional[dict]:
        """
        Valida la señal, calcula SL/TP con DeepSeek y ejecuta en OANDA.
        """
        par = senal["par"]

        # ── VALIDACIONES PYTHON PURO (sin LLM) ───────────────────────────────
        if not self._validar_capital():
            return None
        if not self._validar_circuit_breaker():
            return None
        if not self._validar_drawdown():
            return None
        if not self._validar_max_posiciones():
            return None
        if not self._validar_correlacion(par, senal["dir"]):
            return None

        # ── CALCULAR SL/TP ────────────────────────────────────────────────────
        entry = senal["entry"]
        df    = self._signal._market.get_df(par, n=20)
        sl, tp = await self._calcular_sl_tp(par, senal["dir"], entry, senal["atr"], df)

        # ── ENFORCEMENT min_sl_pips (belt-and-suspenders) ─────────────────────
        # Garantiza que el SL enviado a OANDA nunca sea menor que min_sl_pips,
        # independientemente de si vino de DeepSeek o de la fórmula.
        direccion   = senal.get("dir", "long")
        pip         = 0.01 if "JPY" in par else 0.0001
        min_sl_pips = self._params.get("min_sl_pips", 10)
        sl_pips_actual = abs(entry - sl) / pip
        if sl_pips_actual < min_sl_pips:
            sl_dist_min = min_sl_pips * pip
            rr_ratio    = self._params.get("rr_ratio", 2.0)
            if direccion == "long":
                sl = entry - sl_dist_min
                tp = entry + sl_dist_min * rr_ratio
            else:
                sl = entry + sl_dist_min
                tp = entry - sl_dist_min * rr_ratio
            logger.warning(
                f"SL corregido por min_sl_pips: {sl_pips_actual:.1f} → {min_sl_pips} pips "
                f"| {par} {direccion.upper()} | SL={sl:.5f} TP={tp:.5f}"
            )

        # Validar dirección además de que sean > 0
        direccion = senal.get("dir", "long")
        if sl <= 0 or tp <= 0:
            logger.warning(f"RiskAgent: SL/TP inválidos para {par} — descartando")
            return None
        if direccion == "long" and sl >= senal["entry"]:
            logger.warning(f"RiskAgent: SL {sl:.5f} >= entry {senal['entry']:.5f} para LONG — descartando")
            return None
        if direccion == "short" and sl <= senal["entry"]:
            logger.warning(f"RiskAgent: SL {sl:.5f} <= entry {senal['entry']:.5f} para SHORT — descartando")
            return None
        if direccion == "long" and tp <= senal["entry"]:
            logger.warning(f"RiskAgent: TP {tp:.5f} <= entry {senal['entry']:.5f} para LONG — descartando")
            return None
        if direccion == "short" and tp >= senal["entry"]:
            logger.warning(f"RiskAgent: TP {tp:.5f} >= entry {senal['entry']:.5f} para SHORT — descartando")
            return None

        # ── CALCULAR TAMAÑO DE POSICIÓN ───────────────────────────────────────
        pip       = 0.01 if "JPY" in par else 0.0001
        sl_dist   = abs(entry - sl)
        sl_pips   = sl_dist / pip
        sl_pips   = max(sl_pips, self._params.get("min_sl_pips", 8))
        risk_usd  = self._capital * self._params.get("riesgo_pct", 0.01)
        units     = int(risk_usd / (sl_pips * pip))
        units     = max(100, min(units, 2000))

        if senal["dir"] == "short":
            units = -units

        # ── EJECUTAR EN OANDA ─────────────────────────────────────────────────
        decimals = 3 if "JPY" in par else 5
        resultado = await self._ejecutar_oanda(
            par=par, units=units, sl=sl, tp=tp,
            decimals=decimals, senal=senal,
        )

        return resultado

    # ── DEEPSEEK V4-PRO: CALCULAR SL/TP ──────────────────────────────────────

    async def _calcular_sl_tp(self, par: str, direccion: str,
                               entry: float, atr: float, df) -> tuple:
        """
        Calcula SL y TP con DeepSeek V4-Pro thinking mode.
        Fallback a fórmula matemática si DeepSeek no está disponible.
        """
        pip      = 0.01 if "JPY" in par else 0.0001
        min_sl   = self._params.get("min_sl_pips", 8) * pip
        rr       = self._params.get("rr_ratio", 2.0)
        sl_mult  = self._params.get("sl_atr_mult", 1.5)

        # Fallback matemático (siempre funciona)
        sl_dist_base = max(atr * sl_mult, min_sl)
        if direccion == "long":
            sl_base = entry - sl_dist_base
            tp_base = entry + sl_dist_base * rr
        else:
            sl_base = entry + sl_dist_base
            tp_base = entry - sl_dist_base * rr

        if not self._ds:
            return sl_base, tp_base

        # DeepSeek V4-Pro con thinking — hasta 3 intentos
        cierres = (
            ",".join(f"{x:.5f}" for x in df["Close"].tail(15).tolist())
            if df is not None else ""
        )
        prompt = f"""
Par:{par} Dir:{direccion} Entry:{entry:.5f}
ATR14:{atr:.5f} RR_min:{rr} SL_mult:{sl_mult}
Cierres15:{cierres}

Calcula SL y TP:
- SL = max({sl_mult}×ATR, {self._params['min_sl_pips']} pips) tras nivel clave
- TP = SL × {rr} mínimo
- JPY: 3 decimales | resto: 5 decimales

JSON: {{"sl":float,"tp":float,"sl_pips":float,"rr":float}}
"""
        loop = asyncio.get_running_loop()
        for intento in range(3):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: self._ds.chat.completions.create(
                        model      = MODEL_DEEP,
                        messages   = [{"role": "user", "content": prompt}],
                        response_format = {"type": "json_object"},
                        max_tokens = 150,
                    )
                )

                content = response.choices[0].message.content
                if not content or not content.strip():
                    raise ValueError("DeepSeek respuesta vacía")

                r  = json.loads(content)
                sl = float(r["sl"])
                tp = float(r["tp"])

                # Validar que SL/TP son razonables
                sl_pips_real = abs(entry - sl) / pip
                if (self._params.get("min_sl_pips", 10) <= sl_pips_real <= self._params.get("max_sl_pips", 40)):
                    # ── Validar y corregir RR mínimo ──────────────────────────
                    sl_dist_ds = abs(entry - sl)
                    tp_dist_ds = abs(tp - entry)
                    rr_real    = tp_dist_ds / sl_dist_ds if sl_dist_ds > 0 else 0.0
                    if rr_real < rr * 0.95:   # tolerancia del 5%
                        tp_old = tp
                        if direccion == "long":
                            tp = entry + sl_dist_ds * rr
                        else:
                            tp = entry - sl_dist_ds * rr
                        logger.warning(
                            f"DeepSeek RR bajo ({rr_real:.2f} < {rr:.2f}) para {par} "
                            f"— TP corregido de {tp_old:.5f} a {tp:.5f}"
                        )
                    else:
                        logger.info(
                            f"DeepSeek SL/TP {par}: SL={sl:.5f} TP={tp:.5f} "
                            f"RR={rr_real:.2f} ({sl_pips_real:.1f} pips)"
                        )
                    return sl, tp
                else:
                    logger.warning(
                        f"DeepSeek SL fuera de rango ({sl_pips_real:.1f} pips) para {par} "
                        f"en intento {intento+1}/3 — reintentando"
                    )

            except Exception as e:
                if intento < 2:
                    logger.warning(f"DeepSeek intento {intento+1}/3 fallido para {par}: {e} — reintentando")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"DeepSeek falló 3 intentos para {par}: {e} — usando fórmula")

        # Guardia de dirección: asegurar SL/TP estén en el lado correcto
        # independientemente de lo que devolvió DeepSeek
        if direccion == "long":
            if sl_base >= entry:
                sl_base = entry - max(atr * sl_mult, min_sl)
            if tp_base <= entry:
                tp_base = entry + max(atr * sl_mult, min_sl) * rr
        else:  # short
            if sl_base <= entry:
                sl_base = entry + max(atr * sl_mult, min_sl)
            if tp_base >= entry:
                tp_base = entry - max(atr * sl_mult, min_sl) * rr

        return sl_base, tp_base

    # ── EJECUCIÓN OANDA ───────────────────────────────────────────────────────

    async def _ejecutar_oanda(self, par: str, units: int,
                               sl: float, tp: float,
                               decimals: int, senal: dict) -> Optional[dict]:
        """Ejecuta la orden en OANDA con retry automático."""
        trade_id   = str(uuid.uuid4())[:8]
        instrument = par  # ya en formato EUR_USD

        try:
            rv = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._enviar_orden(instrument, units, sl, tp, decimals)
            )

            fill       = rv.get("orderFillTransaction", {})
            oanda_id   = fill.get("tradeOpened", {}).get("tradeID", "?")
            fill_price = float(fill.get("price", senal["entry"]))
            risk_usd   = self._capital * self._params.get("riesgo_pct", 0.01)

            # Registrar posición abierta
            info = {
                "trade_id":   trade_id,
                "oanda_id":   oanda_id,
                "par":        par,
                "dir":        senal["dir"],
                "estrategia": senal["estrategia"],
                "entry":      fill_price,
                "sl":         sl,
                "sl_original": sl,     # referencia para BE/trailing
                "tp":         tp,
                "units":      units,
                "risk_usd":   risk_usd,
                "be_activado": False,  # True cuando SL ya movido a entry
                "opened_at":  datetime.now(timezone.utc).isoformat(),
            }
            # Bug 2 fix: ignorar trades sin ID OANDA válido
            if oanda_id == "?":
                logger.error(
                    f"ORDEN {par}: fill sin tradeID — orden no registrada "
                    f"(puede ser fill parcial o netting). Fill: {fill}"
                )
                return None

            self._pos_abiertas[trade_id] = info
            self._guardar_trade(info)

            logger.info(
                f"ORDEN OANDA | {PARES_DISPLAY.get(par, par)} "
                f"{senal['dir'].upper()} | {abs(units)} @ {fill_price:.5f} | "
                f"SL={sl:.5f} TP={tp:.5f} | ID={trade_id}"
            )

            # Notificar Telegram
            if self._audit:
                await self._audit.notificar_orden_abierta(info)

            return info

        except Exception as e:
            logger.error(f"Error ejecutando orden {par}: {e}")
            return None

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=1, max=8),
    )
    def _enviar_orden(self, instrument: str, units: int,
                       sl: float, tp: float, decimals: int) -> dict:
        """Envía la orden a OANDA con 5 reintentos automáticos."""
        mkt = MarketOrderRequest(
            instrument       = instrument,
            units            = units,
            takeProfitOnFill = TakeProfitDetails(
                price=str(round(tp, decimals))
            ).data,
            stopLossOnFill   = StopLossDetails(
                price=str(round(sl, decimals))
            ).data,
        )
        r  = oanda_orders.OrderCreate(OANDA_ACCOUNT, data=mkt.data)
        rv = self._oanda.request(r)
        return rv

    # ── MONITOR DE POSICIONES ─────────────────────────────────────────────────

    async def _monitor_posiciones(self):
        """Consulta OANDA cada 30s para detectar cierres por SL/TP."""
        logger.info("Monitor de posiciones OANDA iniciado (30s)")
        while self._running:
            await asyncio.sleep(30)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._verificar_cierres
                )
            except Exception as e:
                logger.error(f"Monitor posiciones error: {e}")


    # ── BREAK-EVEN + TRAILING STOP ───────────────────────────────────────────

    def _obtener_precio_actual(self, par: str) -> float:
        """Precio mid actual del par via OANDA Pricing."""
        try:
            from oandapyV20.endpoints.pricing import PricingInfo
            r = PricingInfo(OANDA_ACCOUNT, params={"instruments": par})
            self._oanda.request(r)
            prices = r.response.get("prices", [])
            if prices:
                bid = float(prices[0]["bids"][0]["price"])
                ask = float(prices[0]["asks"][0]["price"])
                return (bid + ask) / 2.0
        except Exception as e:
            logger.warning(f"_obtener_precio_actual {par}: {e}")
        return 0.0

    def _modificar_sl_oanda(self, oanda_id: str, new_sl: float, decimals: int) -> bool:
        """Modifica el Stop Loss de un trade abierto via OANDA TradeCRCDO."""
        try:
            body = {
                "stopLoss": {
                    "price": str(round(new_sl, decimals)),
                    "timeInForce": "GTC",
                }
            }
            r = oanda_trades.TradeCRCDO(OANDA_ACCOUNT, oanda_id, data=body)
            self._oanda.request(r)
            logger.info(f"SL modificado OANDA | ID={oanda_id} -> {round(new_sl, decimals)}")
            return True
        except V20Error as e:
            logger.error(f"OANDA TradeCRCDO error (ID={oanda_id}): {e}")
            return False
        except Exception as e:
            logger.error(f"Error modificando SL OANDA (ID={oanda_id}): {e}")
            return False

    def _gestionar_be_trailing(self, abiertas_oanda: dict):
        """
        Break-even automatico + trailing stop.

        Fase 1 - Break-even (trigger: precio alcanza entry +/- 1R):
            Mueve SL al entry (+1 pip buffer). Riesgo = 0.

        Fase 2 - Trailing (solo tras activar BE):
            Trailear SL a 0.5 x sl_dist detras del precio actual.
            El SL solo se mueve a favor (nunca en contra).
        """
        for trade_id, info in list(self._pos_abiertas.items()):
            oanda_id  = info.get("oanda_id")
            if not oanda_id or oanda_id not in abiertas_oanda:
                continue

            par       = info["par"]
            dir_      = info["dir"]
            entry     = info["entry"]
            sl_orig   = info.get("sl_original", info["sl"])
            sl_actual = info["sl"]
            be_activo = info.get("be_activado", False)
            sl_dist   = abs(entry - sl_orig)

            if sl_dist == 0:
                continue

            precio = self._obtener_precio_actual(par)
            if precio == 0:
                continue

            pip      = 0.01 if "JPY" in par else 0.0001
            decimals = 3    if "JPY" in par else 5

            if dir_ == "long":
                be_nivel = entry + sl_dist
                trail_sl = precio - sl_dist * 0.5

                if not be_activo and precio >= be_nivel:
                    new_sl = round(entry + pip, decimals)
                    if self._modificar_sl_oanda(oanda_id, new_sl, decimals):
                        info["sl"] = new_sl
                        info["be_activado"] = True
                        ganancia_pips = (precio - entry) / pip
                        logger.info(
                            f"BE OK | {par} LONG | ID={trade_id} | "
                            f"precio={precio:.5f} (+{ganancia_pips:.1f}p) | "
                            f"SL {sl_orig:.5f} -> {new_sl:.5f} (entry+1pip)"
                        )
                        self._notificar_be(trade_id, par, dir_, entry, new_sl, precio, sl_dist)

                elif be_activo and trail_sl > sl_actual + pip:
                    new_sl = round(trail_sl, decimals)
                    if self._modificar_sl_oanda(oanda_id, new_sl, decimals):
                        logger.info(
                            f"TRAIL | {par} LONG | ID={trade_id} | "
                            f"precio={precio:.5f} SL {sl_actual:.5f}->{new_sl:.5f}"
                        )
                        info["sl"] = new_sl

            else:  # short
                be_nivel = entry - sl_dist
                trail_sl = precio + sl_dist * 0.5

                if not be_activo and precio <= be_nivel:
                    new_sl = round(entry - pip, decimals)
                    if self._modificar_sl_oanda(oanda_id, new_sl, decimals):
                        info["sl"] = new_sl
                        info["be_activado"] = True
                        ganancia_pips = (entry - precio) / pip
                        logger.info(
                            f"BE OK | {par} SHORT | ID={trade_id} | "
                            f"precio={precio:.5f} (+{ganancia_pips:.1f}p) | "
                            f"SL {sl_orig:.5f} -> {new_sl:.5f} (entry-1pip)"
                        )
                        self._notificar_be(trade_id, par, dir_, entry, new_sl, precio, sl_dist)

                elif be_activo and trail_sl < sl_actual - pip:
                    new_sl = round(trail_sl, decimals)
                    if self._modificar_sl_oanda(oanda_id, new_sl, decimals):
                        logger.info(
                            f"TRAIL | {par} SHORT | ID={trade_id} | "
                            f"precio={precio:.5f} SL {sl_actual:.5f}->{new_sl:.5f}"
                        )
                        info["sl"] = new_sl

    def _notificar_be(self, trade_id: str, par: str, dir_: str,
                      entry: float, new_sl: float, precio: float, sl_dist: float):
        """Despacha notificacion Telegram de break-even desde thread."""
        if self._audit and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._audit.notificar_break_even(
                    trade_id, par, dir_, entry, new_sl, precio, sl_dist
                ),
                self._loop,
            )

    def _verificar_cierres(self):
        """Verifica si alguna posicion fue cerrada por OANDA."""
        try:
            r = oanda_trades.OpenTrades(OANDA_ACCOUNT)
            self._oanda.request(r)
            abiertas_oanda = {
                t["id"]: t
                for t in r.response.get("trades", [])
            }

            # Break-even y trailing antes de verificar cierres
            self._gestionar_be_trailing(abiertas_oanda)

            cerradas = [
                (tid, info)
                for tid, info in list(self._pos_abiertas.items())
                if info.get("oanda_id") and info["oanda_id"] not in abiertas_oanda
            ]

            for trade_id, info in cerradas:
                par      = info["par"]
                oanda_id = info.get("oanda_id")

                # Obtener PnL real del cierre
                pnl = self._obtener_pnl_cierre(par, oanda_id)

                # Bug 1 fix: persistir cierre en trades.json
                self._actualizar_trade_cerrado(oanda_id, pnl)

                # Actualizar capital
                self._capital += pnl
                if self._capital > self._peak:
                    self._peak = self._capital
                self._dd_dia += min(pnl, 0)

                # Registrar en historial rolling para circuit breaker
                self._registrar_capital()

                del self._pos_abiertas[trade_id]

                resultado = "GANADORA" if pnl > 0 else "PERDEDORA"
                logger.info(
                    f"CIERRE | {PARES_DISPLAY.get(par, par)} | "
                    f"{info['estrategia']} | PnL=${pnl:+.4f} | {resultado} | "
                    f"Capital=${self._capital:.2f} | ID={trade_id}"
                )

                # Notificar Telegram (schedule async desde thread)
                if self._audit and self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._audit.notificar_orden_cerrada(
                            trade_id, par, info["estrategia"],
                            pnl, self._capital
                        ),
                        self._loop,  # Bug 3: usar loop capturado en run()
                    )

        except Exception as e:
            logger.error(f"Error verificando cierres: {e}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    def _obtener_pnl_cierre(self, par: str, oanda_id: str) -> float:
        """Obtiene el PnL real de una posición cerrada."""
        try:
            params = {"state": "CLOSED", "instrument": par}
            r2     = oanda_trades.TradesList(OANDA_ACCOUNT, params=params)
            self._oanda.request(r2)
            for t in r2.response.get("trades", []):
                if t["id"] == oanda_id:
                    return float(t.get("realizedPL", 0))
        except Exception:
            pass
        return 0.0

    # ── VALIDACIONES ──────────────────────────────────────────────────────────

    def _validar_capital(self) -> bool:
        if self._capital <= 0:
            logger.warning("Capital agotado")
            return False
        return True

    def _registrar_capital(self):
        """
        Añade el capital actual al historial rolling y purga
        entradas con más de 4 semanas (28 días).
        """
        now = datetime.now(timezone.utc)
        self._capital_history.append((now, self._capital))
        cutoff = now - timedelta(days=28)
        while self._capital_history and self._capital_history[0][0] < cutoff:
            self._capital_history.popleft()

    def _validar_circuit_breaker(self) -> bool:
        """
        Circuit breaker rolling de 4 semanas.

        Lógica:
        - Calcula el pico máximo de capital dentro de los últimos 28 días.
        - Si el capital actual está más de `circuit_breaker_pct` por debajo
          de ese pico, activa el breaker y pausa el bot 7 días.
        - Pasada la pausa, el breaker se desactiva automáticamente.
        - El umbral por defecto es 20% (circuit_breaker_pct=0.20).
        """
        cb_pct = self._params.get("circuit_breaker_pct", 0.20)
        now    = datetime.now(timezone.utc)

        # ── Si ya está activo, verificar si la pausa terminó ─────────────────
        if self._cb_activo:
            if self._cb_hasta and now >= self._cb_hasta:
                self._cb_activo = False
                self._cb_hasta  = None
                logger.info(
                    "Circuit breaker: pausa finalizada. "
                    f"Capital actual: ${self._capital:.2f}. Bot reactivado."
                )
            else:
                mins_rest = int(
                    (self._cb_hasta - now).total_seconds() / 60
                ) if self._cb_hasta else 0
                logger.warning(
                    f"Circuit breaker activo — {mins_rest} min restantes | "
                    f"Capital=${self._capital:.2f}"
                )
                return False

        # ── Calcular pico rolling de las últimas 4 semanas ───────────────────
        if len(self._capital_history) > 1:
            peak_rolling = max(c for _, c in self._capital_history)
        else:
            peak_rolling = self._capital

        if peak_rolling <= 0:
            return True

        dd_rolling = (peak_rolling - self._capital) / peak_rolling

        # Actualizar máximo DD observado (para snapshot/audit)
        if dd_rolling > self._cb_dd_max:
            self._cb_dd_max = dd_rolling

        # ── Disparar si supera el umbral ──────────────────────────────────────
        if dd_rolling >= cb_pct:
            self._cb_activo = True
            self._cb_hasta  = now + timedelta(days=7)
            logger.warning(
                f"⚠️  CIRCUIT BREAKER DISPARADO | "
                f"DD rolling={dd_rolling*100:.1f}% ≥ {cb_pct*100:.0f}% | "
                f"Pico4W=${peak_rolling:.2f} → Actual=${self._capital:.2f} | "
                f"Pausa hasta {self._cb_hasta.strftime('%Y-%m-%d %H:%M UTC')}"
            )
            if self._audit:
                try:
                    asyncio.create_task(
                        self._audit.notificar_circuit_breaker(
                            dd_pct    = dd_rolling,
                            peak      = peak_rolling,
                            capital   = self._capital,
                            hasta     = self._cb_hasta,
                        )
                    )
                except Exception:
                    pass
            return False

        # ── Log informativo cuando el DD rolling es significativo ─────────────
        if dd_rolling > cb_pct * 0.6:
            logger.info(
                f"DD rolling={dd_rolling*100:.1f}% | "
                f"Pico4W=${peak_rolling:.2f} | Capital=${self._capital:.2f} | "
                f"Margen: {(cb_pct - dd_rolling)*100:.1f}% para breaker"
            )

        return True

    def _validar_drawdown(self) -> bool:
        max_dd = self._params.get("max_drawdown_dia", 0.03)
        if self._dd_dia <= -(self._capital * max_dd):
            logger.warning(f"Drawdown diario alcanzado: ${self._dd_dia:.2f}")
            return False
        return True

    def _validar_max_posiciones(self) -> bool:
        max_p = self._params.get("max_posiciones", 2)
        if len(self._pos_abiertas) >= max_p:
            logger.debug(f"Max posiciones abiertas ({max_p})")
            return False
        return True

    def _validar_correlacion(self, par: str, direccion: str) -> bool:
        """Evita posiciones correlacionadas en la misma dirección."""
        correlados = {
            "EUR_USD": ["GBP_USD"],
            "GBP_USD": ["EUR_USD"],
            "USD_JPY": ["USD_CHF"],
            "USD_CHF": ["USD_JPY"],
        }
        relacionados = correlados.get(par, [])
        for tid, info in self._pos_abiertas.items():
            if (info["par"] in relacionados and
                    info["dir"] == direccion):
                logger.debug(f"Correlación: {par} ya tiene {info['par']} {direccion}")
                return False
        return True

    # ── UTILIDADES ────────────────────────────────────────────────────────────

    def _guardar_trade(self, trade: dict):
        """Guarda el trade en el log JSON."""
        try:
            trades = json.loads(TRADES_LOG.read_text())
            trades.append(trade)
            TRADES_LOG.write_text(json.dumps(trades, indent=2, default=str))
        except Exception as e:
            logger.error(f"Error guardando trade: {e}")

    def _actualizar_trade_cerrado(self, oanda_id: str, pnl: float):
        """Bug 1 fix: actualiza pnl y closed_at en trades.json al cerrar."""
        try:
            trades = json.loads(TRADES_LOG.read_text())
            for t in trades:
                if t.get("oanda_id") == oanda_id and "pnl" not in t:
                    t["pnl"]       = round(pnl, 4)
                    t["closed_at"] = datetime.now(timezone.utc).isoformat()
                    break
            TRADES_LOG.write_text(json.dumps(trades, indent=2, default=str))
        except Exception as e:
            logger.error(f"Error actualizando cierre trade {oanda_id}: {e}")

    def snapshot(self) -> dict:
        """Estado actual del agente para el AuditAgent."""
        # Calcular DD rolling actual para el snapshot
        peak_rolling = (
            max(c for _, c in self._capital_history)
            if len(self._capital_history) > 1
            else self._capital
        )
        dd_rolling = (
            (peak_rolling - self._capital) / peak_rolling
            if peak_rolling > 0 else 0.0
        )
        cb_pct = self._params.get("circuit_breaker_pct", 0.20)

        return {
            "capital":          self._capital,
            "peak":             self._peak,
            "posiciones":       len(self._pos_abiertas),
            "dd_dia":           self._dd_dia,
            "pos_lista":        list(self._pos_abiertas.values()),
            "cb_activo":        self._cb_activo,
            "cb_hasta":         self._cb_hasta.isoformat() if self._cb_hasta else None,
            "cb_dd_rolling":    round(dd_rolling, 4),
            "cb_peak_rolling":  round(peak_rolling, 2),
            "cb_dd_max":        round(self._cb_dd_max, 4),
            "cb_umbral":        cb_pct,
            "cb_margen":        round(max(cb_pct - dd_rolling, 0), 4),
        }

    def stop(self):
        self._running = False
        logger.info("RiskExecutionAgent detenido")
