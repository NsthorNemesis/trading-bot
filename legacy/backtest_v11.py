"""
⚠️  HISTÓRICO — NO USAR COMO REFERENCIA DE LA CONFIGURACIÓN ACTUAL
Este backtest es de la arquitectura original v11.0.0 (mayo 2026) con parámetros
hardcodeados de esa época (Doji/Hammer activos). La configuración viva está en
strategy_params.json y el motor actual en agents/signal_agent/.
Se conserva solo como referencia histórica.
"""
"""
backtest_v11.py — Backtest autónomo para trading_bot v11
═══════════════════════════════════════════════════════════════════
100% self-contained — no importa nada del bot principal.
Embebe el SignalAgent v11 completo (3-filtro cascade + DeepSeek-chat).

Uso:
  python backtest_v11.py                      # 4 semanas, pares principales
  python backtest_v11.py --semanas 8
  python backtest_v11.py --pares EUR_USD GBP_USD
  python backtest_v11.py --max-signals 50     # limitar llamadas DeepSeek
  python backtest_v11.py --sin-deepseek       # solo pre-filtro técnico
═══════════════════════════════════════════════════════════════════
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# ── PATHS ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
# Buscar .env en este directorio o en el directorio padre (raíz del bot)
for _env_path in [BASE_DIR / ".env", BASE_DIR.parent.parent / ".env"]:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

HIST_DIR = BASE_DIR / "data" / "historical"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "backtest_v11.log", mode="w"),
    ],
)
logger = logging.getLogger("backtest_v11")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# ── PARÁMETROS v11 (hardcodeados — arquitectura original) ──────────────────────
PARAMS_V11 = {
    "estrategias_activas":  ["Doji", "Hammer"],
    "estrategias_pausadas": ["RSI_Bollinger", "RSI_Divergence", "Engulfing"],
    "pares_activos":        ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"],
    "sl_atr_mult":          1.5,
    "min_sl_pips":          10,
    "max_sl_pips":          30,
    "rr_ratio":             1.8,
    "min_win_rate":         0.45,
    "min_confidence":       0.35,
    "sesiones_activas":     ["london", "overlap", "new_york"],
    "cooldown_minutes":     20,
    "max_posiciones":       3,
    "riesgo_pct":           0.005,
    "max_drawdown_dia":     0.05,
    "circuit_breaker_pct":  0.10,
    "nota": "v11.0.0 — Doji WR=79% calibrado 2026-05-09",
}

DEEPSEEK_KEY      = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_FAST        = "deepseek-chat"


# ══════════════════════════════════════════════════════════════════════════════
# INDICADORES (librería ta)
# ══════════════════════════════════════════════════════════════════════════════
from ta.volatility import AverageTrueRange, BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD


# ══════════════════════════════════════════════════════════════════════════════
# FAKE MARKET AGENT
# ══════════════════════════════════════════════════════════════════════════════
class FakeMarketAgent:
    BUFFER_SIZE = 150
    VELAS_MIN   = 60

    def __init__(self, pares: list):
        self._buffer: dict[str, deque] = {
            p: deque(maxlen=self.BUFFER_SIZE) for p in pares
        }
        self._replay_ts: datetime = datetime.utcnow()

    def set_replay_ts(self, ts: datetime):
        self._replay_ts = ts

    def feed(self, par: str, vela: dict):
        self._buffer[par].append(vela)

    def get_df(self, par: str, n: int = 100) -> Optional[pd.DataFrame]:
        buf = list(self._buffer[par])
        if len(buf) < self.VELAS_MIN:
            return None
        ultimas = buf[-min(n, len(buf)):]
        df = pd.DataFrame(ultimas)
        df = df.rename(columns={
            "open": "Open", "high": "High",
            "low":  "Low",  "close": "Close", "volume": "Volume",
        })
        # Normalizar timestamp (quitar sufijos de nanosegundos si los hay)
        df["timestamp"] = df["timestamp"].str[:19].str.replace(".000000000", "", regex=False) + "Z"
        df.index = pd.to_datetime(df["timestamp"])
        for col in ("Open", "High", "Low", "Close", "Volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])

        df["ATR_14"]   = AverageTrueRange(df["High"], df["Low"], df["Close"], window=14, fillna=True).average_true_range()
        df["RSI_14"]   = RSIIndicator(df["Close"], window=14, fillna=True).rsi()
        df["EMA_9"]    = EMAIndicator(df["Close"], window=9,  fillna=True).ema_indicator()
        df["EMA_20"]   = EMAIndicator(df["Close"], window=20, fillna=True).ema_indicator()
        df["EMA_50"]   = EMAIndicator(df["Close"], window=50, fillna=True).ema_indicator()

        bb = BollingerBands(df["Close"], window=20, window_dev=2, fillna=True)
        df["BBL_20"]   = bb.bollinger_lband()
        df["BBM_20"]   = bb.bollinger_mavg()
        df["BBU_20"]   = bb.bollinger_hband()

        macd_ind       = MACD(df["Close"], fillna=True)
        df["MACD"]     = macd_ind.macd()
        df["MACD_SIG"] = macd_ind.macd_signal()

        df = _calcular_patrones(df)
        df["tendencia"] = (df["EMA_20"] > df["EMA_50"]).map({True: "up", False: "down"})
        return df

    def datos_frescos(self, par: str) -> bool:
        return len(self._buffer[par]) >= self.VELAS_MIN

    def sesion_actual(self) -> str:
        return _sesion(self._replay_ts)


def _sesion(dt) -> str:
    h = dt.hour if hasattr(dt, "hour") else int(str(dt)[11:13])
    if 7  <= h < 13: return "london"
    if 13 <= h < 17: return "overlap"
    if 17 <= h < 22: return "new_york"
    return "asia"


def _calcular_patrones(df: pd.DataFrame) -> pd.DataFrame:
    o = df["Open"]; h = df["High"]
    l = df["Low"];  c = df["Close"]
    cuerpo    = abs(c - o)
    rango     = h - l
    sombra_inf = o.combine(c, min) - l
    sombra_sup = h - o.combine(c, max)

    df["CDL_HAMMER"] = (
        (cuerpo > 0) &
        (sombra_inf >= 2 * cuerpo) &
        (sombra_sup <= cuerpo * 0.5)
    ).astype(int)

    df["CDL_DOJI"] = (
        (rango > 0) &
        (cuerpo / rango.replace(0, 1) < 0.10)
    ).astype(int)

    df["CDL_ENGULFING_BULL"] = (
        (c > o) & (c.shift(1) < o.shift(1)) &
        (c > o.shift(1)) & (o < c.shift(1)) &
        (cuerpo > cuerpo.shift(1) * 1.05)
    ).astype(int)

    df["CDL_ENGULFING_BEAR"] = (
        (c < o) & (c.shift(1) > o.shift(1)) &
        (c < o.shift(1)) & (o > c.shift(1)) &
        (cuerpo > cuerpo.shift(1) * 1.05)
    ).astype(int)

    return df


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL AGENT v11 (embebido — idéntico al original)
# ══════════════════════════════════════════════════════════════════════════════
from tenacity import retry, stop_after_attempt, wait_exponential
try:
    from openai import OpenAI
    _OPENAI_OK = True
except ImportError:
    _OPENAI_OK = False


class SignalAgentV11:
    """
    SignalAgent v11 embebido directamente en el backtest.
    3 filtros: Python puro → pre-filtro técnico → DeepSeek-chat
    """

    def __init__(self, market_agent, params: dict, use_deepseek: bool = True):
        self._market   = market_agent
        self._params   = params
        self._cooldown: dict[str, datetime] = {}
        self._ds       = None

        if use_deepseek and _OPENAI_OK and DEEPSEEK_KEY:
            self._ds = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)
            logger.info(f"SignalAgent v11: DeepSeek {MODEL_FAST} activo")
        else:
            logger.warning("SignalAgent v11: modo solo indicadores (sin DeepSeek)")

    async def evaluar(self, par: str) -> Optional[dict]:
        # Filtro 1: Python puro
        if not self._filtro_cooldown(par):
            return None
        if not self._filtro_sesion():
            return None
        if not self._market.datos_frescos(par):
            return None

        # Obtener DataFrame
        df = self._market.get_df(par, n=50)
        if df is None or len(df) < 30:
            return None

        # Filtro 2: Pre-filtro técnico
        presenal = self._prefiltro_tecnico(df, par)
        if not presenal:
            return None

        # Filtro 3: DeepSeek-chat
        if self._ds:
            try:
                return await self._consultar_deepseek(par, df, presenal)
            except Exception as e:
                logger.debug(f"DeepSeek error {par}: {e} — usando pre-filtro")
                return presenal if presenal.get("conf", 0) >= self._params["min_confidence"] else None
        else:
            return presenal if presenal.get("conf", 0) >= self._params["min_confidence"] else None

    def _filtro_cooldown(self, par: str) -> bool:
        ultimo = self._cooldown.get(par)
        if not ultimo:
            return True
        mins     = self._params.get("cooldown_minutes", 20)
        ahora    = self._market._replay_ts   # tiempo del replay, no el reloj real
        return (ahora - ultimo) > timedelta(minutes=mins)

    def _filtro_sesion(self) -> bool:
        sesion  = self._market.sesion_actual()
        activas = self._params.get("sesiones_activas", ["london", "overlap", "new_york"])
        return sesion in activas

    def _prefiltro_tecnico(self, df, par: str) -> Optional[dict]:
        u = df.iloc[-1]

        rsi_val  = float(u.get("RSI_14", 50))
        atr_val  = float(u.get("ATR_14", 0))
        ema20_v  = float(u.get("EMA_20", 0))
        ema50_v  = float(u.get("EMA_50", 0))
        precio   = float(u.get("Close", 0))
        bbl_cols = [c for c in df.columns if c.startswith("BBL_")]
        bbu_cols = [c for c in df.columns if c.startswith("BBU_")]
        tendencia = "up" if ema20_v > ema50_v else "down"

        hay_hammer = bool(u.get("CDL_HAMMER", 0) != 0)
        hay_doji   = bool(u.get("CDL_DOJI",   0) != 0)
        hay_engulf = bool(u.get("CDL_ENGULFING_BULL", 0) != 0 or
                          u.get("CDL_ENGULFING_BEAR", 0) != 0)

        rsi_sobre = rsi_val > 68
        rsi_bajo  = rsi_val < 32

        precio_bbl = precio < float(u.get(bbl_cols[0], 0)) * 1.002 if bbl_cols else False
        precio_bbu = precio > float(u.get(bbu_cols[0], 0)) * 0.998 if bbu_cols else False

        senal_long  = (
            (hay_hammer and tendencia == "down") or
            (hay_doji   and rsi_bajo) or
            (rsi_bajo   and precio_bbl) or
            (hay_engulf and tendencia == "up")
        )
        senal_short = (
            (hay_doji   and rsi_sobre) or
            (rsi_sobre  and precio_bbu)
        )

        if not senal_long and not senal_short:
            return None

        if senal_long:
            dir_ = "long"
            if hay_hammer:   estrat = "Hammer"
            elif hay_doji:   estrat = "Doji"
            elif hay_engulf: estrat = "Engulfing"
            else:            estrat = "RSI_Bollinger" if precio_bbl else "RSI_Divergence"
        else:
            dir_   = "short"
            estrat = "Doji" if hay_doji else "RSI_Bollinger"

        if estrat not in self._params.get("estrategias_activas", []):
            return None

        return {
            "par":        par,
            "dir":        dir_,
            "conf":       0.45,
            "entry":      precio,
            "razon":      f"{estrat} | RSI={rsi_val:.1f} | tendencia={tendencia}",
            "estrategia": estrat,
            "atr":        atr_val,
        }

    async def _consultar_deepseek(self, par: str, df,
                                   presenal: dict) -> Optional[dict]:
        u        = df.iloc[-1]
        rsi_val  = float(u.get("RSI_14", 50))
        atr_val  = float(u.get("ATR_14", 0))
        ema20    = float(u.get("EMA_20", 0))
        ema50    = float(u.get("EMA_50", 0))
        precio   = float(u.get("Close", 0))
        sesion   = self._market.sesion_actual()

        cierres_str = ",".join(
            f"{x:.5f}" for x in df["Close"].tail(10).tolist()
        )

        prompt = f"""Par:{par} Dir:{presenal['dir']} Sesion:{sesion}
RSI:{rsi_val:.1f} ATR:{atr_val:.5f} EMA20:{ema20:.5f} EMA50:{ema50:.5f}
Precio:{precio:.5f}
Patron:{presenal['estrategia']}
Cierres10:{cierres_str}
WR_min:{self._params['min_win_rate']:.0%} RR:{self._params['rr_ratio']}

¿Confirmas señal? JSON:
{{"señal":bool,"dir":"long/short","conf":0-1,"razon":"max 15 palabras"}}"""

        loop     = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._ds.chat.completions.create(
                model           = MODEL_FAST,
                messages        = [{"role": "user", "content": prompt}],
                response_format = {"type": "json_object"},
                max_tokens      = 80,
                timeout         = 30,
            )
        )

        resultado = json.loads(response.choices[0].message.content)

        if (resultado.get("señal") and
                resultado.get("conf", 0) >= self._params["min_confidence"]):
            self._cooldown[par] = self._market._replay_ts   # usar tiempo del replay
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


# ══════════════════════════════════════════════════════════════════════════════
# FAKE RISK AGENT
# ══════════════════════════════════════════════════════════════════════════════
class FakeRiskAgent:
    PIP = {
        "EUR_USD": 0.0001, "GBP_USD": 0.0001, "USD_CHF": 0.0001,
        "AUD_USD": 0.0001, "NZD_USD": 0.0001, "USD_CAD": 0.0001,
        "USD_JPY": 0.01,
    }

    def __init__(self, capital: float, params: dict):
        self._capital = capital
        self._peak    = capital
        self._params  = params
        self._pos:    dict[str, dict] = {}
        self._trades: list[dict]      = []
        self._dd_dia  = 0.0

    @property
    def capital(self) -> float:
        return self._capital

    def recibir_senal(self, senal: dict) -> Optional[dict]:
        par = senal["par"]
        if par in self._pos:
            return None
        if len(self._pos) >= self._params.get("max_posiciones", 3):
            return None
        if self._dd_dia >= self._params.get("max_drawdown_dia", 0.05):
            return None

        pip     = self.PIP.get(par, 0.0001)
        atr     = senal.get("atr", 0) or 0
        sl_mult = self._params.get("sl_atr_mult", 1.5)
        sl_dist = max(atr * sl_mult,
                      self._params.get("min_sl_pips", 10) * pip)
        sl_dist = min(sl_dist, self._params.get("max_sl_pips", 30) * pip)
        tp_dist = sl_dist * self._params.get("rr_ratio", 1.8)

        entry = senal["entry"]
        if senal["dir"] == "long":
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            sl = entry + sl_dist
            tp = entry - tp_dist

        riesgo_usd = self._capital * self._params.get("riesgo_pct", 0.005)
        trade = {
            "id":          f"{par}_{len(self._trades)+1:04d}",
            "par":         par,
            "dir":         senal["dir"],
            "entry":       entry,
            "sl":          sl,
            "tp":          tp,
            "sl_dist":     sl_dist,
            "tp_dist":     tp_dist,
            "riesgo_usd":  riesgo_usd,
            "estrategia":  senal.get("estrategia", ""),
            "conf":        senal.get("conf", 0),
            "razon":       senal.get("razon", ""),
            "ts_apertura": senal.get("ts", ""),
            "resultado":   None,
            "pnl_usd":     None,
            "ts_cierre":   None,
            "pips":        None,
        }
        self._pos[par] = trade
        logger.info(
            f"  ↳ TRADE {trade['id']} | {par} {senal['dir'].upper()} | "
            f"entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} | "
            f"conf={senal['conf']:.0%} | {senal.get('estrategia','')}"
        )
        return trade

    def update_posiciones(self, par: str, vela: dict, ts: str):
        if par not in self._pos:
            return
        t    = self._pos[par]
        hi   = float(vela["high"])
        lo   = float(vela["low"])
        dir_ = t["dir"]

        hit_sl = (dir_ == "long"  and lo  <= t["sl"]) or \
                 (dir_ == "short" and hi >= t["sl"])
        hit_tp = (dir_ == "long"  and hi >= t["tp"]) or \
                 (dir_ == "short" and lo <= t["tp"])

        if not hit_sl and not hit_tp:
            return

        if hit_tp and not hit_sl:
            resultado  = "win"
            pnl_factor = t["tp_dist"] / t["sl_dist"]
        else:
            resultado  = "loss"
            pnl_factor = -1.0

        pnl_usd  = t["riesgo_usd"] * pnl_factor
        pip      = self.PIP.get(par, 0.0001)
        pips_val = (t["tp_dist"] / pip) if resultado == "win" else -(t["sl_dist"] / pip)

        t.update({"resultado": resultado, "pnl_usd": pnl_usd,
                  "ts_cierre": ts, "pips": round(pips_val, 1)})

        self._capital += pnl_usd
        self._peak     = max(self._peak, self._capital)
        self._dd_dia   = max(self._dd_dia, (self._peak - self._capital) / self._peak)

        self._trades.append(dict(t))
        del self._pos[par]

        emoji = "✅" if resultado == "win" else "❌"
        logger.info(
            f"  {emoji} {resultado.upper()} {t['id']} | "
            f"pnl=${pnl_usd:+.2f} ({pips_val:+.1f}p) | capital=${self._capital:.2f}"
        )

    def reset_dd_diario(self):
        self._dd_dia = 0.0

    @property
    def trades(self) -> list:
        return self._trades

    @property
    def posiciones_abiertas(self) -> dict:
        return self._pos


# ══════════════════════════════════════════════════════════════════════════════
# CARGA DE DATOS
# ══════════════════════════════════════════════════════════════════════════════
def cargar_historico_m15(pares: list, semanas: int,
                          hist_dir: Path) -> dict:
    cutoff = datetime.utcnow() - timedelta(weeks=semanas)
    datos  = {}
    for par in pares:
        # Buscar en el directorio local primero, luego en el del bot principal
        candidatos = [
            hist_dir / f"{par}_M15.json",
            hist_dir.parent.parent.parent / "data" / "historical" / f"{par}_M15.json",
        ]
        path = next((p for p in candidatos if p.exists()), None)
        if not path:
            logger.warning(f"Sin datos M15 para {par}")
            continue

        with open(path) as f:
            velas = json.load(f)

        # Normalizar timestamps (algunos tienen nanosegundos)
        for v in velas:
            v["timestamp"] = v["timestamp"][:19] + "Z"

        filtradas = [
            v for v in velas
            if datetime.fromisoformat(v["timestamp"].replace("Z", "")) >= cutoff
        ]
        if len(filtradas) < 100:
            filtradas = velas[-max(200, len(filtradas)):]

        datos[par] = sorted(filtradas, key=lambda v: v["timestamp"])
        logger.info(f"  {par}: {len(datos[par])} velas M15 ({path.name})")

    return datos


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════
async def correr_backtest(capital, semanas, pares, max_signals,
                           sin_deepseek, verbose, hist_dir) -> dict:

    logger.info(f"Parámetros v11: {json.dumps({k: v for k, v in PARAMS_V11.items()}, ensure_ascii=False)}")

    logger.info(f"\n{'='*60}")
    logger.info("Cargando datos históricos...")
    datos   = cargar_historico_m15(pares, semanas, hist_dir)
    pares_ok = [p for p in pares if p in datos]
    if not pares_ok:
        logger.error("Sin datos — abortando")
        return {}

    # Timeline unificada
    timeline: dict[str, dict] = {}
    for par, velas in datos.items():
        for v in velas:
            ts = v["timestamp"]
            if ts not in timeline:
                timeline[ts] = {}
            timeline[ts][par] = v
    timestamps = sorted(timeline.keys())
    logger.info(f"Timeline: {len(timestamps)} candles | {timestamps[0][:16]} → {timestamps[-1][:16]}")

    # Agentes
    fake_market   = FakeMarketAgent(pares_ok)
    fake_risk     = FakeRiskAgent(capital, PARAMS_V11)
    signal_agent  = SignalAgentV11(fake_market, PARAMS_V11,
                                   use_deepseek=not sin_deepseek)

    señales_emitidas = 0
    ultimo_dia       = None
    pares_eval       = {p: 0 for p in pares_ok}

    logger.info(f"\n{'='*60}")
    logger.info(f"BACKTEST v11 INICIANDO")
    logger.info(f"  Capital: ${capital:.2f} | Semanas: {semanas} | Pares: {', '.join(pares_ok)}")
    logger.info(f"  DeepSeek: {'NO' if sin_deepseek else 'SÍ (llamadas reales)'}")
    logger.info(f"  Max señales: {max_signals if max_signals else 'ilimitado'}")
    logger.info(f"{'='*60}\n")

    for i, ts in enumerate(timestamps):
        ts_dt = datetime.fromisoformat(ts.replace("Z", ""))
        fake_market.set_replay_ts(ts_dt)

        # Reset DD diario
        dia = ts_dt.date()
        if dia != ultimo_dia:
            fake_risk.reset_dd_diario()
            ultimo_dia = dia
            if verbose:
                logger.info(f"📅 {dia} | capital=${fake_risk.capital:.2f}")

        # Alimentar buffers y evaluar SL/TP
        for par, vela in timeline[ts].items():
            if par in pares_ok:
                fake_market.feed(par, vela)
        for par, vela in timeline[ts].items():
            if par in pares_ok:
                fake_risk.update_posiciones(par, vela, ts)

        # Solo evaluar señales en sesiones activas
        sesion = fake_market.sesion_actual()
        if sesion not in PARAMS_V11["sesiones_activas"]:
            continue
        if max_signals and señales_emitidas >= max_signals:
            continue

        for par in pares_ok:
            if max_signals and señales_emitidas >= max_signals:
                break
            try:
                senal = await signal_agent.evaluar(par)
            except Exception as e:
                logger.debug(f"Error {par}@{ts}: {e}")
                senal = None

            if senal:
                senal["ts"] = ts
                señales_emitidas += 1
                pares_eval[par] += 1
                logger.info(
                    f"🔔 SEÑAL #{señales_emitidas} | {ts[:16]} [{sesion.upper()}] | "
                    f"{par} {senal['dir'].upper()} conf={senal['conf']:.0%} | "
                    f"{senal.get('estrategia','?')} | {senal.get('razon','')[:50]}"
                )
                fake_risk.recibir_senal(senal)

        if i % 500 == 0 and i > 0:
            logger.info(
                f"  [{i}/{len(timestamps)}] señales={señales_emitidas} | "
                f"trades={len(fake_risk.trades)} | capital=${fake_risk.capital:.2f}"
            )

    # Cerrar posiciones abiertas al precio actual
    for par, pos in list(fake_risk.posiciones_abiertas.items()):
        buf = list(fake_market._buffer[par])
        if buf:
            px = float(buf[-1]["close"])
            dif = (px - pos["entry"]) if pos["dir"] == "long" else (pos["entry"] - px)
            pnl = dif * (pos["riesgo_usd"] / pos["sl_dist"]) if pos["sl_dist"] else 0
            pos.update({"resultado": "abierto", "pnl_usd": pnl,
                        "ts_cierre": timestamps[-1],
                        "pips": round(dif / FakeRiskAgent.PIP.get(par, 0.0001), 1)})
            fake_risk._capital += pnl
            fake_risk.trades.append(dict(pos))
            logger.info(f"  ⏹ CIERRE FORZADO {pos['id']} | pnl=${pnl:+.2f}")

    return _calcular_metricas(fake_risk, capital, señales_emitidas, pares_eval)


# ══════════════════════════════════════════════════════════════════════════════
# MÉTRICAS Y REPORTE
# ══════════════════════════════════════════════════════════════════════════════
def _calcular_metricas(risk, capital_inicial, señales, pares_eval):
    trades = risk.trades
    total  = len(trades)
    if total == 0:
        logger.warning("Sin trades — no se generaron señales")
        return {"total_trades": 0, "señales_emitidas": señales,
                "capital_inicial": capital_inicial, "capital_final": risk.capital}

    wins    = [t for t in trades if t["resultado"] == "win"]
    losses  = [t for t in trades if t["resultado"] == "loss"]
    wr      = len(wins) / total
    ganado  = sum(t["pnl_usd"] for t in wins)
    perdido = abs(sum(t["pnl_usd"] for t in losses)) or 1e-9
    pf      = ganado / perdido
    roi     = (risk.capital - capital_inicial) / capital_inicial

    # Estimar semanas reales de los trades
    fechas = sorted(set(t["ts_apertura"][:10] for t in trades if t.get("ts_apertura")))
    dias_reales = max(1, (datetime.fromisoformat(fechas[-1]) -
                          datetime.fromisoformat(fechas[0])).days + 1) if len(fechas) > 1 else 7
    roi_anual = ((1 + roi) ** (365 / dias_reales) - 1)

    por_estrat = {}
    for t in trades:
        e = t.get("estrategia", "?")
        por_estrat.setdefault(e, {"wins": 0, "losses": 0, "pnl": 0.0})
        if t["resultado"] == "win":   por_estrat[e]["wins"]   += 1
        elif t["resultado"] == "loss": por_estrat[e]["losses"] += 1
        if t.get("pnl_usd"):          por_estrat[e]["pnl"]    += t["pnl_usd"]

    por_par = {}
    for t in trades:
        p = t["par"]
        por_par.setdefault(p, {"wins": 0, "losses": 0, "pnl": 0.0})
        if t["resultado"] == "win":   por_par[p]["wins"]   += 1
        elif t["resultado"] == "loss": por_par[p]["losses"] += 1
        if t.get("pnl_usd"):          por_par[p]["pnl"]    += t["pnl_usd"]

    m = {
        "total_trades":    total,
        "señales_emitidas": señales,
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(wr, 4),
        "profit_factor":   round(pf, 3),
        "capital_inicial": capital_inicial,
        "capital_final":   round(risk.capital, 2),
        "pnl_usd":         round(risk.capital - capital_inicial, 2),
        "roi_pct":         round(roi * 100, 2),
        "roi_anual_pct":   round(roi_anual * 100, 2),
        "avg_win_usd":     round(ganado / len(wins), 2) if wins else 0,
        "avg_loss_usd":    round(-perdido / len(losses), 2) if losses else 0,
        "por_estrategia":  por_estrat,
        "por_par":         por_par,
    }

    out = LOGS_DIR / "backtest_v11_trades.json"
    with open(out, "w") as f:
        json.dump({"metricas": m, "trades": trades}, f,
                  indent=2, ensure_ascii=False, default=str)
    logger.info(f"\nTrades guardados → {out}")
    return m


def imprimir_reporte(m: dict):
    sep = "═" * 62
    print(f"\n{sep}")
    print(f"  BACKTEST v11 — RESULTADOS")
    print(f"{sep}")
    print(f"  Total trades:      {m.get('total_trades', 0)}")
    print(f"  Señales emitidas:  {m.get('señales_emitidas', 0)}")
    print(f"  Wins / Losses:     {m.get('wins',0)} / {m.get('losses',0)}")
    print(f"  Win Rate:          {m.get('win_rate',0):.1%}")
    print(f"  Profit Factor:     {m.get('profit_factor',0):.3f}")
    print(f"  Capital inicial:   ${m.get('capital_inicial',0):.2f}")
    print(f"  Capital final:     ${m.get('capital_final',0):.2f}")
    print(f"  PnL total:         ${m.get('pnl_usd',0):+.2f}")
    print(f"  ROI período:       {m.get('roi_pct',0):+.2f}%")
    print(f"  ROI anualizado:    {m.get('roi_anual_pct',0):+.1f}%")
    print(f"  Avg win:           ${m.get('avg_win_usd',0):+.2f}")
    print(f"  Avg loss:          ${m.get('avg_loss_usd',0):+.2f}")
    if m.get("por_estrategia"):
        print(f"\n  POR ESTRATEGIA:")
        for e, s in sorted(m["por_estrategia"].items(), key=lambda x: -x[1]["pnl"]):
            tot = s["wins"] + s["losses"]
            wr  = s["wins"] / tot if tot else 0
            print(f"    {e:<20} {tot:>3} trades | WR={wr:.0%} | pnl=${s['pnl']:+.2f}")
    if m.get("por_par"):
        print(f"\n  POR PAR:")
        for p, s in sorted(m["por_par"].items(), key=lambda x: -x[1]["pnl"]):
            tot = s["wins"] + s["losses"]
            wr  = s["wins"] / tot if tot else 0
            print(f"    {p:<10} {tot:>3} trades | WR={wr:.0%} | pnl=${s['pnl']:+.2f}")
    print(f"{sep}\n")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Backtest v11 autónomo")
    parser.add_argument("--capital",      type=float, default=200.0)
    parser.add_argument("--semanas",      type=int,   default=4)
    parser.add_argument("--pares",        nargs="+",
                        default=["EUR_USD", "GBP_USD", "USD_CHF", "USD_JPY"])
    parser.add_argument("--max-signals",  type=int,   default=0)
    parser.add_argument("--sin-deepseek", action="store_true")
    parser.add_argument("--verbose",      action="store_true")
    parser.add_argument("--hist-dir",     type=Path,  default=HIST_DIR,
                        help="Directorio con archivos *_M15.json")
    args = parser.parse_args()

    m = asyncio.run(correr_backtest(
        capital      = args.capital,
        semanas      = args.semanas,
        pares        = args.pares,
        max_signals  = args.max_signals,
        sin_deepseek = args.sin_deepseek,
        verbose      = args.verbose,
        hist_dir     = args.hist_dir,
    ))
    if m:
        imprimir_reporte(m)


if __name__ == "__main__":
    main()
