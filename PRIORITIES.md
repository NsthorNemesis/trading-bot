# Trading Bot v11 — Lista de Mejoras Priorizadas
_Última actualización: 2026-05-14_

---

## ✅ Implementadas (2026-05-12)

### Alta prioridad
- [x] **Estrategias como clases independientes** — `strategies/` plugin system con BaseStrategy, auto-descubrimiento dinámico
- [x] **Single source of truth para parámetros** — `DEFAULTS` eliminado de settings.py; cargar_params() falla explícito si JSON falta
- [x] **Timeframe como parámetro de configuración** — `signal_timeframe` en strategy_params.json; MarketAgent lo lee dinámicamente
- [x] **Pares activos dinámicos** — `pares_activos` en strategy_params.json; agregar un par = editar JSON + restart
- [x] **Hot-reload de parámetros** — ParamsWatcher detecta cambios en strategy_params.json y los aplica sin reinicio
- [x] **Detección de JSON duplicado en raíz** — advertencia al arranque si existe strategy_params.json fuera de data/calibration/

---

## 🔜 Pendientes

### 🚨 Urgente — Verificación post-deploy (2026-05-14)
- [ ] **Verificar main.py en VPS** — El archivo local fue reparado con `cat >>` pero no se confirmó SCP al VPS. Verificar que no esté truncado en línea 264: `tail -5 /root/trading_bot_v11/main.py`
- [ ] **Healthcheck 13/13** — Correr `python3 ~/trading_bot_v11/healthcheck.py --slow` y confirmar que los 3 bugs fixes están activos (Bug1: `_actualizar_trade_cerrado`, Bug2: `oanda_id=="?"`, Bug3: labs ImportError)
- [ ] **Actualizar Changelog** — Agregar a `docs/traidingbot/06 - Changelog.md` los 3 bugfixes del 2026-05-14:
  - Bug 1: `_actualizar_trade_cerrado` — trades.json no registraba cierre ni PnL
  - Bug 2: Guard `oanda_id == "?"` — zombie trades con fill sin tradeOpened
  - Bug 3: `oandapyV20.endpoints.labs` no existe en v0.7.2 → catch ImportError → neutral

### Alta prioridad
- [ ] **Eliminar JSON duplicado en raíz** — Borrar `./strategy_params.json` si existe. VPS: `rm /root/trading_bot_v11/strategy_params.json`
- [ ] **Subir min_confidence de 0.4 a 0.5** — Valor actual deja pasar señales débiles. Cambiar en `data/calibration/strategy_params.json`. Monitorear impacto en cantidad de trades/día.
- [ ] **Workflow Git** — Establecer flujo local → GitHub → VPS para evitar SCP manual. Mientras tanto, todo cambio en Windows debe SCPearse explícitamente antes de restart.

### Media prioridad
1. [ ] **Reemplazar deepseek-reasoner en RiskAgent** por deepseek-chat. El SL/TP matemático es suficiente. Reducirá latencia y costo por operación.
2. [x] **Motor de backtest compartido** — `backtest_engine.py` implementado. `backtest_variantes.py` y `backtest_ds_sim_1year.py` refactorizados como wrappers delgados (-69% y -53% de código respectivamente).
3. [ ] **Fix del firewall en DigitalOcean** — Agregar IP local al inbound rule del Cloud Firewall para SSH directo sin consola web.

### Baja prioridad
4. [ ] **Tests unitarios por estrategia** — Mínimo un test por estrategia que valide señal correcta con datos sintéticos conocidos.
5. [ ] **Logging estructurado en JSON** — Cambiar logs de texto libre a JSON para filtrado con `jq` por par, estrategia, resultado.

---

## 💰 Valor Comercial — Pendiente (no tocar hasta tener datos reales)

> **Prioridad: BLOQUEADA** hasta alcanzar los hitos de track record abajo.
> El sistema tiene mérito técnico real pero zero valor de mercado sin datos auditados.
> Leer análisis completo de la sesión 2026-05-14 si se retoma este tema.

### Hoja de ruta hacia valor comercial

| Hito | Fecha objetivo | Acción al llegar |
|------|---------------|-----------------|
| 30+ trades reales documentados en trades.json | ~Mayo 31 | Primera revisión honesta de WR real |
| 3 meses live, WR ≥ 36% sostenido | Agosto 2026 | Publicar en MyFXBook para auditoría pública |
| 6 meses live, WR ≥ 42%, DD < 30% | Noviembre 2026 | Evaluar modelo SaaS de señales ($49–$149/mes) |
| 12 meses live, Sharpe > 1.5, DD < 20% | Mayo 2027 | Contactar prop firms / licenciamiento |

### Gates de escalado de capital (independientes del valor comercial)
- WR real ≥ 38% con ≥ 30 trades → agregar USD_JPY a pares activos
- WR real ≥ 42% con ≥ 60 trades → subir riesgo/trade de 1.5% a 2.0%
- WR real < 33% en cualquier semana → reducir riesgo/trade a 1.0% y revisar

---

## 📋 Roadmap de comparación de resultados

**19 de mayo 2026** — Revisar bot en vivo vs Variante A (backtest RSI_Bollinger solo M15):

```bash
# En VPS — ejecutar el 19 de mayo
grep "SENAL DETECTADA" /root/trading_bot_v11/logs/trading_bot.log | wc -l
grep "ORDEN EJECUTADA\|trade ejecutado" /root/trading_bot_v11/logs/trading_bot.log
python3 -c "
import json
from pathlib import Path
trades = json.loads(Path('logs/trades.json').read_text()) if Path('logs/trades.json').exists() else []
wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
total = len(trades)
print(f'Trades: {total} | Wins: {wins} | WR: {wins/total:.1%}' if total else 'Sin trades aún')
"
```

**Benchmarks a comparar:**
- Variante A (backtest): WR 37.7%, ROI +337.1%, DD 28.6%
- Bot en vivo (7 días): ?, ?, ?

---

## 🏗️ Arquitectura actual (post-refactor)

```
trading_bot_v11/
├── strategies/              ← NUEVO: plugin system
│   ├── __init__.py          # auto-descubrimiento
│   ├── base_strategy.py     # clase abstracta
│   ├── rsi_bollinger.py     # ✅ activa
│   ├── rsi_divergence.py    # pausada
│   ├── ema_crossover.py     # pausada
│   ├── hammer.py            # pausada
│   ├── doji.py              # pausada
│   └── engulfing.py         # pausada
├── agents/
│   ├── market_agent/        # M1+M15+H4, pares dinámicos
│   ├── signal_agent/        # usa plugins, hot-reload
│   ├── risk_execution_agent/
│   └── audit_agent/
├── config/
│   └── settings.py          # sin DEFAULTS, falla explícito
├── data/calibration/
│   └── strategy_params.json # ÚNICA fuente de verdad
└── main.py                  # ParamsWatcher integrado
```

---

---

## 📊 Hallazgos del backtest 3 años (May 2022 → May 2025)

_Ejecutado 2026-05-11 | RSI_Bollinger M15 | Capital $200 | RR 2.0_

### Resultado global
| Métrica | Valor |
|---|---|
| Capital final | $734.85 |
| ROI 3 años | +267.4% |
| Win Rate global | 35.1% |
| **Max Drawdown** | **69.0% ⚠️** |
| Total trades | 2,810 |
| Semanas positivas | 83/156 |

### Conclusión crítica: el edge existe pero es frágil
El WR de 35.1% supera el breakeven (33.3% para RR 2.0), pero por solo **1.8 puntos porcentuales**. El drawdown del 69% indica que el sistema puede perder dos tercios del capital en un mal período. **No es seguro escalar capital sin resolver esto.**

### Desglose anual — inestabilidad por régimen de mercado
| Año | WR | PnL | Diagnóstico |
|---|---|---|---|
| 2022 | 37.4% ✅ | +$247 | Año de rangos y alta volatilidad — ideal para mean-reversion |
| 2023 | 35.6% ⚠️ | +$393 | Rentable pero debilitándose |
| 2024 | 33.6% ⚠️ | −$165 | **Año problemático**: USD bull run, carry trades. Mean-reversion sufre en tendencias fuertes |
| 2025 | 34.7% ⚠️ | +$59 | Recuperación parcial |

**Agosto 2024 fue el peor mes: WR 18.6%, −$103.** Los meses de tendencia fuerte destruyen el edge.

### Pares — quién gana y quién pierde (3 años)
| Par | WR | PnL neto | Decisión |
|---|---|---|---|
| USD_CAD | 39.2% ✅ | +$476 | **Mantener** |
| USD_CHF | 36.2% ⚠️ | +$549 | Mantener |
| AUD_USD | 34.1% ⚠️ | +$208 | Mantener con cautela |
| GBP_USD | 34.0% ⚠️ | −$228 | **Investigar pausa** |
| EUR_USD | 33.5% ⚠️ | −$288 | **Investigar pausa** |
| USD_JPY | 33.3% ⚠️ | −$182 | **Investigar pausa** |

### Sesiones — el overlap destruye valor
| Sesión | WR | PnL neto | Decisión |
|---|---|---|---|
| New York | 38.4% ✅ | +$29 | Mantener |
| London | 35.4% ⚠️ | +$814 | Mantener (mayor volumen) |
| Overlap | 33.6% ⚠️ | −$308 | **Investigar pausa — peor PnL** |

### Problema raíz identificado
RSI_Bollinger es **mean-reversion pura**. Funciona en mercados en rango. En tendencias fuertes (USD bull run 2024, carry trades) el RSI puede estar "oversold" durante semanas mientras el precio sigue cayendo. **La solución no es ajustar parámetros — es detectar el régimen y pausar en tendencias fuertes.**

---

## 🔬 Lista de investigación — Estrategias, Pares y Mercados

> **Metodología obligatoria antes de activar cualquier elemento:**
> 1. Backtest mínimo 2 años con `backtest_engine.py`
> 2. WR ≥ 37% sostenido (no solo promedio global)
> 3. DD máximo < 40%
> 4. Al menos 200 trades en el período
> 5. Comparar con operaciones reales del bot antes de activar en producción

---

### 🔧 Mejoras de infraestructura prioritarias (antes de escalar)

| Mejora | Objetivo | Impacto estimado |
|---|---|---|
| **Detector de régimen de mercado** | Pausar RSI_Bollinger cuando ADX > 30 sostenido (tendencia fuerte) | Eliminar pérdidas en 2024-style markets |
| **Pausa overlap session** | Desactivar sesión overlap (13-17 UTC) | −$308 PnL en 3 años → recuperar |
| **Pausar EUR_USD + GBP_USD + USD_JPY** | Concentrar en pares rentables | +$700 PnL acumulado en 3 años |
| **Circuit breaker mensual** | Pausar si WR del mes cae < 28% | Proteger contra meses tipo Aug-2024 |

---

### 📈 Estrategias a investigar

#### Complementarias a RSI_Bollinger (misma infraestructura)

| Estrategia | Tipo | Por qué investigar | Prioridad |
|---|---|---|---|
| **BB Squeeze + Breakout** | Breakout de volatilidad | Captura movimientos que RSI_Bollinger pierde en tendencias | Alta |
| **RSI Multi-TF** | Mean-reversion confirmada | RSI oversold en M15 + H1 + H4 simultáneo → señal mucho más fuerte | Alta |
| **VWAP Mean Reversion** | Institucional | Niveles de referencia institucional, funciona en intradía | Media |
| **Divergencia RSI mejorada** | Momentum | Ya existe en el sistema (pausada) — refinar umbrales con datos reales | Media |
| **Supertrend como filtro** | Filtro de régimen | Usar Supertrend para detectar tendencia y pausar mean-reversion | Media |
| **Hammer/Engulfing en soporte** | Price action | Ya existen (pausadas) — activar solo en S/R confirmados | Baja |

#### Estrategias para mercados en tendencia (cubrir el gap de 2024)

| Estrategia | Tipo | Por qué investigar | Prioridad |
|---|---|---|---|
| **EMA Crossover + ADX** | Trend-following | Rentable en USD bull runs como 2024 donde RSI_Bollinger pierde | Alta |
| **Breakout de rango semanal** | Breakout | Opera la ruptura del rango H4/D1, captura tendencias largas | Media |
| **Momentum ROC** | Momentum | Entra cuando el precio ya lleva impulso confirmado | Baja |

---

### 💱 Pares a investigar (Forex)

| Par | Razón | Data disponible en OANDA |
|---|---|---|
| **NZD_USD** | Correlacionado con AUD_USD (mejor par actual), menor spread | ✅ |
| **EUR_GBP** | Menor volatilidad que EUR_USD/GBP_USD individualmente, buen rango | ✅ |
| **GBP_JPY** | Alta volatilidad, grandes movimientos — testar RSI extremo | ✅ |
| **EUR_JPY** | Carry trade popular, comportamiento diferente en tendencias | ✅ |
| **USD_SGD** | Mercado asiático menos saturado | ✅ |
| **XAU_USD** | Oro — mean-reversion fuerte, alta volatilidad, spreads altos | ✅ (CFD) |

---

### 🌍 Otros mercados a investigar

| Mercado | Instrumento | Tipo | Por qué | Complejidad |
|---|---|---|---|---|
| **Oro** | XAU_USD | CFD OANDA | RSI_Bollinger funciona muy bien en oro según literatura | Media |
| **Índices US** | US500, NAS100 | CFD OANDA | Tendencias más limpias, horario definido | Alta |
| **Índices EU** | DE40, UK100 | CFD OANDA | Sesión London — misma infraestructura | Alta |
| **Petróleo** | BCO_USD | CFD OANDA | Alta volatilidad, movimientos bruscos | Alta |
| **Cripto** | BTC/USD | API externa | 24/7, sin sesiones — requiere adaptar filtros | Muy alta |

> ⚠️ **Nota cripto**: requiere cambiar el broker/API. OANDA no ofrece cripto. Evaluar Binance API o similar solo después de validar el edge en Forex.

---

### 📋 Pipeline de investigación sugerido

```
Fase 1 (ahora — antes del 19 mayo):
  → Comparar bot en vivo vs backtest RSI_Bollinger
  → Decisión: ¿el edge se sostiene en producción?

Fase 2 (mayo-junio 2026):
  → Implementar detector de régimen más agresivo
  → Pausar overlap session y pares negativos
  → Backtest 3 años con configuración corregida → objetivo DD < 40%

Fase 3 (junio-agosto 2026):
  → Investigar BB Squeeze + EMA Crossover con datos reales
  → Testear NZD_USD y EUR_GBP con el motor existente
  → Si WR real ≥ 36% en 60 días → escalar capital

Fase 4 (Q3 2026):
  → Investigar XAU_USD (Oro) — requiere ajuste de SL por spread
  → Evaluar índices US (US500) con sesión NY
  → Comparar con 90 días de operaciones reales antes de activar
```

---

## 🔗 Herramientas de referencia

| Herramienta | URL | Uso recomendado |
|---|---|---|
| **HobbieCode Generador de Prompts** | https://hobbiecode.com/generador-prompt-robot | Prototipar ideas de estrategia en Pine Script/TradingView antes de portarlas a Python. No usar para el bot principal. |

---

## 💡 Para agregar una nueva estrategia

1. Crear `strategies/mi_estrategia.py` con clase que herede `BaseStrategy`
2. Establecer `NAME = "MiEstrategia"`
3. Implementar `generate_signal(df, par, params) -> Optional[dict]`
4. Agregar `"MiEstrategia"` a `estrategias_activas` en `strategy_params.json`
5. **Sin tocar ningún otro archivo** — el sistema la descubre automáticamente

## 💡 Para agregar un nuevo par

1. Agregar el par a `pares_activos` en `strategy_params.json`  
   Ej: `"pares_activos": ["EUR_USD", ..., "NZD_USD"]`
2. Reiniciar el bot: `sudo systemctl restart trading_bot`
3. El sistema descarga automáticamente M1, M15 y H4 para el nuevo par
