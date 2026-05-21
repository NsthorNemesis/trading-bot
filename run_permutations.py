"""
run_permutations.py
═══════════════════════════════════════════════════════════════════════
Grid search de configuraciones para Trading Bot v11.

Testea 15 permutaciones sistemáticas variando:
  - Estrategia activa (Hammer, RSI_Bollinger, Ensemble)
  - Pares activos (EUR_GBP+NZD_USD, solo EUR_GBP, solo NZD_USD)
  - RR ratio (1.5, 2.0, 2.5)
  - sl_atr_mult (1.0, 1.5, 2.0)
  - min_confidence (0.35, 0.40, 0.45)
  - adx_max_rsi_bollinger (20, 25) para RSI_Bollinger

Uso:
    python run_permutations.py [--semanas N]

Output:
    logs/permutaciones/perm_NNN_<nombre>.log  → log completo de cada run
    logs/permutaciones/resumen.csv            → tabla comparativa
    logs/permutaciones/resumen.html           → tabla HTML con colores
═══════════════════════════════════════════════════════════════════════
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR     = Path(__file__).parent
PARAMS_FILE  = BASE_DIR / "data" / "calibration" / "strategy_params.json"
TRADES_LOG   = BASE_DIR / "data" / "trades" / "trades_log.json"
HARNESS_LOG  = BASE_DIR / "logs" / "backtest_harness.log"
PERM_DIR     = BASE_DIR / "logs" / "permutaciones"
PERM_DIR.mkdir(parents=True, exist_ok=True)

# ── Parámetros base que no cambian entre permutaciones ───────────────────────
BASE_PARAMS = {
    "_comentario":      "Config permutación automática",
    "signal_timeframe": "M15",  # sobreescribible por permutación
    "pares_pausados":   ["EUR_USD", "GBP_USD", "USD_CAD", "USD_CHF", "USD_JPY",
                         "AUD_USD", "NZD_USD"],
    "sesiones_pausadas": [],
    "sesiones_activas": ["london", "overlap", "new_york"],
    "cooldown_minutes": 15,
    "max_posiciones":   3,
    "max_pos_par":      2,
    "riesgo_pct":       0.015,
    "max_drawdown_dia": 0.04,
    "circuit_breaker_pct": 0.20,
    "min_sl_pips":      10,
    "max_sl_pips":      40,
    "rsi_low":          30,
    "rsi_high":         70,
    "min_win_rate":     0.38,
    # ── v13: protección de capital (activos en todos los runs) ───────────────
    "m1_entry_refinement":        False,   # M1 confirm desactivado por defecto en perms
    "m1_entry_timeout_min":       3,
    "max_trade_hours":            8,
    "min_pips_to_hold":           3,
    "max_consecutive_losses":     3,
    "consecutive_loss_pause_hours": 24,
}

# ── Definición de permutaciones ───────────────────────────────────────────────
# Cada entrada define las claves que varían respecto a BASE_PARAMS
PERMUTACIONES = [
    # ── Grupo A: Comparación de estrategias (mismos pares y params)
    {
        "nombre": "A1_Hammer_base",
        "grupo":  "A_estrategias",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "Baseline Hammer — media reversión en tendencia",
    },
    {
        "nombre": "A2_RSI_Bollinger_base",
        "grupo":  "A_estrategias",
        "estrategias_activas":  ["RSI_Bollinger"],
        "estrategias_pausadas": ["Hammer"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "Baseline RSI_Bollinger — referencia (ya sabemos que falla en tendencia)",
    },
    {
        "nombre": "A3_Ensemble_Hammer_RSI",
        "grupo":  "A_estrategias",
        "estrategias_activas":  ["Hammer", "RSI_Bollinger"],
        "estrategias_pausadas": [],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "Ensemble Hammer+RSI_Bollinger — acuerdo de dirección requerido",
    },

    # ── Grupo B: Selección de pares (solo Hammer)
    {
        "nombre": "B1_Hammer_NZD_solo",
        "grupo":  "B_pares",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "NZD_USD solo — en downtrend durante Feb-May 2026, ideal para Hammer",
    },
    {
        "nombre": "B2_Hammer_EURGBP_solo",
        "grupo":  "B_pares",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "EUR_GBP solo — datos desde 2021, más estable",
    },

    # ── Grupo C: RR ratio (Hammer)
    {
        "nombre": "C1_Hammer_RR15",
        "grupo":  "C_rr_ratio",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         1.5,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "RR=1.5 — breakeven WR=40%, más fácil de alcanzar",
    },
    {
        "nombre": "C2_Hammer_RR25",
        "grupo":  "C_rr_ratio",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.5,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "RR=2.5 — breakeven WR=28.6%, mayor reward por señal",
    },

    # ── Grupo D: SL multiplier (Hammer)
    {
        "nombre": "D1_Hammer_SL10",
        "grupo":  "D_sl_mult",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.0,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "SL=1.0×ATR — stop tighter, más trades, más SLs hit",
    },
    {
        "nombre": "D2_Hammer_SL20",
        "grupo":  "D_sl_mult",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      2.0,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "SL=2.0×ATR — stop más amplio, precio puede respirar",
    },

    # ── Grupo E: min_confidence (Hammer)
    {
        "nombre": "E1_Hammer_conf35",
        "grupo":  "E_confidence",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.35,
        "adx_max_rsi_bollinger": 25,
        "nota": "min_conf=0.35 — más señales (regime débil pasa el umbral)",
    },
    {
        "nombre": "E2_Hammer_conf45",
        "grupo":  "E_confidence",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.45,
        "adx_max_rsi_bollinger": 25,
        "nota": "min_conf=0.45 — solo señales en tendencia clara (regime_score≥0.35)",
    },

    # ── Grupo F: RSI_Bollinger con ADX más restrictivo
    {
        "nombre": "F1_RSI_ADX20",
        "grupo":  "F_rsiBB_adx",
        "estrategias_activas":  ["RSI_Bollinger"],
        "estrategias_pausadas": ["Hammer"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 20,
        "nota": "RSI_Bollinger ADX≤20 (score≤0.25) — solo mercados muy en rango",
    },
    {
        "nombre": "F2_RSI_RR15",
        "grupo":  "F_rsiBB_adx",
        "estrategias_activas":  ["RSI_Bollinger"],
        "estrategias_pausadas": ["Hammer"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         1.5,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "RSI_Bollinger RR=1.5 — breakeven más fácil para mean-rev",
    },

    # ── Grupo G: Configuraciones combinadas prometedoras
    {
        "nombre": "G1_Hammer_NZD_RR15",
        "grupo":  "G_combinados",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["NZD_USD"],
        "rr_ratio":         1.5,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.40,
        "adx_max_rsi_bollinger": 25,
        "nota": "Hammer NZD solo + RR=1.5 — aprovecha downtrend NZD con TP fácil",
    },
    {
        "nombre": "G2_Hammer_agresivo",
        "grupo":  "G_combinados",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         1.5,
        "sl_atr_mult":      1.0,
        "min_confidence":   0.35,
        "adx_max_rsi_bollinger": 25,
        "nota": "Hammer agresivo — RR=1.5 + SL=1.0 + conf=0.35, max señales",
    },
    {
        "nombre": "G3_Hammer_conservador",
        "grupo":  "G_combinados",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "NZD_USD"],
        "rr_ratio":         2.5,
        "sl_atr_mult":      2.0,
        "min_confidence":   0.45,
        "adx_max_rsi_bollinger": 25,
        "nota": "Hammer conservador — RR=2.5 + SL=2.0 + conf=0.45, calidad máxima",
    },

    # ── Grupo H: Sin filtro ADX de régimen para Hammer (EUR_GBP solo)
    # Hipótesis: EUR_GBP pasa mucho tiempo con ADX 15-22 (bloqueado por régimen).
    # En rango, los hammers de EUR_GBP SÍ revierten. Bypass con conf=0.20 → peso×0.20
    # siempre pasa (0.45×0.70=0.315 ≥ 0.20). Comprobamos si más trades = más PnL.
    {
        "nombre": "H1_Hammer_EURGBP_noADX",
        "grupo":  "H_sin_adx",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "nota": "Hammer EUR_GBP sin bloqueo ADX — conf=0.20 deja pasar todos los regimes",
    },
    {
        "nombre": "H2_Hammer_EURGBP_noADX_RR15",
        "grupo":  "H_sin_adx",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         1.5,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "nota": "Hammer EUR_GBP sin ADX + RR=1.5 — máximas señales + TP fácil (baseline ganador)",
    },

    # ── Grupo I: Fase 2 — Grid de calidad de señal (EUR_GBP solo, no-ADX)
    # Objetivo: subir WR desde 35% filtrando señales de menor calidad.
    # Variables: rsi_hammer_max (50 vs 45 vs 40) × prev_bearish_min (1 vs 2)
    # RR=2.0 en todos (breakeven=33.3% < WR=35% → ya rentable si WR se mantiene).
    # Cada celda = 1 permutación. 6 combos × 8 semanas ≈ 2-3 horas de cómputo.
    {
        "nombre": "I1_RSI50_PB1_baseline",
        "grupo":  "I_calidad_senal",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "nota": "Baseline Fase 2: RSI<50 + ≥1/2 bajistas (equivale a H1 con RR=2.0)",
    },
    {
        "nombre": "I2_RSI45_PB1",
        "grupo":  "I_calidad_senal",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   45,
        "prev_bearish_min": 1,
        "nota": "RSI<45 + ≥1/2 bajistas — filtra señales RSI 45-50 (tibia zona)",
    },
    {
        "nombre": "I3_RSI40_PB1",
        "grupo":  "I_calidad_senal",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   40,
        "prev_bearish_min": 1,
        "nota": "RSI<40 + ≥1/2 bajistas — solo sobrevendido real, menos trades",
    },
    {
        "nombre": "I4_RSI50_PB2",
        "grupo":  "I_calidad_senal",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 2,
        "nota": "RSI<50 + 2/2 bajistas obligatorio — contexto de caída más sólido",
    },
    {
        "nombre": "I5_RSI45_PB2",
        "grupo":  "I_calidad_senal",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   45,
        "prev_bearish_min": 2,
        "nota": "RSI<45 + 2/2 bajistas — combinación estricta, señales de máxima calidad",
    },
    {
        "nombre": "I6_RSI40_PB2",
        "grupo":  "I_calidad_senal",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   40,
        "prev_bearish_min": 2,
        "nota": "RSI<40 + 2/2 bajistas — filtro máximo, pocos trades, WR esperado más alto",
    },

    # ── Grupo J: SL multiplier (EUR_GBP solo, no-ADX, RR=2.0)
    # El único que no probamos con EUR_GBP solo. SL más amplio → WR sube?
    {
        "nombre": "J1_SL10_RR20",
        "grupo":  "J_sl_eurgbp",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.0,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "nota": "SL=1.0×ATR — stop ajustado, más SLs prematuros pero pérdidas menores",
    },
    {
        "nombre": "J2_SL20_RR20",
        "grupo":  "J_sl_eurgbp",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      2.0,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "nota": "SL=2.0×ATR — el precio puede respirar más, WR hipotéticamente más alto",
    },

    # ── Grupo O: Optimización RR con Hammer M1 ADX≤25 en EUR_GBP
    # Base: L2 validada (WR=33.3% estable en 26 sem, PF=0.97, spread come el margen).
    # Con WR=33.3%, el RR mínimo para cubrir spread (~0.8 pip en 10-15 pip SL ≈ 5-8%
    # de drag por trade) es aproximadamente 2.1-2.2.
    # Hipótesis: si WR se mantiene ≥30% al subir RR, el ROI se vuelve positivo.
    # RR=2.2 → breakeven WR=31.3%  | RR=2.5 → breakeven WR=28.6% | RR=3.0 → 25%
    # Riesgo: TP más lejano → menos trades alcanzan TP → WR puede caer.
    {
        "nombre": "O1_RR22",
        "grupo":  "O_rr_optimizacion",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.2,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "RR=2.2 — breakeven WR=31.3%, mínimo ajuste para cubrir spread",
    },
    {
        "nombre": "O2_RR25",
        "grupo":  "O_rr_optimizacion",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.5,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "RR=2.5 — breakeven WR=28.6%, margen cómodo si WR se mantiene ~33%",
    },
    {
        "nombre": "O3_RR30",
        "grupo":  "O_rr_optimizacion",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         3.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "RR=3.0 — breakeven WR=25%, máxima ambición, WR probablemente cae bajo 28%",
    },

    # ── Grupo N: Más volumen — pares adicionales con Hammer M1 ADX≤25
    # Objetivo: aumentar el nº de trades/semana añadiendo pares con historial
    # M1 completo (desde 2021). Base: config L2 validada (WR=33.3% en 26 sem).
    # N1 = EUR_GBP + GBP_USD   → mismo espacio GBP, patrones similares
    # N2 = EUR_GBP + EUR_USD   → mismo espacio EUR, el par más líquido
    # N3 = EUR_GBP + USD_CAD   → par no correlacionado, diversificación real
    # N4 = EUR_GBP + GBP_USD + EUR_USD  → 3 pares, máximo volumen posible
    # Todos: Hammer | M1 | RR=2.0 | SL=1.5×ATR | ADX≤25 | v13 completo
    {
        "nombre": "N1_EURGBP_GBPUSD",
        "grupo":  "N_mas_pares",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "GBP_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "EUR_GBP + GBP_USD — pares GBP, patrones similares, +50% volumen esperado",
    },
    {
        "nombre": "N2_EURGBP_EURUSD",
        "grupo":  "N_mas_pares",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "EUR_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "EUR_GBP + EUR_USD — el par más líquido, spreads bajos, más señales",
    },
    {
        "nombre": "N3_EURGBP_USDCAD",
        "grupo":  "N_mas_pares",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "USD_CAD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "EUR_GBP + USD_CAD — diversificación real, correlación baja con EUR_GBP",
    },
    {
        "nombre": "N4_EURGBP_GBPUSD_EURUSD",
        "grupo":  "N_mas_pares",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP", "GBP_USD", "EUR_USD"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "max_pos_par":      2,
        "max_posiciones":   4,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "3 pares — máximo volumen, max_pos=4 para soportar más trades simultáneos",
    },

    # ── Grupo M: Análisis de calidad del patrón Hammer en M1 (ADX≤25 activo en todos)
    # Objetivo: identificar qué mejora adicional sube el WR por encima de 33.3%.
    # M0 = control idéntico a L2 (sin cambios, referencia 8-semanas).
    # M1 = confirmación de vela M1 posterior al Hammer (m1_entry_refinement).
    # M2 = filtro de cuerpo mínimo 5% del rango (elimina mini-hammers de microestructura).
    # M3 = confirmación estructural M15 (EMA20<EMA50 en M15 antes de ejecutar M1).
    # Todos comparten: Hammer | EUR_GBP | M1 | RR=2.0 | SL=1.5×ATR | ADX≤25.
    {
        "nombre": "M0_control_L2",
        "grupo":  "M_calidad_hammer",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "m1_entry_refinement":        False,
        "hammer_min_body_pct":        0.0,
        "hammer_m15_confirm":         False,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Control M0: idéntico a L2 — referencia base antes de mejoras",
    },
    {
        "nombre": "M1_entry_refinement",
        "grupo":  "M_calidad_hammer",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "m1_entry_refinement":        True,   # espera vela M1 alcista tras Hammer
        "m1_entry_timeout_min":       3,
        "hammer_min_body_pct":        0.0,
        "hammer_m15_confirm":         False,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Paso 1: confirmación vela M1 alcista posterior (evita entrar en Hammers fallidos)",
    },
    {
        "nombre": "M2_min_body",
        "grupo":  "M_calidad_hammer",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "m1_entry_refinement":        False,
        "hammer_min_body_pct":        0.05,  # cuerpo ≥ 5% del rango total
        "hammer_m15_confirm":         False,
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Paso 2: cuerpo mínimo 5% rango — filtra mini-hammers de microestructura M1",
    },
    {
        "nombre": "M3_m15_confirm",
        "grupo":  "M_calidad_hammer",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,
        "m1_entry_refinement":        False,
        "hammer_min_body_pct":        0.0,
        "hammer_m15_confirm":         True,  # EMA20 < EMA50 en M15 obligatorio
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Paso 3: confirmación M15 estructural — EMA20<EMA50 en M15 antes de ejecutar M1",
    },

    # ── Grupo L: ADX activo vs sin ADX en Hammer (EUR_GBP, M1, v13 completo)
    # Objetivo: medir si filtrar por ADX≤25 sube el WR y compensa los trades perdidos.
    # L1 = control sin ADX (igual a K2, referencia limpia post-fix StaleExit).
    # L2 = con ADX activo: bloquea señales cuando ADX>25 (tendencia fuerte).
    # Hipótesis: en sem 6-7 (EUR/GBP caída sostenida), ADX era >30 → L2 habría
    # bloqueado esos trades, reduciendo SLs a costa de menos trades totales.
    # Si WR de L2 ≥ 33.3% → estrategia rentable con RR=2.0.
    {
        "nombre": "L1_M1_sinADX_control",
        "grupo":  "L_adx_hammer",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   99,   # sin filtro ADX (control)
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Control M1 sin ADX — referencia post-fix StaleExit (equivale a K2 corregido)",
    },
    {
        "nombre": "L2_M1_conADX25",
        "grupo":  "L_adx_hammer",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "adx_max_hammer":   25,   # bloquea si ADX > 25 (tendencia fuerte)
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "ADX≤25 activo — bloquea hammers en tendencias sostenidas (ADX>25)",
    },

    # ── Grupo K: Comparativa M1 vs M15 (con v13 fixes activos)
    # Objetivo: medir empíricamente si señales M1 superan M15 en EUR_GBP.
    # Mismos parámetros en ambos, solo cambia signal_timeframe.
    # K1 = control M15 con todos los fixes v13.
    # K2 = M1 señales: Hammer detectado en velas de 1 minuto, EMA/RSI en M1.
    #   Esperado: más señales, menor WR por ruido, spread relativo mayor.
    #   Si K2 supera K1 → M1 viable. Si no → M15 es el timeframe correcto.
    {
        "nombre": "K1_M15_v13_control",
        "grupo":  "K_m1_vs_m15",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M15",
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Control M15 con v13 fixes: stale_exit + consec_loss_cooldown + fix max_pos_par",
    },
    {
        "nombre": "K2_M1_experimental",
        "grupo":  "K_m1_vs_m15",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["RSI_Bollinger"],
        "pares_activos": ["EUR_GBP"],
        "rr_ratio":         2.0,
        "sl_atr_mult":      1.5,
        "min_confidence":   0.20,
        "adx_max_rsi_bollinger": 25,
        "rsi_hammer_max":   50,
        "prev_bearish_min": 1,
        "signal_timeframe": "M1",
        "max_trade_hours":            8,
        "min_pips_to_hold":           3,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Experimental M1: señales Hammer en velas de 1 min — más frecuencia, más ruido",
    },

    # ── Grupo P: Restauración checkpoint_20260509 — RSI_Bollinger M15 6 pares ──
    {
        "nombre": "P1_RSI_checkpoint",
        "grupo":  "P_checkpoint",
        "estrategias_activas":  ["RSI_Bollinger", "RSI_Divergence"],
        "estrategias_pausadas": ["Doji", "EMA_Crossover", "Hammer", "Engulfing"],
        "pares_activos": ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"],
        "signal_timeframe":  "M15",
        "rr_ratio":          2.0,
        "sl_atr_mult":       1.5,
        "min_sl_pips":       10,
        "max_sl_pips":       40,
        "min_confidence":    0.40,
        "riesgo_pct":        0.01,
        "max_posiciones":    3,
        "max_pos_par":       2,
        "adx_max_rsi_bollinger": 25,
        "max_trade_hours":   8,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Restauración checkpoint_20260509: RSI_Bollinger+RSI_Div M15 6 pares — backtest referencia 1yr +120%",
    },

    # ── Grupo Q: Restauración backup_20260509_2240 ────────────────────────────
    # Arquitectura fiel al backup: _prefiltro_tecnico + DeepSeek Flash.
    # Sin ensemble, sin régimen ADX, sin H4, sin calendario.
    # Timeframe M1, conf=0.35, max_pos=2, 5 estrategias (sin EMA_Crossover).
    {
        "nombre": "Q1_backup_2240",
        "grupo":  "Q_backup_2240",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["Doji", "Engulfing", "RSI_Bollinger", "RSI_Divergence", "EMA_Crossover"],
        "pares_activos": ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"],
        "signal_timeframe":  "M1",
        "rr_ratio":          2.0,
        "sl_atr_mult":       1.5,
        "min_sl_pips":       8,
        "max_sl_pips":       50,
        "min_confidence":    0.35,
        "riesgo_pct":        0.01,
        "max_posiciones":    2,
        "max_pos_par":       2,
        "adx_max_rsi_bollinger": 25,
        "max_trade_hours":   8,
        "max_drawdown_dia":  0.03,
        "min_win_rate":      0.38,
        "rsi_hammer_max":    50,
        "prev_bearish_min":  1,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Restauración backup_20260509_2240: prefiltro_tecnico+DS, M1, conf=0.35, max_pos=2, 5 estrategias",
    },

    # ── Grupo Q2: Restauración backup_20260510_0020_prefase4 ─────────────────
    # Arquitectura: prefiltro_tecnico → régimen ADX (soft block 0.7×umbral) →
    # DeepSeek con contexto de régimen incluido. 5 filtros en cascada.
    # Diferencias clave vs Q1: regime integrado en conf, min_confidence=0.20,
    # riesgo_pct=0.005, RR=1.8, max_sl_pips=30, max_posiciones=5.
    {
        "nombre": "Q2_prefase4_hammer",
        "grupo":  "Q_backup_prefase4",
        "estrategias_activas":  ["Hammer"],
        "estrategias_pausadas": ["Doji", "Engulfing", "RSI_Bollinger", "RSI_Divergence", "EMA_Crossover"],
        "pares_activos": ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"],
        "signal_timeframe":  "M1",
        "rr_ratio":          1.8,
        "sl_atr_mult":       1.5,
        "min_sl_pips":       10,
        "max_sl_pips":       30,
        "min_confidence":    0.2,
        "riesgo_pct":        0.005,
        "max_posiciones":    5,
        "max_pos_par":       2,
        "adx_max_rsi_bollinger": 25,
        "max_trade_hours":   8,
        "max_drawdown_dia":  0.05,
        "min_win_rate":      0.45,
        "rsi_hammer_max":    50,
        "prev_bearish_min":  1,
        "max_consecutive_losses":     3,
        "consecutive_loss_pause_hours": 24,
        "nota": "Restauración backup_20260510_0020_prefase4: prefiltro+régimen_soft+DS, M1, conf=0.20, RR=1.8, Hammer solo",
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _escribir_params(perm: dict) -> None:
    """Construye y escribe strategy_params.json para esta permutación."""
    p = dict(BASE_PARAMS)
    # Campos obligatorios que vienen de la permutación
    for k in ("estrategias_activas", "estrategias_pausadas", "pares_activos",
              "rr_ratio", "sl_atr_mult", "min_confidence",
              "adx_max_rsi_bollinger"):
        p[k] = perm[k]
    # Campos opcionales — usan valor de permutación si existe, si no el default de BASE_PARAMS
    for k_opt, default in (
        ("rsi_hammer_max",             50),
        ("prev_bearish_min",            1),
        ("signal_timeframe",         "M15"),
        ("m1_entry_refinement",       False),
        ("m1_entry_timeout_min",          3),
        ("max_trade_hours",               8),
        ("min_pips_to_hold",              3),
        ("max_consecutive_losses",        3),
        ("consecutive_loss_pause_hours", 24),
        ("adx_max_hammer",               99),   # 99=desactivado por defecto
        ("hammer_min_body_pct",         0.0),   # 0=desactivado
        ("hammer_m15_confirm",        False),   # False=desactivado
        ("max_drawdown_dia",           0.04),
        ("min_win_rate",               0.38),
        ("max_posiciones",                3),
        ("riesgo_pct",                 0.015),
        ("min_sl_pips",                  10),
        ("max_sl_pips",                  40),
    ):
        p[k_opt] = perm.get(k_opt, default)
    # Garantía: ningún par activo puede estar también en pausados
    p["pares_pausados"] = [
        par for par in p.get("pares_pausados", [])
        if par not in p.get("pares_activos", [])
    ]
    p["calibrado_por"] = f"permutacion_{perm['nombre']}"
    p["calibrado_en"]  = datetime.now().isoformat()
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def _limpiar_trades_log() -> None:
    """Borra el log de trades y el harness.log para que cada run empiece limpio."""
    TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    TRADES_LOG.write_text("[]", encoding="utf-8")
    # Limpiar el log del harness para que el parser solo vea este run
    try:
        HARNESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        HARNESS_LOG.write_text("", encoding="utf-8")
    except Exception:
        pass


def _parsear_metricas(log_text: str) -> dict:
    """
    Extrae las métricas clave del log del harness.
    Busca las líneas del RESUMEN FINAL.
    """
    m = {}

    # Retorno total:     +5.23%
    r = re.search(r"Retorno total:\s+([+-]?\d+\.\d+)%", log_text)
    m["roi_pct"] = float(r.group(1)) if r else None

    # Trades totales:    42
    r = re.search(r"Trades totales:\s+(\d+)", log_text)
    m["trades"] = int(r.group(1)) if r else None

    # Win Rate global:   38.1%
    r = re.search(r"Win Rate global:\s+(\d+\.\d+)%", log_text)
    m["wr_pct"] = float(r.group(1)) if r else None

    # Profit Factor:     1.23
    r = re.search(r"Profit Factor:\s+(\d+\.\d+)", log_text)
    m["pf"] = float(r.group(1)) if r else None

    # Max Drawdown:      8.5%
    r = re.search(r"Max Drawdown:\s+(\d+\.\d+)%", log_text)
    m["maxdd_pct"] = float(r.group(1)) if r else None

    # Capital final
    r = re.search(r"Capital final:\s+\$(\d+\.\d+)", log_text)
    m["capital_fin"] = float(r.group(1)) if r else None

    return m


def _score_config(m: dict) -> float:
    """
    Puntuación compuesta para rankear configuraciones.
    Prioriza PF > WR > ROI > (−MaxDD).
    Retorna -999 si no hay trades.
    """
    if not m.get("trades"):
        return -999.0
    pf    = m.get("pf",       0.0) or 0.0
    wr    = m.get("wr_pct",   0.0) or 0.0
    roi   = m.get("roi_pct",  0.0) or 0.0
    dd    = m.get("maxdd_pct",100.0) or 100.0
    # Normalizar: PF peso 40%, WR peso 30%, ROI peso 20%, DD penalización 10%
    score = (pf * 40) + (wr * 0.30) + (roi * 0.20) - (dd * 10)
    return round(score, 3)


# ── Runner principal ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Grid search de configuraciones")
    parser.add_argument("--semanas", type=int, default=13,
                        help="Semanas de backtest por run (default: 13)")
    parser.add_argument("--solo", type=str, default=None,
                        help="Correr solo la permutación con este nombre (para debug)")
    parser.add_argument("--grupo", type=str, default=None,
                        help="Correr solo las permutaciones de este grupo (ej: K_m1_vs_m15)")
    parser.add_argument("--desde", type=int, default=1,
                        help="Retomar desde la permutación N (1=inicio). Útil si se interrumpió.")
    args = parser.parse_args()

    # Backup del params original
    backup_path = PARAMS_FILE.parent / "strategy_params_backup_perm.json"
    if PARAMS_FILE.exists():
        shutil.copy2(PARAMS_FILE, backup_path)
        print(f"✓ Backup: {backup_path.name}")

    resultados = []
    perms_a_correr = PERMUTACIONES[args.desde - 1:]  # retomar desde N
    if args.solo:
        perms_a_correr = [p for p in PERMUTACIONES if args.solo in p["nombre"]]
        if not perms_a_correr:
            print(f"✗ No encontré permutación con nombre '{args.solo}'")
            sys.exit(1)
    elif args.grupo:
        perms_a_correr = [p for p in PERMUTACIONES if p.get("grupo") == args.grupo]
        if not perms_a_correr:
            print(f"✗ No encontré permutaciones en grupo '{args.grupo}'")
            print(f"  Grupos disponibles: {sorted(set(p['grupo'] for p in PERMUTACIONES))}")
            sys.exit(1)

    total = len(perms_a_correr)
    print(f"\n{'═'*65}")
    print(f"  GRID SEARCH — {total} permutaciones × {args.semanas} semanas")
    print(f"  Tiempo estimado: {total * 15}–{total * 30} minutos")
    print(f"  (cada run puede tardar 15-30 min — déjalo correr)")
    print(f"{'═'*65}\n")

    for idx, perm in enumerate(perms_a_correr, 1):
        nombre = perm["nombre"]
        grupo  = perm["grupo"]
        nota   = perm.get("nota", "")
        pares_str = "+".join(perm["pares_activos"])
        strats_str = "+".join(perm["estrategias_activas"])

        print(f"[{idx:02d}/{total:02d}] {nombre}")
        print(f"         Strats={strats_str} | Pares={pares_str} | "
              f"RR={perm['rr_ratio']} | SL={perm['sl_atr_mult']}×ATR | "
              f"conf={perm['min_confidence']} | ADX={perm['adx_max_rsi_bollinger']}")
        print(f"         {nota}")

        # ── Preparar ────────────────────────────────────────────────────────────
        _escribir_params(perm)
        _limpiar_trades_log()

        log_path = PERM_DIR / f"perm_{idx:03d}_{nombre}.log"

        # ── Correr backtest ─────────────────────────────────────────────────────
        t0 = time.time()
        error_msg = ""
        try:
            proc = subprocess.run(
                [sys.executable, "backtest_harness.py", "--semanas", str(args.semanas)],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7200,   # 2 horas máximo — los runs de 13 semanas tardan 15-30 min
            )
            stdout_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            error_msg = "ERROR: Timeout después de 7200s (2 horas)"
            stdout_output = ""
            print(f"         ⚠️  Timeout (>2h)")
        except Exception as e:
            error_msg = f"ERROR: {e}"
            stdout_output = ""
            print(f"         ⚠️  Error: {e}")

        elapsed = time.time() - t0

        # ── Leer log del harness (fuente principal — siempre completo) ──────────
        # El harness escribe a logs/backtest_harness.log VIA FileHandler.
        # stdout es una copia, pero si hay buffering o el proceso fue roto,
        # el archivo es más fiable para parsear el RESUMEN FINAL.
        harness_log_text = ""
        try:
            if HARNESS_LOG.exists() and HARNESS_LOG.stat().st_size > 0:
                harness_log_text = HARNESS_LOG.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

        # Usar archivo de log si contiene RESUMEN FINAL; si no, usar stdout
        if "RESUMEN FINAL" in harness_log_text:
            output = harness_log_text
        elif "RESUMEN FINAL" in stdout_output:
            output = stdout_output
        else:
            # Ninguno tiene el resumen: concatenar ambos para diagnóstico
            output = harness_log_text + "\n--- STDOUT ---\n" + stdout_output
            if error_msg:
                output = error_msg + "\n" + output

        # ── Guardar log ─────────────────────────────────────────────────────────
        header = (
            f"{'='*65}\n"
            f"PERMUTACIÓN: {nombre}\n"
            f"Estrategias: {strats_str}\n"
            f"Pares:       {pares_str}\n"
            f"RR:          {perm['rr_ratio']}\n"
            f"SL:          {perm['sl_atr_mult']}×ATR\n"
            f"conf:        {perm['min_confidence']}\n"
            f"ADX max:     {perm['adx_max_rsi_bollinger']}\n"
            f"Nota:        {nota}\n"
            f"Semanas:     {args.semanas}\n"
            f"Duración:    {elapsed:.1f}s\n"
            f"{'='*65}\n\n"
        )
        log_path.write_text(header + output, encoding="utf-8")

        # ── Parsear métricas ────────────────────────────────────────────────────
        metricas = _parsear_metricas(output)
        score    = _score_config(metricas)

        roi_str    = f"{metricas.get('roi_pct',0):+.1f}%" if metricas.get('roi_pct') is not None else "N/A"
        wr_str     = f"{metricas.get('wr_pct',0):.1f}%" if metricas.get('wr_pct') is not None else "N/A"
        trades_str = str(metricas.get('trades', 'N/A'))
        pf_str     = f"{metricas.get('pf',0):.2f}" if metricas.get('pf') is not None else "N/A"
        dd_str     = f"{metricas.get('maxdd_pct',0):.1f}%" if metricas.get('maxdd_pct') is not None else "N/A"

        estado = "✅" if (metricas.get('pf', 0) or 0) >= 1.4 else (
                  "⚠️" if (metricas.get('trades', 0) or 0) == 0 else "❌")

        print(f"         {estado} ROI={roi_str} | WR={wr_str} | "
              f"Trades={trades_str} | PF={pf_str} | MaxDD={dd_str} | "
              f"Score={score:.1f} | {elapsed:.0f}s\n")

        resultados.append({
            "rank":        idx,
            "nombre":      nombre,
            "grupo":       grupo,
            "estrategias": strats_str,
            "pares":       pares_str,
            "rr":          perm["rr_ratio"],
            "sl_mult":     perm["sl_atr_mult"],
            "confidence":  perm["min_confidence"],
            "adx_max":     perm["adx_max_rsi_bollinger"],
            "trades":      metricas.get("trades", 0) or 0,
            "wr_pct":      metricas.get("wr_pct", 0.0) or 0.0,
            "pf":          metricas.get("pf", 0.0) or 0.0,
            "roi_pct":     metricas.get("roi_pct", 0.0) or 0.0,
            "maxdd_pct":   metricas.get("maxdd_pct", 0.0) or 0.0,
            "capital_fin": metricas.get("capital_fin", 200.0) or 200.0,
            "score":       score,
            "nota":        nota,
            "log":         log_path.name,
        })

    # ── Restaurar params originales ─────────────────────────────────────────────
    if backup_path.exists():
        shutil.copy2(backup_path, PARAMS_FILE)
        print(f"✓ strategy_params.json restaurado desde backup")

    # ── Rankear por score ────────────────────────────────────────────────────────
    resultados_sorted = sorted(resultados, key=lambda x: x["score"], reverse=True)
    for rank, r in enumerate(resultados_sorted, 1):
        r["rank_final"] = rank

    # ── Guardar CSV ─────────────────────────────────────────────────────────────
    csv_path = PERM_DIR / "resumen.csv"
    campos = ["rank_final", "nombre", "grupo", "estrategias", "pares",
              "rr", "sl_mult", "confidence", "adx_max",
              "trades", "wr_pct", "pf", "roi_pct", "maxdd_pct",
              "capital_fin", "score", "nota"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for r in resultados_sorted:
            w.writerow({k: r.get(k, "") for k in campos})
    print(f"\n✓ CSV guardado: {csv_path}")

    # ── Guardar HTML ─────────────────────────────────────────────────────────────
    html_path = PERM_DIR / "resumen.html"
    _escribir_html(resultados_sorted, html_path, args.semanas)
    print(f"✓ HTML guardado: {html_path}")

    # ── Imprimir top 5 ───────────────────────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  TOP 5 CONFIGURACIONES (por Score compuesto)")
    print(f"{'═'*65}")
    for r in resultados_sorted[:5]:
        medal = ["🥇","🥈","🥉","4️⃣","5️⃣"][r["rank_final"]-1]
        print(f"\n{medal} #{r['rank_final']}  {r['nombre']}")
        print(f"   Strats={r['estrategias']} | Pares={r['pares']}")
        print(f"   RR={r['rr']} | SL={r['sl_mult']}×ATR | conf={r['confidence']}")
        print(f"   ROI={r['roi_pct']:+.1f}% | WR={r['wr_pct']:.1f}% | "
              f"PF={r['pf']:.2f} | MaxDD={r['maxdd_pct']:.1f}% | "
              f"Trades={r['trades']}")

    # ── Mejor config: escribir a strategy_params.json ────────────────────────────
    mejor = resultados_sorted[0]
    mejor_perm = next(p for p in PERMUTACIONES if p["nombre"] == mejor["nombre"])
    _escribir_params(mejor_perm)
    # Añadir nota del análisis
    sp = json.loads(PARAMS_FILE.read_text())
    sp["_comentario"] = (
        f"Mejor config permutación — {mejor['nombre']} | "
        f"Score={mejor['score']:.1f} | ROI={mejor['roi_pct']:+.1f}% | "
        f"WR={mejor['wr_pct']:.1f}% | PF={mejor['pf']:.2f}"
    )
    PARAMS_FILE.write_text(json.dumps(sp, ensure_ascii=False, indent=2))
    print(f"\n✓ Mejor config aplicada a strategy_params.json: {mejor['nombre']}")
    print(f"\nLogs individuales en: {PERM_DIR}")


# ── Generador HTML ────────────────────────────────────────────────────────────

def _escribir_html(resultados: list, path: Path, semanas: int) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    filas = ""
    for r in resultados:
        roi   = r["roi_pct"]
        pf    = r["pf"]
        wr    = r["wr_pct"]
        dd    = r["maxdd_pct"]
        trades= r["trades"]
        score = r["score"]

        # Color de fondo por score
        if score > 100:
            row_bg = "#1a3a1a"   # verde oscuro
        elif score > 0:
            row_bg = "#1a2a1a"   # verde muy oscuro
        elif score > -50:
            row_bg = "#2a2a1a"   # amarillo oscuro
        else:
            row_bg = "#3a1a1a"   # rojo oscuro

        roi_color = "#4ade80" if roi >= 0 else "#f87171"
        pf_color  = "#4ade80" if pf >= 1.4 else ("#facc15" if pf >= 1.0 else "#f87171")
        wr_color  = "#4ade80" if wr >= 40 else ("#facc15" if wr >= 33 else "#f87171")
        dd_color  = "#4ade80" if dd < 6 else ("#facc15" if dd < 15 else "#f87171")

        rank_medal = {1:"🥇",2:"🥈",3:"🥉"}.get(r["rank_final"], f"#{r['rank_final']}")

        filas += f"""
        <tr style="background:{row_bg}">
          <td style="text-align:center;font-weight:bold">{rank_medal}</td>
          <td style="font-family:monospace;font-size:12px">{r['nombre']}</td>
          <td style="font-size:11px;color:#94a3b8">{r['grupo']}</td>
          <td><b>{r['estrategias']}</b></td>
          <td>{r['pares']}</td>
          <td style="text-align:center">{r['rr']}</td>
          <td style="text-align:center">{r['sl_mult']}×</td>
          <td style="text-align:center">{r['confidence']}</td>
          <td style="text-align:center;color:{wr_color};font-weight:bold">{wr:.1f}%</td>
          <td style="text-align:center;color:{pf_color};font-weight:bold">{pf:.2f}</td>
          <td style="text-align:center;color:{roi_color};font-weight:bold">{roi:+.1f}%</td>
          <td style="text-align:center;color:{dd_color}">{dd:.1f}%</td>
          <td style="text-align:center">{r['trades']}</td>
          <td style="text-align:center;font-weight:bold;color:#e2e8f0">{score:.0f}</td>
          <td style="font-size:11px;color:#94a3b8;max-width:200px">{r.get('nota','')}</td>
        </tr>"""

    html = html.replace("{{FILAS}}", filas)
    path.write_text(html, encoding='utf-8')


if __name__ == "__main__":
    main()
