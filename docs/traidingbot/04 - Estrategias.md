# Catálogo de Estrategias

## Parámetros globales actuales

```json
{
  "rr_ratio": 2.0,
  "sl_atr_mult": 1.5,
  "riesgo_pct": 0.015,
  "min_confidence": 0.30,
  "min_win_rate": 0.34
}
```

## Estrategias disponibles

### RSI_Bollinger ✅ Activa
- **Tipo**: Mean-reversion (rango)
- **Régimen óptimo**: Mercado en rango (ADX < 20), regime_score < 0.4
- **Lógica**: RSI sobrevendido/sobrecomprado + precio tocando banda de Bollinger
- **Peso régimen**: 1.40x en rango, 0.40x en tendencia (bloqueada si score > 0.62)
- **Pares mejores**: EUR_USD, GBP_USD (spreads ajustados, movimiento lateral frecuente)

### EMA_Crossover ⏸ Inactiva
- **Tipo**: Trend-following
- **Régimen óptimo**: Tendencia fuerte (ADX > 25), regime_score > 0.6
- **Lógica**: Cruce de EMA20 y EMA50 con confirmación H4
- **Peso régimen**: 0.55x en rango, 1.20x en tendencia
- **Filtro especial**: Requiere alineación H4 (no opera contra la tendencia mayor)

### Engulfing ⏸ Inactiva
- **Tipo**: Price action, trend-following
- **Régimen óptimo**: Tendencia con retrocesos
- **Lógica**: Vela envolvente en dirección de tendencia
- **Peso régimen**: 0.60x en rango, 1.40x en tendencia

### Hammer ⏸ Inactiva
- **Tipo**: Price action, reversión en soporte/resistencia
- **Régimen óptimo**: Tendencia con niveles clave
- **Lógica**: Patrón martillo/estrella fugaz en nivel de soporte/resistencia
- **Peso régimen**: 0.70x en rango, 1.35x en tendencia

### Doji ⏸ Inactiva
- **Tipo**: Indecisión, reversión
- **Régimen óptimo**: Rango / transición
- **Lógica**: Doji en nivel técnico relevante
- **Peso régimen**: 1.10x en rango, 0.85x en tendencia

### RSI_Divergence ⏸ Inactiva
- **Tipo**: Mean-reversion avanzada
- **Régimen óptimo**: Rango / final de tendencia
- **Lógica**: Divergencia entre precio y RSI (precio hace nuevo máximo, RSI no)
- **Peso régimen**: 1.20x en rango, 0.55x en tendencia

## Plan de activación gradual

1. **Semana 1-2**: Solo RSI_Bollinger — establecer baseline
2. **Fin de semana 1** (recalibración): Evaluar RSI_Bollinger, activar EMA_Crossover si backtest > 55% WR
3. **Fin de semana 2**: Evaluar ensemble RSI_Bollinger + EMA_Crossover, considerar Doji
4. **Semana 3+**: Activar estrategias adicionales según régimen dominante del mercado

## Cómo activar una estrategia

Editar `data/calibration/strategy_params.json`:
```json
{
  "estrategias_activas": ["RSI_Bollinger", "EMA_Crossover"]
}
```
El bot hace hot-reload automático — no necesita reiniciarse.
