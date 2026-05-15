# Trading Bot v11 — Registro de Cambios

---

## Sesión 2026-05-14

### Problema detectado: sobretrading por cooldown bajo
- **Síntoma**: backtest mostraba 128 trades en 3.5 semanas, clusters de 10 trades del mismo par en 2 horas
- **Causa**: `cooldown_minutes` era 15 en el motor de backtest
- **Fix**: `backtest_lab/engine.py` — cooldown actualizado a 240 min, max_pos a 6, max_pos_par a 2

### Cambios en Backtest Lab
| Archivo | Cambio |
|---|---|
| `backtest_lab/engine.py` | `cooldown_mins` default: 15 → 240, `max_pos`: 3 → 6, `max_pos_par`: nuevo campo = 2 |
| `backtest_lab/templates/index.html` | Pares por defecto: EUR_USD+AUD_USD+USD_CHF+USD_CAD → **AUD_USD+NZD_USD** |
| `backtest_lab/templates/index.html` | DeepSeek: activado por defecto (mejora WR) |
| `backtest_lab/templates/base.html` | Colores de texto mejorados (opacity 0.6 → color #cbd5e1, nav-link más brillante, form-labels en blanco) |

### Resultados de backtests (período: Apr 15 – May 13 2026)
| Config | Trades | WR | PnL |
|---|---|---|---|
| 3 pares con cooldown 15min (original) | 128 | 48.4% | +$230.71 |
| 3 pares con cooldown 240min | 48 | 41.7% | +$35.93 |
| 2 pares AUD+NZD con cooldown 240min | 37 | 48.6% | **+$54.51** |

### Conclusión de backtests
- **EUR/GBP descartado**: WR 33.3%, arrastra el portfolio
- **AUD/USD**: mejor par (55-58.8% WR)
- **NZD/USD**: segundo mejor (41.2% WR), se ve afectado negativamente con EUR/GBP en el mix
- **Config óptima validada**: RSI_Bollinger | AUD_USD + NZD_USD | cooldown 240 min

---

## Sesión 2026-05-15

### Diagnóstico VPS
- **Estado servicio**: ✅ active (running) desde 2026-05-15 02:19 UTC
- **main.py en VPS**: ✅ limpio, sin bug de código duplicado
- **main.py local**: ⚠️ tiene código duplicado en líneas 273-283 (pendiente fix)
- **Ruta VPS**: `/root/trading_bot_v11` (usuario root)
- **RAM**: 94.7MB | **CPU**: 6.6s acumulado

### Config VPS antes de actualizar (2026-05-15)
```json
{
  "estrategias_activas": ["RSI_Bollinger", "RSI_Divergence"],
  "pares_activos": ["GBP_USD", "USD_CAD", "AUD_USD"],
  "cooldown_minutes": 15,
  "max_posiciones": 3
}
```

### ✅ COMPLETADO — Actualizar strategy_params.json en VPS (2026-05-15 03:36 UTC)
- Config aplicada con `cat > ... << EOF`
- Verificado: `OK — ['AUD_USD', 'NZD_USD'] | cooldown: 240 min`
- Servicio reiniciado: PID 16779, active (running) desde 03:36:39 UTC
- RAM: 108.1MB | Sin errores en arranque

### Config validada a aplicar
- estrategias_activas: RSI_Bollinger
- pares_activos: AUD_USD, NZD_USD
- cooldown_minutes: 240
- max_posiciones: 4
- riesgo_pct: 0.015, sl_atr_mult: 2.0, rr_ratio: 2.0

---

## Pendientes
- [ ] Aplicar strategy_params.json validado en VPS
- [x] ✅ Corregir main.py local (código duplicado líneas 273-283) — 2026-05-15
- [ ] Habilitar SSH con contraseña desde PowerShell
- [ ] Descargar datos históricos 2023-2026 para backtest largo
