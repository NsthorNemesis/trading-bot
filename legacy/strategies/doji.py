"""
strategies/doji.py
Doji — vela de indecisión + RSI en zona extrema.

Estado: PAUSADA

Lógica:
  LONG:  Doji + RSI < 32  (indecisión en sobreventa → posible rebote)
  SHORT: Doji + RSI > 68  (indecisión en sobrecompra → posible caída)
"""
from typing import Optional
import pandas as pd
from .base_strategy import BaseStrategy


class DojiStrategy(BaseStrategy):

    NAME = "Doji"

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

        # Columna puede llamarse "es_doji" (calculada en market_agent)
        hay_doji = self._last_flag(df, "es_doji") or self._last_flag(df, "DOJI")

        if hay_doji and rsi_val < 32:
            return {
                "conf":       0.45,
                "dir":        "long",
                "estrategia": self.NAME,
                "razon":      f"Doji+oversold | RSI={rsi_val:.1f}",
                "entry":      precio,
                "atr":        atr_val,
            }

        if hay_doji and rsi_val > 68:
            return {
                "conf":       0.45,
                "dir":        "short",
                "estrategia": self.NAME,
                "razon":      f"Doji+overbought | RSI={rsi_val:.1f}",
                "entry":      precio,
                "atr":        atr_val,
            }

        return None
