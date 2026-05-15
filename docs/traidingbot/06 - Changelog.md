# Changelog

## 2026-05-14 — Recalibración estratégica + Bugfixes producción

### Recalibración completa del sistema (backtest 4 semanas, 31-mar → 08-may 2026)

**Hallazgos clave del backtest:**

- RSI_Bollinger como única estrategia activa → 286 trades, WR=33%, PnL=**-$65.69** (breakeven requiere 34%)
- GBP/USD es el par más dañino: WR=28%, contribuye -$111 al PnL global. **Pausado.**
- EMA_Crossover (pausada) es la estrategia más sólida: WR=38.3%, PnL=+$69.97
- RSI_Divergence → WR=27.9%, confirmado como perdedora. **Mantener pausada.**
- SL 2.0× ATR vs 1.5× ATR: WR sube de 33% a 38%, MaxDD baja de 22% a 16%

**Análisis exhaustivo de 15 combinaciones (individual + pares + tríos + ALL4):**

Hallazgo crítico: RSI_Bollinger es negativo en todos los mercados trending. Cualquier combo que lo incluye sin EMA_Crossover → PnL negativo. EMA_Crossover es la estrategia dominante con WR=46.2% solo.

| Combinación | WR | PnL | ROI/DD |
|---|---|---|---|
| **EMA_X + Hammer + Doji** ← elegida | 42.2% | +$280 | 5.90x |
| EMA_X solo | 46.2% | +$265 | 6.91x |
| EMA_X + Hammer | 45.6% | +$287 | 7.05x |
| ALL4 (descartada) | 37.6% | +$221 | 3.04x |
| RSI_BB solo | 32.8% | -$23 | -0.27x |

**Configuración final aplicada — "EMA_X + Hammer + Doji":**

| Parámetro | Antes | Ahora |
|---|---|---|
| Estrategias activas | RSI_Bollinger | **EMA_Crossover, Hammer, Doji** |
| Estrategias pausadas | EMA_Crossover, RSI_Divergence... | **RSI_Bollinger**, RSI_Divergence, Engulfing |
| Pares activos | EUR, GBP, JPY, CHF, AUD | **EUR_USD, AUD_USD** |
| sl_atr_mult | 1.5 | **2.0** |

**Resultados esperados (backtest 4 semanas):**
- 253 trades, WR=42.2%, PnL=+$280, MaxDD=23.7%, ROI=+140%, ROI/DD=5.90x

**Archivos actualizados:**
- `data/calibration/strategy_params.json` — config final aplicada y validada
- `scripts/deploy_sabado.sh` — script de deploy automatizado

**Plan de fase 2 (en 2 semanas si WR real ≥ 38%):**
- Evaluar reactivar RSI_Bollinger solo en mercados de rango (H4 lateral)
- Si WR ≥ 42%: incrementar riesgo a 2.0% y añadir USD_JPY

---

## 2026-05-14 — Bugfixes producción (incluidos en paquete sábado)

### Bugs corregidos en `risk_execution_agent.py`

- **Bug 1 — SL demasiado ajustado**: Añadido enforcement de `min_sl_pips` en `procesar_senal()` como guardia final antes de ejecutar en OANDA. Independientemente del origen del SL (DeepSeek o fórmula), si el SL es menor que `min_sl_pips` (10 pips), se corrige automáticamente y se recalcula el TP manteniendo el RR.
- **Bug 2 — DeepSeek JSON vacío**: Añadido bucle de 3 intentos con `asyncio.sleep(1)` entre reintentos. Validación de contenido vacío antes de `json.loads`. Log diferenciado: warning por intento, error solo al agotar los 3 intentos.
- **Bug 3 — Event loop en thread**: `asyncio.get_event_loop()` fallaba en el thread del monitor de posiciones. Fix: `self._loop` capturado con `asyncio.get_running_loop()` en `run()`, usado en `_verificar_cierres`. Todas las llamadas `get_event_loop()` en contexto async reemplazadas por `get_running_loop()`.

---

## 2026-05-13 — Paquete fin de semana (listo para deploy)

### Archivos listos
- `agents/signal_agent/signal_agent.py` — v11.2 (ensemble + live sentiment)
- `scripts/recalibrar_semanal.py` — nuevo script de recalibración semanal

### signal_agent v11.2
- Ensemble ponderado: múltiples estrategias combinadas por consenso de dirección
- Bonus de confianza: +5% por cada estrategia adicional que confirma (max +15%)
- Conflictos de dirección (long=short) se descartan automáticamente
- OandaSentiment en modo live — llama al endpoint OANDA con cache 15min

### recalibrar_semanal.py
- Descarga datos frescos OANDA (18 meses M15)
- Rolling window 8 semanas, paso 2 semanas
- Evalúa 6 estrategias × 6 pares
- Aprobación: WR >= 34%, PF >= 1.1, trades >= 30
- Valida con DeepSeek Reasoner
- Genera nota Obsidian automáticamente

### Deploy
```bash
# Sabado: correr recalibracion, commit, push, restart VPS
python scripts/recalibrar_semanal.py --dry-run
python scripts/recalibrar_semanal.py
git add . && git commit -m "feat: ensemble + live sentiment + recalibrador semanal" && git push
# En VPS: sudo systemctl restart trading_bot && python3 healthcheck.py --slow
```

---

## 2026-05-13 — Sesión de estabilización y observabilidad

### Fixes críticos
- **SIGTERM handling**: shutdown limpio en 1s (antes 30s con SIGKILL). Fix: `asyncio.all_tasks()` cancelados en `_shutdown_completo()`
- **systemd config**: `StartLimitIntervalSec/Burst` movidos de `[Service]` a `[Unit]`
- **Bug RR post-DeepSeek**: si DeepSeek devuelve SL/TP con RR < mínimo, se corrige TP matemáticamente
- **Bug startup sync**: trades abiertos en OANDA se adoptan al arrancar, evitando huérfanos ciegos
- **CP12 falso positivo**: comparación capital vs inicial ($200), no vs OANDA demo ($99k)
- **CP10 falso positivo**: distingue crashes reales (`exit-code`) de paradas limpias (`Deactivated successfully`)

### Nuevas funcionalidades
- **Healthcheck expandido**: 8 → 13 checkpoints (CP9 systemd, CP10 estabilidad, CP11 consistencia, CP12 capital, CP13 señal sintética)
- **Flag --slow**: pausa 1s entre checkpoints para lectura cómoda
- **GitHub conectado**: `NsthorNemesis/trading-bot`, token guardado en VPS

### Root cause de crashes del 12-may
- `signal_agent.py` sobreescrito con `$(cat /ruta/local/signal_agent.py)` (shell syntax, no el contenido)
- 14 crashes entre 11:11-11:15 UTC antes de que systemd activara el circuit breaker
- Causa: heredoc mal formado durante una sesión de patching anterior

---

## 2026-05-12 — Refactoring de agentes y plugins

### Cambios
- Sistema de plugins `strategies/` implementado
- `signal_agent.py` refactorizado para usar plugins
- `market_agent.py` actualizado para M15 + H4 multi-timeframe
- `config/settings.py` como single source of truth
- `backtest_engine.py` motor compartido implementado

---

## 2026-05-11 — Deploy inicial VPS

### Cambios
- Bot desplegado en VPS DigitalOcean NYC1
- Servicio systemd configurado
- Cuenta OANDA practice conectada
- Telegram bot activo (@trading_v11_bot)
- Primeros 3 trades ejecutados (huérfanos ID=285, ID=352 cerrados posteriormente)
