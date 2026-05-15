# Trading Bot v11 — Base de Conocimiento

Vault de documentación técnica del proyecto. Cada nota cubre un aspecto del sistema.

## Índice

| Nota | Contenido |
|------|-----------|
| [[01 - Arquitectura]] | Diseño del sistema, agentes, flujo de datos |
| [[02 - Estado del Sistema]] | Estado actual, métricas, healthcheck |
| [[03 - Decisiones Tecnicas]] | Log de decisiones de diseño y por qué se tomaron |
| [[04 - Estrategias]] | Catálogo de estrategias, parámetros, rendimiento |
| [[05 - Backtest y Calibracion]] | Resultados de backtest semanales, rolling window |
| [[06 - Changelog]] | Historial de cambios por sesión |
| [[07 - VPS y Deploy]] | Guía de operaciones en el VPS |

## Estado rápido

- **Bot**: activo en VPS (DigitalOcean NYC1, 1vCPU / 1GB)
- **Pares activos**: EUR_USD, GBP_USD, USD_JPY, USD_CHF, AUD_USD, USD_CAD
- **Estrategia activa**: RSI_Bollinger
- **Capital**: $200 USD (cuenta practice OANDA)
- **Healthcheck**: 13 checkpoints — correr con `python3 healthcheck.py --slow`

## Comandos esenciales

```bash
# Estado del bot
systemctl is-active trading_bot

# Healthcheck completo
cd /root/trading_bot_v11 && source venv/bin/activate && python3 healthcheck.py --slow

# Logs en tiempo real
journalctl -u trading_bot -f

# Deploy de cambios
git add . && git commit -m "mensaje" && git push && sudo systemctl restart trading_bot
```
