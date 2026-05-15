# Estado del Sistema

## Healthcheck — última ejecución

**Fecha**: 2026-05-13 03:31 UTC
**Resultado**: 11 PASS / 1 WARN / 1 FAIL

| CP | Nombre | Estado | Nota |
|----|--------|--------|------|
| CP1 | Configuración | ✅ PASS | 6 pares, RSI_Bollinger |
| CP2 | OANDA API | ✅ PASS | Balance: $99,997.42 |
| CP3 | Datos de mercado | ✅ PASS | 3 pares frescos M15 |
| CP4 | Pipeline Signal→Risk | ✅ PASS | Callback chain OK |
| CP5 | DeepSeek Signal | ✅ PASS | Chat model OK |
| CP6 | DeepSeek Risk | ✅ PASS | Reasoner OK |
| CP7 | OANDA Ejecución | ✅ PASS | 0 trades abiertos |
| CP8 | Audit / Log / Telegram | ✅ PASS | Log 3.6MB activo |
| CP9 | Servicio systemd | ✅ PASS | Config correcta |
| CP10 | Estabilidad proceso | ❌ FAIL | 14 crashes históricos (12-may, resueltos) — auto-resuelve a las 11:11 UTC |
| CP11 | Consistencia trades | ✅ PASS | 0 huérfanos |
| CP12 | Sincronía capital | ✅ PASS | $200.00 (+0.0% vs inicial) |
| CP13 | Señal sintética | ⚠️ WARN | DeepSeek devuelve SL=0 en contexto minimal — fallback matemático OK |

## Métricas de operación

- **Uptime actual**: desde 2026-05-13 02:58 UTC
- **CPU**: 0.2% promedio
- **RAM**: ~122 MB
- **Shutdown**: limpio en ~1 segundo (SIGTERM → cancel all tasks)
- **Trades registrados**: 3 históricos, 0 abiertos actualmente

## Fixes aplicados (esta sesión)

- ✅ Bug 1: RR validation post-DeepSeek — TP corregido si RR < mínimo
- ✅ Bug 2: Startup sync — adopta trades huérfanos de OANDA al arranque
- ✅ SIGTERM handling — `asyncio.all_tasks()` cancelados en shutdown
- ✅ systemd config — `StartLimitIntervalSec/Burst` movidos a `[Unit]`
- ✅ Healthcheck expandido de 8 a 13 checkpoints
- ✅ GitHub conectado — `NsthorNemesis/trading-bot`

## Pendientes

- [ ] CP10 auto-resuelve a las 11:11 UTC (crashes históricos fuera de ventana 24h)
- [ ] CP13 WARN — ajustar prompt DeepSeek en contexto sintético
- [ ] Activar estrategias adicionales (paquete fin de semana)
- [ ] Integrar OandaSentiment en modo live (actualmente en modo backtest)
- [ ] Script de recalibración automática semanal
