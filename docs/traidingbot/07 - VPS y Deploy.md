# VPS y Deploy

## Acceso

```bash
ssh root@<VPS_IP>
cd /root/trading_bot_v11
source venv/bin/activate
```

## Comandos de operación diaria

```bash
# Estado del servicio
systemctl is-active trading_bot
systemctl status trading_bot --no-pager -l

# Logs en tiempo real
journalctl -u trading_bot -f

# Últimos 50 logs
journalctl -u trading_bot -n 50 --no-pager

# Healthcheck completo
python3 healthcheck.py --slow

# Healthcheck rápido (sin APIs externas)
python3 healthcheck.py --fast
```

## Deploy de cambios

```bash
# Desde el VPS (cambios hechos directamente)
git add .
git commit -m "tipo: descripcion breve"
git push
sudo systemctl restart trading_bot

# Verificar que arrancó bien
sleep 3 && systemctl is-active trading_bot
journalctl -u trading_bot -n 20 --no-pager
```

## Tipos de commit

| Prefijo | Uso |
|---------|-----|
| `fix:` | Corrección de bug |
| `feat:` | Nueva funcionalidad |
| `calibracion:` | Actualización de strategy_params.json |
| `refactor:` | Refactorización sin cambio funcional |
| `docs:` | Solo documentación |

## Servicio systemd

**Archivo**: `/etc/systemd/system/trading_bot.service`

```ini
[Unit]
Description=Trading Bot v11 — DeepSeek + OANDA Forex
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/root/trading_bot_v11
ExecStart=/root/trading_bot_v11/venv/bin/python main.py
Restart=on-failure
RestartSec=30s
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

**Después de modificar el service file**:
```bash
sudo systemctl daemon-reload
sudo systemctl restart trading_bot
```

## Estructura de archivos clave en VPS

```
/root/trading_bot_v11/
├── main.py                          # Orquestador principal
├── healthcheck.py                   # 13 checkpoints
├── .env                             # Credenciales (NO en Git)
├── data/calibration/
│   └── strategy_params.json         # Parámetros activos
├── logs/
│   ├── trades.json                  # Historial de trades
│   ├── trading_bot.log              # Log principal
│   └── trading_bot_error.log        # Solo errores
├── agents/
│   ├── market_agent/
│   ├── signal_agent/
│   ├── risk_execution_agent/
│   └── audit_agent/
├── strategies/                      # Plugins de estrategias
├── utils/                           # regime_detector, calendar, sentiment
└── config/settings.py               # Configuración central
```

## Variables de entorno (.env)

```env
OANDA_ACCESS_TOKEN=...
OANDA_ACCOUNT_ID=...
OANDA_ENVIRONMENT=practice
DEEPSEEK_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Rollback de emergencia

```bash
# Ver commits disponibles
git log --oneline -10

# Volver a un commit anterior
git checkout <HASH> -- agents/signal_agent/signal_agent.py
sudo systemctl restart trading_bot

# O rollback completo
git reset --hard <HASH>
sudo systemctl restart trading_bot
```
