# 🔍 Code Review Report

**Generado:** 2026-05-11 02:37  
**Archivos analizados:** 26  
**Líneas totales:** 10,342  
**Funciones totales:** 291  
**Salud promedio:** 86.1/100 (Bueno)

---

## 📊 Resumen por severidad

- 🔴 **Crítico**: 5 hallazgo(s)
- 🟠 **Alto**: 23 hallazgo(s)
- 🟡 **Medio**: 15 hallazgo(s)
- 🔵 **Bajo**: 2 hallazgo(s)

---

## 🚨 Archivos que necesitan atención

### `backtest_harness.py` — Salud: 67/100 (Regular)
- Líneas: 1518 | Funciones: 58 | Complejidad: 162
- Docstrings: 15.5% | Logging: 112 | TODOs: 1
- **Problemas:**
  - 🟠 `Alto` — Función '_aplicar_patches' tiene 85 líneas (>80)
  - 🟠 `Alto` — Función '_instanciar' tiene 115 líneas (>80)
  - 🟠 `Alto` — Función 'correr' tiene 288 líneas (>80)
  - 🟠 `Alto` — Función '_guardar_calibracion_rolling' tiene 188 líneas (>80)
  - 🟡 `Medio` — Función '_guardar' tiene 70 líneas (>60)
  - 🟡 `Medio` — Cobertura de docstrings baja: 15.5%
  - 🔵 `Bajo` — 1 comentario(s) TODO/FIXME pendiente(s)

### `scripts/setup_datos.py` — Salud: 72/100 (Regular)
- Líneas: 339 | Funciones: 10 | Complejidad: 64
- Docstrings: 0.0% | Logging: 0 | TODOs: 0
- **Problemas:**
  - 🔴 `Crítico` — bare except: en línea 124 (silencia todos los errores)
  - 🟠 `Alto` — Función 'backtest_estrategia' tiene 89 líneas (>80)
  - 🟡 `Medio` — Función 'calibrar' tiene 66 líneas (>60)
  - 🟡 `Medio` — Cobertura de docstrings baja: 0.0%

### `monitor_v2.py` — Salud: 73/100 (Regular)
- Líneas: 335 | Funciones: 11 | Complejidad: 64
- Docstrings: 0.0% | Logging: 0 | TODOs: 1
- **Problemas:**
  - 🔴 `Crítico` — bare except: en línea 70 (silencia todos los errores)
  - 🟠 `Alto` — Función 'dashboard' tiene 104 líneas (>80)
  - 🟡 `Medio` — Cobertura de docstrings baja: 0.0%
  - 🔵 `Bajo` — 1 comentario(s) TODO/FIXME pendiente(s)

### `agents/signal_agent/signal_agent.py` — Salud: 74/100 (Regular)
- Líneas: 425 | Funciones: 12 | Complejidad: 53
- Docstrings: 16.7% | Logging: 0 | TODOs: 0
- **Problemas:**
  - 🟠 `Alto` — Función '_prefiltro_tecnico' tiene 122 líneas (>80)
  - 🟠 `Alto` — Función '_consultar_deepseek' tiene 89 líneas (>80)
  - 🟠 `Alto` — Archivo de agente sin llamadas a logging (ciego en producción)
  - 🟡 `Medio` — Función 'evaluar' tiene 63 líneas (>60)
  - 🟡 `Medio` — Cobertura de docstrings baja: 16.7%

### `agents/risk_execution_agent/risk_execution_agent.py` — Salud: 79/100 (Regular)
- Líneas: 607 | Funciones: 19 | Complejidad: 52
- Docstrings: 73.7% | Logging: 0 | TODOs: 0
- **Problemas:**
  - 🟠 `Alto` — Función '_calcular_sl_tp' tiene 83 líneas (>80)
  - 🟠 `Alto` — Función '_validar_circuit_breaker' tiene 81 líneas (>80)
  - 🟠 `Alto` — Archivo de agente sin llamadas a logging (ciego en producción)
  - 🟡 `Medio` — Función 'procesar_senal' tiene 61 líneas (>60)

---

## 📋 Todos los hallazgos

### 🔴 Crítico (5)

- **`comparar_resultados.py`** — SyntaxError: f-string expression part cannot include a backslash (comparar_resultados.py, line 109)
- **`config/settings.py`** — SyntaxError: invalid non-printable character U+FEFF (settings.py, line 1)
- **`monitor_v2.py`** — bare except: en línea 70 (silencia todos los errores)
- **`scripts/setup_datos.py`** — bare except: en línea 124 (silencia todos los errores)
- **`sim_semana.py`** — SyntaxError: invalid non-printable character U+FEFF (sim_semana.py, line 1)

### 🟠 Alto (23)

- **`agents/audit_agent/audit_agent.py`** — Archivo de agente sin llamadas a logging (ciego en producción)
- **`agents/market_agent/market_agent.py`** — Archivo de agente sin llamadas a logging (ciego en producción)
- **`agents/risk_execution_agent/risk_execution_agent.py`** — Función '_calcular_sl_tp' tiene 83 líneas (>80)
- **`agents/risk_execution_agent/risk_execution_agent.py`** — Función '_validar_circuit_breaker' tiene 81 líneas (>80)
- **`agents/risk_execution_agent/risk_execution_agent.py`** — Archivo de agente sin llamadas a logging (ciego en producción)
- **`agents/signal_agent/signal_agent.py`** — Función '_prefiltro_tecnico' tiene 122 líneas (>80)
- **`agents/signal_agent/signal_agent.py`** — Función '_consultar_deepseek' tiene 89 líneas (>80)
- **`agents/signal_agent/signal_agent.py`** — Archivo de agente sin llamadas a logging (ciego en producción)
- **`backtest_harness.py`** — Función '_aplicar_patches' tiene 85 líneas (>80)
- **`backtest_harness.py`** — Función '_instanciar' tiene 115 líneas (>80)
- **`backtest_harness.py`** — Función 'correr' tiene 288 líneas (>80)
- **`backtest_harness.py`** — Función '_guardar_calibracion_rolling' tiene 188 líneas (>80)
- **`backtest_planA_1year.py`** — Función 'run' tiene 243 líneas (>80)
- **`fast_backtest_1year.py`** — Función 'run' tiene 255 líneas (>80)
- **`fast_backtest_26w.py`** — Función 'run' tiene 255 líneas (>80)
- **`fast_backtest_3years.py`** — Función 'run' tiene 310 líneas (>80)
- **`main.py`** — Función 'main' tiene 117 líneas (>80)
- **`main.py`** — Archivo de agente sin llamadas a logging (ciego en producción)
- **`monitor_v2.py`** — Función 'dashboard' tiene 104 líneas (>80)
- **`scripts/setup_datos.py`** — Función 'backtest_estrategia' tiene 89 líneas (>80)
- **`sim_30dias_real.py`** — Función 'calibrar' tiene 118 líneas (>80)
- **`sim_30dias_real.py`** — Función 'correr' tiene 109 líneas (>80)
- **`utils/economic_calendar.py`** — Función '_cargar_eventos' tiene 98 líneas (>80)

### 🟡 Medio (15)

- **`agents/audit_agent/audit_agent.py`** — Función '_consultar_deepseek_calibracion' tiene 71 líneas (>60)
- **`agents/risk_execution_agent/risk_execution_agent.py`** — Función 'procesar_senal' tiene 61 líneas (>60)
- **`agents/signal_agent/signal_agent.py`** — Función 'evaluar' tiene 63 líneas (>60)
- **`agents/signal_agent/signal_agent.py`** — Cobertura de docstrings baja: 16.7%
- **`backtest_harness.py`** — Función '_guardar' tiene 70 líneas (>60)
- **`backtest_harness.py`** — Cobertura de docstrings baja: 15.5%
- **`backtest_planA_1year.py`** — Cobertura de docstrings baja: 0.0%
- **`fast_backtest_1year.py`** — Cobertura de docstrings baja: 11.1%
- **`fast_backtest_26w.py`** — Cobertura de docstrings baja: 11.1%
- **`fast_backtest_3years.py`** — Cobertura de docstrings baja: 10.0%
- **`monitor.py`** — Cobertura de docstrings baja: 0.0%
- **`monitor_v2.py`** — Cobertura de docstrings baja: 0.0%
- **`scripts/recalibrar.py`** — Función 'calibrar_con_deepseek' tiene 75 líneas (>60)
- **`scripts/setup_datos.py`** — Función 'calibrar' tiene 66 líneas (>60)
- **`scripts/setup_datos.py`** — Cobertura de docstrings baja: 0.0%

### 🔵 Bajo (2)

- **`backtest_harness.py`** — 1 comentario(s) TODO/FIXME pendiente(s)
- **`monitor_v2.py`** — 1 comentario(s) TODO/FIXME pendiente(s)

---

## 📁 Detalle por archivo

| Archivo | Salud | Líneas | Funciones | Complejidad | Logging | TODOs |
|---------|-------|--------|-----------|-------------|---------|-------|
| `backtest_harness.py` | 67/100 | 1518 | 58 | 162 | 112 | 1 |
| `scripts/setup_datos.py` | 72/100 | 339 | 10 | 64 | 0 | 0 |
| `monitor_v2.py` | 73/100 | 335 | 11 | 64 | 0 | 1 |
| `agents/signal_agent/signal_agent.py` | 74/100 | 425 | 12 | 53 | 0 | 0 |
| `agents/risk_execution_agent/risk_execution_agent.py` | 79/100 | 607 | 19 | 52 | 0 | 0 |
| `main.py` | 80/100 | 180 | 3 | 14 | 0 | 0 |
| `sim_30dias_real.py` | 84/100 | 964 | 27 | 111 | 0 | 0 |
| `backtest_planA_1year.py` | 85/100 | 501 | 15 | 56 | 0 | 0 |
| `comparar_resultados.py` | 85/100 | 144 | 0 | 0 | 0 | 0 |
| `config/settings.py` | 85/100 | 237 | 0 | 0 | 0 | 0 |
| `sim_semana.py` | 85/100 | 514 | 0 | 0 | 0 | 0 |
| `fast_backtest_1year.py` | 86/100 | 457 | 9 | 42 | 0 | 0 |
| `fast_backtest_26w.py` | 86/100 | 457 | 9 | 42 | 0 | 0 |
| `fast_backtest_3years.py` | 86/100 | 573 | 10 | 50 | 0 | 0 |
| `agents/audit_agent/audit_agent.py` | 88/100 | 790 | 30 | 45 | 0 | 0 |
| `agents/market_agent/market_agent.py` | 88/100 | 437 | 23 | 45 | 0 | 0 |
| `analisis_backtest.py` | 90/100 | 282 | 3 | 34 | 0 | 0 |
| `monitor.py` | 90/100 | 274 | 20 | 34 | 0 | 0 |
| `run_backtest_13s.py` | 90/100 | 50 | 0 | 2 | 0 | 0 |
| `run_backtest_anual.py` | 90/100 | 135 | 0 | 7 | 0 | 0 |
| `scripts/recalibrar.py` | 90/100 | 184 | 3 | 11 | 0 | 0 |
| `utils/economic_calendar.py` | 92/100 | 206 | 7 | 8 | 0 | 0 |
| `analisis_pares.py` | 95/100 | 349 | 8 | 51 | 0 | 0 |
| `utils/oanda_sentiment.py` | 98/100 | 145 | 5 | 8 | 0 | 0 |
| `utils/pattern_utils.py` | 100/100 | 28 | 2 | 2 | 0 | 0 |
| `utils/regime_detector.py` | 100/100 | 211 | 7 | 18 | 0 | 0 |

---

## 💡 Quick Wins

Las mejoras más fáciles con mayor impacto:

1. **Eliminar bare `except:`** en 2 archivo(s) — cada uno resta 10 puntos de salud
3. **Resolver 2 TODOs/FIXMEs**
4. **Agregar logging** en 5 agente(s) sin cobertura