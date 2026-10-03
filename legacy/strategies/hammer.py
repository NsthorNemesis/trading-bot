"""
strategies/hammer.py
Hammer — vela de reversión con sombra inferior larga en tendencia bajista.

Estado: PAUSADA
"""
from typing import Optional
import pandas as pd
from .base_strategy import BaseStrategy


class HammerStrategy(BaseStrategy):

    NAME = "Hammer"

    def generate_signal(
        self,
        df: pd.DataFrame,
        par: str,
        params: dict,
    ) -> Optional[dict]:

        if len(df) < 5:
            return None

        rsi_val = self._last(df, "RSI_14", 50.0)
        atr_val = self._last(df, "ATR_14",  0.0)
        precio  = self._last(df, "Close",   0.0)
        ema20   = self._last(df, "EMA_20",  0.0)
        ema50   = self._last(df, "EMA_50",  0.0)

        hay_hammer = self._last_flag(df, "CDL_HAMMER")
        tendencia  = "up" if ema20 > ema50 else "down"

        if hay_hammer and tendencia == "down":
            return {
                "conf":       0.45,
                "dir":        "long",
                "estrategia": self.NAME,
                "razon":      f"Hammer | RSI={rsi_val:.1f} | tend={tendencia}",
                "entry":      precio,
                "atr":        atr_val,
            }

        return None
