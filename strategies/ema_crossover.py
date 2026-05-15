"""
strategies/ema_crossover.py
EMA_Crossover — cruce EMA20/EMA50 con confirmación MACD.

Estado: PAUSADA
WR histórico: ~25%  (por debajo del breakeven para RR 2.0)

Lógica (trend-following — SÍ se filtra por tendencia H4):
  LONG:  EMA20 cruza por encima de EMA50 en últimas 3 velas  +  MACD_DIF > 0
  SHORT: EMA20 cruza por debajo de EMA50 en últimas 3 velas  +  MACD_DIF < 0

El filtro H4 para esta estrategia aplica en signal_agent._filtro_tendencia_h4():
  H4 UP   → solo LONG
  H4 DOWN → solo SHORT
  H4 RANGO → ambas
"""
from typing import Optional
import pandas as pd
from .base_strategy import BaseStrategy


class EMACrossoverStrategy(BaseStrategy):

    NAME = "EMA_Crossover"

    def generate_signal(
        self,
        df: pd.DataFrame,
        par: str,
        params: dict,
    ) -> Optional[dict]:

        if len(df) < 5:
            return None
        if "EMA_20" not in df.columns or "EMA_50" not in df.columns:
            return None

        atr_val  = self._last(df, "ATR_14",   0.0)
        precio   = self._last(df, "Close",     0.0)
        macd_dif = self._last(df, "MACD_DIF",  0.0)

        ema20 = df["EMA_20"].values
        ema50 = df["EMA_50"].values

        # Cruce detectado en cualquiera de las últimas 3 velas
        cruce_up = any(
            ema20[i - 1] <= ema50[i - 1] and ema20[i] > ema50[i]
            for i in range(-3, 0)
        )
        cruce_down = any(
            ema20[i - 1] >= ema50[i - 1] and ema20[i] < ema50[i]
            for i in range(-3, 0)
        )

        if cruce_up and macd_dif > 0:
            return {
                "conf":       0.50,
                "dir":        "long",
                "estrategia": self.NAME,
                "razon":      f"EMA_Cross alcista | MACD_DIF={macd_dif:.5f}",
                "entry":      precio,
                "atr":        atr_val,
            }

        if cruce_down and macd_dif < 0:
            return {
                "conf":       0.50,
                "dir":        "short",
                "estrategia": self.NAME,
                "razon":      f"EMA_Cross bajista | MACD_DIF={macd_dif:.5f}",
                "entry":      precio,
                "atr":        atr_val,
            }

        return None
