# Arquitectura del Sistema

## Diagrama de flujo

```
MarketAgent → SignalAgent → RiskExecutionAgent → OANDA
                  ↑               ↑
           strategies/       DeepSeek Reasoner
           regime_detector
           economic_calendar
           oanda_sentiment
                                  ↓
                            AuditAgent → trades.json
                                       → Telegram
```

## Agentes

### MarketAgent (`agents/market_agent/market_agent.py`)
- Mantiene stream de velas M15 y H4 para todos los pares activos
- Calcula indicadores: RSI, EMA20, EMA50, ATR, Bollinger Bands
- Detecta sesión de mercado (london / overlap / new_york / asia)
- Expone `get_df_m15()`, `get_df_h4()`, `tendencia_h4()`, `datos_frescos()`

### SignalAgent (`agents/signal_agent/signal_agent.py`)
Pipeline de 5 filtros:
1. **Cooldown + Sesión + Datos frescos** — no evalúa el mismo par más de 1x cada 15 min
2. **Calendario económico** — blackout 30min antes / 15min después de NFP, FOMC, CPI, ECB, BOE, GDP
3. **Plugin de estrategias** — evalúa todas las estrategias activas, toma la de mayor confianza
4. **Régimen ADX** — pondera la confianza según si el mercado está en tendencia o rango
5. **DeepSeek Chat** — confirmación final con contexto M15 + H4
6. **Sentimiento OANDA** — ajuste contrarian final (+0.05 / -0.08)

### RiskExecutionAgent (`agents/risk_execution_agent/risk_execution_agent.py`)
- Recibe señal del SignalAgent via callback
- Calcula SL/TP con ATR × SL_mult, verifica RR mínimo
- Consulta DeepSeek Reasoner para refinar SL/TP
- Valida RR post-DeepSeek (si RR < mínimo, corrige TP matemáticamente)
- Ejecuta orden en OANDA
- Monitorea posiciones abiertas cada 30s
- Sincroniza trades huérfanos de OANDA al arranque

### AuditAgent (`agents/audit_agent/audit_agent.py`)
- Registra cada trade en `logs/trades.json`
- Envía alertas por Telegram (entrada, salida, errores)
- Monitorea capital en `capital_state.json`

## Sistema de estrategias (plugins)

Cada estrategia es un archivo independiente en `strategies/`:
- Hereda de `BaseStrategy`
- Implementa `generate_signal(df, par, params) → Optional[dict]`
- Se activa listándola en `strategy_params.json → estrategias_activas`
- **No requiere modificar ningún otro archivo**

### Estrategias disponibles
| Nombre | Archivo | Estado | Régimen óptimo |
|--------|---------|--------|----------------|
| RSI_Bollinger | rsi_bollinger.py | ✅ Activa | Rango |
| EMA_Crossover | ema_crossover.py | ⏸ Inactiva | Tendencia |
| Engulfing | engulfing.py | ⏸ Inactiva | Tendencia |
| Hammer | hammer.py | ⏸ Inactiva | Tendencia |
| Doji | doji.py | ⏸ Inactiva | Rango |
| RSI_Divergence | rsi_divergence.py | ⏸ Inactiva | Rango |

## Configuración

- **Single source of truth**: `data/calibration/strategy_params.json`
- **Variables de entorno**: `.env` (nunca en Git)
- **Settings**: `config/settings.py` — lee el JSON y expone `PARAMS`

## Infraestructura

- **VPS**: DigitalOcean NYC1, Ubuntu 22.04, 1vCPU / 1GB RAM
- **Servicio**: systemd `trading_bot.service`
  - Restart=on-failure, RestartSec=30s
  - StartLimitBurst=5 en ventana de 300s
  - TimeoutStopSec=15 (shutdown limpio en ~1s)
- **Backup**: GitHub `NsthorNemesis/trading-bot`
