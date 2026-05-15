"""
sim_30dias_real.py — Trading Bot v11
Simulación de 30 días con auto-calibración semanal

Uso:
    python sim_30dias_real.py                          # simulación completa
    python sim_30dias_real.py --par EURUSD             # un solo par
    python sim_30dias_real.py --semanas 2              # solo 2 semanas
    python sim_30dias_real.py --capital 500            # capital inicial distinto
    python sim_30dias_real.py --sin-calibracion        # sin recalibrar entre semanas

Output:
    - Consola: resumen por semana + tabla final
    - data/backtesting/sim_30dias_resultado.json
"""

import os
import sys
import json
import argparse
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ─────────────────────────────────────────────
# Dependencias externas
# ─────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
    from ta.volatility import AverageTrueRange, BollingerBands
    from ta.trend import EMAIndicator, MACD as TaMACD
    from ta.momentum import RSIIndicator
except ImportError as e:
    sys.exit(f"[ERROR] Dependencia faltante: {e}\n"
             f"Activa el venv y corre: pip install ta pandas numpy")

# ─────────────────────────────────────────────
# Rutas del proyecto
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data" / "historical"
RESULTS_DIR = BASE_DIR / "data" / "backtesting"
CALIBRATION_FILE = BASE_DIR / "data" / "calibration" / "strategy_params.json"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────
# Parámetros por defecto (espejo de strategy_params.json)
# ─────────────────────────────────────────────
DEFAULT_PARAMS = {
    "estrategias_activas": ["Doji", "Hammer"],
    "estrategias_pausadas": ["RSI_Bollinger", "RSI_Divergence", "Engulfing"],
    "sl_atr_mult": 1.5,
    "rr_ratio": 1.8,
    "min_confidence": 0.35,
    "cooldown_minutes": 20,
    "max_posiciones": 3,
    "riesgo_pct": 0.005,
    "sesiones_activas": ["london", "overlap", "new_york"],
}

PARES = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "NZD_USD"]

# ─────────────────────────────────────────────
# Sesiones forex (UTC)
# ─────────────────────────────────────────────
SESIONES = {
    "sydney":   (21, 6),
    "tokyo":    (0,  9),
    "london":   (8,  17),
    "new_york": (13, 22),
    "overlap":  (13, 17),   # London + NY
}


def hora_en_sesion(hora_utc: int, sesiones_activas: list) -> bool:
    for sesion in sesiones_activas:
        if sesion not in SESIONES:
            continue
        inicio, fin = SESIONES[sesion]
        if inicio < fin:
            if inicio <= hora_utc < fin:
                return True
        else:   # cruza medianoche
            if hora_utc >= inicio or hora_utc < fin:
                return True
    return False


# ═══════════════════════════════════════════════════════════
# 1. DATA LOADER
# ═══════════════════════════════════════════════════════════
class DataLoader:
    """
    Carga datos históricos desde data/historical/.
    Soporta CSV y JSON (formato OANDA oandapyV20).
    Columnas esperadas en CSV: time, open, high, low, close, volume
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir

    def cargar_par(self, par: str, granularidad: str = "M15") -> pd.DataFrame:
        """Intenta cargar CSV → JSON. Devuelve DataFrame con índice datetime."""
        nombre = par.replace("/", "_")

        # Buscar archivo
        candidatos = [
            self.data_dir / f"{nombre}_{granularidad}.csv",
            self.data_dir / f"{nombre}_{granularidad}.json",
            self.data_dir / par / f"{granularidad}.csv",
            self.data_dir / par / f"{granularidad}.json",
        ]
        archivo = next((c for c in candidatos if c.exists()), None)

        if archivo is None:
            return pd.DataFrame()

        try:
            if archivo.suffix == ".csv":
                df = pd.read_csv(archivo)
            else:
                with open(archivo) as f:
                    raw = json.load(f)
                # Formato OANDA candles
                if "candles" in raw:
                    registros = []
                    for c in raw["candles"]:
                        if not c.get("complete", True):
                            continue
                        mid = c.get("mid", {})
                        registros.append({
                            "time":   c["time"],
                            "open":   float(mid.get("o", 0)),
                            "high":   float(mid.get("h", 0)),
                            "low":    float(mid.get("l", 0)),
                            "close":  float(mid.get("c", 0)),
                            "volume": int(c.get("volume", 0)),
                        })
                    df = pd.DataFrame(registros)
                else:
                    df = pd.DataFrame(raw)

            # Normalizar columnas
            df.columns = [c.lower().strip() for c in df.columns]
            col_time = next((c for c in df.columns if "time" in c or "date" in c), None)
            if col_time:
                df["time"] = pd.to_datetime(df[col_time], utc=True)
                df = df.set_index("time").sort_index()

            for col in ["open", "high", "low", "close"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["open", "high", "low", "close"])
            return df

        except Exception as exc:
            print(f"[DataLoader] Error leyendo {archivo}: {exc}")
            return pd.DataFrame()

    def pares_disponibles(self) -> list:
        disponibles = []
        for par in PARES:
            df = self.cargar_par(par)
            if not df.empty:
                disponibles.append(par)
        return disponibles


# ═══════════════════════════════════════════════════════════
# 2. INDICATOR ENGINE
# ═══════════════════════════════════════════════════════════
class IndicatorEngine:
    """
    Calcula indicadores sobre un DataFrame OHLCV.
    Espejo fiel de lo que usa MarketAgent (librería ta).
    """

    @staticmethod
    def calcular(df: pd.DataFrame) -> pd.DataFrame:
        if len(df) < 55:
            return df

        df = df.copy()
        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        # ATR(14)
        df["atr"] = AverageTrueRange(high, low, close, window=14).average_true_range()

        # RSI(14)
        df["rsi"] = RSIIndicator(close, window=14).rsi()

        # EMAs
        df["ema9"]  = EMAIndicator(close, window=9).ema_indicator()
        df["ema20"] = EMAIndicator(close, window=20).ema_indicator()
        df["ema50"] = EMAIndicator(close, window=50).ema_indicator()

        # Bollinger Bands(20)
        bb = BollingerBands(close, window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"]   = bb.bollinger_mavg()
        df["bb_pct"]   = bb.bollinger_pband()   # 0=lower, 1=upper

        # MACD
        macd = TaMACD(close, window_slow=26, window_fast=12, window_sign=9)
        df["macd"]        = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"]   = macd.macd_diff()

        return df


# ═══════════════════════════════════════════════════════════
# 3. PATTERN DETECTOR
# ═══════════════════════════════════════════════════════════
class PatternDetector:
    """
    Detecta Doji, Hammer y Engulfing.
    Lógica propia igual a MarketAgent (no pandas-ta).
    """

    @staticmethod
    def es_doji(row) -> bool:
        try:
            cuerpo = abs(row["close"] - row["open"])
            rango  = row["high"] - row["low"]
            if rango == 0:
                return False
            return (cuerpo / rango) < 0.1
        except Exception:
            return False

    @staticmethod
    def es_hammer(row) -> bool:
        try:
            cuerpo     = abs(row["close"] - row["open"])
            rango      = row["high"] - row["low"]
            sombra_inf = min(row["open"], row["close"]) - row["low"]
            sombra_sup = row["high"] - max(row["open"], row["close"])
            if rango == 0 or cuerpo == 0:
                return False
            return sombra_inf >= 2 * cuerpo and sombra_sup <= cuerpo
        except Exception:
            return False

    @staticmethod
    def es_engulfing(row, prev_row) -> bool:
        try:
            alcista = (row["close"] > row["open"] and
                       prev_row["close"] < prev_row["open"] and
                       row["open"] <= prev_row["close"] and
                       row["close"] >= prev_row["open"])
            bajista = (row["close"] < row["open"] and
                       prev_row["close"] > prev_row["open"] and
                       row["open"] >= prev_row["close"] and
                       row["close"] <= prev_row["open"])
            return alcista or bajista
        except Exception:
            return False

    @classmethod
    def detectar(cls, row, prev_row=None) -> list:
        patrones = []
        if cls.es_doji(row):
            patrones.append("Doji")
        if cls.es_hammer(row):
            patrones.append("Hammer")
        if prev_row is not None and cls.es_engulfing(row, prev_row):
            patrones.append("Engulfing")
        return patrones


# ═══════════════════════════════════════════════════════════
# 4. SIGNAL FILTER + SCORER
# ═══════════════════════════════════════════════════════════
class SignalFilter:
    """
    Filtros equivalentes a SignalAgent:
    Filtro 1: cooldown, sesión, frescura
    Filtro 2: pre-filtro técnico (sin LLM)
    Filtro 3: scoring de confianza (regla-based, sustituye DeepSeek en sim)
    """

    def __init__(self, params: dict):
        self.params = params
        self.ultimo_trade_por_par = {}   # par → datetime última señal

    def filtro_1(self, par: str, ts: datetime, sesiones_activas: list) -> tuple:
        """Cooldown + sesión + frescura."""
        hora = ts.hour

        # Sesión activa
        if not hora_en_sesion(hora, sesiones_activas):
            return False, "fuera_sesion"

        # Cooldown
        ultimo = self.ultimo_trade_por_par.get(par)
        if ultimo:
            diff = (ts - ultimo).total_seconds() / 60
            if diff < self.params["cooldown_minutes"]:
                return False, f"cooldown ({diff:.0f}min)"

        return True, "ok"

    @staticmethod
    def filtro_2(row: pd.Series, patrones: list, params: dict) -> tuple:
        """Pre-filtro técnico puro."""
        if not patrones:
            return False, "sin_patron"

        # RSI fuera de rango extremo (no operar en sobrecompra/sobreventa extrema)
        rsi = row.get("rsi", 50)
        if rsi > 80 or rsi < 20:
            return False, f"rsi_extremo ({rsi:.1f})"

        # ATR mínimo: evitar mercados muertos
        atr = row.get("atr", 0)
        close = row.get("close", 1)
        if atr / close < 0.0002:
            return False, "atr_bajo"

        return True, "ok"

    @staticmethod
    def scoring_confianza(row: pd.Series, patrones: list, params: dict) -> float:
        """
        Score 0.0-1.0 que simula el output de DeepSeek.
        Basado en confluencia técnica real.
        """
        score = 0.0
        rsi   = row.get("rsi", 50)
        close = row.get("close", 0)
        ema20 = row.get("ema20", close)
        ema50 = row.get("ema50", close)
        bb_pct = row.get("bb_pct", 0.5)
        macd_hist = row.get("macd_hist", 0)

        # Patrón base
        if "Doji" in patrones:
            score += 0.30
        if "Hammer" in patrones:
            score += 0.25
        if "Engulfing" in patrones:
            score += 0.20

        # RSI en zona interesante
        if 35 <= rsi <= 65:
            score += 0.10
        elif 30 <= rsi <= 70:
            score += 0.05

        # Precio respecto a EMAs (tendencia)
        if close > ema20 > ema50:
            score += 0.10   # tendencia alcista
        elif close < ema20 < ema50:
            score += 0.10   # tendencia bajista

        # BB: cerca de bandas = potencial reversión
        if bb_pct < 0.15 or bb_pct > 0.85:
            score += 0.08

        # MACD alineado con dirección (más selectivo)
        if macd_hist > 0.0001:
            score += 0.07
        elif macd_hist < -0.0001:
            score += 0.07

        # Penalización: señal débil sin confluencia (score base bajo)
        # Requiere al menos patrón + un confirmador técnico para llegar al umbral
        confirmadores = sum([
            1 if (35 <= rsi <= 65) else 0,
            1 if (close > ema20 > ema50 or close < ema20 < ema50) else 0,
            1 if (bb_pct < 0.15 or bb_pct > 0.85) else 0,
            1 if abs(macd_hist) > 0.0001 else 0,
        ])
        if confirmadores == 0:
            score -= 0.10  # sin confluencia, penalizar

        # Ruido aleatorio ±2% (reducido para ser más estable)
        import random
        score += random.uniform(-0.02, 0.02)

        return max(0.0, min(1.0, score))

    def registrar_trade(self, par: str, ts: datetime):
        self.ultimo_trade_por_par[par] = ts


# ═══════════════════════════════════════════════════════════
# 5. TRADE SIMULATOR
# ═══════════════════════════════════════════════════════════
class TradeSimulator:
    """
    Simula ejecución de órdenes con SL/TP y gestión de posiciones.
    """

    def __init__(self, capital_inicial: float, params: dict):
        self.capital = capital_inicial
        self.capital_inicial = capital_inicial
        self.params = params
        self.posiciones_abiertas = []
        self.trades_cerrados = []
        self.peak_capital = capital_inicial

    def abrir_trade(self, par: str, ts: datetime, row: pd.Series,
                    patron: str, confianza: float, direction: str = None):
        """Abre un trade si hay espacio (max_posiciones)."""
        if len(self.posiciones_abiertas) >= self.params["max_posiciones"]:
            return None

        atr   = row.get("atr", 0)
        close = row.get("close", 0)
        if atr == 0 or close == 0:
            return None

        # Determinar dirección basada en patrón + contexto
        if direction is None:
            ema20 = row.get("ema20", close)
            ema50 = row.get("ema50", close)
            if patron == "Hammer":
                direction = "LONG"
            elif patron == "Doji":
                direction = "LONG" if close > ema20 else "SHORT"
            elif patron == "Engulfing":
                direction = "LONG" if row.get("close", 0) > row.get("open", 0) else "SHORT"
            else:
                direction = "LONG" if close > ema20 else "SHORT"

        sl_dist = atr * self.params["sl_atr_mult"]
        tp_dist = sl_dist * self.params["rr_ratio"]

        if direction == "LONG":
            sl_precio = close - sl_dist
            tp_precio = close + tp_dist
        else:
            sl_precio = close + sl_dist
            tp_precio = close - tp_dist

        # Tamaño de posición
        riesgo_capital = self.capital * self.params["riesgo_pct"]
        unidades = riesgo_capital / sl_dist if sl_dist > 0 else 0

        trade = {
            "id":          len(self.trades_cerrados) + len(self.posiciones_abiertas) + 1,
            "par":         par,
            "patron":      patron,
            "direction":   direction,
            "entry_time":  ts,
            "entry_price": close,
            "sl":          sl_precio,
            "tp":          tp_precio,
            "unidades":    unidades,
            "confianza":   confianza,
            "atr_entry":   atr,
        }
        self.posiciones_abiertas.append(trade)
        return trade

    def actualizar_posiciones(self, par: str, ts: datetime, row: pd.Series):
        """Cierra posiciones que tocaron SL o TP."""
        high  = row.get("high",  row.get("close", 0))
        low   = row.get("low",   row.get("close", 0))
        close = row.get("close", 0)

        cerradas = []
        for trade in self.posiciones_abiertas:
            if trade["par"] != par:
                continue

            resultado = None
            precio_cierre = close

            if trade["direction"] == "LONG":
                if low <= trade["sl"]:
                    resultado = "SL"
                    precio_cierre = trade["sl"]
                elif high >= trade["tp"]:
                    resultado = "TP"
                    precio_cierre = trade["tp"]
            else:  # SHORT
                if high >= trade["sl"]:
                    resultado = "SL"
                    precio_cierre = trade["sl"]
                elif low <= trade["tp"]:
                    resultado = "TP"
                    precio_cierre = trade["tp"]

            if resultado:
                pnl_precio = precio_cierre - trade["entry_price"]
                if trade["direction"] == "SHORT":
                    pnl_precio = -pnl_precio
                pnl = pnl_precio * trade["unidades"]

                self.capital += pnl
                self.peak_capital = max(self.peak_capital, self.capital)

                trade_cerrado = {**trade,
                    "exit_time":    ts,
                    "exit_price":   precio_cierre,
                    "resultado":    resultado,
                    "pnl":          round(pnl, 4),
                    "capital_tras": round(self.capital, 4),
                    "duracion_min": int((ts - trade["entry_time"]).total_seconds() / 60),
                }
                self.trades_cerrados.append(trade_cerrado)
                cerradas.append(trade)

        for t in cerradas:
            self.posiciones_abiertas.remove(t)

        return cerradas

    def metricas(self, trades_subset: list = None) -> dict:
        """Calcula WR, PF, MaxDD, etc. sobre un subconjunto o todos los trades."""
        trades = trades_subset if trades_subset is not None else self.trades_cerrados
        if not trades:
            return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                    "max_dd_pct": 0.0, "pnl_total": 0.0, "expectancy": 0.0}

        ganados  = [t for t in trades if t["resultado"] == "TP"]
        perdidos = [t for t in trades if t["resultado"] == "SL"]
        pnl_total = sum(t["pnl"] for t in trades)

        bruto_ganado  = sum(t["pnl"] for t in ganados)
        bruto_perdido = abs(sum(t["pnl"] for t in perdidos))

        win_rate      = len(ganados) / len(trades) if trades else 0
        profit_factor = (bruto_ganado / bruto_perdido) if bruto_perdido > 0 else (999.0 if bruto_ganado > 0 else 0.0)

        # Drawdown máximo (sobre capital reconstruido)
        capital_sim = self.capital_inicial
        peak = capital_sim
        max_dd = 0.0
        for t in sorted(trades, key=lambda x: x["exit_time"]):
            capital_sim += t["pnl"]
            peak = max(peak, capital_sim)
            dd = (peak - capital_sim) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        expectancy = pnl_total / len(trades) if trades else 0.0

        return {
            "trades":        len(trades),
            "ganados":       len(ganados),
            "perdidos":      len(perdidos),
            "win_rate":      round(win_rate, 4),
            "profit_factor": round(profit_factor, 4),
            "max_dd_pct":    round(max_dd * 100, 2),
            "pnl_total":     round(pnl_total, 4),
            "expectancy":    round(expectancy, 4),
        }


# ═══════════════════════════════════════════════════════════
# 6. CALIBRATOR
# ═══════════════════════════════════════════════════════════
class Calibrator:
    """
    Calibración semanal sin llamar a DeepSeek.
    Evalúa WR y PF por estrategia y ajusta parámetros.
    Espeja la lógica del AuditAgent weekend calibration.
    """

    @staticmethod
    def calibrar(params: dict, trades_semana: list, semana: int) -> tuple:
        """
        Devuelve (nuevos_params, reporte_calibracion).
        """
        nuevos = {k: v for k, v in params.items()}
        cambios = []

        if not trades_semana:
            return nuevos, {"semana": semana, "cambios": ["sin trades — parámetros sin cambio"]}

        # Métricas globales de la semana
        ganados  = [t for t in trades_semana if t["resultado"] == "TP"]
        perdidos = [t for t in trades_semana if t["resultado"] == "SL"]
        wr  = len(ganados) / len(trades_semana)
        bruto_g = sum(t["pnl"] for t in ganados)
        bruto_p = abs(sum(t["pnl"] for t in perdidos)) or 0.001
        pf  = bruto_g / bruto_p

        # ─── Métricas por estrategia ───────────────────────────
        por_estrategia = defaultdict(list)
        for t in trades_semana:
            por_estrategia[t["patron"]].append(t)

        estrategias_activas   = list(nuevos["estrategias_activas"])
        estrategias_pausadas  = list(nuevos["estrategias_pausadas"])

        # Calcular PF por estrategia (para el rescate de emergencia)
        pf_por_patron = {}
        for patron, ts in por_estrategia.items():
            if len(ts) < 3:
                continue   # muestra insuficiente
            g = [x for x in ts if x["resultado"] == "TP"]
            p = [x for x in ts if x["resultado"] == "SL"]
            wr_s = len(g) / len(ts)
            pf_s = (sum(x["pnl"] for x in g) / (abs(sum(x["pnl"] for x in p)) or 0.001))
            pf_por_patron[patron] = pf_s

            if pf_s < 1.0 and patron in estrategias_activas:
                estrategias_activas.remove(patron)
                if patron not in estrategias_pausadas:
                    estrategias_pausadas.append(patron)
                cambios.append(f"⏸ {patron} pausada (PF={pf_s:.2f} < 1.0)")

            elif pf_s >= 1.4 and wr_s >= 0.50 and patron in estrategias_pausadas:
                estrategias_pausadas.remove(patron)
                if patron not in estrategias_activas:
                    estrategias_activas.append(patron)
                cambios.append(f"▶ {patron} reactivada (PF={pf_s:.2f}, WR={wr_s:.0%})")

        # ─── REGLA DE SEGURIDAD: nunca dejar estrategias_activas vacío ──────
        # Si todas fueron pausadas, reactivar la de mejor PF de la semana
        if not estrategias_activas:
            if pf_por_patron:
                mejor = max(pf_por_patron, key=pf_por_patron.get)
            else:
                # Sin datos suficientes: rescatar la primera pausada disponible
                mejor = estrategias_pausadas[0] if estrategias_pausadas else None

            if mejor:
                if mejor in estrategias_pausadas:
                    estrategias_pausadas.remove(mejor)
                estrategias_activas.append(mejor)
                cambios.append(f"🔄 {mejor} rescatada como estrategia de reserva "
                               f"(mejor PF={pf_por_patron.get(mejor, 0):.2f}) — "
                               f"mínimo 1 estrategia siempre activa")

        nuevos["estrategias_activas"]  = estrategias_activas
        nuevos["estrategias_pausadas"] = estrategias_pausadas

        # ─── Ajustar cooldown ─────────────────────────────────
        if wr >= 0.60 and pf >= 1.5:
            nuevo_cd = max(10, nuevos["cooldown_minutes"] - 2)
            if nuevo_cd != nuevos["cooldown_minutes"]:
                cambios.append(f"⚡ cooldown {nuevos['cooldown_minutes']}→{nuevo_cd}min (buen rendimiento)")
                nuevos["cooldown_minutes"] = nuevo_cd
        elif wr < 0.45 or pf < 1.0:
            nuevo_cd = min(40, nuevos["cooldown_minutes"] + 5)
            if nuevo_cd != nuevos["cooldown_minutes"]:
                cambios.append(f"🛑 cooldown {nuevos['cooldown_minutes']}→{nuevo_cd}min (rendimiento bajo)")
                nuevos["cooldown_minutes"] = nuevo_cd

        # ─── Ajustar min_confidence ───────────────────────────
        if pf < 1.0:
            nueva_conf = min(0.60, nuevos["min_confidence"] + 0.05)
            cambios.append(f"📈 min_confidence {nuevos['min_confidence']:.2f}→{nueva_conf:.2f}")
            nuevos["min_confidence"] = nueva_conf
        elif pf >= 1.8 and wr >= 0.60:
            nueva_conf = max(0.25, nuevos["min_confidence"] - 0.02)
            cambios.append(f"📉 min_confidence {nuevos['min_confidence']:.2f}→{nueva_conf:.2f} (PF excelente)")
            nuevos["min_confidence"] = nueva_conf

        # ─── Ajustar RR ratio ─────────────────────────────────
        if wr >= 0.65:
            nuevo_rr = min(2.5, round(nuevos["rr_ratio"] + 0.1, 1))
            if nuevo_rr != nuevos["rr_ratio"]:
                cambios.append(f"🎯 rr_ratio {nuevos['rr_ratio']}→{nuevo_rr} (WR alto)")
                nuevos["rr_ratio"] = nuevo_rr
        elif wr < 0.40:
            nuevo_rr = max(1.3, round(nuevos["rr_ratio"] - 0.1, 1))
            if nuevo_rr != nuevos["rr_ratio"]:
                cambios.append(f"⚠ rr_ratio {nuevos['rr_ratio']}→{nuevo_rr} (WR bajo)")
                nuevos["rr_ratio"] = nuevo_rr

        if not cambios:
            cambios.append("✓ parámetros estables, sin cambios")

        reporte = {
            "semana": semana,
            "metricas_semana": {
                "trades": len(trades_semana),
                "wr": round(wr, 4),
                "pf": round(pf, 4),
            },
            "cambios": cambios,
            "params_resultado": {k: v for k, v in nuevos.items()},
        }

        return nuevos, reporte


# ═══════════════════════════════════════════════════════════
# 7. SIMULATION RUNNER
# ═══════════════════════════════════════════════════════════
class SimulationRunner:

    def __init__(self, capital: float = 200.0, pares: list = None,
                 n_semanas: int = 4, con_calibracion: bool = True):
        self.capital        = capital
        self.pares          = pares or PARES
        self.n_semanas      = n_semanas
        self.con_calibracion = con_calibracion
        self.loader         = DataLoader()
        self.params         = self._cargar_params()
        self.simulador      = TradeSimulator(capital, self.params)
        self.filtro         = SignalFilter(self.params)
        self.detector       = PatternDetector()
        self.motor          = IndicatorEngine()
        self.historial_calibraciones = []
        self.metricas_por_semana     = []

    def _cargar_params(self) -> dict:
        if CALIBRATION_FILE.exists():
            try:
                with open(CALIBRATION_FILE) as f:
                    p = json.load(f)
                    # Asegurar que tenga todas las claves
                    for k, v in DEFAULT_PARAMS.items():
                        if k not in p:
                            p[k] = v
                    return p
            except Exception:
                pass
        return dict(DEFAULT_PARAMS)

    def _preparar_datos(self) -> dict:
        """Carga y calcula indicadores para todos los pares."""
        datos = {}
        for par in self.pares:
            df = self.loader.cargar_par(par, "M15")
            if df.empty:
                print(f"  [!] Sin datos para {par} — se omitirá")
                continue
            df = self.motor.calcular(df)
            df = df.dropna()
            datos[par] = df
            print(f"  [✓] {par}: {len(df):,} velas M15 cargadas "
                  f"({df.index[0].date()} → {df.index[-1].date()})")
        return datos

    def _slice_semana(self, df: pd.DataFrame, inicio: datetime, fin: datetime) -> pd.DataFrame:
        """Filtra el DataFrame a una semana específica."""
        mask = (df.index >= inicio) & (df.index < fin)
        return df[mask]

    def _simular_semana(self, datos: dict, inicio: datetime, fin: datetime) -> list:
        """Corre la simulación para una semana, devuelve trades abiertos en ese período."""
        trades_semana = []
        params = self.params  # usa params actuales (pueden haber cambiado por calibración)

        # Actualizar simulador y filtro con params actuales
        self.simulador.params = params
        self.filtro.params = params

        for par, df in datos.items():
            semana_df = self._slice_semana(df, inicio, fin)
            if semana_df.empty:
                continue

            filas = list(semana_df.iterrows())
            for i, (ts, row) in enumerate(filas):
                # 1. Actualizar posiciones abiertas
                cerradas = self.simulador.actualizar_posiciones(par, ts, row)
                trades_semana.extend(cerradas)

                # 2. Detectar patrones
                prev_row = filas[i - 1][1] if i > 0 else None
                patrones = self.detector.detectar(row, prev_row)

                # Filtrar a estrategias activas
                patrones = [p for p in patrones if p in params["estrategias_activas"]]
                if not patrones:
                    continue

                # 3. Filtro 1
                ok, razon = self.filtro.filtro_1(par, ts, params["sesiones_activas"])
                if not ok:
                    continue

                # 4. Filtro 2
                ok, razon = self.filtro.filtro_2(row, patrones, params)
                if not ok:
                    continue

                # 5. Scoring de confianza
                for patron in patrones:
                    confianza = self.filtro.scoring_confianza(row, [patron], params)
                    if confianza < params["min_confidence"]:
                        continue

                    # 6. Abrir trade
                    trade = self.simulador.abrir_trade(par, ts, row, patron, confianza)
                    if trade:
                        self.filtro.registrar_trade(par, ts)
                        break  # un solo trade por vela por par

        return trades_semana

    def correr(self) -> dict:
        print("\n" + "═" * 60)
        print("  SIM_30DIAS_REAL — Trading Bot v11")
        print(f"  Capital: ${self.capital:.2f} | Semanas: {self.n_semanas}")
        print(f"  Calibración semanal: {'SÍ' if self.con_calibracion else 'NO'}")
        print("═" * 60)

        # Cargar datos
        print("\n[1/3] Cargando datos históricos...")
        datos = self._preparar_datos()
        if not datos:
            print("\n[ERROR] No se encontraron datos históricos en data/historical/")
            print("Corre primero: python scripts/setup_datos.py")
            return {}

        # Determinar rango de fechas: últimas n semanas disponibles
        todas_fechas = []
        for df in datos.values():
            todas_fechas.extend([df.index[-1]])
        fecha_fin = min(todas_fechas)
        fecha_inicio = fecha_fin - timedelta(weeks=self.n_semanas)

        print(f"\n[2/3] Simulando {self.n_semanas} semanas: "
              f"{fecha_inicio.date()} → {fecha_fin.date()}")

        # Loop semanal
        print("\n" + "─" * 60)
        for semana in range(1, self.n_semanas + 1):
            semana_inicio = fecha_inicio + timedelta(weeks=semana - 1)
            semana_fin    = fecha_inicio + timedelta(weeks=semana)

            print(f"\n📅 SEMANA {semana} | {semana_inicio.date()} → {semana_fin.date()}")
            n_trades_antes = len(self.simulador.trades_cerrados)

            trades_semana = self._simular_semana(datos, semana_inicio, semana_fin)
            trades_nuevos = self.simulador.trades_cerrados[n_trades_antes:]

            metricas_s = self.simulador.metricas(trades_nuevos)
            metricas_s["capital_fin_semana"] = round(self.simulador.capital, 2)
            metricas_s["semana"] = semana
            self.metricas_por_semana.append(metricas_s)

            # Imprimir resumen de semana
            wr_pct = metricas_s["win_rate"] * 100
            pf     = metricas_s["profit_factor"]
            pnl    = metricas_s["pnl_total"]
            n_tr   = metricas_s["trades"]
            dd     = metricas_s["max_dd_pct"]

            estado = "✅" if wr_pct >= 55 and pf >= 1.4 else ("⚠️" if n_tr == 0 else "❌")
            print(f"  {estado} Trades: {n_tr:3d} | WR: {wr_pct:5.1f}% | "
                  f"PF: {pf:5.2f} | PnL: ${pnl:+.2f} | DD: {dd:.1f}%")
            print(f"     Capital: ${metricas_s['capital_fin_semana']:.2f}")
            print(f"     Estrategias activas: {self.params['estrategias_activas']}")

            # Calibración al fin de cada semana
            if self.con_calibracion:
                nuevos_params, reporte = Calibrator.calibrar(
                    self.params, trades_nuevos, semana)
                self.params = nuevos_params
                self.historial_calibraciones.append(reporte)

                if reporte["cambios"] and reporte["cambios"][0] != "✓ parámetros estables, sin cambios":
                    print(f"\n  🔧 Calibración semana {semana}:")
                    for c in reporte["cambios"]:
                        print(f"     {c}")

        # Métricas totales
        print("\n" + "═" * 60)
        print("  RESUMEN FINAL — 30 DÍAS")
        print("═" * 60)
        metricas_totales = self.simulador.metricas()
        capital_final = self.simulador.capital
        retorno_pct = ((capital_final - self.capital) / self.capital) * 100

        print(f"\n  Capital inicial:  ${self.capital:.2f}")
        print(f"  Capital final:    ${capital_final:.2f}")
        print(f"  Retorno total:    {retorno_pct:+.2f}%")
        print(f"  Trades totales:   {metricas_totales['trades']}")
        print(f"  Win Rate global:  {metricas_totales['win_rate']*100:.1f}%")
        print(f"  Profit Factor:    {metricas_totales['profit_factor']:.2f}")
        print(f"  Max Drawdown:     {metricas_totales['max_dd_pct']:.1f}%")
        print(f"  Expectancy/trade: ${metricas_totales['expectancy']:.4f}")

        # Evaluación criterios para live
        print("\n  CRITERIOS PARA LIVE TRADING:")
        wr_global = metricas_totales["win_rate"] * 100
        pf_global = metricas_totales["profit_factor"]
        dd_global = metricas_totales["max_dd_pct"]

        cr_wr = "✅" if wr_global >= 55 else "❌"
        cr_pf = "✅" if pf_global >= 1.4 else "❌"
        cr_dd = "✅" if dd_global < 6   else "❌"

        print(f"  {cr_wr} WR ≥ 55%:     {wr_global:.1f}%")
        print(f"  {cr_pf} PF ≥ 1.4:     {pf_global:.2f}")
        print(f"  {cr_dd} MaxDD < 6%:   {dd_global:.1f}%")

        listo_live = wr_global >= 55 and pf_global >= 1.4 and dd_global < 6
        if listo_live:
            print("\n  🚀 RESULTADO: LISTO para considerar live trading")
        else:
            print("\n  ⏸  RESULTADO: Continuar paper trading — criterios no cumplidos")

        print("\n" + "═" * 60)

        # ─── Guardar JSON ─────────────────────────────────────
        resultado = self._guardar_json(metricas_totales, capital_final, retorno_pct)
        return resultado

    def _guardar_json(self, metricas_totales: dict, capital_final: float,
                      retorno_pct: float) -> dict:
        resultado = {
            "metadata": {
                "fecha_ejecucion":  datetime.now().isoformat(),
                "capital_inicial":  self.capital,
                "capital_final":    round(capital_final, 4),
                "retorno_pct":      round(retorno_pct, 4),
                "n_semanas":        self.n_semanas,
                "pares_simulados":  self.pares,
                "con_calibracion":  self.con_calibracion,
            },
            "metricas_globales": metricas_totales,
            "metricas_por_semana": self.metricas_por_semana,
            "historial_calibraciones": self.historial_calibraciones,
            "params_finales": self.params,
            "trades_detalle": [
                {k: (str(v) if isinstance(v, datetime) else v)
                 for k, v in t.items()}
                for t in self.simulador.trades_cerrados
            ],
        }

        out_path = RESULTS_DIR / "sim_30dias_resultado.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)

        print(f"\n[3/3] Resultados guardados en:")
        print(f"      {out_path}")
        return resultado


# ===========================================================
# MAIN
# ===========================================================
def main():
    parser = argparse.ArgumentParser(
        description="Simulacion 30 dias con auto-calibracion semanal - Trading Bot v11"
    )
    parser.add_argument("--par",             type=str,   help="Par especifico, ej: EUR_USD")
    parser.add_argument("--semanas",         type=int,   default=4,     help="Numero de semanas (default: 4)")
    parser.add_argument("--capital",         type=float, default=200.0, help="Capital inicial (default: 200)")
    parser.add_argument("--sin-calibracion", action="store_true",       help="Deshabilitar auto-calibracion semanal")
    args = parser.parse_args()

    pares = [args.par] if args.par else PARES

    runner = SimulationRunner(
        capital=args.capital,
        pares=pares,
        n_semanas=args.semanas,
        con_calibracion=not args.sin_calibracion,
    )
    runner.correr()


if __name__ == "__main__":
    main()
