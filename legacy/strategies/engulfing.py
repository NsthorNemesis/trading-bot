"""
strategies/engulfing.py
Engulfing — patrón de vela envolvente alcista en tendencia alcista.

Estado: PAUSADA
"""
from typing import Optional
import pandas as pd
from .base_strategy import BaseStrategy


class EngulfingStrategy(BaseStrategy):

    NAME = "Engulfing"

    def generate_signal(
        self,
        df: pd.DataFrame,
        par: str,
        params: dict,
    ) -> Optional[dict]:

        if len(df) < 5:
            return None

        atr_val = self._last(df, "ATR_14", 0.0)
        precio  = self._last(df, "Close",  0.0)
        ema20   = self._last(df, "EMA_20", 0.0)
        ema50   = self._last(df, "EMA_50", 0.0)

        hay_engulf = self._last_flag(df, "CDL_ENGULF")
        tendencia  = "up" if ema20 > ema50 else "down"

        if hay_engulf and tendencia == "up":
            return {
                "conf":       0.45,
                "dir":        "long",
                "estrategia": self.NAME,
                "razon":      f"Engulfing alcista | tend={tendencia}",
                "entry":      precio,
                "atr":        atr_val,
            }

        return None
