# Plan de Mejoras — Trading Bot v12
**Fecha:** 2026-05-11
**Estado actual:** v11 en producción — paper trading activo en 24.199.87.217
**Backup:** backup_v11_20260511_012332 (completo, sin venv/logs)

---

## IMPACTO ESPERADO POR MEJORA

| Prioridad | Mejora                          | Win Rate | Trades/sem | Riesgo impl. |
|-----------|---------------------------------|----------|------------|--------------|
| 1         | Multi-timeframe H1/H4           | +12-15%  | sin cambio | Medio        |
| 2         | RSI_Divergence arreglar         | +5-8%    | x2         | Bajo         |
| 3         | EMA_Crossover (tendencia)       | +5-8%    | +40%       | Medio        |
| 4         | Trailing stop dinámico          | +3-5%    | sin cambio | Bajo         |
| 5         | Sentimiento real OANDA          | +2-4%    | sin cambio | Muy bajo     |
| 6         | Calendario dinámico BCE/BOE/BOJ | +2-3%    | sin cambio | Bajo         |
| 7         | Sizing dinámico por volatilidad | 0%       | sin cambio | Bajo         |
| 8         | Fix capital inicial             | 0%       | sin cambio | Muy bajo     |
| 9         | Dashboard web (Emergent)        | 0%       | sin cambio | Ninguno      |
| 10        | Fix logging duplicado           | 0%       | sin cambio | Ninguno      |
| 11        | Limpieza de archivos            | 0%       | sin cambio | Ninguno      |

**Win rate acumulado proyectado: ~45% actual → 62-68% con todas las mejoras**

---

## BLOQUE A — IMPACTO CRÍTICO EN WIN RATE
> Estas mejoras atacan la causa raíz del problema principal: señales ruidosas
> en M1 y ausencia de estrategia para mercados tendenciales.
> **Impacto estimado: +20-30% en win rate**

---

### A1 — Multi-timeframe: M1 → H1 para señales + H4 para tendencia
**Archivos:** `market_agent.py`, `signal_agent.py`
**Sesiones estimadas:** 2

**El problema central:**
El bot descarga velas M1 y calcula RSI, Bollinger y EMA sobre ellas.
El RSI en M1 entra en sobrecompra/sobreventa decenas de veces por hora sin
significado predictivo real. Esto genera falsas señales constantemente.

**Qué cambia:**

*Paso A1.1 — MarketAgent descarga 3 timeframes:*
- M1 → solo para monitoreo de posiciones abiertas (ya existe)
- H1 → para detección de setup y cálculo de indicadores (nuevo)
- H4 → para identificar la tendencia mayor (nuevo)
- Nuevos métodos: `get_df_h1(par, n)` y `get_df_h4(par, n)`

*Paso A1.2 — Prefiltro técnico sobre H1:*
- `self._market.get_df(par, n=60)` → `self._market.get_df_h1(par, n=60)`
- RSI en H1 tiene peso estadístico real: sobrecompra/sobreventa son
  eventos significativos, no ruido de 2 minutos

*Paso A1.3 — Filtro de tendencia H4 (nuevo Filtro 3.5):*
```
Si EMA20_H4 > EMA50_H4 (tendencia UP):   solo señales LONG
Si EMA20_H4 < EMA50_H4 (tendencia DOWN): solo señales SHORT
Si EMA20_H4 ≈ EMA50_H4 (rango):          ambas direcciones OK
```
Elimina la principal causa de SL: entrar en reversión contra la tendencia mayor.

*Paso A1.4 — Prompt DeepSeek enriquecido:*
Agregar al prompt: tendencia H4, RSI H1, últimos 5 cierres H4.
DeepSeek decide con contexto de 3 timeframes en lugar de solo M1.

**Criterio de éxito antes de deploy:**
Backtest 52 semanas con H1: win rate ≥ 50% (vs ~45% actual)

---

### A2 — RSI_Divergence: diagnosticar y reparar
**Archivo:** `agents/signal_agent/signal_agent.py` → `_prefiltro_tecnico()`
**Sesiones estimadas:** 0.5

**El problema:**
RSI_Divergence está activa en strategy_params.json pero nunca genera
señales. La mitad de la capacidad estratégica del bot está muerta.

**Análisis del prefiltro actual:**
La rama que asigna `estrat = "RSI_Divergence"` en el código es:
```python
elif rsi_bajo:   estrat = "RSI_Divergence"
```
Solo llega aquí si RSI < 32, no hay patrón de velas, no hay precio en banda
Bollinger inferior → condición prácticamente imposible de cumplir en M1.

**Fix:**
Definir condiciones propias para RSI_Divergence basadas en divergencia real:
- Precio hace mínimo más bajo que anterior
- RSI hace mínimo más alto que anterior (divergencia alcista)
- O inverso para short
- Funciona mejor en H1 (dependencia de A1)

**Impacto:** Potencialmente duplica el número de señales válidas.

---

### A3 — EMA_Crossover activo para mercados tendenciales
**Archivos:** `signal_agent.py`, `strategy_params.json`
**Sesiones estimadas:** 1.5

**El problema:**
RSI_Bollinger y RSI_Divergence son estrategias de reversión (comprar en
sobrevendido, vender en sobrecomprado). El mercado forex trendea el 60%
del tiempo. En mercados tendenciales estas estrategias fallan continuamente.
El bot no tiene ninguna estrategia para capturar tendencias.

**Condiciones de entrada EMA_Crossover:**
```
LONG:  EMA20 cruza EMA50 hacia arriba en H1 (últimas 3 velas)
       + MACD_DIF > 0 en H1
       + Tendencia H4 UP (requiere A1)
       + ADX H1 > 22 (mercado con momentum)

SHORT: inverso
```

**Lógica de activación:**
- Si régimen ADX = TENDENCIA (score > 0.60) → evaluar EMA_Crossover primero
- Si régimen ADX = RANGO (score < 0.40) → evaluar RSI_Bollinger primero
- Si TRANSICIÓN → evaluar ambas, tomar la de mayor confianza

**Backtesting requerido antes de deploy:**
Criterio mínimo: WR EMA_Crossover ≥ 48% y Profit Factor ≥ 1.3

---

## BLOQUE B — IMPACTO ALTO EN RESULTADO POR TRADE
> Mejoran lo que pasa después de entrar — capturan más ganancia
> y eliminan los trades que casi ganan pero terminan en SL.
> **Impacto estimado: +5-9% adicional en win rate efectivo**

---

### B1 — Trailing stop dinámico
**Archivo:** `agents/risk_execution_agent/risk_execution_agent.py`
→ `_monitor_posiciones()`
**Sesiones estimadas:** 1

**El problema:**
Los trades cierran solo por TP fijo o SL fijo. Los movimientos que llegan
al 70-80% del TP y revierten terminan en pérdida completa. Son los trades
más frustrantes y más comunes en reversión de mercado.

**Mecánica del trailing stop:**
```
Al abrir trade:     SL original (1.5×ATR bajo entry/sobre entry)
Al llegar al 50%TP: Mover SL a breakeven (entry) → riesgo = 0
Al llegar al 80%TP: Mover SL al 40%TP → asegura ganancia parcial
```

**Implementación:**
En `_monitor_posiciones()` cada 30 segundos:
1. Calcular % del recorrido: `(precio_actual - entry) / (tp - entry)`
2. Si ≥ 50% y SL actual < entry → `ModifyTrade` SL = entry
3. Si ≥ 80% y SL actual < 40%TP → `ModifyTrade` SL = entry + 40%×dist

**Impacto:** Convierte "casi-TP que revierten" en breakeven o ganancia parcial.

---

### B2 — Sentimiento real de OANDA
**Archivo:** `agents/signal_agent/signal_agent.py` línea 49
**Sesiones estimadas:** 0.5

**El problema:**
`OandaSentiment(oanda_api=None, modo_backtest=True)` — el ajuste de
sentimiento usa datos simulados. OANDA publica posicionamiento real de
sus clientes retail en tiempo real y no lo estamos usando.

**Fix (2 líneas de código):**
```python
# Antes:
self._sentiment = OandaSentiment(oanda_api=None, modo_backtest=True)

# Después (en main.py, pasar cliente OANDA al SignalAgent):
self._sentiment = OandaSentiment(oanda_api=oanda_client, modo_backtest=False)
```

**Lógica del sentimiento:**
- Si 70%+ traders retail están LONG → señal SHORT más probable (mercado retail
  suele estar equivocado en extremos)
- Si 70%+ están SHORT → señal LONG más probable
- Ajusta la confianza de DeepSeek en ±0.05 a ±0.15

---

## BLOQUE C — IMPACTO MEDIO EN GESTIÓN DE RIESGO
> Mejoran la consistencia del riesgo y protegen capital en eventos extremos.
> **Impacto: protección, no aumento directo de win rate**

---

### C1 — Calendario económico dinámico (BCE, BOE, BOJ)
**Archivo:** `utils/economic_calendar.py`
**Sesiones estimadas:** 1

**El problema:**
El calendario tiene fechas hardcodeadas solo para USD. Si el bot opera
GBP_USD el día de decisión del BOE, un spike de 80 pips ignora el SL
de 15 pips. Actualmente no hay protección para EUR, GBP, JPY, CAD, CHF.

**Fix:**
Integrar ForexFactory RSS (gratuito) al arrancar el bot:
- Descarga eventos de alto impacto de las próximas 2 semanas
- Cachea en `data/calibration/calendar_cache.json`
- Refresh automático cada 24h
- Cubre: USD, EUR, GBP, JPY, CAD, CHF, AUD

**Eventos críticos a cubrir por moneda:**
- EUR: BCE, IPC Eurozona, PIB
- GBP: BOE, IPC UK, PIB UK
- JPY: BOJ, IPC Japón, Balanza comercial
- CAD: BOC, Empleo Canada
- CHF: BNS, IPC Suiza

---

### C2 — Sizing dinámico por volatilidad ATR
**Archivo:** `risk_execution_agent.py` → `procesar_senal()` línea 160-166
**Sesiones estimadas:** 0.5

**El problema:**
Todos los pares usan el mismo riesgo_pct=1.5%. USD_JPY puede tener ATR
de 50 pips y EUR_USD de 8 pips. Con SL basado en ATR, el riesgo real
en USD es inconsistente.

**Fix:**
```python
atr_ratio = atr_actual / atr_promedio_20_periodos
if atr_ratio > 1.5:    # volatilidad alta
    risk_usd *= 0.75
elif atr_ratio < 0.7:  # volatilidad baja
    risk_usd *= 1.10
```

---

### C3 — Fix capital inicial en RiskExecutionAgent
**Archivo:** `risk_execution_agent.py` línea 57
**Sesiones estimadas:** 0.25

`self._capital = 200.0` hardcodeado. Si el bot se reinicia con $350 de
capital, sigue calculando riesgo sobre $200.

**Fix:**
```python
try:
    state = json.loads(Path("data/capital_state.json").read_text())
    self._capital = float(state.get("capital", 200.0))
except Exception:
    self._capital = 200.0
```

---

## BLOQUE D — MANTENIMIENTO Y HERRAMIENTAS
> No afectan el trading. Mejoran operabilidad y visibilidad del sistema.

---

### D1 — Dashboard web con Emergent
Construir interfaz visual en emergent.sh para monitorear el bot:
- Capital en tiempo real y curva de equity
- Historial de trades con P&L por par
- Estado del circuit breaker
- Botones start/stop del servicio
**Requiere:** Primero exponer API REST en el bot (5 endpoints básicos)

### D2 — Fix logging duplicado
Eliminar `StreamHandler` de `setup_logging()` en `main.py`.
systemd ya captura stdout → cada línea aparece 2 veces en el log.

### D3 — Limpieza de archivos del proyecto
Eliminar: `fix_*.py`, `ver_*.py`, `patch_*.py`, `diag*.py`, archivos `.bak_*`
Resultado: repositorio limpio con solo código de producción.

---

## HOJA DE RUTA — ORDEN DE EJECUCIÓN

```
SEMANA 1  ─── BLOQUE A parcial ────────────────────────────────────────────
  · A1: MarketAgent descarga H1 + H4
  · A1: Prefiltro técnico sobre H1 (no M1)
  · C3: Fix capital inicial (2 líneas, costo cero)
  · D2: Fix logging duplicado (1 línea)
  · Backtest 52 semanas → verificar WR ≥ 50%
  · Deploy al servidor

SEMANA 2  ─── BLOQUE A continuación ────────────────────────────────────────
  · A1: Filtro tendencia H4 + prompt DeepSeek enriquecido
  · A2: Reparar RSI_Divergence con divergencia real
  · B2: Sentimiento real OANDA (2 líneas)
  · Backtest → verificar WR ≥ 53%
  · Deploy al servidor

SEMANA 3  ─── BLOQUE A completo + BLOQUE B ─────────────────────────────────
  · A3: EMA_Crossover para mercados tendenciales
  · B1: Trailing stop dinámico
  · Backtest extenso → EMA_Crossover WR ≥ 48%
  · Deploy al servidor

SEMANA 4  ─── BLOQUE C + BLOQUE D ──────────────────────────────────────────
  · C1: Calendario dinámico ForexFactory
  · C2: Sizing dinámico por volatilidad
  · D1: Dashboard web con Emergent
  · D3: Limpieza de archivos
  · Deploy final
```

---

## MÉTRICAS DE ÉXITO

| Métrica           | v11 actual  | Tras Semana 1 | Tras Semana 2 | Objetivo v12 |
|-------------------|-------------|---------------|---------------|--------------|
| Win Rate          | ~45%        | ~52%          | ~57%          | 62-68%       |
| Profit Factor     | ~1.2        | ~1.35         | ~1.45         | ≥ 1.6        |
| Max Drawdown      | 24.3%       | ~21%          | ~19%          | < 16%        |
| Trades/semana     | 3-5         | 3-5           | 6-8           | 8-12         |
| Estrategias vivas | 1           | 1             | 2             | 3            |

---

## REGLAS DE ORO

1. Nunca se modifica el servidor directamente — siempre via `deploy_vps.ps1`
2. Antes de cada semana: backup automático con timestamp
3. Ningún cambio llega a producción sin pasar backtest de 52 semanas
4. El bot sigue corriendo en paper trading durante todo el desarrollo
5. Si un backtest no cumple el criterio mínimo, se ajusta antes de avanzar
