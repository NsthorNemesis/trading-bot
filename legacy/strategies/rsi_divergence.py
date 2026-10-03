"""
strategies/rsi_divergence.py
RSI_Divergence — divergencia precio vs RSI en ventanas cortas.

Estado: PAUSADA
WR histórico: ~28%  (por debajo del breakeven para RR 2.0)

Lógica:
  LONG  (divergencia alcista):  precio hace mínimo más bajo, RSI mínimo más alto
  SHORT (divergencia bajista):  precio hace máximo más alto, RSI máximo más bajo

Comparación de ventanas: últimas 5 velas vs 5 velas anteriores (posiciones -12 a -5).
"""
import numpy as np
from typing import Optional
import pandas as pd
from .base_strategy import BaseStrategy


class RSIDivergenceStrategy(BaseStrategy):

    NAME = "RSI_Divergence"

    def generate_signal(
        self,
        df: pd.DataFrame,
        par: str,
        params: dict,
    ) -> Optional[dict]:

        if len(df) < 12 or "RSI_14" not in df.columns:
            return None

        rsi_val = self._last(df, "RSI_14", 50.0)
        atr_val = self._last(df, "ATR_14", 0.0)
        precio  = self._last(df, "Close",  0.0)

        closes = df["Close"].values
        rsi    = df["RSI_14"].values

        c_rec = closes[-5:];    r_rec = rsi[-5:]
        c_old = closes[-12:-5]; r_old = rsi[-12:-5]

        # ── Divergencia alcista ──────────────────────────────────────────────
        i_r_min = int(np.argmin(c_rec))
        i_o_min = int(np.argmin(c_old))
        if (
            c_rec[i_r_min] < c_old[i_o_min] * 0.9998   # precio: mínimo más bajo
            and r_rec[i_r_min] > r_old[i_o_min] + 3     # RSI:    mínimo más alto
            and r_rec[i_r_min] < 48                      # RSI en zona neutra-baja
        ):
            return {
                "conf":       0.52,
                "dir":        "long",
                "estrategia": self.NAME,
                "razon":      f"Div. alcista RSI | RSI={rsi_val:.1f}",
                "entry":      precio,
                "atr":        atr_val,
            }

        # ── Divergencia bajista ──────────────────────────────────────────────
        i_r_max = int(np.argmax(c_rec))
        i_o_max = int(np.argmax(c_old))
        if (
            c_rec[i_r_max] > c_old[i_o_max] * 1.0002   # precio: máximo más alto
            and r_rec[i_r_max] < r_old[i_o_max] - 3     # RSI:    máximo más bajo
            and r_rec[i_r_max] > 52                      # RSI en zona neutra-alta
        ):
            return {
                "conf":       0.52,
                "dir":        "short",
                "estrategia": self.NAME,
                "razon":      f"Div. bajista RSI | RSI={rsi_val:.1f}",
                "entry":      precio,
                "atr":        atr_val,
            }

        return None
