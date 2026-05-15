"""
main.py — Trading Bot v11
Punto de entrada limpio del sistema.

4 agentes en paralelo:
  1. MarketAgent        -> datos OANDA + indicadores (M1 + M15 + H4)
  2. SignalAgent        -> señales con DeepSeek + plugins de estrategias
  3. RiskExecutionAgent -> SL/TP + ejecución OANDA
  4. AuditAgent         -> Telegram + calibración fin de semana

Extras v11.1:
  - ParamsWatcher: detecta cambios en strategy_params.json y hace hot-reload
    sin necesidad de reiniciar. Aplica a estrategias activas, pares, y
    parámetros de riesgo. Cambios en pares_activos requieren restart manual.

Uso:
  python main.py          (paper trading)
  python main.py --live   (live trading)
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging(nivel=logging.INFO):
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level   = nivel,
        format  = fmt,
        handlers= [
            logging.FileHandler("logs/trading_bot.log", encoding="utf-8"),
        ],
    )

Path("logs").mkdir(exist_ok=True)
setup_logging()
logger = logging.getLogger("main")


# ── Validación temprana de configuración ──────────────────────────────────────
# Importar settings aquí para detectar errores de configuración antes de
# instanciar cualquier agente.
try:
    from config.settings import cargar_params, OANDA_ENV, PARAMS_FILE
except (FileNotFoundError, ValueError) as exc:
    logging.getLogger("main").critical(str(exc))
    sys.exit(1)

# ── Importar agentes ──────────────────────────────────────────────────────────
from agents.market_agent.market_agent                  import MarketAgent
from agents.signal_agent.signal_agent                  import SignalAgent
from agents.risk_execution_agent.risk_execution_agent  import RiskExecutionAgent
from agents.audit_agent.audit_agent                    import AuditAgent


# ── ParamsWatcher — hot-reload de parámetros ──────────────────────────────────

class ParamsWatcher:
    """
    Monitorea strategy_params.json y recarga parámetros sin reinicio.

    Comprueba la fecha de modificación del archivo cada CHECK_INTERVAL segundos.
    Si detecta un cambio, llama cargar_params() y notifica a todos los agentes.

    Qué se recarga automáticamente:
      ✅ estrategias_activas / pausadas
      ✅ min_confidence, min_win_rate, rr_ratio
      ✅ cooldown_minutes, max_posiciones, riesgo_pct
      ✅ sesiones_activas
      ⚠️  pares_activos — detectado y advertido, requiere restart para aplicarse
      ⚠️  signal_timeframe — detectado y advertido, requiere restart para aplicarse
    """
    CHECK_INTERVAL = 30  # segundos entre comprobaciones

    def __init__(self, agentes: list):
        self._agentes    = agentes   # lista de agentes con método reload_params()
        self._last_mtime = self._mtime()
        self._running    = False

    def _mtime(self) -> float:
        try:
            return os.path.getmtime(PARAMS_FILE)
        except OSError:
            return 0.0

    async def run(self):
        self._running = True
        logger.info(
            f"ParamsWatcher: monitoreando {PARAMS_FILE.name} "
            f"cada {self.CHECK_INTERVAL}s"
        )
        while self._running:
            await asyncio.sleep(self.CHECK_INTERVAL)
            try:
                mtime = self._mtime()
                if mtime != self._last_mtime:
                    self._last_mtime = mtime
                    logger.info("ParamsWatcher: cambio detectado — recargando parámetros...")
                    new_params = cargar_params()
                    for agente in self._agentes:
                        if hasattr(agente, "reload_params"):
                            agente.reload_params(new_params)
                    logger.info(
                        f"ParamsWatcher: parámetros aplicados | "
                        f"estrategias activas: {new_params.get('estrategias_activas', [])}"
                    )
            except (FileNotFoundError, ValueError) as exc:
                logger.error(f"ParamsWatcher: error al recargar — {exc}")
            except Exception as exc:
                logger.debug(f"ParamsWatcher: error inesperado — {exc}")

    def stop(self):
        self._running = False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(modo: str = "paper"):
    logger.info("=" * 60)
    logger.info("   TRADING BOT v11.1 — DeepSeek + OANDA Forex")
    logger.info(f"   Modo: {modo.upper()} | Entorno: {OANDA_ENV}")
    logger.info(
        f"   Inicio: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    logger.info("=" * 60)

    # Cargar parámetros calibrados
    params = cargar_params()
    logger.info(
        f"Parametros: SL={params['sl_atr_mult']}xATR | "
        f"RR={params['rr_ratio']} | "
        f"WR_min={params['min_win_rate']:.0%} | "
        f"TF={params.get('signal_timeframe', 'M15')} | "
        f"Estrategias: {params['estrategias_activas']} | "
        f"Pares: {len(params['pares_activos'])}"
    )

    # ── Instanciar agentes ────────────────────────────────────────────────────
    market = MarketAgent(params=params)
    signal = SignalAgent(market_agent=market, params=params)
    audit  = AuditAgent(params=params)
    risk   = RiskExecutionAgent(
        signal_agent = signal,
        audit_agent  = audit,
        params       = params,
    )
    audit._risk = risk

    # ParamsWatcher — propaga hot-reload a todos los agentes
    watcher = ParamsWatcher(agentes=[market, signal, risk, audit])

    # ── Cargar capital desde estado persistido ────────────────────────────────
    state_file  = Path("data/capital_state.json")
    capital_ini = 200.0
    if state_file.exists():
        try:
            capital_ini = json.loads(
                state_file.read_text(encoding="utf-8")
            ).get("capital", 200.0)
            logger.info(f"Capital cargado desde estado: ${capital_ini:.2f}")
        except Exception as e:
            logger.warning(f"No se pudo leer capital_state.json: {e} — usando $200.00")
    else:
        logger.info("capital_state.json no existe — usando capital base $200.00")

    # ── Notificar arranque ────────────────────────────────────────────────────
    await audit.notificar_arranque(capital=capital_ini, modo=modo)

    # ── Log circuit breaker al arranque ──────────────────────────────────────
    try:
        snap = risk.snapshot()
        cb_activo = snap.get("cb_activo", False)
        if cb_activo:
            cb_hasta = snap.get("cb_hasta", "desconocido")
            logger.warning(f"CIRCUIT BREAKER ACTIVO — pausado hasta {cb_hasta}")
        else:
            logger.info(
                f"Circuit breaker: OK | "
                f"DD rolling: {snap.get('cb_dd_rolling', 0)*100:.1f}% | "
                f"Pico 4sem: ${snap.get('cb_peak_rolling', capital_ini):.2f}"
            )
    except Exception:
        pass

    # ── Arrancar todos los agentes en paralelo ────────────────────────────────
    logger.info("Arrancando todos los agentes...")
    loop = asyncio.get_event_loop()

    def _shutdown():
        logger.info("Señal de interrupción recibida — deteniendo agentes...")
        watcher.stop()
        for ag in (market, signal, risk, audit):
            try:
                ag.stop()
            except Exception:
                pass

    try:
        loop.add_signal_handler(__import__("signal").SIGINT,  _shutdown)
        loop.add_signal_handler(__import__("signal").SIGTERM, _shutdown)
    except (NotImplementedError, RuntimeError):
        pass  # Windows no soporta add_signal_handler

    try:
        await asyncio.gather(
            market.run(),
            signal.run(),
            risk.run(),
            audit.run(),
            watcher.run(),       # hot-reload en background
            return_exceptions=True,
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Interrupción manual — apagando sistema...")
    finally:
        watcher.stop()
        for ag in (market, signal, risk, audit):
            try:
                ag.stop()
            except Exception:
                pass

        # Persistir capital final
        try:
            snap      = risk.snapshot()
            cap_final = snap.get("capital", capital_ini)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                json.dumps(
                    {
                        "capital":    round(cap_final, 4),
                        "actualizado": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info(f"Capital final ${cap_final:.2f} guardado en capital_state.json")
        except Exception as e:
            logger.warning(f"No se pudo guardar capital_state.json: {e}")

        logger.info("Sistema detenido correctamente")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading Bot v11.2")
    parser.add_argument("--live", action="store_true",
                        help="Activar modo live trading")
    args = parser.parse_args()
    modo = "live" if args.live else "paper"

    if modo == "live":
        resp = input("Confirmas LIVE TRADING con dinero real? (si/no): ")
        if resp.strip().lower() != "si":
            print("Cancelado.")
            sys.exit(0)

    try:
        asyncio.run(main(modo=modo))
    except (KeyboardInterrupt, SystemExit):
        pass   # SIGTERM/SIGINT — apagado limpio solicitado por systemd o usuario
    except Exception as e:
        import logging
        logging.getLogger(__name__).critical(f"Error fatal en main: {e}", exc_info=True)
        sys.exit(1)

    sys.exit(0)   # Siempre salir con 0 tras shutdown limpio → systemd registra "Stopped" no "exit-code"
