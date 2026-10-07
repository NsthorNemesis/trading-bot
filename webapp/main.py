"""
webapp/main.py
Backend FastAPI para la Telegram Mini App del Trading Bot v11.

Endpoints:
  GET  /api/candles          → OHLCV desde OANDA (pair, tf, count)
  GET  /api/params           → strategy_params.json actual
  POST /api/params           → actualiza un campo (requiere X-Bot-Token)
  GET  /api/signals          → últimas señales (trades + shadow)
  GET  /api/status           → estado resumido del bot
  GET  /api/stream           → SSE — push de señales en tiempo real
  GET  /api/system           → estado del sistema (requiere X-Session-Token)
  GET  /api/logs             → tail del log del bot (requiere X-Session-Token)
  POST /api/system/restart   → reinicia trading_bot/webapp (requiere X-Session-Token)
  GET  /                     → sirve index.html
  GET  /static/*             → archivos estáticos

Arrancar: uvicorn webapp.main:app --host 0.0.0.0 --port 8080 --workers 1
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# ── Paths y config ────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
PARAMS_FILE = BASE_DIR / "data" / "calibration" / "strategy_params.json"
TRADES_LOG  = BASE_DIR / "logs" / "trades.json"
SHADOW_LOG  = BASE_DIR / "logs" / "shadow_signals.jsonl"
BOT_LOG     = BASE_DIR / "logs" / "trading_bot.log"
STATIC_DIR  = Path(__file__).parent / "static"

OANDA_TOKEN      = os.getenv("OANDA_ACCESS_TOKEN", "")
OANDA_ENV        = os.getenv("OANDA_ENVIRONMENT", "practice")
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")[:20]
WEBAPP_PASSWORD  = os.getenv("WEBAPP_PASSWORD", "")
if not WEBAPP_PASSWORD:
    raise RuntimeError("WEBAPP_PASSWORD no configurado en .env — el webapp no arranca sin contraseña")

# ── Sesiones (fase 2i): tokens únicos con expiración, no más token estático ──
_SESSIONS: dict[str, float] = {}  # token -> timestamp de expiración
_SESSION_TTL = 24 * 3600  # 24 horas

def _crear_sesion() -> str:
    """Genera un token de sesión único con expiración."""
    _limpiar_sesiones()
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    return token

def _limpiar_sesiones() -> None:
    """Elimina sesiones expiradas."""
    ahora = time.time()
    for tok in [t for t, exp in _SESSIONS.items() if exp < ahora]:
        del _SESSIONS[tok]

def _check_token(request: Request) -> bool:
    """Verifica el token en header X-Session-Token contra sesiones activas."""
    token = request.headers.get("X-Session-Token", "")
    if not token:
        return False
    exp = _SESSIONS.get(token)
    if exp is None:
        return False
    if exp < time.time():
        del _SESSIONS[token]
        return False
    return True

def _invalidar_sesion(request: Request) -> bool:
    """Cierra la sesión del token actual."""
    token = request.headers.get("X-Session-Token", "")
    return _SESSIONS.pop(token, None) is not None

logger = logging.getLogger("webapp")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Trading Bot v11 Mini App", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nsthor.duckdns.org"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Montar estáticos
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=False), name="static")

# Middleware anti-caché para todos los archivos estáticos y la raíz
@app.middleware("http")
async def no_cache_middleware(request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", ) or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
    return response

# ── OANDA helper ──────────────────────────────────────────────────────────────
def _oanda_client() -> oandapyV20.API:
    return oandapyV20.API(
        access_token=OANDA_TOKEN,
        environment=OANDA_ENV,
    )


def _fetch_candles(pair: str, tf: str, count: int) -> list[dict]:
    """Descarga candles de OANDA y retorna lista de dicts {time, open, high, low, close, volume}."""
    # Mapeo frontend → granularidad OANDA
    tf_map = {"M1": "M1", "M5": "M5", "M15": "M15", "H1": "H1", "H4": "H4", "D1": "D"}
    granularity = tf_map.get(tf, tf)
    # OANDA permite máx 5000 candles por request
    client = _oanda_client()
    params = {"granularity": granularity, "count": min(count, 5000), "price": "M"}
    ep = instruments.InstrumentsCandles(instrument=pair, params=params)
    client.request(ep)
    result = []
    for c in ep.response.get("candles", []):
        mid = c.get("mid", {})
        result.append({
            "time":   int(datetime.fromisoformat(c["time"].replace("Z", "+00:00")).timestamp()),
            "open":   float(mid.get("o", 0)),
            "high":   float(mid.get("h", 0)),
            "low":    float(mid.get("l", 0)),
            "close":  float(mid.get("c", 0)),
            "volume": int(c.get("volume", 0)),
        })
    return result


# ── Params helper ─────────────────────────────────────────────────────────────
def _load_params() -> dict:
    return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))


def _save_params(params: dict) -> None:
    PARAMS_FILE.write_text(
        json.dumps(params, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Indicadores técnicos (calculados en servidor) ─────────────────────────────
def _calc_ema(closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return result
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result[period - 1] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = ema
    return result


def _calc_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / period, sum(losses) / period
    if al == 0:
        result[period] = 100.0
    else:
        result[period] = 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        result[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return result


def _calc_bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0):
    upper: list[float | None] = [None] * len(closes)
    middle: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        m = sum(window) / period
        std = (sum((x - m) ** 2 for x in window) / period) ** 0.5
        middle[i] = m
        upper[i]  = m + std_mult * std
        lower[i]  = m - std_mult * std
    return upper, middle, lower


def _enrich_candles(candles: list[dict]) -> dict:
    """Calcula todos los indicadores necesarios para las 6 estrategias."""
    closes = [c["close"] for c in candles]
    ts = [c["time"] for c in candles]

    ema9  = _calc_ema(closes, 9)
    ema21 = _calc_ema(closes, 21)
    ema50 = _calc_ema(closes, 50)
    rsi   = _calc_rsi(closes, 14)
    bb_up, bb_mid, bb_low = _calc_bollinger(closes, 20)

    def series(values):
        return [{"time": t, "value": v} for t, v in zip(ts, values) if v is not None]

    return {
        "ema9":    series(ema9),
        "ema21":   series(ema21),
        "ema50":   series(ema50),
        "rsi":     series(rsi),
        "bb_upper": series(bb_up),
        "bb_middle": series(bb_mid),
        "bb_lower": series(bb_low),
    }


# ── SSE — cola de eventos ─────────────────────────────────────────────────────
_sse_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
_last_shadow_size: int = 0


async def _shadow_watcher():
    """Lee shadow_signals.jsonl y envía nuevas líneas al SSE queue."""
    global _last_shadow_size
    while True:
        await asyncio.sleep(10)
        try:
            if not SHADOW_LOG.exists():
                continue
            size = SHADOW_LOG.stat().st_size
            if size > _last_shadow_size:
                with SHADOW_LOG.open() as f:
                    lines = f.readlines()
                new_lines = lines[max(0, len(lines) - 5):]  # últimas 5
                for line in new_lines:
                    try:
                        sig = json.loads(line.strip())
                        await _sse_queue.put({"type": "shadow", "data": sig})
                    except Exception:
                        pass
                _last_shadow_size = size
        except Exception as e:
            logger.warning(f"shadow_watcher error: {e}")


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_shadow_watcher())


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/auth")
async def auth(request: Request):
    """
    Verifica la contraseña y retorna un token de sesión único (24h).
    Body: {"password": "..."}
    El token se guarda en localStorage y se envía en X-Session-Token en cada request.
    """
    body = await request.json()
    password = body.get("password", "")
    if not password or not hmac.compare_digest(password, WEBAPP_PASSWORD):
        raise HTTPException(status_code=401, detail="Contraseña incorrecta")
    return {"token": _crear_sesion(), "ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    """Invalida la sesión actual."""
    _invalidar_sesion(request)
    return {"ok": True}


@app.get("/api/ping")
async def ping():
    """Health check público — no requiere auth."""
    return {"ok": True, "ts": time.time()}


@app.get("/")
async def root():
    """Sirve la Mini App con headers anti-caché para Telegram."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(
            str(index),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma":        "no-cache",
                "Expires":       "0",
            }
        )
    return HTMLResponse("<h1>Trading Bot v11</h1><p>index.html not found</p>")


@app.get("/api/candles")
async def get_candles(
    pair:  str = Query("EUR_USD"),
    tf:    str = Query("M15"),
    count: int = Query(200, ge=10, le=5000),
    indicators: bool = Query(True),
):
    """Retorna candles OHLCV + indicadores técnicos opcionales."""
    try:
        candles = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_candles, pair, tf, count
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OANDA error: {e}")

    response: dict[str, Any] = {"pair": pair, "tf": tf, "candles": candles}
    if indicators and candles:
        response["indicators"] = _enrich_candles(candles)
    return response


@app.get("/api/params")
async def get_params():
    """Retorna strategy_params.json."""
    try:
        return _load_params()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/params")
async def update_param(request: Request):
    """
    Actualiza un campo de strategy_params.json.
    Body global:       {"campo": "min_confidence", "valor": 0.75}
    Body per_strategy: {"campo": "min_confidence", "valor": 0.80, "estrategia": "RSI_Bollinger"}
    Header: X-Bot-Token
    """
    if not _check_token(request):
        raise HTTPException(status_code=401, detail="Sesión no válida — recarga e inicia sesión")

    body    = await request.json()
    campo   = body.get("campo", "")
    valor   = body.get("valor")
    estrat  = body.get("estrategia", "")   # si viene → editar per_strategy

    CAMPOS_FLOAT = {
        "min_confidence", "riesgo_pct", "rr_ratio", "sl_atr_mult",
        "min_win_rate", "max_drawdown_dia", "circuit_breaker_pct",
        "adx_max_rsi_bollinger", "adx_max_hammer",
        "engulfing_cierre_pct", "rsi_div_min_pts",
        "doji_rsi_low", "doji_rsi_high",
    }
    CAMPOS_INT = {
        "adx_min_operar", "cooldown_minutes", "max_posiciones",
        "max_pos_par", "max_consecutive_losses", "min_sl_pips",
        "max_sl_pips", "max_trade_hours",
        "rsi_divergencia_ventana", "rsi_div_frescura",
    }
    CAMPOS_BOOL  = {"filtro_h4_activo", "hammer_m15_confirm", "m1_entry_refinement", "activa", "use_deepseek"}
    CAMPOS_LISTA = {"estrategias_activas", "estrategias_pausadas", "pares_activos", "pares_pausados"}

    ESTRATEGIAS_VALIDAS = {"EMA_Crossover","Engulfing","RSI_Bollinger","RSI_Divergence","Hammer","Doji"}

    if campo not in (CAMPOS_FLOAT | CAMPOS_INT | CAMPOS_BOOL | CAMPOS_LISTA):
        raise HTTPException(status_code=400, detail=f"Campo no permitido: {campo}")

    try:
        params = _load_params()

        if estrat:
            # ── Guardar en per_strategy[estrat][campo] ────────────────────────
            if estrat not in ESTRATEGIAS_VALIDAS:
                raise HTTPException(status_code=400, detail=f"Estrategia no válida: {estrat}")
            per = params.setdefault("per_strategy", {})
            cfg = per.setdefault(estrat, {})
            if campo in CAMPOS_FLOAT:
                cfg[campo] = float(valor)
            elif campo in CAMPOS_INT:
                cfg[campo] = int(valor)
            elif campo in CAMPOS_BOOL:
                cfg[campo] = bool(valor)
            # Sincronizar activa/inactiva con listas globales
            if campo == "activa":
                activas  = list(params.get("estrategias_activas",  []))
                pausadas = list(params.get("estrategias_pausadas", []))
                if bool(valor):
                    if estrat not in activas:  activas.append(estrat)
                    if estrat in pausadas:     pausadas.remove(estrat)
                else:
                    if estrat in activas:      activas.remove(estrat)
                    if estrat not in pausadas: pausadas.append(estrat)
                params["estrategias_activas"]  = activas
                params["estrategias_pausadas"] = pausadas
            _save_params(params)
            nuevo = cfg.get(campo, valor)
            await _sse_queue.put({"type": "params_changed", "data": {"per_strategy": per}})
            return {"ok": True, "campo": campo, "estrategia": estrat, "nuevo_valor": nuevo}

        else:
            # ── Guardar en raíz (parámetro global) ───────────────────────────
            if campo in CAMPOS_FLOAT:
                params[campo] = float(valor)
            elif campo in CAMPOS_INT:
                params[campo] = int(valor)
            elif campo in CAMPOS_BOOL:
                params[campo] = bool(valor)
            elif campo in CAMPOS_LISTA:
                params[campo] = list(valor)
            _save_params(params)
            await _sse_queue.put({"type": "params_changed", "data": {campo: params[campo]}})
            return {"ok": True, "campo": campo, "nuevo_valor": params[campo]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _es_senal_valida(s: dict) -> bool:
    """Filtra señales obsoletas de versiones anteriores del bot."""
    nombre = (s.get("estrategia") or s.get("strategy") or
              s.get("nombre") or s.get("name") or "")
    # Eliminar señales del sistema antiguo con DeepSeek/MultiAgente
    fantasmas = ["MultiAgente", "v14", "reasoner", "deepseek", "briefing"]
    return not any(f.lower() in nombre.lower() for f in fantasmas)


@app.get("/api/signals")
async def get_signals(
    limit: int  = Query(50, ge=5, le=200),
    days:  int  = Query(30, ge=1, le=365),
    tipo:  str  = Query("all"),   # "all" | "trades" | "shadow"
):
    """Retorna señales separadas por tipo, filtradas y ordenadas."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    trades_list  = []
    shadows_list = []

    # ── Trades ejecutados ────────────────────────────────────────────────────
    if tipo in ("all", "trades") and TRADES_LOG.exists():
        try:
            raw = json.loads(TRADES_LOG.read_text())
            if isinstance(raw, list):
                for t in raw:
                    if not isinstance(t, dict): continue
                    if not _es_senal_valida(t): continue
                    ts = t.get("opened_at") or t.get("timestamp") or ""
                    if ts < cutoff: continue
                    t["_tipo"] = "trade"
                    trades_list.append(t)
        except Exception:
            pass
        trades_list.sort(key=lambda x: x.get("opened_at", x.get("timestamp", "")), reverse=True)
        trades_list = trades_list[:limit]

    # ── Shadow signals ────────────────────────────────────────────────────────
    if tipo in ("all", "shadow") and SHADOW_LOG.exists():
        try:
            lines = SHADOW_LOG.read_text().strip().splitlines()
            seen  = set()   # deduplicar por (estrategia, par, dir, hora)
            for line in reversed(lines):
                try:
                    s = json.loads(line)
                    if not isinstance(s, dict): continue
                    if not _es_senal_valida(s): continue
                    ts = s.get("ts") or s.get("timestamp") or ""
                    if ts < cutoff: continue
                    # Deduplicar: misma estrategia+par+dir en la misma hora
                    key = (s.get("estrategia",""), s.get("par",""),
                           s.get("dir_hint",""), ts[:13])
                    if key in seen: continue
                    seen.add(key)
                    s["_tipo"] = "shadow"
                    shadows_list.append(s)
                    if len(shadows_list) >= limit: break
                except Exception:
                    continue
        except Exception:
            pass
        shadows_list.sort(key=lambda x: x.get("ts", x.get("timestamp", "")), reverse=True)

    return {
        "trades":  trades_list,
        "shadows": shadows_list,
        "total_trades":  len(trades_list),
        "total_shadows": len(shadows_list),
    }


@app.get("/api/status")
async def get_status():
    """Estado resumido del bot."""
    try:
        params = _load_params()
    except Exception:
        params = {}

    # Leer últimas líneas del log (fase2f-fix: con deque, sin cargar el
    # archivo entero en memoria — el log supera 1GB y causaba OOM kills)
    log_tail: list[str] = []
    if BOT_LOG.exists():
        try:
            from collections import deque
            with BOT_LOG.open(errors="replace") as f:
                log_tail = [l.strip() for l in deque(f, maxlen=10) if l.strip()]
        except Exception:
            pass

    # Contar trades del día
    trades_hoy = 0
    if TRADES_LOG.exists():
        try:
            trades = json.loads(TRADES_LOG.read_text())
            hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            trades_hoy = sum(
                1 for t in trades
                if isinstance(t, dict) and t.get("timestamp", "").startswith(hoy)
            )
        except Exception:
            pass

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "estrategias_activas": params.get("estrategias_activas", []),
        "estrategias_pausadas": params.get("estrategias_pausadas", []),
        "pares_activos": params.get("pares_activos", []),
        "min_confidence": params.get("min_confidence"),
        "riesgo_pct": params.get("riesgo_pct"),
        "adx_min_operar": params.get("adx_min_operar"),
        "trades_hoy": trades_hoy,
        "log_tail": log_tail,
    }




@app.get("/api/performance")
async def get_performance(days: int = Query(30, ge=1, le=365)):
    """Curva de equity acumulada + stats por estrategia y par."""
    from datetime import timedelta
    trades: list[dict] = []
    if TRADES_LOG.exists():
        try:
            raw = json.loads(TRADES_LOG.read_text())
            trades = [t for t in raw if isinstance(t, dict) and "pnl" in t]
        except Exception:
            pass

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    trades = [t for t in trades
              if str(t.get("opened_at", t.get("timestamp", "")))[:10] >= cutoff]
    trades_sorted = sorted(trades, key=lambda t: t.get("opened_at", t.get("timestamp", "")))

    equity_curve, acum = [], 0.0
    for t in trades_sorted:
        acum += float(t.get("pnl", 0))
        ts_raw = t.get("closed_at", t.get("opened_at", t.get("timestamp", "")))
        try:
            ts = int(datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp())
            equity_curve.append({"time": ts, "value": round(acum, 5)})
        except Exception:
            continue

    n = len(trades)
    wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
    pnl_total = sum(float(t.get("pnl", 0)) for t in trades)
    pnl_list  = [float(t.get("pnl", 0)) for t in trades]

    max_dd, peak, acum2 = 0.0, 0.0, 0.0
    for t in trades_sorted:
        acum2 += float(t.get("pnl", 0))
        peak = max(peak, acum2)
        max_dd = max(max_dd, peak - acum2)

    def _agg(key_fn):
        agg: dict = {}
        for t in trades:
            k = key_fn(t)
            s = agg.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
            pnl = float(t.get("pnl", 0))
            s["n"] += 1; s["pnl"] += pnl
            if pnl > 0: s["wins"] += 1
        return {k: {"n": v["n"], "wins": v["wins"],
                    "wr": round(v["wins"]/v["n"], 3) if v["n"] else 0,
                    "pnl": round(v["pnl"], 5)} for k, v in agg.items()}

    markers = []
    for t in trades_sorted[-200:]:
        ts_raw = t.get("opened_at", t.get("timestamp", ""))
        try:
            ts = int(datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp())
            markers.append({"time": ts,
                "pair": t.get("par", t.get("instrument", "")),
                "direction": t.get("direccion", t.get("side", "")),
                "pnl": round(float(t.get("pnl", 0)), 5),
                "strategy": t.get("estrategia", t.get("strategy", ""))})
        except Exception:
            continue

    return {
        "days": days, "n_trades": n, "wins": wins,
        "wr": round(wins/n, 3) if n else 0,
        "pnl_total": round(pnl_total, 5),
        "best_trade":  round(max(pnl_list), 5) if pnl_list else 0,
        "worst_trade": round(min(pnl_list), 5) if pnl_list else 0,
        "max_drawdown": round(max_dd, 5),
        "equity_curve": equity_curve,
        "by_strategy": _agg(lambda t: t.get("estrategia", t.get("strategy", "?"))),
        "by_pair":     _agg(lambda t: t.get("par", t.get("instrument", "?"))),
        "trade_markers": markers,
    }

@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events — push de señales y cambios de params."""
    async def generator():
        # Ping inicial
        yield {"event": "connected", "data": json.dumps({"ts": time.time()})}
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(_sse_queue.get(), timeout=30)
                yield {
                    "event": event["type"],
                    "data":  json.dumps(event["data"]),
                }
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"ts": time.time()})}
            except Exception:
                break


    return EventSourceResponse(generator())


# -- /api/system -----------------------------------------------------------
@app.get("/api/system")
async def get_system(request: Request):
    """Estado del sistema: recursos VPS, agentes, OANDA, ultimo trade."""
    if not _check_token(request):
        raise HTTPException(status_code=401, detail="No autorizado")

    import subprocess

    result: dict[str, Any] = {}

    try:
        import psutil
        result["cpu_pct"]     = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        result["ram_pct"]     = round(vm.percent, 1)
        result["ram_used_mb"] = round(vm.used / 1024**2)
        result["ram_total_mb"]= round(vm.total / 1024**2)
        du = psutil.disk_usage("/")
        result["disk_pct"]    = round(du.percent, 1)
        result["disk_free_gb"]= round(du.free / 1024**3, 1)
    except Exception as e:
        result["psutil_error"] = str(e)

    for svc in ["trading_bot", "webapp"]:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", f"{svc}.service"],
                capture_output=True, text=True, timeout=3
            )
            result[f"svc_{svc}"] = r.stdout.strip()
        except Exception:
            result[f"svc_{svc}"] = "unknown"

    # Uptime del bot
    try:
        r = subprocess.run(
            ["systemctl", "show", "trading_bot.service", "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=3
        )
        ts_str = r.stdout.strip().replace("ActiveEnterTimestamp=", "")
        if ts_str:
            from datetime import datetime, timezone
            # Parse systemd timestamp (e.g. "Mon 2026-06-01 03:07:41 UTC")
            try:
                started = datetime.strptime(ts_str, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                uptime_sec = int((datetime.now(timezone.utc) - started).total_seconds())
                h, rem = divmod(uptime_sec, 3600)
                m = rem // 60
                result["bot_uptime"] = f"{h}h {m}m"
            except Exception:
                result["bot_uptime"] = ts_str[:19]
    except Exception:
        result["bot_uptime"] = "–"

    # Estrategias y pares activos desde config
    try:
        params_path = BASE_DIR / "data" / "calibration" / "strategy_params.json"
        cfg = json.loads(params_path.read_text())
        result["estrategias_activas"]  = cfg.get("estrategias_activas", [])
        result["estrategias_pausadas"] = cfg.get("estrategias_pausadas", [])
        result["pares_activos"]        = cfg.get("pares_activos", [])
        result["pares_pausados"]       = cfg.get("pares_pausados", [])
        result["sesiones_activas"]     = cfg.get("sesiones_activas", [])
        result["sesiones_pausadas"]    = cfg.get("sesiones_pausadas", [])
    except Exception:
        pass

    # Trades abiertos ahora mismo
    try:
        raw = json.loads(TRADES_LOG.read_text()) if TRADES_LOG.exists() else []
        abiertos = [t for t in raw if isinstance(t, dict) and t.get("estado") == "abierto"]
        result["trades_abiertos"] = len(abiertos)
        result["trades_abiertos_detalle"] = [
            {"par": t.get("par","?"), "dir": t.get("dir","?"),
             "estrategia": t.get("estrategia","?"), "pnl": round(float(t.get("pnl",0)),4)}
            for t in abiertos
        ]
    except Exception:
        result["trades_abiertos"] = 0

    # Último error en el log del bot
    try:
        log_path = BASE_DIR / "logs" / "trading_bot.log"
        if not log_path.exists():
            # Intentar obtener del journal
            r = subprocess.run(
                ["journalctl", "-u", "trading_bot.service", "-n", "50", "--no-pager", "-o", "short"],
                capture_output=True, text=True, timeout=5
            )
            lines = r.stdout.splitlines()
        else:
            lines = log_path.read_text().splitlines()[-200:]

        _ignorar = ["This Application is not running", "NoneType", "unattended-upgrade", "connection reset"]
        errores = [l for l in lines if any(w in l.upper() for w in ["ERROR", "EXCEPTION", "TRACEBACK", "CRITICAL"])
                   and not any(ig.lower() in l.lower() for ig in _ignorar)]
        if errores:
            result["ultimo_error"] = errores[-1][-150:]
            result["errores_recientes"] = len(errores)
        else:
            result["ultimo_error"] = None
            result["errores_recientes"] = 0
    except Exception:
        result["ultimo_error"] = None
        result["errores_recientes"] = 0

    try:
        import oandapyV20.endpoints.accounts as accounts
        OANDA_TOKEN_  = os.getenv("OANDA_ACCESS_TOKEN", "")
        OANDA_ACCOUNT_= os.getenv("OANDA_ACCOUNT_ID", "")
        OANDA_ENV2    = os.getenv("OANDA_ENVIRONMENT", "practice")
        cl2 = oandapyV20.API(access_token=OANDA_TOKEN_, environment=OANDA_ENV2)
        req = accounts.AccountSummary(OANDA_ACCOUNT_)
        cl2.request(req)
        bal = float(req.response["account"]["balance"])
        nav = float(req.response["account"]["NAV"])
        result["oanda_ok"]       = True
        result["oanda_balance"]  = round(bal, 2)
        result["oanda_nav"]      = round(nav, 2)
        result["oanda_currency"] = req.response["account"].get("currency", "USD")
    except Exception as e:
        result["oanda_ok"]    = False
        result["oanda_error"] = str(e)[:120]

    try:
        raw = json.loads(TRADES_LOG.read_text()) if TRADES_LOG.exists() else []
        trades = [t for t in raw if isinstance(t, dict) and "pnl" in t]
        if trades:
            last = max(trades, key=lambda t: t.get("opened_at", t.get("timestamp", "")))
            result["last_trade_ts"]  = last.get("opened_at", last.get("timestamp", ""))
            result["last_trade_par"] = last.get("par", last.get("instrument", ""))
            result["last_trade_pnl"] = round(float(last.get("pnl", 0)), 5)
            result["total_trades"]   = len(trades)
    except Exception:
        pass

    try:
        shadow_path = BASE_DIR / "logs" / "shadow_signals.jsonl"
        if shadow_path.exists():
            lines = [l for l in shadow_path.read_text().splitlines() if l.strip()]
            result["shadow_signals"] = len(lines)
    except Exception:
        result["shadow_signals"] = 0

    result["ts"] = datetime.now(timezone.utc).isoformat()
    return result
# -- /api/positions --------------------------------------------------------
@app.get("/api/positions")
async def get_positions(request: Request):
    """Trades abiertos en OANDA con entry, SL, TP."""
    if not _check_token(request):
        raise HTTPException(status_code=401, detail="No autorizado")

    OANDA_TOKEN_  = os.getenv("OANDA_ACCESS_TOKEN", "")
    OANDA_ACCOUNT_= os.getenv("OANDA_ACCOUNT_ID", "")
    OANDA_ENV2    = os.getenv("OANDA_ENVIRONMENT", "practice")

    try:
        import oandapyV20.endpoints.trades as oanda_trades
        cl = oandapyV20.API(access_token=OANDA_TOKEN_, environment=OANDA_ENV2)
        req = oanda_trades.OpenTrades(OANDA_ACCOUNT_)
        cl.request(req)
        trades_raw = req.response.get("trades", [])

        positions = []
        for t in trades_raw:
            instrument = t.get("instrument", "").replace("_", "_")
            side  = "buy" if int(t.get("currentUnits", t.get("initialUnits", "1"))) > 0 else "sell"
            entry = float(t.get("price", 0))
            sl    = None
            tp    = None
            if t.get("stopLossOrder"):
                sl = float(t["stopLossOrder"].get("price", 0))
            if t.get("takeProfitOrder"):
                tp = float(t["takeProfitOrder"].get("price", 0))
            unrealized = float(t.get("unrealizedPL", 0))
            positions.append({
                "id":          t.get("id"),
                "instrument":  instrument,
                "side":        side,
                "units":       abs(int(t.get("currentUnits", t.get("initialUnits", 0)))),
                "entry":       entry,
                "sl":          sl,
                "tp":          tp,
                "unrealized_pl": unrealized,
                "open_time":   t.get("openTime", ""),
            })
        return {"ok": True, "positions": positions, "count": len(positions)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "positions": []}



# ── Administración remota (Fase 2a) ─────────────────────────────────────────
# Permiten operar el bot por HTTPS sin necesidad de SSH.

@app.get("/api/logs")
async def get_logs(
    request: Request,
    lines: int = Query(120, ge=10, le=1000),
    level: str = Query("all", pattern="^(all|info|warn|error|signal)$"),
):
    """
    Tail del log del bot (trading_bot.log).
    level=all    → últimas N líneas tal cual
    level=error  → solo ERROR/EXCEPTION/TRACEBACK/CRITICAL
    level=warn   → solo WARNING
    level=signal → solo evaluación de patrones ([ADX], [H4], señales, frescura)
    Requiere header X-Session-Token (ver POST /api/auth).
    """
    from collections import deque

    if not _check_token(request):
        raise HTTPException(status_code=401, detail="No autorizado")
    try:
        with BOT_LOG.open("r", errors="replace") as fh:
            raw = list(deque(fh, maxlen=5000))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Log no encontrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el log: {e}")

    if level == "error":
        keys = ("ERROR", "EXCEPTION", "TRACEBACK", "CRITICAL")
        raw = [l for l in raw if any(k in l.upper() for k in keys)]
    elif level == "warn":
        raw = [l for l in raw if "WARN" in l.upper()]
    elif level == "signal":
        keys = ("evaluación de patrones", "evaluacion de patrones", "[ADX]", "[H4]",
                "señal", "signal", "shadow", "frescura", "datos_frescos")
        raw = [l for l in raw if any(k in l for k in keys)]
    # level=all → sin filtrar

    tail = [l.rstrip("\n") for l in raw[-lines:]]
    return {"ok": True, "level": level, "lines": tail, "count": len(tail)}


@app.post("/api/system/restart")
async def restart_service(request: Request):
    """
    Reinicia un servicio systemd del VPS.
    Body: {"service": "trading_bot"} — valores permitidos: trading_bot, webapp.
    NOTA: al reiniciar "webapp" esta respuesta puede cortarse; el servicio
    vuelve solo en segundos. Requiere header X-Session-Token.
    """
    import subprocess

    if not _check_token(request):
        raise HTTPException(status_code=401, detail="No autorizado")
    try:
        body = await request.json()
    except Exception:
        body = {}
    svc = (body.get("service") or "").strip()
    if svc not in ("trading_bot", "webapp"):
        raise HTTPException(status_code=400,
                            detail="service debe ser 'trading_bot' o 'webapp'")
    try:
        r = subprocess.run(["systemctl", "restart", f"{svc}.service"],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error ejecutando restart: {e}")
    if r.returncode != 0:
        raise HTTPException(status_code=500,
                            detail=f"systemctl falló: {r.stderr.strip()[:200]}")
    return {"ok": True, "service": svc, "action": "restart",
            "detail": "orden de reinicio enviada"}


@app.get("/api/shadow")
async def get_shadow(request: Request):
    """
    Fase2e — resumen de señales shadow resueltas (estrategias en pausa).
    Requiere header X-Session-Token.
    """
    if not _check_token(request):
        raise HTTPException(status_code=401, detail="No autorizado")
    import json as _json
    p = Path("logs/shadow_resultados.jsonl")
    por_est: dict = {}
    total = 0
    if p.exists():
        for l in p.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            try:
                r = _json.loads(l)
            except Exception:
                continue
            total += 1
            e = por_est.setdefault(r.get("estrategia", "?"),
                                   {"n": 0, "win": 0, "loss": 0, "timeout": 0, "R": 0.0})
            e["n"] += 1
            res = r.get("resultado")
            if res in e:
                e[res] += 1
            e["R"] = round(e["R"] + float(r.get("R", 0)), 2)
    return {"ok": True, "total": total, "por_estrategia": por_est}
