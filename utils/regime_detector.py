"""
utils/regime_detector.py
═══════════════════════════════════════════════════════════════
Detecta el régimen de mercado (tendencia vs rango) usando ADX continuo.

regime_score:
  0.0 = rango puro    → RSI_Bollinger, RSI_Divergence dominan
  0.5 = transición    → pesos equilibrados
  1.0 = tendencia fuerte → Hammer, Engulfing dominan

Uso:
    rd = RegimeDetector()
    score = rd.calcular_regime_score(df)      # float 0.0-1.0
    trans = rd.detectar_transicion(hist)      # str
═══════════════════════════════════════════════════════════════
"""
import numpy as np
import pandas as pd
from typing import List


class RegimeDetector:
    """
    Detector de régimen basado en ADX continuo + pendiente.
    Pesos dinámicos de estrategia según el score resultante.
    """

    # Pesos por estrategia en cada extremo del régimen
    # regime_score=0.0 (rango)     → usar pesos RANGO
    # regime_score=1.0 (tendencia) → usar pesos TENDENCIA
    PESOS_RANGO = {
        "RSI_Bollinger":  1.40,
        "RSI_Divergence": 1.20,
        "Doji":           1.10,
        "Hammer":         0.70,
        "Engulfing":      0.60,
        "EMA_Crossover":  0.55,
    }
    PESOS_TENDENCIA = {
        "Engulfing":      1.40,
        "Hammer":         1.35,
        "EMA_Crossover":  1.20,
        "Doji":           0.85,
        "RSI_Bollinger":  0.40,   # bloqueado cuando regime_score > 0.62
        "RSI_Divergence": 0.55,
    }

    def calcular_regime_score(self, df: pd.DataFrame,
                               periodo: int = 14) -> float:
        """
        Calcula score continuo 0.0-1.0 basado en ADX.

        0.0 = rango puro (ADX < 15)
        1.0 = tendencia fuerte (ADX > 35)
        + ajuste por pendiente para detectar transiciones tempranas.
        """
        try:
            if df is None or len(df) < periodo + 5:
                return 0.5  # neutral si no hay suficientes datos

            adx = self._calcular_adx(df, periodo)
            if adx is None or np.isnan(adx):
                return 0.5

            # Score base desde ADX (normalizado entre 15 y 35)
            if adx < 15:
                score_base = 0.0
            elif adx > 35:
                score_base = 1.0
            else:
                score_base = (adx - 15.0) / 20.0

            # Ajuste por pendiente del ADX (últimas 3 velas)
            adx_serie = self._serie_adx(df, periodo)
            if adx_serie is not None and len(adx_serie) >= 4:
                slope = float(adx_serie.iloc[-1] - adx_serie.iloc[-4])
                # Normalizar pendiente: ±5 puntos de ADX = ±0.20 de ajuste
                ajuste = np.clip(slope / 25.0, -0.20, 0.20)
            else:
                ajuste = 0.0

            score = float(np.clip(score_base + ajuste, 0.0, 1.0))
            return score

        except Exception:
            return 0.5  # neutral en caso de error

    def detectar_transicion(self, scores_historicos: List[float]) -> str:
        """
        Detecta si estamos EN transición antes de que sea obvia.

        Args:
            scores_historicos: últimos N regime_scores (más antiguo primero)

        Returns:
            'hacia_tendencia' | 'hacia_rango' | 'estable'
        """
        if not scores_historicos or len(scores_historicos) < 5:
            return "estable"

        ultimos = scores_historicos[-5:]
        try:
            # Regresión lineal sobre los últimos 5 scores
            x = np.arange(len(ultimos), dtype=float)
            slope = float(np.polyfit(x, ultimos, 1)[0])

            if slope > 0.03:
                return "hacia_tendencia"
            elif slope < -0.03:
                return "hacia_rango"
            return "estable"
        except Exception:
            return "estable"

    def peso_estrategia(self, estrategia: str,
                         regime_score: float,
                         transicion: str = "estable") -> float:
        """
        Interpola el peso de la estrategia entre rango y tendencia.
        Ajusta ligeramente en caso de transición detectada.

        Returns:
            Multiplicador de confianza (0.55 – 1.45 aprox)
        """
        peso_rango     = self.PESOS_RANGO.get(estrategia, 1.0)
        peso_tendencia = self.PESOS_TENDENCIA.get(estrategia, 1.0)

        # Interpolación lineal entre los dos extremos
        score_efectivo = regime_score

        # Anticipar transición: mover el score 0.15 en la dirección detectada
        if transicion == "hacia_tendencia":
            score_efectivo = min(score_efectivo + 0.15, 1.0)
        elif transicion == "hacia_rango":
            score_efectivo = max(score_efectivo - 0.15, 0.0)

        peso = (peso_rango * (1.0 - score_efectivo) +
                peso_tendencia * score_efectivo)
        return float(np.clip(peso, 0.40, 1.60))

    # ── Helpers ADX ───────────────────────────────────────────────────────────

    def _calcular_adx(self, df: pd.DataFrame, periodo: int) -> float:
        """Calcula el ADX actual (último valor)."""
        serie = self._serie_adx(df, periodo)
        if serie is not None and len(serie) > 0:
            return float(serie.iloc[-1])
        return None

    def _serie_adx(self, df: pd.DataFrame, periodo: int):
        """Calcula la serie completa de ADX."""
        try:
            # Columnas con distintos nombres posibles
            h = self._col(df, ["high", "High", "HIGH"])
            l = self._col(df, ["low",  "Low",  "LOW"])
            c = self._col(df, ["close","Close","CLOSE"])
            if h is None or l is None or c is None:
                return None

            high  = df[h].astype(float)
            low   = df[l].astype(float)
            close = df[c].astype(float)

            # True Range
            tr1 = high - low
            tr2 = (high - close.shift(1)).abs()
            tr3 = (low  - close.shift(1)).abs()
            tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            # Directional Movement
            up_move   = high - high.shift(1)
            down_move = low.shift(1) - low

            plus_dm  = np.where((up_move > down_move) & (up_move > 0),
                                 up_move, 0.0)
            minus_dm = np.where((down_move > up_move) & (down_move > 0),
                                 down_move, 0.0)

            # Smoothed ATR y DM con Wilder
            atr_s    = self._wilder(pd.Series(tr),            periodo)
            plus_s   = self._wilder(pd.Series(plus_dm,
                                    index=df.index),  periodo)
            minus_s  = self._wilder(pd.Series(minus_dm,
                                    index=df.index),  periodo)

            plus_di  = 100 * plus_s  / atr_s.replace(0, np.nan)
            minus_di = 100 * minus_s / atr_s.replace(0, np.nan)
            dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
            adx      = self._wilder(dx.fillna(0), periodo)
            return adx

        except Exception:
            return None

    @staticmethod
    def _wilder(series: pd.Series, periodo: int) -> pd.Series:
        """Suavizado de Wilder (EMA modificada)."""
        result = series.copy().astype(float)
        alpha = 1.0 / periodo
        for i in range(1, len(result)):
            result.iloc[i] = (result.iloc[i - 1] * (1 - alpha) +
                              result.iloc[i] * alpha)
        return result

    @staticmethod
    def _col(df: pd.DataFrame, names: list):
        """Retorna el primer nombre de columna que exista en el df."""
        for n in names:
            if n in df.columns:
                return n
        return None
