# legacy/ — código NO usado por el bot en vivo

**No modificar estos archivos esperando cambios en producción: el bot en vivo NO los usa.**

| Archivo | Qué es | Estado |
|---|---|---|
| `strategies/` | Implementación antigua/duplicada de las 6 estrategias | Huérfana desde v11: solo la usaba `app.py` |
| `app.py` | "Backtest Lab" web (puerto 5050) | Legacy: systemd no lo ejecuta |

**La implementación viva de las estrategias está en `agents/signal_agent/strategies.py`**
(es la que usa `SignalAgent` en `main.py`, el servicio `trading_bot`).

Estos archivos se conservan solo como referencia histórica. Si algún día se confirma
que nada los necesita, se pueden borrar sin efecto en el bot.
