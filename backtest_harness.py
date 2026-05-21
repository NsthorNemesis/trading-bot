"""
backtest_harness.py — Trading Bot v11
======================================
Replay backtest que corre los AGENTES REALES contra datos históricos M1.

Qué hace:
  - Parchea datetime, oandapyV20.API y telegram.Bot ANTES de importar los agentes
  - Cada vela M1 se convierte en ticks OANDA sintéticos → MarketAgent los consume
  - OANDA orders → FakePositionTracker (en memoria, sin ejecutar en real)
  - DeepSeek → llamadas REALES a la API (SignalAgent + RiskAgent)
  - Telegram → redirigido a consola / log
  - AuditAgent calibra en los horarios reales (Sáb/Dom UTC)

Uso:
  python backtest_harness.py                   # 4 semanas, $200
  python backtest_harness.py --semanas 2
  python backtest_harness.py --capital 500
  python backtest_harness.py --par EUR_USD

Output:
  - Consola + logs/backtest_harness.log
  - data/backtesting/backtest_real_resultado.json

NOTAS DE AJUSTE:
  Si tus agentes usan nombres de clase distintos a MarketAgent / SignalAgent /
  RiskExecutionAgent / AuditAgent, edita AGENT_MODULES abajo.
  Si tu método de calibración en AuditAgent no se llama "weekend_analysis",
  edita AUDIT_CALIBRATION_METHODS abajo.
"""

import sys
import os
import json
import time
import queue
import threading
import logging
import importlib
import random
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from unittest.mock import patch, MagicMock

# Sleep real capturado antes del patch
_REAL_SLEEP = time.sleep
import datetime as _dt_module

# ─────────────────────────────────────────────────────────
# AJUSTAR SI: tus clases tienen nombres distintos
# ─────────────────────────────────────────────────────────
AGENT_MODULES = [
    ("market_agent",         "agents.market_agent.market_agent",                 "MarketAgent"),
    ("signal_agent",         "agents.signal_agent.signal_agent",                 "SignalAgent"),
    ("risk_execution_agent", "agents.risk_execution_agent.risk_execution_agent", "RiskExecutionAgent"),
    ("audit_agent",          "agents.audit_agent.audit_agent",                   "AuditAgent"),
]

# AJUSTAR SI: tu AuditAgent usa otro nombre para la calibración de fin de semana
# Para backtest puro sin calibración dinámica: dejar vacío []
# Con calibración activa: restaurar la lista completa
AUDIT_CALIBRATION_METHODS = []   # DESACTIVADO para backtest largo — params fijos durante todo el run
# AUDIT_CALIBRATION_METHODS = [
#     "weekend_analysis", "run_calibration", "calibrar",
#     "weekly_analysis",  "analyze",         "calibrate",
#     "run_weekend",      "run_analysis",    "do_calibration",
# ]

# Horarios de calibración del AuditAgent (weekday UTC, hour, minute)
# 5=sábado, 6=domingo
# Calibración SEMANAL: con menos de ~20 trades acumulados por semana,
# la calibración diaria produce decisiones estadísticamente inválidas
# (pausar pares por WR de 3 trades, cambiar sesiones por 1-2 operaciones).
# Semanal garantiza suficiente muestra antes de tomar decisiones.
CALIBRACION_SCHEDULE = [
    (5,  0, 0),   # Sábado 00:00 UTC
    (5,  8, 0),   # Sábado 08:00 UTC
    (6, 20, 0),   # Domingo 20:00 UTC
    (6, 22, 0),   # Domingo 22:00 UTC
]

PARES_DEFAULT = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP"]

TICK_SPREAD = {
    "EUR_USD": 0.00015,
    "GBP_USD": 0.00020,
    "USD_JPY": 0.015,
    "USD_CHF": 0.00018,
    "AUD_USD": 0.00018,
    "USD_CAD": 0.00018,
    "NZD_USD": 0.00020,
}

BASE_DIR = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data" / "historical"
RESULTS_DIR = BASE_DIR / "data" / "backtesting"
LOGS_DIR    = BASE_DIR / "logs"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Archivo de resultados — puede ser parcheado por wrappers externos (ej: run_backtest_anual.py)
RESULTS_FILE = str(RESULTS_DIR / "backtest_real_resultado.json")

try:
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("[ERROR] Activa el venv: pip install pandas numpy ta")


# ═══════════════════════════════════════════════════════════
# 1. FAKE CLOCK
# Parchea datetime.datetime globalmente antes de cualquier import.
# ═══════════════════════════════════════════════════════════
_real_datetime_cls = _dt_module.datetime
_fake_now_container = [None]   # lista para poder mutar desde funciones


class FakeDatetime(_real_datetime_cls):
    """Subclase de datetime que sobreescribe now() y utcnow()."""

    @classmethod
    def now(cls, tz=None):
        t = _fake_now_container[0]
        if t is None:
            return _real_datetime_cls.now(tz)
        return t.astimezone(tz) if tz else t.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        t = _fake_now_container[0]
        if t is None:
            return _real_datetime_cls.utcnow()
        return t.replace(tzinfo=None)

    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        return _real_datetime_cls.fromtimestamp(ts, tz)


# Aplicar patch de datetime ANTES de cualquier import de agentes
_dt_module.datetime = FakeDatetime


def set_fake_time(t: datetime):
    """Avanza el reloj simulado al timestamp dado."""
    _fake_now_container[0] = t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def get_fake_time() -> datetime:
    return _fake_now_container[0]


# ═══════════════════════════════════════════════════════════
# 2. FAKE POSITION TRACKER
# Simula cuenta OANDA: órdenes, posiciones abiertas, P&L.
# ═══════════════════════════════════════════════════════════
class FakePositionTracker:

    def __init__(self, capital_inicial: float, account_id: str):
        self.account_id    = account_id
        self.capital       = capital_inicial
        self.capital_ini   = capital_inicial
        self.peak_capital  = capital_inicial
        self._next_id      = 1
        self.open_trades   = {}   # tradeId → dict
        self.closed_trades = []
        self._prices       = {}   # instrumento → {"bid": x, "ask": x}
        self._lock         = threading.Lock()
        self._estrategia_por_trade = {}  # tradeId → nombre estrategia

    # ── Precios ────────────────────────────────────────────
    def update_price(self, instrument: str, bid: float, ask: float):
        with self._lock:
            self._prices[instrument] = {"bid": bid, "ask": ask}

    # ── Account summary (OANDA format) ────────────────────
    def get_account_summary(self) -> dict:
        with self._lock:
            unreal = sum(self._unrealized(t) for t in self.open_trades.values())
            return {
                "account": {
                    "id": self.account_id,
                    "balance":       str(round(self.capital, 5)),
                    "NAV":           str(round(self.capital + unreal, 5)),
                    "unrealizedPL":  str(round(unreal, 5)),
                    "openTradeCount": str(len(self.open_trades)),
                    "currency": "USD",
                }
            }

    def _unrealized(self, trade: dict) -> float:
        p = self._prices.get(trade.get("instrument", ""), {})
        if not p:
            return 0.0
        cur = p["bid"] if trade["_direction"] == "LONG" else p["ask"]
        entry = float(trade["price"])
        units = abs(float(trade["currentUnits"]))
        diff = (cur - entry) if trade["_direction"] == "LONG" else (entry - cur)
        return diff * units

    # ── Order create ───────────────────────────────────────
    def create_order(self, order_data: dict) -> dict:
        """Simula la ejecución instantánea de una orden de mercado."""
        with self._lock:
            order = order_data.get("order", {})
            instrument = order.get("instrument", "UNKNOWN")
            units      = float(order.get("units", 0))
            direction  = "LONG" if units > 0 else "SHORT"

            p = self._prices.get(instrument, {})
            fill_price = p.get("ask", 1.0) if direction == "LONG" else p.get("bid", 1.0)

            trade_id = str(self._next_id)
            self._next_id += 1

            tp_on_fill = order.get("takeProfitOnFill")
            sl_on_fill = order.get("stopLossOnFill")

            trade = {
                "id":           trade_id,
                "instrument":   instrument,
                "price":        str(fill_price),
                "openTime":     get_fake_time().isoformat() if get_fake_time() else "",
                "state":        "OPEN",
                "initialUnits": str(units),
                "currentUnits": str(units),
                "realizedPL":   "0",
                "_direction":   direction,
                "takeProfitOrder": tp_on_fill,
                "stopLossOrder":   sl_on_fill,
            }
            self.open_trades[trade_id] = trade

            return {
                "orderFillTransaction": {
                    "type":        "ORDER_FILL",
                    "instrument":  instrument,
                    "units":       str(units),
                    "price":       str(fill_price),
                    "time":        trade["openTime"],
                    "tradeOpened": {"tradeID": trade_id},
                }
            }

    # ── Open trades ────────────────────────────────────────
    def get_open_trades(self) -> dict:
        with self._lock:
            return {"trades": list(self.open_trades.values())}

    # ── Trade CRCDO (modificar SL/TP) ─────────────────────
    def update_trade(self, trade_id: str, data: dict) -> dict:
        with self._lock:
            if trade_id in self.open_trades:
                if "takeProfit" in data:
                    self.open_trades[trade_id]["takeProfitOrder"] = data["takeProfit"]
                if "stopLoss" in data:
                    self.open_trades[trade_id]["stopLossOrder"] = data["stopLoss"]
                return {"tradeOrdersTransaction": {"type": "TRADE_ORDERS"}}
        return {"errorMessage": "trade not found"}

    # ── Cerrar trades por SL/TP ────────────────────────────
    def check_fills(self, instrument: str, high: float, low: float) -> list:
        """Evalúa si algún trade abierto tocó SL o TP en esta vela."""
        cerrados = []
        with self._lock:
            to_remove = []
            for tid, trade in self.open_trades.items():
                if trade["instrument"] != instrument:
                    continue

                sl_ord = trade.get("stopLossOrder")
                tp_ord = trade.get("takeProfitOrder")
                sl = float(sl_ord["price"]) if sl_ord and sl_ord.get("price") else None
                tp = float(tp_ord["price"]) if tp_ord and tp_ord.get("price") else None

                direction  = trade["_direction"]
                entry      = float(trade["price"])
                units      = abs(float(trade["currentUnits"]))
                resultado  = None
                exit_price = None

                if direction == "LONG":
                    if sl and low  <= sl: resultado = "SL"; exit_price = sl
                    if tp and high >= tp: resultado = "TP"; exit_price = tp
                else:
                    if sl and high >= sl: resultado = "SL"; exit_price = sl
                    if tp and low  <= tp: resultado = "TP"; exit_price = tp

                if resultado:
                    pnl = ((exit_price - entry) if direction == "LONG"
                           else (entry - exit_price)) * units
                    self.capital    += pnl
                    self.peak_capital = max(self.peak_capital, self.capital)

                    cerrado = {**trade,
                        "closeTime":  get_fake_time().isoformat() if get_fake_time() else "",
                        "exitPrice":  str(exit_price),
                        "resultado":  resultado,
                        "pnl":        round(pnl, 5),
                        "capitalTras": round(self.capital, 4),
                        "estrategia": self._estrategia_por_trade.pop(tid, "desconocida"),
                    }
                    self.closed_trades.append(cerrado)
                    to_remove.append(tid)
                    cerrados.append(cerrado)

            for tid in to_remove:
                del self.open_trades[tid]
        return cerrados


# ═══════════════════════════════════════════════════════════
# 3. FAKE OANDA API
# Intercepta cada request y lo enruta al tracker o al stream.
# ═══════════════════════════════════════════════════════════
class FakeOandaAPI:
    """
    Drop-in replacement para oandapyV20.API.
    Se instancia igual: FakeOandaAPI(access_token=..., environment=...)
    """

    def __init__(self, tracker: FakePositionTracker,
                 tick_queue: "queue.Queue",
                 access_token: str = None,
                 environment: str = "practice"):
        self._tracker   = tracker
        self._tick_queue = tick_queue

    def request(self, endpoint):
        cls  = type(endpoint).__name__
        mod  = type(endpoint).__module__.lower()

        # ── Streaming ────────────────────────────────────
        # AJUSTAR SI: tu MarketAgent usa una clase de stream con otro nombre
        if ("stream" in cls.lower() or
                ("pricing" in mod and hasattr(endpoint, "STREAM"))):
            endpoint.response = self._stream_iterator()
            return endpoint.response

        # ── Pricing (no-stream: snapshot) ────────────────
        # CRÍTICO: filtrar por los instrumentos solicitados en params.
        # Sin esto _obtener_precio_actual("NZD_USD") devuelve EUR/USD price
        # como prices[0] → el trailing mueve SL a niveles EUR (~1.17) en NZD
        # → SL hit inmediato en todos los trades.
        if "pricing" in mod and "stream" not in cls.lower():
            req_str  = getattr(endpoint, "params", {}).get("instruments", "")
            req_set  = set(i.strip() for i in req_str.split(",") if i.strip())
            prices   = []
            for inst, p in self._tracker._prices.items():
                if not req_set or inst in req_set:
                    prices.append({
                        "instrument": inst,
                        "asks": [{"price": str(p["ask"]), "liquidity": 1000000}],
                        "bids": [{"price": str(p["bid"]), "liquidity": 1000000}],
                        "status": "tradeable",
                        "tradeable": True,
                    })
            result = {"prices": prices}
            endpoint.response = result
            return result

        # ── Create order ─────────────────────────────────
        if "ordercreate" in cls.lower() or (
                "orders" in mod and hasattr(endpoint, "data")):
            result = self._tracker.create_order(getattr(endpoint, "data", {}))
            endpoint.response = result
            return result

        # ── Open trades ──────────────────────────────────
        if "opentrades" in cls.lower() or (
                "trades" in mod and "open" in cls.lower()):
            result = self._tracker.get_open_trades()
            endpoint.response = result
            return result

        # ── Trade CRCDO (SL/TP modify) ───────────────────
        if "crcdo" in cls.lower() or "tradeorder" in cls.lower():
            trade_id = getattr(endpoint, "tradeID", None)
            data     = getattr(endpoint, "data", {})
            result   = self._tracker.update_trade(trade_id or "", data)
            endpoint.response = result
            return result

        # ── Account summary ──────────────────────────────
        if "account" in mod or "account" in cls.lower():
            result = self._tracker.get_account_summary()
            endpoint.response = result
            return result

        # ── Fallback ─────────────────────────────────────
        logging.debug(f"[FakeOandaAPI] endpoint no mapeado: {cls} — retornando {{}}")
        result = {}
        endpoint.response = result
        return result

    def _stream_iterator(self):
        """Generador que produce ticks desde el queue hasta recibir None."""
        def _gen():
            while True:
                try:
                    tick = self._tick_queue.get(timeout=60)
                    if tick is None:
                        return
                    yield tick
                except queue.Empty:
                    return
        return _gen()


# ═══════════════════════════════════════════════════════════
# 4. FAKE TELEGRAM BOT
# Redirige send_message, send_document, etc. a la consola/log.
# ═══════════════════════════════════════════════════════════
class FakeTelegramBot:
    def __init__(self, token: str = None, *args, **kwargs):
        self.token = token

    async def send_message(self, chat_id=None, text="", **kwargs):
        logging.info(f"[TELEGRAM→LOG] {str(text)[:200]}")

    async def send_document(self, *args, **kwargs):
        logging.info("[TELEGRAM→LOG] send_document (ignorado en backtest)")

    async def get_updates(self, *args, **kwargs):
        return []

    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return MagicMock()
        return _noop


class FakeTelegramApplication:
    """Por si AuditAgent usa Application.builder().token().build()"""
    def __init__(self, token=None):
        self.bot = FakeTelegramBot(token)

    def run_polling(self, *args, **kwargs):
        logging.info("[TELEGRAM→LOG] run_polling ignorado en backtest")

    @classmethod
    def builder(cls):
        class _Builder:
            def token(self_, t):
                self_._token = t
                return self_
            def build(self_):
                return FakeTelegramApplication(getattr(self_, "_token", None))
        return _Builder()

    def add_handler(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return MagicMock()
        return _noop


# ═══════════════════════════════════════════════════════════
# 5. DATA FEEDER
# Carga M1 histórico y genera ticks sintéticos por vela.
# ═══════════════════════════════════════════════════════════
class DataFeeder:

    def __init__(self, data_dir: Path, pares: list):
        self.data_dir = data_dir
        self.pares    = pares
        self.data     = {}   # par → DataFrame

    def cargar(self) -> bool:
        for par in self.pares:
            df = self._cargar_par(par)
            if not df.empty:
                self.data[par] = df
                logging.info(f"  [✓] {par}: {len(df):,} velas M1 "
                             f"({df.index[0].date()} → {df.index[-1].date()})")
            else:
                logging.warning(f"  [!] Sin datos M1 para {par}")
        return bool(self.data)

    def _cargar_par(self, par: str) -> pd.DataFrame:
        nombre = par.replace("/", "_")
        # M15 primero (5 años de cobertura) → M1 como fallback (solo ~7 días)
        candidatos = [
            self.data_dir / f"{nombre}_M15.csv",
            self.data_dir / f"{nombre}_M15.json",
            self.data_dir / par / "M15.csv",
            self.data_dir / par / "M15.json",
            self.data_dir / f"{nombre}_M1.csv",
            self.data_dir / f"{nombre}_M1.json",
            self.data_dir / par / "M1.csv",
            self.data_dir / par / "M1.json",
        ]
        for path in candidatos:
            if not path.exists():
                continue
            try:
                if path.suffix == ".csv":
                    df = pd.read_csv(path)
                else:
                    with open(path) as f:
                        raw = json.load(f)
                    if "candles" in raw:
                        rows = []
                        for c in raw["candles"]:
                            if not c.get("complete", True):
                                continue
                            mid = c.get("mid", {})
                            rows.append({
                                "time":   c["time"],
                                "open":   float(mid.get("o", 0)),
                                "high":   float(mid.get("h", 0)),
                                "low":    float(mid.get("l", 0)),
                                "close":  float(mid.get("c", 0)),
                            })
                        df = pd.DataFrame(rows)
                    else:
                        df = pd.DataFrame(raw)

                df.columns = [c.lower().strip() for c in df.columns]
                col_t = next((c for c in df.columns if "time" in c or "date" in c), None)
                if col_t:
                    df["time"] = pd.to_datetime(df[col_t], utc=True)
                    df = df.set_index("time").sort_index()
                return df.dropna(subset=["open", "high", "low", "close"])
            except Exception as e:
                logging.warning(f"[DataFeeder] Error en {path}: {e}")
        return pd.DataFrame()

    def generar_ticks(self, par: str, ts: datetime, row) -> list:
        """
        Genera 4 ticks OANDA sintéticos a partir de una vela M1.
        Formato: {"type":"PRICE","instrument":...,"asks":[...],"bids":[...],"time":...}
        """
        spread = TICK_SPREAD.get(par, 0.0002)
        half   = spread / 2
        o = float(row.get("open",  row.get("Open",  0)))
        h = float(row.get("high",  row.get("High",  0)))
        l = float(row.get("low",   row.get("Low",   0)))
        c = float(row.get("close", row.get("Close", 0)))

        def tick(price, offset_s):
            t = (ts + timedelta(seconds=offset_s)).strftime(
                "%Y-%m-%dT%H:%M:%S.000000000Z")
            return {
                "type":       "PRICE",
                "instrument": par,
                "time":       t,
                "asks":       [{"price": f"{price + half:.5f}", "liquidity": 1000000}],
                "bids":       [{"price": f"{price - half:.5f}", "liquidity": 1000000}],
                "closeoutAsk": f"{price + half:.5f}",
                "closeoutBid": f"{price - half:.5f}",
                "status":     "tradeable",
                "tradeable":  True,
            }

        # Open → High/Low → Low/High → Close (orden aleatorio para el medio)
        if random.random() > 0.5:
            return [tick(o, 0), tick(h, 15), tick(l, 35), tick(c, 55)]
        else:
            return [tick(o, 0), tick(l, 15), tick(h, 35), tick(c, 55)]

    def slice_semana(self, inicio: datetime, fin: datetime) -> dict:
        result = {}
        for par, df in self.data.items():
            mask = (df.index >= inicio) & (df.index < fin)
            if mask.any():
                result[par] = df[mask]
        return result


# ═══════════════════════════════════════════════════════════
# 6. BACKTEST HARNESS
# ═══════════════════════════════════════════════════════════
class BacktestHarness:

    def __init__(self, capital: float = 200.0, n_semanas: int = 4,
                 pares: list = None):
        self.capital   = capital
        self.n_semanas = n_semanas
        self.pares     = pares or PARES_DEFAULT

        self.tick_queue  = queue.Queue(maxsize=2000)
        self.tracker     = None
        self.feeder      = DataFeeder(DATA_DIR, self.pares)

        self._active_patches    = []
        self._calibraciones_log = []
        self._calibraciones_hechas = set()
        self.metricas_por_semana   = []

        self._setup_logging()

    # ── Logging ────────────────────────────────────────────
    def _setup_logging(self):
        log_path = LOGS_DIR / "backtest_harness.log"
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
        handlers = [logging.StreamHandler(sys.stdout)]
        try:
            handlers.append(logging.FileHandler(log_path, mode="w", encoding="utf-8"))
        except Exception:
            pass
        logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers,
                            force=True)

    # ── Account ID ─────────────────────────────────────────
    def _get_account_id(self) -> str:
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "OANDA_ACCOUNT" in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return "101-001-39239286-001"

    # ── Patches ────────────────────────────────────────────
    def _aplicar_patches(self):
        """
        Parchea oandapyV20.API, telegram.Bot, telegram.ext.Application
        y time.sleep ANTES de importar los módulos de agentes.
        """
        account_id = self._get_account_id()
        self.tracker = FakePositionTracker(self.capital, account_id)

        # Patch time.sleep: acelerar sleeps largos (≥1s) × 500
        # Esto hace que el "esperando 5 min" de SignalAgent dure ~0.6s real
        # Los yields cortos (< 1s) se respetan para no romper sincronización
        import time as _time_mod
        _real_sleep = _time_mod.sleep
        def _fast_sleep(seconds):
            if seconds >= 1.0:
                _real_sleep(max(0.002, seconds / 500))
            else:
                _real_sleep(seconds)
        _time_mod.sleep = _fast_sleep
        logging.info("  [patch] time.sleep → fast_sleep (×500 para sleeps ≥1s)")

        import asyncio as _aio_patch
        _real_aio_sleep = _aio_patch.sleep
        async def _fast_aio_sleep(delay, result=None):
            if delay >= 1.0:
                await _real_aio_sleep(max(0.0005, delay / 500))
            else:
                await _real_aio_sleep(delay)
        _aio_patch.sleep = _fast_aio_sleep
        logging.info("  [patch] asyncio.sleep -> fast_aio_sleep (x500)")

        # Patch asyncio.sleep: SignalAgent/RiskAgent usan await asyncio.sleep()
        # Sin este patch, asyncio.sleep(300) = 5 min REALES bloqueando el thread.
        import asyncio as _asyncio_mod
        _real_asyncio_sleep = _asyncio_mod.sleep
        async def _fast_asyncio_sleep(delay, result=None):
            if delay >= 1.0:
                await _real_asyncio_sleep(max(0.002, delay / 500))
            else:
                await _real_asyncio_sleep(delay)
        _asyncio_mod.sleep = _fast_asyncio_sleep
        self._real_asyncio_sleep = _real_asyncio_sleep
        logging.info("  [patch] asyncio.sleep → fast_asyncio_sleep (×500)")

        # Capturar tracker para el closure
        tracker    = self.tracker
        tick_queue = self.tick_queue

        def make_fake_api(access_token=None, environment="practice", **kw):
            return FakeOandaAPI(tracker, tick_queue,
                                access_token=access_token,
                                environment=environment)

        # oandapyV20.API
        try:
            import oandapyV20
            p = patch.object(oandapyV20, "API", make_fake_api)
            p.start()
            self._active_patches.append(p)
            logging.info("  [patch] oandapyV20.API → FakeOandaAPI")
        except ImportError:
            logging.warning("  [patch] oandapyV20 no encontrado — ¿venv activo?")

        # telegram.Bot
        try:
            import telegram
            p2 = patch.object(telegram, "Bot",
                               lambda token=None, **kw: FakeTelegramBot(token))
            p2.start()
            self._active_patches.append(p2)
            logging.info("  [patch] telegram.Bot → FakeTelegramBot")
        except ImportError:
            logging.warning("  [patch] python-telegram-bot no encontrado")

        # telegram.ext.Application (para bots modernos con Application.builder())
        # Nota: puede lanzar TypeError de metaclase en algunas versiones de PTB
        # cuando datetime ya está parchado — se ignora con seguridad.
        try:
            import telegram.ext
            p3 = patch.object(telegram.ext, "Application", FakeTelegramApplication)
            p3.start()
            self._active_patches.append(p3)
            logging.info("  [patch] telegram.ext.Application → FakeTelegramApplication")
        except Exception:
            logging.info("  [patch] telegram.ext.Application omitido (metaclass conflict — normal)")

        # openai.OpenAI: en modo backtest local usa DeepSeek real si hay DEEPSEEK_API_KEY.
        # El RiskAgent cae a fallback matemático si la key no está configurada.

    def _detener_patches(self):
        for p in reversed(self._active_patches):
            try:
                p.stop()
            except RuntimeError:
                pass
        self._active_patches.clear()

    # ── Import y instanciar agentes ────────────────────────
    def _importar_agentes(self) -> dict:
        """Importa los módulos de agentes (con patches ya activos)."""
        sys.path.insert(0, str(BASE_DIR))

        # Pre-inyectar stubs para módulos auxiliares del proyecto que
        # pueden no estar accesibles desde el path del harness.
        # Esto permite que los agentes importen sin error aunque
        # 'utils/', 'config/', etc. no estén en sys.path todavía.
        # Stub SOLO para sub-módulos que realmente no existen.
        # NO stubbear "utils" como padre — rompería utils.regime_detector etc.
        _stub_modulos = [
            "utils.indicators", "utils.candle_utils",
            "utils.helpers",    "utils.logger",
            "utils.risk_utils", "utils.signal_utils",
        ]
        for mod_name in _stub_modulos:
            if mod_name not in sys.modules:
                try:
                    importlib.import_module(mod_name)
                except ImportError:
                    sys.modules[mod_name] = MagicMock()
                    logging.debug(f"  [stub] {mod_name} → MagicMock")

        clases = {}
        for key, module_path, class_name in AGENT_MODULES:
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                clases[key] = cls
                logging.info(f"  [import] ✓ {class_name}")
            except Exception as e:
                logging.error(f"  [import] ✗ {class_name}: {e}")
                clases[key] = None
        return clases

    def _instanciar(self, clases: dict) -> dict:
        """
        Instancia cada agente. Prueba múltiples firmas de __init__
        incluyendo pasar market_agent instanciado (buffer compartido).
        Si falla todo, muestra la firma real para diagnóstico.
        """
        import inspect
        account_id = self._get_account_id()
        instancias = {}

        # Orden de instanciación: audit antes de risk para que audit esté disponible
        # cuando se trate de instanciar risk_execution_agent (que lo necesita).
        for key in ["market_agent", "signal_agent", "audit_agent", "risk_execution_agent"]:
            cls = clases.get(key)
            if cls is None:
                continue

            market  = instancias.get("market_agent")
            signal  = instancias.get("signal_agent")
            audit   = instancias.get("audit_agent")

            # Cargar params desde calibration file si existe
            params_data = {}
            try:
                cal_file = BASE_DIR / "data" / "calibration" / "strategy_params.json"
                if cal_file.exists():
                    with open(cal_file) as f:
                        params_data = json.load(f)
            except Exception:
                pass

            # Construir firmas a probar según dependencias disponibles
            firmas = []

            # Firmas con signal_agent + audit_agent (RiskExecutionAgent)
            if signal and audit:
                firmas += [
                    {"signal_agent": signal, "audit_agent": audit, "params": params_data},
                    {"signal_agent": signal, "audit_agent": audit, "params": None},
                    {"signal_agent": signal, "audit_agent": audit, "params": {}},
                    {"signal_agent": signal, "audit_agent": audit},
                    {"signal_agent": signal},
                ]

            # Firmas con market_agent (SignalAgent)
            if market:
                firmas += [
                    {"market_agent": market},
                    {"marketagent":  market},
                    {"agent":        market},
                    {"market":       market},
                    {"market_agent": market, "account_id": account_id},
                ]

            # Firmas genéricas
            firmas += [
                {"account_id": account_id},
                {"accountID":  account_id},
                {"account":    account_id},
                {},
            ]

            ok = False
            last_err = None
            for kwargs in firmas:
                try:
                    instancias[key] = cls(**kwargs)
                    kw_str = list(kwargs.keys()) or "ninguno"
                    logging.info(f"  [init] ✓ {key} (kwargs={kw_str})")
                    ok = True
                    break
                except TypeError as e:
                    last_err = e
                    continue
                except Exception as e:
                    logging.error(f"  [init] ✗ {key}: {e}")
                    last_err = e
                    break

            if not ok:
                # Mostrar firma real para diagnóstico
                try:
                    sig = inspect.signature(cls.__init__)
                    params = [p for p in sig.parameters if p != "self"]
                    logging.warning(
                        f"  [init] ✗ {key} — no se pudo instanciar.\n"
                        f"          Firma real: __init__(self, {', '.join(params)})\n"
                        f"          Último error: {last_err}\n"
                        f"          → Agrega la firma correcta en _instanciar() del harness."
                    )
                except Exception:
                    logging.warning(f"  [init] ✗ {key} — fallo silencioso: {last_err}")

        # Patch de backtest: datos_frescos siempre True.
        # En backtest el fake_time avanza mas rapido que MAX_EDAD (180s),
        # por lo que sin este patch todos los datos se marcan como 'rancios'.
        market = instancias.get("market_agent")
        if market:
            market.datos_frescos = lambda par: True
            logging.info("  [patch] market_agent.datos_frescos -> always True (backtest)")

            # CRITICO: sesion_actual() usa datetime.utcnow() del módulo (real clock).
            # En backtest esto devuelve la hora REAL del PC, no la hora simulada,
            # lo que bloquea todas las señales con el filtro de sesión.
            # Solución: parchear sesion_actual() para usar el fake_time.
            _market_ref = market
            _sesion_fn  = market._sesion   # método estático, no usa self

            def _sesion_fake():
                t = get_fake_time()
                if t is not None:
                    return _sesion_fn(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
                return _sesion_fn(
                    _real_datetime_cls.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                )

            _market_ref.sesion_actual = _sesion_fake
            logging.info("  [patch] market_agent.sesion_actual -> fake_time (backtest)")

        _mkt = instancias.get('market_agent')
        if _mkt:
            _mkt.datos_frescos = lambda par: True
            _mkt.datos_frescos_patched = True
            logging.info("  [patch] datos_frescos -> True")

        # Fix D / BACKTEST: suscribir_señal siempre = noop.
        # Sin esto risk_agent.run() llama signal.suscribir_señal(self._on_senal)
        # y re-registra el callback DESPUÉS del patch _on_senal_ext=None.
        # Resultado: signal_agent.run() thread también abre trades → duplicados
        # con estrategia="desconocida" y WR distorsionado.
        # El harness poll llama evaluar()+procesar_senal() directamente y es
        # la ÚNICA fuente de trades en backtest.
        _sa_fix = instancias.get('signal_agent')
        if _sa_fix:
            _sa_fix.suscribir_señal = lambda cb: None
            if hasattr(_sa_fix, '_on_senal_ext'):
                _sa_fix._on_senal_ext = None
            logging.info(
                "  [BACKTEST] signal_agent.suscribir_señal = noop "
                "(harness poll es el único generador de trades)"
            )

        return instancias

    # ── Calibración ────────────────────────────────────────
    def _deberia_calibrar(self, ts: datetime) -> bool:
        wd, h, m = ts.weekday(), ts.hour, ts.minute
        for sched_wd, sched_h, sched_m in CALIBRACION_SCHEDULE:
            if wd == sched_wd and h == sched_h and m == sched_m:
                key = f"{ts.date()}_{h}:{m:02d}"
                if key not in self._calibraciones_hechas:
                    return True
        return False

    def _ejecutar_calibracion(self, agentes: dict, ts: datetime):
        audit = agentes.get("audit_agent")
        key   = f"{ts.date()}_{ts.hour}:{ts.minute:02d}"
        self._calibraciones_hechas.add(key)

        logging.info(f"\n{'─'*55}")
        logging.info(f"[AuditAgent] Calibración: "
                     f"{ts.strftime('%A %Y-%m-%d %H:%M UTC')}")

        if audit is None:
            logging.warning("[AuditAgent] No instanciado — calibración omitida")
            self._calibraciones_log.append(
                {"ts": ts.isoformat(), "resultado": "agente_no_instanciado"})
            return

        for nombre in AUDIT_CALIBRATION_METHODS:
            if hasattr(audit, nombre):
                try:
                    getattr(audit, nombre)()
                    logging.info(f"[AuditAgent] ✓ {nombre}() ejecutado")
                    self._calibraciones_log.append(
                        {"ts": ts.isoformat(), "metodo": nombre, "resultado": "ok"})
                    # ── Recargar params en agentes activos post-calibración ──────
                    # Sin esto, los cambios de calibrar() solo afectan al siguiente
                    # backtest (el archivo cambia pero los agentes usan RAM antigua).
                    try:
                        _cal = BASE_DIR / "data" / "calibration" / "strategy_params.json"
                        if _cal.exists():
                            with open(_cal, encoding="utf-8") as _fh:
                                _new_p = json.load(_fh)
                            sa = agentes.get("signal_agent")
                            if sa and hasattr(sa, "reload_params"):
                                sa.reload_params(_new_p)
                                logging.info("[AuditAgent] signal_agent params recargados post-calibración")
                            ra = agentes.get("risk_execution_agent")
                            if ra and hasattr(ra, "_params") and isinstance(ra._params, dict):
                                ra._params.update(_new_p)
                                logging.info("[AuditAgent] risk_agent params recargados post-calibración")
                    except Exception as _re:
                        logging.warning(f"[AuditAgent] No se pudo recargar params post-calibración: {_re}")
                    return
                except Exception as e:
                    logging.warning(f"[AuditAgent] {nombre}() error: {e}")

        # Si ningún método funcionó
        logging.warning("[AuditAgent] No se encontró método de calibración.")
        logging.warning(f"  Métodos disponibles: {[m for m in dir(audit) if not m.startswith('_')]}")
        logging.warning(f"  Agrega el nombre correcto a AUDIT_CALIBRATION_METHODS en este archivo.")
        self._calibraciones_log.append(
            {"ts": ts.isoformat(), "resultado": "metodo_no_encontrado"})

    # ── Métricas ────────────────────────────────────────────
    def _metricas(self, trades: list) -> dict:
        if not trades:
            return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                    "max_dd_pct": 0.0, "pnl_total": 0.0, "expectancy": 0.0}
        ganos  = [t for t in trades if t.get("resultado") == "TP"]
        perdas = [t for t in trades if t.get("resultado") == "SL"]
        pnl    = sum(t["pnl"] for t in trades)
        bg = sum(t["pnl"] for t in ganos)
        bp = abs(sum(t["pnl"] for t in perdas)) or 0.001
        wr = len(ganos) / len(trades)
        pf = bg / bp

        cap = self.capital; peak = cap; max_dd = 0.0
        for t in sorted(trades, key=lambda x: x.get("closeTime", "")):
            cap += t["pnl"]
            peak = max(peak, cap)
            dd   = (peak - cap) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        return {
            "trades":        len(trades),
            "ganados":       len(ganos),
            "perdidos":      len(perdas),
            "win_rate":      round(wr, 4),
            "profit_factor": round(pf, 4),
            "max_dd_pct":    round(max_dd * 100, 2),
            "pnl_total":     round(pnl, 4),
            "expectancy":    round(pnl / len(trades), 5),
        }

    # ── Main correr ────────────────────────────────────────
    def correr(self):
        logging.info("═" * 60)
        logging.info("BACKTEST HARNESS — Trading Bot v11")
        logging.info(f"Capital: ${self.capital:.2f} | Semanas: {self.n_semanas}")
        logging.info("Modo: AGENTES REALES | DeepSeek: REAL | OANDA: SIMULADO")
        logging.info("═" * 60)

        # 1. Datos
        logging.info("\n[1/4] Cargando datos históricos M1...")
        if not self.feeder.cargar():
            logging.error("Sin datos M1 en data/historical/ — corre setup_datos.py primero")
            return {}

        fechas_fin = [df.index[-1].to_pydatetime() for df in self.feeder.data.values()]
        fecha_fin  = min(fechas_fin)
        if not fecha_fin.tzinfo:
            fecha_fin = fecha_fin.replace(tzinfo=timezone.utc)
        fecha_inicio = fecha_fin - timedelta(weeks=self.n_semanas)
        logging.info(f"Período: {fecha_inicio.date()} → {fecha_fin.date()}")

        # ── Cargar datos M15 completos para buffer dinámico ────────────────────
        # El market_agent._buffer_m15 se preloada ESTATICAMENTE con las últimas
        # 700 velas del archivo.  Para que signal_agent vea las condiciones
        # históricas correctas (RSI extremos) hay que actualizar ese buffer
        # con las velas M15 correspondientes al fake_time actual.
        import bisect as _bisect
        _m15_data   = {}   # par -> list[dict] ordenada por timestamp
        _m15_ts_idx = {}   # par -> list[str]  para bisect_right

        # Cargar TODOS los archivos M15 disponibles (no solo self.pares)
        # para que funcione aunque pares_activos ≠ PARES_DEFAULT
        for _m15_f in sorted(DATA_DIR.glob("*_M15.json")):
            _pp = _m15_f.stem.replace("_M15", "")
            try:
                _m15_all = json.loads(_m15_f.read_text(encoding="utf-8"))
                if isinstance(_m15_all, list) and len(_m15_all) > 10:
                    _sorted = sorted(_m15_all, key=lambda x: x.get("timestamp", ""))
                    _m15_data[_pp]   = _sorted
                    _m15_ts_idx[_pp] = [v["timestamp"] for v in _sorted]
                    logging.info(
                        f"  [M15-DYN] {_pp}: {len(_sorted):,} velas "
                        f"({_sorted[0]['timestamp'][:10]} -> "
                        f"{_sorted[-1]['timestamp'][:10]})"
                    )
            except Exception as _me:
                logging.warning(f"  [M15-DYN] {_pp}: no cargado ({_me})")

        # 2. Patches + imports
        logging.info("\n[2/4] Aplicando patches e importando agentes...")
        set_fake_time(fecha_inicio)
        self._aplicar_patches()
        clases    = self._importar_agentes()
        agentes   = self._instanciar(clases)
        # Fix B: Sincronizar signal_agent Y risk_agent._params con strategy_params.json
        _sp_path = BASE_DIR / 'data' / 'calibration' / 'strategy_params.json'
        if _sp_path.exists():
            try:
                with open(_sp_path, encoding='utf-8') as _f:
                    _sp_data = json.load(_f)
                for _agent_key in ('signal_agent', 'risk_execution_agent'):
                    _ag = agentes.get(_agent_key)
                    if not _ag:
                        continue
                    if isinstance(getattr(_ag, '_params', None), dict):
                        # Actualizar claves existentes + añadir las nuevas
                        _ag._params.update(_sp_data)
                        logging.info(f'  [Fix B] {_agent_key}._params sincronizado desde strategy_params.json')
                    else:
                        # Si _params no es dict, reemplazarlo directamente
                        try:
                            _ag._params = dict(_sp_data)
                            logging.info(f'  [Fix B] {_agent_key}._params asignado desde strategy_params.json')
                        except Exception:
                            pass
            except Exception as _eb:
                logging.warning(f'  [Fix B] No se pudo leer strategy_params.json: {_eb}')
        else:
            logging.info('  [Fix B] strategy_params.json no encontrado')

        if not agentes:
            logging.error("No se pudo instanciar ningún agente.")
            self._detener_patches()
            return {}

        # 3. Arrancar threads (agentes con método run())
        logging.info("\n[3/4] Arrancando agentes en threads...")
        threads = []
        for key, agent in agentes.items():
            if hasattr(agent, "run"):
                t = threading.Thread(target=self._run_agent_safe,
                                     args=(key, agent),
                                     name=key, daemon=True)
                t.start()
                threads.append(t)
                logging.info(f"  ✓ {key} arrancado en thread")
            elif hasattr(agent, "start"):
                t = threading.Thread(target=self._start_agent_safe,
                                     args=(key, agent),
                                     name=key, daemon=True)
                t.start()
                threads.append(t)
                logging.info(f"  ✓ {key} arrancado (via start()) en thread")
            else:
                logging.info(f"  · {key} sin método run()/start() — solo calibración directa")

        # Esperar que los agentes pasen su startup:
        # - SignalAgent "espera 5 min" → con fast_sleep (×500) = ~0.6s real
        # - MarketAgent precarga buffer → ~0.2s
        # Total: 1.5s debería ser suficiente
        logging.info("  Esperando startup de agentes (~1.2s real)...")
        _REAL_SLEEP(1.2)

        # 4. Replay
        # bridge:trades_log_init — limpiar TRADES_LOG para datos de backtest
        _trades_log_path = BASE_DIR / "data" / "trades" / "trades_log.json"
        _trades_log_path.parent.mkdir(parents=True, exist_ok=True)
        _trades_log_path.write_text("[]", encoding="utf-8")
        logging.info("  [backtest] TRADES_LOG inicializado para calibracion")
        logging.info("\n[4/4] Iniciando replay semana a semana...\n")

        _pair_cooldown = {}  # Fix A: fake-time cooldown por par
        for semana in range(1, self.n_semanas + 1):
            s_ini = fecha_inicio + timedelta(weeks=semana - 1)
            s_fin = fecha_inicio + timedelta(weeks=semana)
            logging.info(f"📅 SEMANA {semana} | {s_ini.date()} → {s_fin.date()}")

            n_antes   = len(self.tracker.closed_trades)
            semana_df = self.feeder.slice_semana(s_ini, s_fin)

            if not semana_df:
                logging.warning(f"  Sin datos para semana {semana}")
                continue

            # Combinar todos los pares en orden cronológico
            all_candles = []
            for par, df in semana_df.items():
                for ts, row in df.iterrows():
                    ts_aw = ts.to_pydatetime()
                    if not ts_aw.tzinfo:
                        ts_aw = ts_aw.replace(tzinfo=timezone.utc)
                    all_candles.append((ts_aw, par, row))
            all_candles.sort(key=lambda x: x[0])

            _candle_idx = 0
            for ts_candle, par, row in all_candles:
                set_fake_time(ts_candle)

                # Actualizar precios en tracker
                mid    = (float(row.get("high", 0)) + float(row.get("low", 0))) / 2
                spread = TICK_SPREAD.get(par, 0.0002) / 2
                self.tracker.update_price(par, mid - spread, mid + spread)

                # Enviar ticks al queue para los agentes
                ticks = self.feeder.generar_ticks(par, ts_candle, row)
                for tick in ticks:
                    try:
                        self.tick_queue.put(tick, block=True, timeout=0.5)
                    except queue.Full:
                        pass   # Si los agentes no consumen rápido, skip

                # Evaluar SL/TP en posiciones abiertas
                cerrados = self.tracker.check_fills(
                    par,
                    float(row.get("high", 0)),
                    float(row.get("low",  0))
                )
                for t in cerrados:
                    emoji = "✅" if t["resultado"] == "TP" else "🔴"
                    logging.info(
                        f"  {emoji} {t['instrument']} [{t['resultado']}] "
                        f"PnL=${t['pnl']:+.4f}  Capital=${t['capitalTras']:.2f}"
                    )

                # Fix E: sincronizar risk._pos_abiertas con trades cerrados en FakePositionTracker
                # Sin esto max_posiciones=2 bloquea todos los trades después del 2do
                if cerrados:
                    _ra_e = agentes.get('risk_execution_agent')
                    if _ra_e and hasattr(_ra_e, '_pos_abiertas'):
                        _closed_ids = {str(t.get('id', '')) for t in cerrados}
                        _to_del = [
                            _tid for _tid, _info in _ra_e._pos_abiertas.items()
                            if str(_info.get('oanda_id', '')) in _closed_ids
                        ]
                        for _tid in _to_del:
                            del _ra_e._pos_abiertas[_tid]
                        if _to_del:
                            logging.info(
                                f"  [Fix E] {len(_to_del)} pos cerrada(s) en risk._pos_abiertas "
                                f"→ ahora {len(_ra_e._pos_abiertas)} abiertas"
                            )
                    # Fix F: persistir TODOS los trades cerrados (batch — un solo write por vela)
                    if cerrados:
                        try:
                            _tl = json.loads(_trades_log_path.read_text())
                            for _tc in cerrados:
                                _tl.append({
                                    "trade_id":   str(_tc.get("id", "")),
                                    "par":        _tc.get("instrument", ""),
                                    "estrategia": _tc.get("estrategia", "desconocida"),
                                    "pnl":        round(float(_tc.get("pnl", 0)), 5),
                                    "resultado":  "GANADORA" if _tc.get("resultado") == "TP" else "PERDEDORA",
                                    "opened_at":  str(_tc.get("openTime", "")),
                                    "closed_at":  str(_tc.get("closeTime", "")),
                                })
                            _trades_log_path.write_text(
                                json.dumps(_tl, indent=2), encoding="utf-8")
                        except Exception:
                            pass

                # Fix C: Sincronizar risk_agent._capital con capital real del tracker
                if cerrados:
                    _ra_sync = agentes.get('risk_execution_agent')
                    if _ra_sync and hasattr(_ra_sync, '_capital'):
                        _ra_sync._capital = self.tracker.capital
                        logging.debug(
                            f'  [Fix C] capital sync: ${self.tracker.capital:.2f}')
                # ¿Hay calibración programada en este minuto?
                if self._deberia_calibrar(ts_candle):
                    self._ejecutar_calibracion(agentes, ts_candle)

                # 1ms real por vela
                _candle_idx += 1
                if _candle_idx % 2 == 0:
                    # ── Actualizar _buffer_m15 con velas históricas apropiadas ──
                    # Slicear el M15 completo hasta el fake_time actual para que
                    # signal_agent evalúe condiciones históricas reales (RSI extremos)
                    # en lugar de ver siempre las últimas 80 velas del archivo.
                    _mkt_agent = agentes.get('market_agent')
                    if _mkt_agent and _m15_data and hasattr(_mkt_agent, '_buffer_m15'):
                        _ts_str = ts_candle.strftime("%Y-%m-%dT%H:%M:%SZ")
                        for _p, _candles in _m15_data.items():
                            if _p not in _mkt_agent._buffer_m15:
                                continue
                            _ts_list = _m15_ts_idx.get(_p, [])
                            _idx = _bisect.bisect_right(_ts_list, _ts_str)
                            if _idx >= 30:   # VELAS_MIN_M15 — necesitamos al menos 30
                                _slice = _candles[max(0, _idx - 700):_idx]
                                buf = _mkt_agent._buffer_m15[_p]
                                buf.clear()
                                buf.extend(_slice)
                    _sa = agentes.get('signal_agent')
                    _ra = agentes.get('risk_execution_agent')
                    if _sa and _ra:
                        import asyncio as _ap, traceback as _tb
                        _tracker_ref = self.tracker   # referencia para fix entry price
                        _pares_activos = list((_sa._params or {}).get('pares_activos') or [])

                        # ── Evaluar todos los pares EN PARALELO con asyncio.gather ──
                        # Antes: secuencial → deepseek-chat (par1) espera → deepseek-chat (par2) espera
                        # Ahora: ambos pares lanzan sus calls a DS simultáneamente →
                        #        tiempo total = max(t_par1, t_par2) en lugar de suma.
                        # Reducción estimada: ~40-50% del tiempo total de backtest.
                        async def _poll(_p, _s=_sa, _r=_ra, _tk=_tracker_ref):
                            try:
                                sn = await _s.evaluar(_p)
                            except Exception as _ee:
                                logging.error(f'  [POLL evaluar {_p}] {_ee}')
                                return
                            if not sn:
                                return
                            _now_fake = get_fake_time()
                            _last_sig = _pair_cooldown.get(_p)
                            _cd_mins = float((_s._params or {}).get(
                                'cooldown_minutes',
                                (_s._params or {}).get('cooldown_minutos', 15)))
                            if (_last_sig and _now_fake and
                                    (_now_fake - _last_sig).total_seconds() < _cd_mins * 60):
                                return
                            _pair_cooldown[_p] = _now_fake
                            # Fix entry price: usar precio real del tracker, no
                            # el close del M15 buffer (que puede ser horas anterior).
                            _cur_prices = _tk._prices.get(_p, {})
                            if _cur_prices:
                                _dir = sn.get("dir", "long")
                                _real_entry = (_cur_prices.get("ask", sn.get("entry", 1.0))
                                               if _dir == "long"
                                               else _cur_prices.get("bid", sn.get("entry", 1.0)))
                                sn["entry"] = _real_entry
                            logging.info(
                                f'  [SENAL] {_p} {sn.get("dir","?").upper()} '
                                f'conf={sn.get("conf",0):.0%} '
                                f'est={sn.get("estrategia","?")} '
                                f'entry={sn.get("entry",0):.5f} '
                                f'regime={sn.get("regime_score",0):.2f}'
                            )
                            try:
                                _res = await _r.procesar_senal(sn)
                                if _res:
                                    logging.info(
                                        f'  [TRADE OK] {_p} ' +
                                        str(sn.get("dir","?")).upper() +
                                        f' id={_res.get("trade_id","?")}'
                                    )
                                    # Guardar estrategia → oanda_id para calibración
                                    _tid_new = str(_res.get("oanda_id", ""))
                                    if _tid_new and _tid_new not in ("", "?"):
                                        self.tracker._estrategia_por_trade[_tid_new] = \
                                            sn.get("estrategia", "desconocida")
                                else:
                                    _mp = (_r._params or {}).get("max_posiciones", "?")
                                    logging.warning(
                                        f'  [RECHAZADO] {_p}: procesar_senal=None '
                                        f'capital={_r._capital:.2f} '
                                        f'pos={len(_r._pos_abiertas)}/{_mp}'
                                    )
                            except Exception as _re:
                                logging.error(f'  [TRADE ERROR] {_p}: {_re}')
                                logging.error(_tb.format_exc())

                        # Lanzar todos los pares en paralelo dentro de un único event loop
                        async def _poll_todos():
                            await _ap.gather(*[_poll(_pp) for _pp in _pares_activos])

                        try:
                            _ap.run(_poll_todos())
                        except RuntimeError as _rte:
                            # Si hay un event loop activo (poco probable), fallback secuencial
                            logging.error(f'  [asyncio gather ERROR]: {_rte} — fallback secuencial')
                            for _pp in _pares_activos:
                                try:
                                    _ap.run(_poll(_pp))
                                except Exception as _oe2:
                                    logging.error(f'  [OUTER ERROR {_pp}]: {_oe2}')
                        except Exception as _oe:
                            logging.error(f'  [OUTER ERROR gather]: {_oe}')
                _REAL_SLEEP(0)  # yield GIL; sin delay real (backtest acelerado)

            # ── Métricas de la semana ──────────────────────
            nuevos   = self.tracker.closed_trades[n_antes:]
            met      = self._metricas(nuevos)
            met["semana"]    = semana
            met["capital_fin"] = round(self.tracker.capital, 2)
            self.metricas_por_semana.append(met)

            wr    = met["win_rate"] * 100
            pf    = met["profit_factor"]
            pnl   = met["pnl_total"]
            dd    = met["max_dd_pct"]
            estado = "✅" if wr >= 55 and pf >= 1.4 else ("⚠️" if met["trades"] == 0 else "❌")

            logging.info(
                f"\n  {estado} Trades: {met['trades']:3d} | WR: {wr:5.1f}% | "
                f"PF: {pf:5.2f} | PnL: ${pnl:+.2f} | DD: {dd:.1f}%"
            )
            logging.info(f"     Capital: ${met['capital_fin']:.2f}")
            logging.info("─" * 55)

        # Señal de fin al stream
        for _ in range(10):
            try:
                self.tick_queue.put_nowait(None)
            except queue.Full:
                break

        # Detener agentes con stop() — evita flood de "cannot schedule" al cerrar asyncio
        for key, agent in agentes.items():
            if hasattr(agent, "stop"):
                try:
                    agent.stop()
                    logging.debug(f"  [shutdown] {key}.stop() llamado")
                except Exception:
                    pass

        # Esperar threads
        for t in threads:
            t.join(timeout=3)

        self._detener_patches()
        return self._guardar(fecha_inicio, fecha_fin)

    def _run_agent_safe(self, name: str, agent):
        import asyncio, inspect
        try:
            if inspect.iscoroutinefunction(agent.run):
                asyncio.run(agent.run())
            else:
                agent.run()
        except Exception as e:
            if "NoneType" not in str(e) and "StopIteration" not in str(e):
                logging.error(f"[{name}] run() terminó con error: {e}")

    def _start_agent_safe(self, name: str, agent):
        import asyncio, inspect
        try:
            if inspect.iscoroutinefunction(agent.start):
                asyncio.run(agent.start())
            else:
                agent.start()
        except Exception as e:
            if "NoneType" not in str(e) and "StopIteration" not in str(e):
                logging.error(f"[{name}] start() terminó con error: {e}")

    # ── Guardar resultado ──────────────────────────────────
    def _guardar(self, fecha_inicio: datetime, fecha_fin: datetime) -> dict:
        met_tot = self._metricas(self.tracker.closed_trades)
        cap_fin = self.tracker.capital
        retorno = ((cap_fin - self.capital) / self.capital) * 100

        logging.info("\n" + "═" * 60)
        logging.info("RESUMEN FINAL — BACKTEST AGENTES REALES")
        logging.info("═" * 60)
        logging.info(f"Capital inicial:   ${self.capital:.2f}")
        logging.info(f"Capital final:     ${cap_fin:.2f}")
        logging.info(f"Retorno total:     {retorno:+.2f}%")
        logging.info(f"Trades totales:    {met_tot['trades']}")
        logging.info(f"Win Rate global:   {met_tot['win_rate']*100:.1f}%")
        logging.info(f"Profit Factor:     {met_tot['profit_factor']:.2f}")
        logging.info(f"Max Drawdown:      {met_tot['max_dd_pct']:.1f}%")

        logging.info("\n  CRITERIOS PARA LIVE TRADING:")
        wr_g = met_tot["win_rate"] * 100
        pf_g = met_tot["profit_factor"]
        dd_g = met_tot["max_dd_pct"]
        logging.info(f"  {'✅' if wr_g >= 55 else '❌'} WR ≥ 55%:    {wr_g:.1f}%")
        logging.info(f"  {'✅' if pf_g >= 1.4 else '❌'} PF ≥ 1.4:    {pf_g:.2f}")
        logging.info(f"  {'✅' if dd_g < 6 else '❌'} MaxDD < 6%:  {dd_g:.1f}%")
        listo = wr_g >= 55 and pf_g >= 1.4 and dd_g < 6
        logging.info("\n  " + ("LISTO para considerar live trading"
                               if listo else "Continuar paper trading"))

        resultado = {
            "metadata": {
                "tipo":            "backtest_agentes_reales",
                "fecha_ejecucion": _real_datetime_cls.now().isoformat(),
                "periodo":         f"{fecha_inicio.date()} → {fecha_fin.date()}",
                "capital_inicial": self.capital,
                "capital_final":   round(cap_fin, 4),
                "retorno_pct":     round(retorno, 4),
                "n_semanas":       self.n_semanas,
                "pares":           self.pares,
            },
            # Resumen plano — compatible con run_backtest_anual.py
            "resumen": {
                "capital_final":    round(cap_fin, 4),
                "pnl_total":        round(cap_fin - self.capital, 4),
                "pnl_pct":          round(retorno, 2),
                "total_trades":     met_tot["trades"],
                "win_rate_global":  met_tot["win_rate"],
                "profit_factor":    met_tot["profit_factor"],
                "max_drawdown_pct": met_tot["max_dd_pct"],
            },
            "metricas_globales":    met_tot,
            "metricas_por_semana":  self.metricas_por_semana,
            "calibraciones":        self._calibraciones_log,
            "trades": [
                {k: (str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v)
                 for k, v in t.items() if not k.startswith("_")}
                for t in self.tracker.closed_trades
            ],
        }

        out = Path(RESULTS_FILE)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)
        logging.info(f"\nResultados: {out}")

        # ── Calibración final con Rolling Window ──────────────────────────────
        # Usa ventana deslizante de 4 semanas para determinar qué estrategias
        # y parámetros usar en live trading. Se guarda en strategy_params.json.
        self._guardar_calibracion_rolling(resultado)

        return resultado

    def _guardar_calibracion_rolling(self, resultado: dict):
        """
        Calcula la mejor calibración usando rolling window de las últimas 4
        semanas y guarda en strategy_params.json para usarla en live trading.

        Criterios:
          - Estrategia activa si WR >= 50% Y n_trades >= 3 en la ventana
          - Si ninguna cumple, se mantienen todas (nunca quedar sin estrategias)
          - min_confidence se baja si WR global < 45% (señal de mercado difícil)
          - riesgo_pct se reduce si MaxDD > 4%
        """
        trades_todos = self.tracker.closed_trades
        semanas_met  = self.metricas_por_semana
        n_sem        = len(semanas_met)

        if not trades_todos:
            logging.warning("[CAL-ROLLING] Sin trades — calibración no actualizada")
            return

        # ── Ventana: últimas 4 semanas (o todas si < 4) ───────────────────────
        ventana = min(4, n_sem)
        # Trades de las últimas `ventana` semanas en orden cronológico
        # Los trades ya están en orden de cierre en closed_trades
        n_trades_ventana = sum(
            m.get("trades", 0) for m in semanas_met[-ventana:]
        )
        trades_ventana = trades_todos[-n_trades_ventana:] if n_trades_ventana else trades_todos

        logging.info(
            f"\n[CAL-ROLLING] Ventana: últimas {ventana} semanas "
            f"| {len(trades_ventana)} trades"
        )

        # ── Métricas globales con peso EXPONENCIAL por semana ────────────────
        # Semanas más recientes pesan más: semana 0 (más antigua) = DECAY^(n-1)
        # semana n-1 (más reciente) = DECAY^0 = 1.0
        # Una semana de hace 12 semanas vale ~50% de la última (DECAY=0.944)
        DECAY = 0.85  # factor de decaimiento por semana hacia atrás
        wr_global = pf_global = dd_global = 0.0
        if trades_ventana and semanas_met:
            # Distribuir trades por semana según índice
            trades_por_semana = []
            _n_antes = 0
            for _sem_met in semanas_met[-ventana:]:
                _n_sem = _sem_met.get("trades", 0)
                trades_por_semana.append(trades_ventana[_n_antes:_n_antes + _n_sem])
                _n_antes += _n_sem

            # Calcular WR ponderado exponencialmente
            _n_semanas = len(trades_por_semana)
            _peso_total = 0.0
            _wins_pond  = 0.0
            _total_pond = 0.0
            _pnl_pos_pond = 0.0
            _pnl_neg_pond = 0.0
            for _i, _sem_trades in enumerate(trades_por_semana):
                # i=0 es la semana más antigua → peso más bajo
                _peso = DECAY ** (_n_semanas - 1 - _i)
                _peso_total += _peso * len(_sem_trades)
                for _t in _sem_trades:
                    _pnl_t = float(_t.get("pnl", 0) or 0)
                    _wins_pond  += _peso * (1 if _pnl_t > 0 else 0)
                    _total_pond += _peso
                    if _pnl_t > 0:
                        _pnl_pos_pond += _peso * _pnl_t
                    else:
                        _pnl_neg_pond += _peso * abs(_pnl_t)

            wr_global = (_wins_pond / _total_pond) if _total_pond > 0 else 0.0
            pf_global = (_pnl_pos_pond / _pnl_neg_pond) if _pnl_neg_pond > 0 else 0.0
            dd_global = min(m.get("max_dd_pct", 0) for m in semanas_met[-ventana:])

            logging.info(
                f"[CAL-ROLLING] WR ponderado (exp decay={DECAY}): "
                f"{wr_global:.1%} | PF: {pf_global:.2f}"
            )

        # ── Métricas por estrategia en la ventana ─────────────────────────────
        # Nombres válidos: nunca incluir artefactos como "desconocida" o "backtest"
        _ESTRATS_INVALIDAS = {"desconocida", "backtest", "unknown", "", None}
        por_estrat = {}
        for t in trades_ventana:
            est = t.get("estrategia", "")
            if est in _ESTRATS_INVALIDAS:
                continue  # ignorar trades sin estrategia real
            if est not in por_estrat:
                por_estrat[est] = {"wins": 0, "total": 0, "pnl": 0.0}
            pnl_t = float(t.get("pnl", 0) or 0)
            por_estrat[est]["total"] += 1
            por_estrat[est]["pnl"]   += pnl_t
            if pnl_t > 0:
                por_estrat[est]["wins"] += 1

        # ── Leer params actuales como base (antes de evaluar estrategias) ───────
        params_base = {}
        try:
            cal_file = BASE_DIR / "data" / "calibration" / "strategy_params.json"
            if cal_file.exists():
                with open(cal_file, encoding="utf-8") as fh:
                    params_base = json.load(fh)
        except Exception:
            pass

        logging.info(f"[CAL-ROLLING] Métricas por estrategia (ventana {ventana}s):")
        estrats_activas   = []
        estrats_pausadas  = []
        for est, m in sorted(por_estrat.items()):
            wr_e = m["wins"] / m["total"] if m["total"] else 0
            emoji = "✅" if wr_e >= 0.50 and m["total"] >= 3 else "⏸"
            logging.info(
                f"  {emoji} {est}: WR={wr_e:.0%} "
                f"n={m['total']} PnL=${m['pnl']:+.2f}"
            )
            if wr_e >= 0.50 and m["total"] >= 3:
                estrats_activas.append(est)
            else:
                estrats_pausadas.append(est)

        # Nunca dejar sin estrategias — usar la lista actual del JSON como fallback
        if not estrats_activas:
            logging.warning(
                "[CAL-ROLLING] Ninguna estrategia cumple criterios — "
                "manteniendo lista actual"
            )
            _fallback = [e for e in params_base.get("estrategias_activas", [])
                         if e not in _ESTRATS_INVALIDAS]
            estrats_activas  = _fallback or ["RSI_Bollinger", "RSI_Divergence"]
            estrats_pausadas = []

        # ── Ajuste dinámico de parámetros ─────────────────────────────────────

        # Parámetros protegidos: nunca se auto-ajustan (valores manuales del analista)
        _PROTECTED_KEYS = {"min_sl_pips", "max_sl_pips", "adx_max_rsi_bollinger", "sl_atr_mult"}

        # min_confidence: bajar si el mercado fue difícil (WR < 45%)
        # Floor en 0.40 — proteger el valor de checkpoint establecido manualmente.
        # NOTA: No ajustar si la ventana tiene menos de 5 trades — esto evita
        # penalizar falsamente cuando el circuit breaker bloqueó el trading y
        # la ventana de 4 semanas quedó vacía (WR=0% por falta de trades).
        _MIN_CONFIDENCE_FLOOR = 0.40
        min_conf = params_base.get("min_confidence", 0.40)
        _trades_en_ventana = len(trades_ventana)
        if _trades_en_ventana < 5:
            logging.info(
                f"[CAL-ROLLING] Solo {_trades_en_ventana} trades en ventana — "
                f"min_confidence sin cambio (evitar ajuste con muestra insuficiente)"
            )
        elif wr_global < 0.45:
            min_conf = max(_MIN_CONFIDENCE_FLOOR, min_conf - 0.05)
            logging.info(f"[CAL-ROLLING] WR bajo → min_confidence ajustado a {min_conf} (floor={_MIN_CONFIDENCE_FLOOR})")
        elif wr_global >= 0.60:
            min_conf = min(0.55, min_conf + 0.05)
            logging.info(f"[CAL-ROLLING] WR alto → min_confidence ajustado a {min_conf}")

        # riesgo_pct: reducir si drawdown > 4%
        riesgo = params_base.get("riesgo_pct", 0.01)
        if abs(dd_global) > 4.0:
            riesgo = max(0.005, riesgo * 0.8)
            logging.info(f"[CAL-ROLLING] DD alto → riesgo_pct ajustado a {riesgo:.3f}")
        elif wr_global >= 0.60 and pf_global >= 1.5:
            riesgo = min(0.02, riesgo * 1.1)
            logging.info(f"[CAL-ROLLING] Rendimiento sólido → riesgo_pct ajustado a {riesgo:.3f}")

        # ── Construir calibración final ───────────────────────────────────────
        from datetime import datetime as _dt_real
        calibracion = {
            **params_base,
            "estrategias_activas":  estrats_activas,
            "estrategias_pausadas": estrats_pausadas,
            "min_confidence":       round(min_conf, 3),
            "riesgo_pct":           round(riesgo, 4),
            "calibrado_en":         _real_datetime_cls.now().isoformat(),
            "calibrado_por":        "backtest_rolling_window",
            "ventana_semanas":      ventana,
            "trades_analizados":    len(trades_ventana),
            "wr_ventana":           round(wr_global, 3),
            "pf_ventana":           round(pf_global, 3),
            "notas_analista":       (
                f"Rolling window {ventana}s: WR={wr_global:.0%} "
                f"PF={pf_global:.2f} | "
                f"Estrategias activas: {', '.join(estrats_activas)}"
            ),
        }
        # Restaurar parámetros protegidos — sobrescriben cualquier ajuste automático
        for _pk in _PROTECTED_KEYS:
            if _pk in params_base:
                calibracion[_pk] = params_base[_pk]

        # ── Guardar ───────────────────────────────────────────────────────────
        cal_out = BASE_DIR / "data" / "calibration" / "strategy_params.json"
        with open(cal_out, "w", encoding="utf-8") as fh:
            json.dump(calibracion, fh, ensure_ascii=False, indent=2)

        logging.info("\n" + "═" * 60)
        logging.info("📐 CALIBRACIÓN ROLLING WINDOW GUARDADA")
        logging.info("═" * 60)
        logging.info(f"  Archivo:       {cal_out}")
        logging.info(f"  Ventana:       últimas {ventana} semanas")
        logging.info(f"  WR ventana:    {wr_global:.1%}")
        logging.info(f"  PF ventana:    {pf_global:.2f}")
        logging.info("  Estrategias:   " + ", ".join(estrats_activas))
        logging.info(f"  WR global cal: {wr_global:.1%}")
        logging.info(f"  Score:         {score_nuevo:.4f}")
        logging.info("═" * 60)
        return calibracion


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backtest Harness — Trading Bot v11")
    parser.add_argument("--semanas", type=int, default=8, help="Número de semanas a simular")
    args = parser.parse_args()
    harness = BacktestHarness(n_semanas=args.semanas)
    harness.correr()
    harness.ejecutar()
