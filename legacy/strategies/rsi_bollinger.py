"""
strategies/rsi_bollinger.py
RSI_Bollinger — mean-reversion: RSI en extremos + precio toca banda de Bollinger.

Estado: ACTIVA
WR histórico en M15: ~37.7%  (backtest 1 año, Variante A)
RR requerido para breakeven al 37.7%: 1.66  (usamos 2.0 → margen positivo)

Lógica:
  LONG:  RSI < 32  Y  precio ≤ BBL × 1.002  → rebote esperado desde sobreventa
  SHORT: RSI > 68  Y  precio ≥ BBU × 0.998  → rebote esperado desde sobrecompra

Esta estrategia es mean-reversion — NO se filtra por tendencia H4.
El filtro H4 aplica únicamente a EMA_Crossover (trend-following).
"""
from typing import Optional
import pandas as pd
from .base_strategy import BaseStrategy


class RSIBollingerStrategy(BaseStrategy):

    NAME = "RSI_Bollinger"

    def generate_signal(
        self,
        df: pd.DataFrame,
        par: str,
        params: dict,
    ) -> Optional[dict]:

        if len(df) < 15:
            return None

        rsi_val = self._last(df, "RSI_14", 50.0)
        atr_val = self._last(df, "ATR_14", 0.0)
        precio  = self._last(df, "Close",  0.0)

        bbl_col = self._find_col(df, "BBL_")
        bbu_col = self._find_col(df, "BBU_")
        if not bbl_col or not bbu_col:
            return None

        bbl_val = self._last(df, bbl_col, 0.0)
        bbu_val = self._last(df, bbu_col, 0.0)

        precio_en_bbl = precio < bbl_val * 1.002 if bbl_val > 0 else False
        precio_en_bbu = precio > bbu_val * 0.998 if bbu_val > 0 else False

        if rsi_val < 32 and precio_en_bbl:
            return {
                "conf":       0.45,
                "dir":        "long",
                "estrategia": self.NAME,
                "razon":      f"RSI_Bollinger | RSI={rsi_val:.1f} | BB_low",
                "entry":      precio,
                "atr":        atr_val,
            }

        if rsi_val > 68 and precio_en_bbu:
            return {
                "conf":       0.45,
                "dir":        "short",
                "estrategia": self.NAME,
                "razon":      f"RSI_Bollinger | RSI={rsi_val:.1f} | BB_high",
                "entry":      precio,
                "atr":        atr_val,
            }

        return None
