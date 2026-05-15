# Decisiones Técnicas

Log de decisiones de diseño importantes con su justificación.

---

## 2026-05-13 — SIGTERM: cancelar asyncio.all_tasks()

**Problema**: El bot tardaba 30 segundos en apagarse (SIGKILL) porque `ag.stop()` solo ponía `_running=False` pero los agentes seguían dormidos en `await asyncio.sleep()`.

**Primera solución intentada**: cancelar solo `_tasks` (las 4 tasks principales). No funcionó porque cada agente crea sus propias tasks internas (`_monitor_posiciones`, etc.).

**Solución final**: en `_shutdown_completo()`, cancelar `asyncio.all_tasks()` excluyendo la task actual. Resultado: shutdown limpio en 1 segundo.

**Lección**: cuando un agente asyncio tiene tasks internas, `stop()` no es suficiente — hay que cancelar el árbol completo de tasks.

---

## 2026-05-13 — systemd: StartLimitIntervalSec en [Unit] no [Service]

**Problema**: `StartLimitIntervalSec=300` y `StartLimitBurst=5` estaban en `[Service]`. systemd los ignora silenciosamente en esa sección.

**Solución**: moverlos a `[Unit]`. También se redujo `TimeoutStopSec` de 30s a 15s (con el fix de SIGTERM, el bot para en 1s de todas formas).

---

## 2026-05-13 — CP12: comparar capital vs inicial, no vs OANDA

**Problema**: CP12 comparaba el capital interno del bot ($200) contra el balance de la cuenta demo de OANDA ($99,997). Daba 99.8% de discrepancia siempre.

**Decisión**: el bot gestiona $200 de capital asignado. OANDA tiene más fondos en la cuenta demo. La métrica correcta es comparar vs el capital inicial configurado ($200), no vs el balance total de OANDA.

---

## 2026-05-11 — Plugin system para estrategias

**Decisión**: cada estrategia es un archivo independiente en `strategies/` que hereda de `BaseStrategy`. El sistema las descubre automáticamente.

**Justificación**: agregar una estrategia nueva no requiere tocar ningún archivo existente — solo crear el archivo y listarlo en `strategy_params.json`. Elimina el riesgo de romper código existente al agregar funcionalidad.

---

## 2026-05-11 — DeepSeek: chat para señal, reasoner para riesgo

**Decisión**: usar `deepseek-chat` (rápido) para confirmar señales y `deepseek-reasoner` (lento, más preciso) para calcular SL/TP.

**Justificación**: las señales necesitan velocidad (el mercado no espera). El sizing de riesgo es donde un error cuesta dinero real — vale la pena el tiempo extra del reasoner.

---

## 2026-05-10 — strategy_params.json como single source of truth

**Decisión**: todos los parámetros operacionales viven en `data/calibration/strategy_params.json`. `settings.py` solo lo lee y valida.

**Justificación**: permite recalibración semanal sin tocar código. El bot detecta cambios en el JSON y hace hot-reload sin reiniciarse.

---

## 2026-05-10 — Calibración: rolling window en local, no en VPS

**Decisión**: el backtest de recalibración corre en la máquina local del usuario los fines de semana, no en el VPS.

**Justificación**: el VPS tiene 1vCPU/1GB — un backtest de 3 años bloquearía o tiraría el bot. El rolling window sobre datos históricos de OANDA da muestra estadística suficiente (miles de velas) que los datos propios del bot nunca alcanzarían.

---

## Convención de patches en VPS

**Lo que NO funciona**:
- Heredocs con caracteres especiales (em-dash, flechas, tildes)
- `content.replace(old, new)` con bloques multilínea (un espacio diferente rompe el match)
- Editar local y asumir que el VPS lo ve igual (desync del sandbox)

**Lo que SÍ funciona**:
- `sed -i 's/patron/reemplazo/'` para líneas simples sin caracteres especiales
- Patch Python con `splitlines()` + índice de línea
- Reescribir la función completa con `tee`
- Verificar siempre con `python3 -m py_compile archivo.py`
