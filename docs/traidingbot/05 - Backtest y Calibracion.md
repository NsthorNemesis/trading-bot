# Backtest y Calibración

## Ciclo semanal

**Cuándo**: Sábados, mercado cerrado (después de 17:00 NYC / 22:00 UTC)
**Dónde**: Máquina local (no el VPS — demasiado pesado para 1vCPU/1GB)
**Resultado**: `data/calibration/strategy_params.json` actualizado

## Scripts disponibles

| Script | Uso | Ventana |
|--------|-----|---------|
| `fast_backtest_1year.py` | Backtest rápido 1 año | Rolling 52 semanas |
| `fast_backtest_26w.py` | Backtest 6 meses | Rolling 26 semanas |
| `fast_backtest_3years.py` | Backtest largo 3 años | Rolling completo |
| `backtest_engine.py` | Motor compartido | — |
| `scripts/recalibrar.py` | Recalibración automática | Ajusta params automáticamente |

## Proceso de recalibración (manual por ahora)

```bash
# 1. Correr backtest rolling window
cd C:\Users\na_sc\trading_bot_v11
python fast_backtest_1year.py

# 2. Analizar resultados
python analisis_backtest.py

# 3. Si los parámetros cambiaron, actualizar strategy_params.json
# 4. Commitear y deployar al VPS
git add data/calibration/strategy_params.json
git commit -m "calibracion: semana YYYY-WW"
git push

# 5. En el VPS
sudo systemctl restart trading_bot
```

## Métricas objetivo

| Métrica | Mínimo | Objetivo |
|---------|--------|----------|
| Win Rate | 34% | > 45% |
| RR Ratio | 2.0 | 2.0-2.5 |
| Profit Factor | 1.2 | > 1.5 |
| Max Drawdown | < 15% | < 10% |
| Sharpe Ratio | > 0.5 | > 1.0 |

## Resultados históricos

### Semana 2026-W20 (primera calibración pendiente)
> Completar después del primer fin de semana de operación

---

## Notas sobre datos históricos

- Fuente: OANDA API (velas M15 y H4)
- Período disponible: hasta 5 años según par
- Rolling window: ventana deslizante de 52 semanas (evita overfitting)
- Los datos del bot propio son complementarios, no reemplazan el histórico
