"""
strategies/base_strategy.py
Clase base abstracta para todas las estrategias de trading.

Para agregar una nueva estrategia al sistema:
  1. Crear un archivo en strategies/mi_estrategia.py
  2. Definir una clase que herede de BaseStrategy
  3. Establecer NAME = "MiEstrategia"  (debe coincidir con strategy_params.json)
  4. Implementar generate_signal(df, par, params) -> Optional[dict]
  5. Agregar "MiEstrategia" a estrategias_activas en strategy_params.json

Sin modificar ningún otro archivo — el sistema la descubre automáticamente.
"""
from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd


class BaseStrategy(ABC):
    """
    Interfaz común para todas las estrategias de trading.

    Cada estrategia es un archivo independiente con lógica propia.
    SignalAgent las carga dinámicamente desde la carpeta strategies/.
    """

    # Identificador único — debe coincidir exactamente con el nombre
    # usado en strategy_params.json / estrategias_activas
    NAME: str = ""

    @abstractmethod
    def generate_signal(
        self,
        df: pd.DataFrame,
        par: str,
        params: dict,
    ) -> Optional[dict]:
        """
        Evalúa el DataFrame y retorna una señal o None.

        Args:
            df:     DataFrame con OHLCV + indicadores calculados
                    (ATR_14, RSI_14, EMA_20, EMA_50, BBL_*, BBU_*,
                     MACD_DIF, CDL_HAMMER, es_doji, CDL_ENGULF_*)
            par:    Par de divisas, ej: "EUR_USD"
            params: Parámetros operacionales del bot (strategy_params.json)

        Returns:
            dict con al menos: conf (float 0-1), dir ("long"/"short"),
                               estrategia (str), razon (str),
                               entry (float), atr (float)
            None si no hay señal válida para esta vela.
        """
        pass

    @property
    def name(self) -> str:
        return self.NAME

    # ── Helpers compartidos por todas las estrategias ──────────────────────────

    @staticmethod
    def _last(df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        """Valor de la última vela para una columna, con default seguro."""
        if col in df.columns:
            try:
                return float(df[col].iloc[-1])
            except (ValueError, TypeError):
                pass
        return default

    @staticmethod
    def _find_col(df: pd.DataFrame, prefix: str) -> Optional[str]:
        """Primera columna cuyo nombre empieza con prefix, o None."""
        matches = [c for c in df.columns if c.startswith(prefix)]
        return matches[0] if matches else None

    def _last_flag(self, df: pd.DataFrame, prefix: str) -> bool:
        """True si la última vela tiene valor != 0 en la columna prefix*."""
        col = self._find_col(df, prefix)
        if col is None:
            return False
        return bool(df[col].iloc[-1] != 0)
