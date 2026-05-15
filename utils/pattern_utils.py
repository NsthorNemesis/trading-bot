"""
utils/pattern_utils.py
Funciones auxiliares de detección de patrones de velas.
"""
import pandas as pd


def es_doji_estricto(open_: float, high: float, low: float, close: float,
                     umbral: float = 0.05) -> bool:
    """
    True si la vela es un Doji estricto.
    El cuerpo real no supera `umbral` del rango total.
    """
    rango = high - low
    if rango <= 0:
        return False
    cuerpo = abs(close - open_)
    return (cuerpo / rango) < umbral


def es_hammer(open_: float, high: float, low: float, close: float) -> bool:
    """True si la vela es un Hammer (sombra inf >= 2x cuerpo, sombra sup pequeña)."""
    cuerpo = abs(close - open_)
    if cuerpo <= 0:
        return False
    sombra_inf = min(open_, close) - low
    sombra_sup = high - max(open_, close)
    return sombra_inf >= 2 * cuerpo and sombra_sup <= cuerpo * 0.5
