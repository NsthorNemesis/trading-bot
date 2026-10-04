"""
agents/audit_agent/audit_agent.py  — v14
════════════════════════════════════════════════════════════════
AUDIT + CALIBRACIÓN + TELEGRAM — Responsabilidad: comunicación y mejora

TIEMPO REAL (Lun-Vie):
  - Notificaciones automáticas: arranque, sesión abierta/cerrada,
    orden abierta, cierre, estado periódico, errores críticos
  - Bot conversacional: comandos /estado /trades /posiciones /sesion etc.
  - Responde preguntas libres en español con contexto real del sistema

SESIONES ACTIVAS (London+Overlap = 07:00-17:00 UTC):
  - 07:00 UTC → notificación de inicio de sesión London
  - 17:00 UTC → notificación de cierre de sesión (fin del día)

FIN DE SEMANA (Sáb 00:00 - Dom 22:00 UTC):
  - Análisis completo con DeepSeek + QuantStats
  - Calibración automática si mejora > 5%
  - Reporte resumen Telegram
════════════════════════════════════════════════════════════════
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import schedule

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    PARAMS, PARAMS_FILE, TRADES_LOG, CALIB_DIR,
)

logger = logging.getLogger("audit_agent")


class AuditAgent:
    """
    Agente de auditoría, calibración y comunicación Telegram.
    Único punto de contacto con el usuario.
    """

    def __init__(self, risk_agent=None, params: dict = None):
        self._risk    = risk_agent
        self._params  = params or PARAMS
        self._running = False
        self._http    = httpx.AsyncClient(timeout=10)
        self._app     = None   # python-telegram-bot Application
        self._tg_stopped = False  # idempotencia del apagado de Telegram
        self._ultima_calibracion = None

        logger.info("AuditAgent iniciado")

    # ════════════════════════════════════════════════════════════════
    # TELEGRAM — BOT CONVERSACIONAL
    # ════════════════════════════════════════════════════════════════

    async def iniciar_telegram(self):
        """Inicia el bot de Telegram con todos los handlers."""
        if not TELEGRAM_TOKEN:
            logger.warning("AuditAgent: sin TELEGRAM_TOKEN — bot desactivado")
            return

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
            from telegram.ext import (
                Application, CommandHandler,
                MessageHandler, CallbackQueryHandler,
                filters,
            )

            app = Application.builder().token(TELEGRAM_TOKEN).build()

            # ── Autorización: solo el chat del dueño puede usar el bot ─────────
            # Sin esto, CUALQUIERA que encontrara el bot en Telegram podía
            # pausarlo (/pausar) o reiniciar el servicio (/reiniciar).
            try:
                _solo_dueno = filters.Chat(chat_id=int(TELEGRAM_CHAT_ID))
            except (TypeError, ValueError):
                logger.warning(
                    "AuditAgent: TELEGRAM_CHAT_ID inválido — "
                    "comandos de Telegram sin filtro de chat"
                )
                _solo_dueno = None

            # ── Comandos de consulta ──────────────────────────────────────────
            app.add_handler(CommandHandler("estado",     self._cmd_estado,     filters=_solo_dueno))
            app.add_handler(CommandHandler("trades",     self._cmd_trades,     filters=_solo_dueno))
            app.add_handler(CommandHandler("posiciones", self._cmd_posiciones, filters=_solo_dueno))
            app.add_handler(CommandHandler("semana",     self._cmd_semana,     filters=_solo_dueno))
            app.add_handler(CommandHandler("shadow",     self._cmd_shadow,     filters=_solo_dueno))
            app.add_handler(CommandHandler("params",     self._cmd_params,     filters=_solo_dueno))
            app.add_handler(CommandHandler("sesion",     self._cmd_sesion,     filters=_solo_dueno))
            app.add_handler(CommandHandler("pausar",     self._cmd_pausar,     filters=_solo_dueno))
            app.add_handler(CommandHandler("reanudar",   self._cmd_reanudar,   filters=_solo_dueno))
            app.add_handler(CommandHandler("grafica",    self._cmd_grafica,    filters=_solo_dueno))
            app.add_handler(CommandHandler("reload",     self._cmd_reload,     filters=_solo_dueno))
            app.add_handler(CommandHandler("reiniciar",  self._cmd_reiniciar,  filters=_solo_dueno))
            app.add_handler(CommandHandler("ayuda",      self._cmd_ayuda,      filters=_solo_dueno))

            # ── Botones inline ────────────────────────────────────────────────
            # NOTA: CallbackQueryHandler no acepta 'filters' en esta versión de
            # python-telegram-bot → la verificación de chat está dentro de _handle_boton.
            app.add_handler(CallbackQueryHandler(self._handle_boton))

            # ── Texto libre: responde con datos reales del sistema ────────────
            _filtro_texto = filters.TEXT & ~filters.COMMAND
            if _solo_dueno is not None:
                _filtro_texto = _filtro_texto & _solo_dueno
            app.add_handler(MessageHandler(
                _filtro_texto,
                self._handle_pregunta,
            ))

            self._app = app
            logger.info("AuditAgent: bot Telegram iniciado")

            # Iniciar polling en background
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            # Registrar comandos en Telegram (aparecen en el menú /)
            from telegram import BotCommand
            # Orden agrupado: primero consultas, luego control, luego app
            await app.bot.set_my_commands([
                # 📊 Consultas
                BotCommand("estado",      "📊 Capital, PnL y posiciones"),
                BotCommand("sesion",      "🕐 Sesión activa y horario"),
                BotCommand("trades",      "📋 Últimos 10 trades"),
                BotCommand("posiciones",  "📌 Posiciones abiertas en OANDA"),
                BotCommand("semana",      "📈 Reporte semanal completo"),
                BotCommand("params",      "⚙️ Parámetros activos"),
                # 🎮 Control
                BotCommand("pausar",      "⏸ Pausar señales nuevas"),
                BotCommand("reanudar",    "▶️ Reanudar operación"),
                BotCommand("reiniciar",   "🔄 Reiniciar el bot"),
                # 📱 Mini App
                BotCommand("grafica",     "📱 Abrir Mini App"),
                BotCommand("reload",      "🔃 Abrir Mini App (recarga forzada)"),
                # ℹ️ Info
                BotCommand("ayuda",       "ℹ️ Lista de comandos"),
            ])

        except Exception as e:
            logger.error(f"AuditAgent: error iniciando Telegram: {e}")

    # ── COMANDOS ──────────────────────────────────────────────────────────────

    async def _cmd_estado(self, update, context):
        """/ estado — estado completo con botones interactivos."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        snap  = self._risk.snapshot() if self._risk else {}
        cap   = snap.get("capital", 0)
        pos   = snap.get("posiciones", 0)
        dd    = snap.get("dd_dia", 0)
        hora  = datetime.now(timezone.utc).strftime("%H:%M UTC")

        trades_sem = self._cargar_trades_semana()
        pnl_sem    = sum(t.get("pnl", 0) for t in trades_sem if "pnl" in t)
        wins_sem   = sum(1 for t in trades_sem if t.get("pnl", 0) > 0)
        wr_sem     = wins_sem / len(trades_sem) if trades_sem else 0

        keyboard = [
            [
                InlineKeyboardButton("📊 Trades", callback_data="trades"),
                InlineKeyboardButton("📌 Posiciones", callback_data="posiciones"),
            ],
            [
                InlineKeyboardButton("📋 Semana", callback_data="semana"),
                InlineKeyboardButton("⚙️ Parámetros", callback_data="params"),
            ],
        ]

        msg = (
            f"📊 <b>ESTADO DEL SISTEMA</b>\n"
            f"{'─'*30}\n"
            f"⏰ {hora}\n"
            f"💰 Capital:     <b>${cap:.2f}</b>\n"
            f"📈 PnL semana:  <b>${pnl_sem:+.4f}</b>\n"
            f"📌 Posiciones:  <b>{pos}</b> abiertas\n"
            f"📉 DD hoy:      <b>${dd:.4f}</b>\n"
            f"🔄 Trades sem:  <b>{len(trades_sem)}</b>\n"
            f"🎯 WR semana:   <b>{wr_sem:.1%}</b>"
        )
        await update.message.reply_text(
            msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_trades(self, update, context):
        """/ trades — últimos 10 trades."""
        trades = self._cargar_trades_recientes(n=10)
        if not trades:
            await update.message.reply_text("Sin trades registrados aún.")
            return

        msg = "📋 <b>ÚLTIMOS TRADES</b>\n" + "─"*28 + "\n"
        for t in trades[:10]:
            pnl   = t.get("pnl", 0)
            emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "⏳"
            par   = t.get("par", "?").replace("_", "/")
            dir_  = t.get("dir", "?").upper()
            strat = t.get("estrategia", "?")[:12]
            hora  = str(t.get("opened_at", ""))[:16]
            msg  += f"{emoji} {par} {dir_} | {strat} | ${pnl:+.4f}\n"
            msg  += f"   {hora}\n"

        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        msg += f"\n🎯 WR últimos {len(trades)}: <b>{wins/len(trades):.0%}</b>"
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_posiciones(self, update, context):
        """/ posiciones — posiciones abiertas en tiempo real desde OANDA."""
        try:
            import oandapyV20
            import oandapyV20.endpoints.trades as ot
            from config.settings import OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV

            client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)
            r      = ot.OpenTrades(OANDA_ACCOUNT)
            client.request(r)
            abiertos = r.response.get("trades", [])

            if not abiertos:
                await update.message.reply_text("Sin posiciones abiertas.")
                return

            msg = "📌 <b>POSICIONES ABIERTAS</b>\n" + "─"*28 + "\n"
            for t in abiertos:
                par   = t["instrument"].replace("_", "/")
                units = int(t["currentUnits"])
                entry = float(t["price"])
                pnl   = float(t["unrealizedPL"])
                dir_  = "LONG" if units > 0 else "SHORT"
                emoji = "📈" if pnl > 0 else "📉"
                msg  += (
                    f"{emoji} <b>{par} {dir_}</b>\n"
                    f"   {abs(units)} uds @ {entry:.5f}\n"
                    f"   PnL: <b>${pnl:+.4f}</b>\n"
                )
            await update.message.reply_text(msg, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"Error consultando OANDA: {e}")

    async def _cmd_semana(self, update, context):
        """/ semana — reporte semanal con QuantStats."""
        trades = self._cargar_trades_semana()
        if not trades:
            await update.message.reply_text("Sin trades esta semana.")
            return

        try:
            import quantstats as qs
            import pandas as pd

            cap_ini = 200.0
            returns = pd.Series(
                [t.get("pnl", 0) / cap_ini for t in trades if "pnl" in t],
                index=pd.to_datetime([t.get("opened_at", "") for t in trades if "pnl" in t])
            ).dropna()

            if len(returns) < 3:
                raise ValueError("insuficientes trades")

            sharpe = qs.stats.sharpe(returns)
            max_dd = qs.stats.max_drawdown(returns)
            wr     = qs.stats.win_rate(returns)
            pf     = qs.stats.profit_factor(returns)
            pnl_t  = sum(t.get("pnl", 0) for t in trades if "pnl" in t)

            msg = (
                f"📋 <b>REPORTE SEMANAL</b>\n"
                f"{'─'*28}\n"
                f"Trades:        {len(trades)}\n"
                f"Win Rate:      <b>{wr:.1%}</b>\n"
                f"Profit Factor: <b>{pf:.2f}</b>\n"
                f"Sharpe Ratio:  <b>{sharpe:.2f}</b>\n"
                f"Max Drawdown:  <b>{max_dd:.1%}</b>\n"
                f"PnL total:     <b>${pnl_t:+.4f}</b>"
            )
        except Exception:
            wins  = sum(1 for t in trades if t.get("pnl", 0) > 0)
            pnl_t = sum(t.get("pnl", 0) for t in trades)
            wr    = wins / len(trades) if trades else 0
            msg   = (
                f"📋 <b>SEMANA</b>\n"
                f"Trades: {len(trades)} | WR: {wr:.1%} | PnL: ${pnl_t:+.4f}"
            )

        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_shadow(self, update, context):
        """Fase2e /shadow — reporte de estrategias en pausa (señales sin operar)."""
        try:
            from pathlib import Path
            import json
            p = Path("logs/shadow_resultados.jsonl")
            if not p.exists() or not p.stat().st_size:
                await update.message.reply_text(
                    "🌑 Shadow vacío: las estrategias en pausa aún no tienen "
                    "señales resueltas. El resolver corre cada hora."
                )
                return
            por_est: dict = {}
            for l in p.read_text(encoding="utf-8").splitlines():
                if not l.strip():
                    continue
                r = json.loads(l)
                e = por_est.setdefault(r["estrategia"], {"n": 0, "w": 0, "l": 0, "R": 0.0})
                e["n"] += 1
                if r.get("resultado") == "win":
                    e["w"] += 1
                elif r.get("resultado") == "loss":
                    e["l"] += 1
                e["R"] += float(r.get("R", 0))
            lineas = ["🌑 <b>SHADOW — estrategias en pausa</b>", "─" * 28]
            for est, e in sorted(por_est.items(), key=lambda x: -x[1]["R"]):
                decid = e["w"] + e["l"]
                wr = e["w"] / decid if decid else 0
                lineas.append(f"<b>{est}</b>: {e['n']} señales · WR {wr:.0%} · R {e['R']:+.1f}")
            lineas.append("─" * 28)
            lineas.append("Señales sin operar. Candidatas con R+ sostenido → evaluar activación.")
            await update.message.reply_text("\n".join(lineas), parse_mode="HTML")
        except Exception as ex:
            logger.warning(f"/shadow falló: {ex}")
            await update.message.reply_text(f"Error leyendo shadow: {ex}")


    async def _cmd_params(self, update, context):
        """/ params — parámetros calibrados activos."""
        p   = self._params
        msg = (
            f"⚙️ <b>PARÁMETROS ACTIVOS</b>\n"
            f"{'─'*28}\n"
            f"SL:      {p['sl_atr_mult']}× ATR (mín {p['min_sl_pips']} pips)\n"
            f"RR:      {p['rr_ratio']}\n"
            f"Riesgo:  {p['riesgo_pct']:.1%}/op\n"
            f"WR mín:  {p['min_win_rate']:.0%}\n"
            f"Cooldown:{p['cooldown_minutes']} min\n"
            f"Max pos: {p['max_posiciones']}\n"
            f"Sesiones:{', '.join(p['sesiones_activas'])}\n\n"
            f"<b>Estrategias activas:</b>\n"
            + "\n".join(f"  ✅ {e}" for e in p["estrategias_activas"])
        )
        if p.get("estrategias_pausadas"):
            msg += "\n<b>Pausadas:</b>\n"
            msg += "\n".join(f"  ⏸ {e}" for e in p["estrategias_pausadas"])
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_sesion(self, update, context):
        """/ sesion — estado de la sesión actual y próxima ventana de trading."""
        hora_utc = datetime.now(timezone.utc)
        h        = hora_utc.hour
        sesiones = self._params.get("sesiones_activas", ["london", "overlap"])

        if 7 <= h < 13:
            sesion_actual = "🟢 LONDON (07:00-13:00 UTC)"
            activa        = "london" in sesiones
            proxima       = "Overlap abre en " + f"{13-h}h"
        elif 13 <= h < 17:
            sesion_actual = "🟢 OVERLAP (13:00-17:00 UTC)"
            activa        = "overlap" in sesiones
            proxima       = f"Sesión cierra en {17-h}h — sin trading hasta mañana 07:00 UTC"
        elif 17 <= h < 22:
            sesion_actual = "⚫ NEW YORK (17:00-22:00 UTC)"
            activa        = False
            proxima       = f"London abre en {31-h}h (mañana 07:00 UTC)"
        else:
            sesion_actual = "⚫ ASIA / FUERA DE SESIÓN"
            activa        = False
            minutos_hasta = (7 - h) % 24
            proxima       = f"London abre en ~{minutos_hasta}h (07:00 UTC)"

        estado_bot = "✅ OPERANDO" if activa else "⏸ EN ESPERA"
        snap       = self._risk.snapshot() if self._risk else {}
        pos        = snap.get("posiciones", 0)

        trades_hoy = self._cargar_trades_hoy()
        wins_hoy   = sum(1 for t in trades_hoy if t.get("pnl", 0) > 0)
        pnl_hoy    = sum(t.get("pnl", 0) for t in trades_hoy)
        wr_hoy     = wins_hoy / len(trades_hoy) if trades_hoy else 0

        msg = (
            f"🕐 <b>SESIÓN ACTUAL</b>\n"
            f"{'─'*30}\n"
            f"⏰ {hora_utc.strftime('%H:%M UTC')}\n"
            f"📍 {sesion_actual}\n"
            f"Bot:        <b>{estado_bot}</b>\n"
            f"📌 Pos:     {pos} abiertas\n"
            f"{'─'*30}\n"
            f"<b>HOY</b>\n"
            f"Trades:     {len(trades_hoy)} ({wins_hoy}W / {len(trades_hoy)-wins_hoy}L)\n"
            f"WR:         {wr_hoy:.1%}\n"
            f"PnL:        ${pnl_hoy:+.4f}\n"
            f"{'─'*30}\n"
            f"⏭ {proxima}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_pausar(self, update, context):
        """Fase2f /pausar — pausa REAL: escribe "pausado" en strategy_params.json;
        el ParamsWatcher lo propaga a SignalAgent y RiskExecutionAgent en ≤30s."""
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                params = json.load(f)
            params["pausado"] = True
            with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False, indent=2)
            self._params["pausado"] = True
            logger.info("[Telegram /pausar] sistema pausado")
            await update.message.reply_text(
                "⏸ <b>Sistema pausado.</b>\n"
                "No se generarán señales nuevas ni se abrirán trades.\n"
                "Las posiciones abiertas siguen monitoreadas.\n"
                "Usa /reanudar para continuar.",
                parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error al pausar: {exc}")

    async def _cmd_reanudar(self, update, context):
        """Fase2f /reanudar — quita la pausa (vía strategy_params.json)."""
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                params = json.load(f)
            params["pausado"] = False
            with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False, indent=2)
            self._params["pausado"] = False
            logger.info("[Telegram /reanudar] sistema reanudado")
            await update.message.reply_text("▶️ <b>Sistema reanudado.</b>", parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error al reanudar: {exc}")

    async def _cmd_setparam(self, update, context):
        """/setparam <campo> <valor> — cambia un parámetro numérico o de lista en tiempo real.

        Ejemplos:
          /setparam min_confidence 0.72
          /setparam riesgo_pct 0.005
          /setparam adx_min_operar 27
          /setparam cooldown_minutes 15
          /setparam max_posiciones 6

        El cambio se escribe en strategy_params.json y el ParamsWatcher lo propaga
        a todos los agentes en ≤30 segundos sin reiniciar el bot.
        """
        # Campos numéricos permitidos (whitelist de seguridad)
        CAMPOS_FLOAT = {
            "min_confidence", "riesgo_pct", "rr_ratio", "sl_atr_mult",
            "min_win_rate", "max_drawdown_dia", "circuit_breaker_pct",
        }
        CAMPOS_INT = {
            "adx_min_operar", "cooldown_minutes", "max_posiciones", "max_pos_par",
            "min_sl_pips", "max_sl_pips", "max_trade_hours", "max_consecutive_losses",
            "m1_entry_timeout_min", "min_pips_to_hold",
        }

        args = context.args if context.args else []
        if len(args) != 2:
            await update.message.reply_text(
                "⚠️ Uso: /setparam <campo> <valor>\n"
                "Ejemplo: /setparam min_confidence 0.72\n\n"
                f"Campos numéricos: {', '.join(sorted(CAMPOS_FLOAT | CAMPOS_INT))}"
            )
            return

        campo, valor_str = args[0], args[1]
        if campo not in CAMPOS_FLOAT and campo not in CAMPOS_INT:
            await update.message.reply_text(
                f"❌ Campo '{campo}' no permitido o no es numérico.\n"
                f"Campos disponibles: {', '.join(sorted(CAMPOS_FLOAT | CAMPOS_INT))}"
            )
            return

        try:
            valor = float(valor_str) if campo in CAMPOS_FLOAT else int(valor_str)
        except ValueError:
            await update.message.reply_text(f"❌ Valor '{valor_str}' no es un número válido.")
            return

        # Validaciones básicas
        if campo == "min_confidence" and not (0.5 <= valor <= 1.0):
            await update.message.reply_text("❌ min_confidence debe estar entre 0.50 y 1.00")
            return
        if campo == "riesgo_pct" and not (0 < valor <= 0.02):
            await update.message.reply_text("❌ riesgo_pct debe estar entre 0.001 y 0.02")
            return

        # Leer → modificar → escribir → informar
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                params = json.load(f)

            valor_anterior = params.get(campo, "N/A")
            params[campo] = valor

            with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False, indent=2)

            # Actualizar params local del AuditAgent también
            self._params[campo] = valor

            logger.info(f"[Telegram /setparam] {campo}: {valor_anterior} → {valor}")
            await update.message.reply_text(
                f"✅ <b>{campo}</b> actualizado\n"
                f"  Antes : {valor_anterior}\n"
                f"  Ahora : {valor}\n\n"
                f"⏱ ParamsWatcher aplicará el cambio a todos los agentes en ≤30 seg.",
                parse_mode="HTML"
            )
        except Exception as exc:
            logger.error(f"[Telegram /setparam] Error: {exc}")
            await update.message.reply_text(f"❌ Error al guardar: {exc}")

    async def _cmd_grafica(self, update, context):
        """/ grafica — abre la Mini App siempre con versión actualizada."""
        import os, time
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

        webapp_url = os.getenv("WEBAPP_URL", "https://nsthor.duckdns.org")
        # Timestamp para bypass de caché — igual que /reload
        url_fresh  = f"{webapp_url}?v={int(time.time())}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                text="📈 Abrir Mini App",
                web_app=WebAppInfo(url=url_fresh),
            )
        ]])

        await update.message.reply_text(
            "📊 <b>Trading Bot v11 — Monitor</b>\n"
            "Toca el botón para abrir la app en tiempo real.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _cmd_reload(self, update, context):
        """/reload — abre la Mini App forzando recarga (bypass caché de Telegram)."""
        import os, time
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

        webapp_url = os.getenv("WEBAPP_URL", "https://nsthor.duckdns.org")
        # Agregar timestamp para que Telegram trate la URL como nueva y no use caché
        url_fresh = f"{webapp_url}?v={int(time.time())}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                text="⟳ Abrir versión actualizada",
                web_app=WebAppInfo(url=url_fresh),
            )
        ]])

        await update.message.reply_text(
            "⟳ <b>Recarga forzada</b>\n"
            "Toca el botón — esta URL incluye un timestamp único\n"
            "que fuerza a Telegram a descargar la versión más reciente.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _cmd_ayuda(self, update, context):
        """/ ayuda — lista de comandos disponibles."""
        sep = "─" * 28
        msg = (
            f"🤖 <b>TRADING BOT v11 — COMANDOS</b>\n"
            f"{sep}\n"
            f"📊 <b>Consultas</b>\n"
            f"/estado     · Capital, PnL, posiciones\n"
            f"/sesion     · Sesión activa y horario\n"
            f"/trades     · Últimos 10 trades\n"
            f"/posiciones · Posiciones abiertas\n"
            f"/semana     · Reporte semanal\n"
            f"/shadow     · Estrategias en pausa\n"
            f"/params     · Parámetros activos\n"
            f"\n🎮 <b>Control</b>\n"
            f"/pausar     · Pausar señales nuevas\n"
            f"/reanudar   · Reanudar operación\n"
            f"/reiniciar  · Reiniciar el bot\n"
            f"\n📱 <b>Mini App</b>\n"
            f"/grafica    · Abrir app (versión fresca)\n"
            f"/reload     · Abrir app forzando recarga\n"
            f"\n/ayuda     · Esta lista\n"
            f"{sep}\n"
            f"💬 Escribe en español y el bot responde con datos reales.\n"
            f"Ej: <i>¿cuál es el capital?</i> · <i>¿hay posiciones abiertas?</i>\n\n"
            f"🕐 Horario activo: Lun-Vie 07:00-17:00 UTC"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _handle_boton(self, update, context):
        """Maneja los botones inline del teclado."""
        query = update.callback_query
        # Verificación de chat: CallbackQueryHandler no acepta 'filters' en esta
        # versión de python-telegram-bot, así que se valida aquí explícitamente.
        try:
            chat_id = query.message.chat_id if query.message else None
            if str(chat_id) != str(TELEGRAM_CHAT_ID):
                await query.answer("No autorizado.", show_alert=True)
                logger.warning(f"Botón inline no autorizado desde chat {chat_id}")
                return
        except Exception:
            pass
        await query.answer()
        data  = query.data

        # Reutilizar handlers de comandos
        class FakeUpdate:
            def __init__(self, q):
                self.message = q.message

        fake = FakeUpdate(query)
        if data == "trades":
            await self._cmd_trades(fake, context)
        elif data == "posiciones":
            await self._cmd_posiciones(fake, context)
        elif data == "semana":
            await self._cmd_semana(fake, context)
        elif data == "params":
            await self._cmd_params(fake, context)
        elif data == "confirm_restart":
            await self._ejecutar_reinicio(query)
        elif data == "cancel_restart":
            await query.edit_message_text("✅ Reinicio cancelado — bot sigue activo.")

    async def _handle_pregunta(self, update, context):
        """
        Responde preguntas en texto libre usando datos reales del sistema.
        Sin IA — detección por keywords y formateo de datos en vivo.
        """
        texto = (update.message.text or "").lower()
        snap  = self._risk.snapshot() if self._risk else {}

        # ── Capital / balance ─────────────────────────────────────────────────
        if any(k in texto for k in ["capital", "balance", "dinero", "cuenta", "saldo"]):
            cap = snap.get("capital", 0)
            dd  = snap.get("dd_dia", 0)
            await update.message.reply_text(
                f"💰 <b>Capital actual:</b> ${cap:.2f}\n"
                f"📉 DD del día: {dd:.1%}",
                parse_mode="HTML"
            )

        # ── Posiciones abiertas ───────────────────────────────────────────────
        elif any(k in texto for k in ["posicion", "abiert", "trade abiert", "en mercado"]):
            await self._cmd_posiciones(update, context)

        # ── Estado general ────────────────────────────────────────────────────
        elif any(k in texto for k in ["estado", "como va", "cómo va", "como está", "resumen", "reporte"]):
            await self._cmd_estado(update, context)

        # ── Trades / historial ────────────────────────────────────────────────
        elif any(k in texto for k in ["trade", "operacion", "operación", "historial", "ultimo", "último"]):
            await self._cmd_trades(update, context)

        # ── Semana / rendimiento ──────────────────────────────────────────────
        elif any(k in texto for k in ["semana", "rendimiento", "pnl", "ganancia", "perdida", "pérdida", "resultado"]):
            await self._cmd_semana(update, context)

        # ── Parámetros / configuración ────────────────────────────────────────
        elif any(k in texto for k in ["param", "config", "estrategia", "adx", "riesgo", "sl", "rr"]):
            await self._cmd_params(update, context)

        # ── Sesión ────────────────────────────────────────────────────────────
        elif any(k in texto for k in ["sesion", "sesión", "horario", "london", "activo"]):
            await self._cmd_sesion(update, context)

        # ── Default: ayuda ────────────────────────────────────────────────────
        else:
            await update.message.reply_text(
                "🤖 No entendí la pregunta. Prueba con:\n\n"
                "• <i>\"¿Cuál es el capital?\"</i>\n"
                "• <i>\"¿Hay posiciones abiertas?\"</i>\n"
                "• <i>\"¿Cómo va la semana?\"</i>\n"
                "• <i>\"¿Cuáles son los parámetros?\"</i>\n\n"
                "O usa los comandos: /estado /trades /semana /ayuda",
                parse_mode="HTML"
            )

    async def _cmd_reiniciar(self, update, context):
        """
        /reiniciar — reinicia el servicio trading_bot con confirmación por botones.
        Envía la confirmación ANTES de ejecutar el restart para que el mensaje llegue.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Sí, reiniciar", callback_data="confirm_restart"),
            InlineKeyboardButton("❌ Cancelar",       callback_data="cancel_restart"),
        ]])
        await update.message.reply_text(
            "⚠️ <b>¿Reiniciar el bot?</b>\n\n"
            "El sistema se detendrá ~5 segundos y volverá a arrancar.\n"
            "Las posiciones abiertas NO se cierran — OANDA las mantiene.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _ejecutar_reinicio(self, query):
        """Ejecuta el reinicio tras confirmación del botón inline."""
        import subprocess, asyncio
        # Defensa en profundidad: el filtro de chat ya bloquea a extraños,
        # pero el reinicio de un servicio systemd merece verificación explícita.
        try:
            chat_id = query.message.chat_id if query.message else None
            if str(chat_id) != str(TELEGRAM_CHAT_ID):
                await query.answer("No autorizado.", show_alert=True)
                logger.warning(f"Intento de reinicio no autorizado desde chat {chat_id}")
                return
        except Exception:
            pass
        await query.edit_message_text(
            "🔄 <b>Reiniciando bot...</b>\n"
            "Recibirás una notificación cuando vuelva a estar activo.",
            parse_mode="HTML"
        )
        # Pequeño delay para que el mensaje llegue antes de que el proceso muera
        await asyncio.sleep(1)
        try:
            subprocess.Popen(
                ["systemctl", "restart", "trading_bot"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error(f"Error reiniciando: {e}")
            await self._enviar(f"⚠️ Error al reiniciar: {e}")

    # ════════════════════════════════════════════════════════════════
    # NOTIFICACIONES AUTOMÁTICAS
    # ════════════════════════════════════════════════════════════════

    async def notificar_arranque(self, capital: float, modo: str):
        hora     = datetime.now(timezone.utc).strftime("%H:%M UTC")
        pares    = self._params.get("pares_activos", [])
        sesiones = self._params.get("sesiones_activas", [])
        timeout  = self._params.get("max_trade_hours", 12)
        adx_min  = self._params.get("adx_min_operar", 23)
        msg = (
            f"🟢 <b>SISTEMA ARRANCADO — v14</b>\n"
            f"{'─'*30}\n"
            f"⏰ {hora}\n"
            f"💰 Capital:     <b>${capital:.2f}</b>\n"
            f"📋 Modo:        <b>{modo.upper()}</b>\n"
            f"📊 Pares:       {', '.join(p.replace('_','/') for p in pares)}\n"
            f"🕐 Sesiones:    {', '.join(sesiones)} (07:00-17:00 UTC)\n"
            f"🧠 Señales:     Patrón puro (matemático, sin IA)\n"
            f"⚙️ ADX mín:     {adx_min} | Timeout: {timeout}h | RR: {self._params.get('rr_ratio',2.0)}"
        )
        await self._enviar(msg)

    async def notificar_orden_abierta(self, orden: dict):
        pip    = 0.01 if "JPY" in orden["par"] else 0.0001
        sl_pip = abs(orden["entry"] - orden["sl"]) / pip
        tp_pip = abs(orden["tp"] - orden["entry"]) / pip
        emoji  = "📈" if orden["dir"] == "long" else "📉"
        par    = orden["par"].replace("_", "/")
        hora   = datetime.now(timezone.utc).strftime("%H:%M UTC")
        conf   = orden.get("conf", 0)
        razon  = orden.get("razon", "")

        # Barra de confianza visual
        bloques   = int(conf * 10)
        conf_bar  = "█" * bloques + "░" * (10 - bloques)

        msg = (
            f"{emoji} <b>NUEVA ORDEN</b> — {par}\n"
            f"{'─'*30}\n"
            f"⏰ {hora}\n"
            f"Dir:        <b>{orden['dir'].upper()}</b>\n"
            f"Entry:      {orden['entry']:.5f}\n"
            f"SL:         {orden['sl']:.5f}  ({sl_pip:.1f} pips)\n"
            f"TP:         {orden['tp']:.5f}  ({tp_pip:.1f} pips)\n"
            f"Unidades:   {abs(orden['units'])}\n"
            f"Riesgo:     ${orden['risk_usd']:.2f}\n"
            f"Confianza:  {conf:.0%}  [{conf_bar}]\n"
            f"Estrategia: {orden['estrategia']}\n"
        )
        if razon:
            msg += f"Razón:      <i>{razon}</i>\n"
        msg += f"ID:         {orden['trade_id']}"
        await self._enviar(msg)

    async def notificar_orden_cerrada(self, trade_id: str, par: str,
                                       estrategia: str, pnl: float,
                                       capital: float):
        trades = self._cargar_trades_semana()
        wins   = sum(1 for t in trades if t.get("pnl", 0) > 0)
        total  = len(trades)
        wr     = wins / total if total > 0 else 0
        pnl_s  = sum(t.get("pnl", 0) for t in trades)

        emoji = "✅" if pnl > 0 else "❌"
        res   = "GANADORA" if pnl > 0 else "PERDEDORA"
        hora  = datetime.now(timezone.utc).strftime("%H:%M UTC")

        msg = (
            f"{emoji} <b>CIERRE</b> — {par.replace('_','/')} | {res}\n"
            f"{'─'*28}\n"
            f"⏰ {hora}\n"
            f"PnL:        <b>${pnl:+.4f}</b>\n"
            f"Capital:    <b>${capital:.2f}</b>\n"
            f"Estrategia: {estrategia}\n"
            f"{'─'*28}\n"
            f"WR semana:  {wr:.1%} ({wins}/{total})\n"
            f"PnL semana: ${pnl_s:+.4f}\n"
            f"ID:         {trade_id}"
        )
        await self._enviar(msg)

    async def notificar_estado(self):
        """Enviado cada 4 horas automáticamente."""
        snap   = self._risk.snapshot() if self._risk else {}
        trades = self._cargar_trades_semana()
        wins   = sum(1 for t in trades if t.get("pnl", 0) > 0)
        wr     = wins / len(trades) if trades else 0
        pnl_s  = sum(t.get("pnl", 0) for t in trades)
        hora   = datetime.now(timezone.utc).strftime("%H:%M UTC")

        # Estado del circuit breaker
        cb_activo  = snap.get("cb_activo", False)
        dd_rolling = snap.get("cb_dd_rolling", 0.0)
        cb_umbral  = snap.get("cb_umbral", 0.20)
        cb_margen  = snap.get("cb_margen", cb_umbral)
        cb_hasta   = snap.get("cb_hasta")

        if cb_activo:
            cb_linea = f"🚨 CB activo hasta {cb_hasta[:10] if cb_hasta else '?'}"
        elif dd_rolling > cb_umbral * 0.6:
            cb_linea = f"⚠️ DD rolling: {dd_rolling*100:.1f}% (margen: {cb_margen*100:.1f}%)"
        else:
            cb_linea = f"✅ DD rolling: {dd_rolling*100:.1f}% / {cb_umbral*100:.0f}%"

        msg = (
            f"📊 <b>ESTADO PERIÓDICO</b>\n"
            f"{'─'*28}\n"
            f"⏰ {hora}\n"
            f"💰 Capital:    ${snap.get('capital', 0):.2f}\n"
            f"🏔 Pico 4 sem: ${snap.get('cb_peak_rolling', 0):.2f}\n"
            f"📌 Pos:        {snap.get('posiciones', 0)}\n"
            f"🎯 WR sem:     {wr:.1%}\n"
            f"💵 PnL sem:    ${pnl_s:+.4f}\n"
            f"{cb_linea}"
        )
        await self._enviar(msg)

    async def notificar_circuit_breaker(
        self,
        dd_pct: float,
        peak: float,
        capital: float,
        hasta: "datetime",
    ):
        """Alerta Telegram cuando el circuit breaker rolling se dispara."""
        hora = datetime.now(timezone.utc).strftime("%H:%M UTC")
        msg  = (
            f"🚨 <b>CIRCUIT BREAKER ACTIVADO</b>\n"
            f"{'─'*28}\n"
            f"⏰ {hora}\n"
            f"📉 DD rolling:  {dd_pct*100:.1f}%\n"
            f"🏔 Pico 4 sem:  ${peak:.2f}\n"
            f"💰 Capital:     ${capital:.2f}\n"
            f"⏸ Pausa hasta: {hasta.strftime('%d %b %Y %H:%M UTC')}\n"
            f"ℹ️ El bot reanudará operaciones automáticamente."
        )
        await self._enviar(msg)

    async def notificar_error(self, error: str, contexto: str = ""):
        hora = datetime.now(timezone.utc).strftime("%H:%M UTC")
        msg  = (
            f"🔴 <b>ERROR CRÍTICO</b>\n"
            f"⏰ {hora}\n"
            f"❌ {error}\n"
            f"📍 {contexto[:100] if contexto else 'N/A'}"
        )
        await self._enviar(msg)

    # ════════════════════════════════════════════════════════════════
    # CALIBRACIÓN AUTOMÁTICA — llamada por backtest_harness cada sábado
    # ════════════════════════════════════════════════════════════════

    def calibrar(self) -> None:
        """
        Calibración automática basada en historial real de trades.
        Llamado sincrónicamente por backtest_harness (AUDIT_CALIBRATION_METHODS).

        Decisiones que toma:
          - Activa/pausa pares según WR vs breakeven (RR=2 → 33.3%)
          - Activa/pausa estrategias según WR con mínimo de trades
          - Ajusta adx_max_rsi_bollinger según WR global observado
          - Ajusta min_sl_pips según ratio de SLs instantáneos
          - Activa/pausa sesiones según PnL acumulado por sesión
          - Escribe resultado a strategy_params.json
        """
        logger.info("[AuditAgent.calibrar] ── Calibración automática iniciando ──")

        trades = self._cargar_todos_los_trades()
        n_total = len(trades)
        if n_total < 20:
            logger.warning(
                f"[AuditAgent.calibrar] Solo {n_total} trades — "
                "calibración omitida (mínimo 20 para decisiones válidas)"
            )
            return

        # ── Leer parámetros base ─────────────────────────────────────────────
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                params = json.load(f)
        except Exception as exc:
            logger.error(f"[AuditAgent.calibrar] No se pudo leer PARAMS_FILE: {exc}")
            return

        rr          = params.get("rr_ratio", 2.0)
        breakeven   = 1.0 / (1.0 + rr)          # 0.333 para RR=2

        # ── Métricas por par ─────────────────────────────────────────────────
        stats_par: dict = {}
        for t in trades:
            par = t.get("par") or t.get("instrument", "")
            if not par:
                continue
            s = stats_par.setdefault(par, {"n": 0, "wins": 0, "gain": 0.0, "loss": 0.0})
            pnl = float(t.get("pnl", 0))
            s["n"] += 1
            if pnl > 0:
                s["wins"] += 1; s["gain"] += pnl
            else:
                s["loss"] += abs(pnl)

        # ── Métricas por estrategia ──────────────────────────────────────────
        stats_strat: dict = {}
        for t in trades:
            strat = t.get("estrategia", "?")
            s = stats_strat.setdefault(strat, {"n": 0, "wins": 0, "gain": 0.0, "loss": 0.0})
            pnl = float(t.get("pnl", 0))
            s["n"] += 1
            if pnl > 0:
                s["wins"] += 1; s["gain"] += pnl
            else:
                s["loss"] += abs(pnl)

        # ── Métricas por sesión (usando hora de apertura) ────────────────────
        session_pnl: dict = {"london": 0.0, "overlap": 0.0, "new_york": 0.0}
        session_n:   dict = {"london": 0,   "overlap": 0,   "new_york": 0}
        for t in trades:
            open_str = t.get("openTime") or t.get("opened_at", "")
            try:
                dt   = datetime.fromisoformat(str(open_str).replace("Z", "+00:00"))
                hour = dt.hour
                if   7  <= hour < 12: ses = "london"
                elif 12 <= hour < 17: ses = "overlap"
                elif 17 <= hour < 22: ses = "new_york"
                else:                  continue
                session_pnl[ses] += float(t.get("pnl", 0))
                session_n[ses]   += 1
            except Exception:
                pass

        # ── Selección de pares ───────────────────────────────────────────────
        MIN_TRADES_PAR = 50   # necesitamos 50+ trades por par para decisión estadísticamente válida
        todos_pares = list({
            *params.get("pares_activos", []),
            *params.get("pares_pausados", []),
        })
        pares_activos_new  = []
        pares_pausados_new = []

        for par in todos_pares:
            s = stats_par.get(par)
            if s and s["n"] >= MIN_TRADES_PAR:
                wr = s["wins"] / s["n"]
                pf = (s["gain"] / s["loss"]) if s["loss"] > 0 else 99.0
                if wr >= breakeven:
                    pares_activos_new.append(par)
                    logger.info(
                        f"[AuditAgent.calibrar]  PAR ACTIVO  : {par} "
                        f"WR={wr:.1%} PF={pf:.2f} n={s['n']}"
                    )
                else:
                    pares_pausados_new.append(par)
                    logger.info(
                        f"[AuditAgent.calibrar]  PAR PAUSADO : {par} "
                        f"WR={wr:.1%} < {breakeven:.1%} n={s['n']}"
                    )
            else:
                # Sin suficientes datos → mantener estado actual
                if par in params.get("pares_activos", []):
                    pares_activos_new.append(par)
                else:
                    pares_pausados_new.append(par)

        if not pares_activos_new:
            pares_activos_new = params.get("pares_activos", ["NZD_USD"])
            logger.warning("[AuditAgent.calibrar] Ningún par supera breakeven — manteniendo actuales")

        # ── Salvaguarda anti-deadlock: evitar dejar solo pares sin datos ────────
        # Si todos los pares activos tienen 0 trades reales (p.ej. AUD_USD sin
        # datos históricos), el bot no generará señales. Restaurar el par pausado
        # con más trades para garantizar que haya al menos un par con actividad.
        pares_activos_con_datos = [
            p for p in pares_activos_new
            if stats_par.get(p, {}).get("n", 0) >= 1
        ]
        if not pares_activos_con_datos and pares_pausados_new:
            mejor_pausado = max(
                pares_pausados_new,
                key=lambda p: stats_par.get(p, {}).get("n", 0),
            )
            pares_activos_new.append(mejor_pausado)
            pares_pausados_new = [p for p in pares_pausados_new if p != mejor_pausado]
            logger.warning(
                f"[AuditAgent.calibrar] Anti-deadlock: todos los pares activos "
                f"tienen 0 trades reales. Restaurando '{mejor_pausado}' "
                f"(n={stats_par.get(mejor_pausado, {}).get('n', 0)} trades) "
                f"para evitar bot inoperativo."
            )

        # Salvaguarda: limpiar pausados de cualquier par que esté en activos
        pares_pausados_new = [p for p in pares_pausados_new if p not in pares_activos_new]

        # ── Selección de estrategias ─────────────────────────────────────────
        MIN_TRADES_STRAT = 50  # 50+ trades por estrategia antes de pausarla

        # Estrategias con implementación nueva: sus trades anteriores a la fecha
        # de corrección no son representativos y no deben usarse para pausarlas.
        # Formato: {"Hammer": "2026-05-29", "Doji": "2026-05-29"}
        reimplementadas = params.get("estrategias_reimplementadas", {})

        todas_strats = list({
            *params.get("estrategias_activas", []),
            *params.get("estrategias_pausadas", []),
        })
        strats_activas_new  = []
        strats_pausadas_new = []

        for strat in todas_strats:
            # Si la estrategia fue reimplementada, recalcular stats solo con
            # trades desde la fecha del fix (implementación nueva)
            if strat in reimplementadas:
                fecha_fix = reimplementadas[strat]
                trades_validos = [
                    t for t in trades
                    if t.get("estrategia") == strat
                    and str(t.get("opened_at", t.get("timestamp", "")))[:10] >= fecha_fix
                ]
                if len(trades_validos) < MIN_TRADES_STRAT:
                    # Sin suficientes datos limpios — mantener activa sin evaluar
                    strats_activas_new.append(strat)
                    logger.info(
                        f"[AuditAgent.calibrar]  PROTEGIDA (reimpl {fecha_fix}): {strat} "
                        f"solo {len(trades_validos)} trades válidos post-fix — mantener activa"
                    )
                    continue
                # Suficientes trades post-fix: evaluar normalmente con datos limpios
                wins_validos = sum(1 for t in trades_validos if float(t.get("pnl", 0)) > 0)
                s = {"n": len(trades_validos), "wins": wins_validos}
            else:
                s = stats_strat.get(strat)

            if s and s["n"] >= MIN_TRADES_STRAT:
                wr = s["wins"] / s["n"]
                # Umbral algo más permisivo que breakeven puro (80%) por ruido estadístico
                if wr >= breakeven * 0.80:
                    strats_activas_new.append(strat)
                    logger.info(
                        f"[AuditAgent.calibrar]  ESTRATEGIA ACTIVA : {strat} "
                        f"WR={wr:.1%} n={s['n']}"
                    )
                else:
                    strats_pausadas_new.append(strat)
                    logger.info(
                        f"[AuditAgent.calibrar]  ESTRATEGIA PAUSADA: {strat} "
                        f"WR={wr:.1%} n={s['n']}"
                    )
            else:
                if strat in params.get("estrategias_activas", []):
                    strats_activas_new.append(strat)
                else:
                    strats_pausadas_new.append(strat)

        if not strats_activas_new:
            strats_activas_new = params.get("estrategias_activas", ["RSI_Bollinger"])
            logger.warning("[AuditAgent.calibrar] Ninguna estrategia supera umbral — manteniendo actuales")
        # Salvaguarda: limpiar pausadas de cualquier estrategia que esté en activas
        strats_pausadas_new = [s for s in strats_pausadas_new if s not in strats_activas_new]

        # ── adx_max_rsi_bollinger: PROTEGIDO — no se auto-ajusta ─────────────
        # Motivo: cuando WR es bajo, la lógica "subir ADX para pasar más señales"
        # llevó adx_max de 35 a 38, lo que desactiva el filtro completamente
        # (score_threshold = (38-15)/20 = 1.15, nunca alcanzable).
        # El valor correcto (adx_max=25, threshold=0.50) debe configurarse
        # manualmente en strategy_params.json. No tocar automáticamente.
        wins_total = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
        wr_global  = wins_total / n_total
        adx_nuevo  = params.get("adx_max_rsi_bollinger", 25)  # conservar sin cambio

        # ── min_sl_pips: PROTEGIDO — no se auto-reduce ───────────────────────
        # El ajuste automático de SL corto → min_sl_pips iba de 10 a 6 en 4 semanas,
        # lo que es contraproducente. El valor se mantiene del config manual.
        # Solo se ajusta hacia ARRIBA si hay SLs instantáneos excesivos.
        instant_sl = sum(
            1 for t in trades
            if t.get("resultado") == "SL"
            and str(t.get("openTime", t.get("opened_at", "")))[:16]
               == str(t.get("closeTime", t.get("closed_at", "")))[:16]
        )
        sl_actual = params.get("min_sl_pips", 10)
        ratio_instant = instant_sl / n_total if n_total > 0 else 0

        # Solo subir (nunca bajar) — proteger el mínimo configurado
        if ratio_instant > 0.30:
            sl_nuevo = min(sl_actual + 2, 18)
            logger.info(
                f"[AuditAgent.calibrar]  min_sl_pips: {sl_actual} → {sl_nuevo} "
                f"(SLs instantáneos={instant_sl}/{n_total} = {ratio_instant:.0%})"
            )
        else:
            sl_nuevo = sl_actual   # nunca bajar automáticamente

        # ── Ajuste de sesiones ───────────────────────────────────────────────
        sesiones_activas = list(params.get("sesiones_activas", ["london", "new_york", "overlap"]))
        for ses in ["london", "overlap", "new_york"]:
            n   = session_n.get(ses, 0)
            pnl = session_pnl.get(ses, 0.0)
            # 30+ trades mínimo para pausar sesión (antes era 20, insuficiente
            # para decisiones estadísticamente válidas — con 21 trades/sesión
            # se pausó london en el backtest cuando WR era ruido estadístico).
            if n >= 30 and pnl < -15.0 and ses in sesiones_activas:
                sesiones_activas.remove(ses)
                logger.info(
                    f"[AuditAgent.calibrar]  SESIÓN PAUSADA : {ses} "
                    f"PnL=${pnl:.2f} n={n}"
                )
            elif n >= 20 and pnl > 5.0 and ses not in sesiones_activas:
                sesiones_activas.append(ses)
                logger.info(
                    f"[AuditAgent.calibrar]  SESIÓN ACTIVADA: {ses} "
                    f"PnL=${pnl:.2f} n={n}"
                )
        if not sesiones_activas:
            sesiones_activas = ["london", "new_york"]

        # ── Guardar calibración ──────────────────────────────────────────────
        params.update({
            "pares_activos":          sorted(pares_activos_new),
            "pares_pausados":         sorted(pares_pausados_new),
            "estrategias_activas":    strats_activas_new,
            "estrategias_pausadas":   strats_pausadas_new,
            "sesiones_activas":       sesiones_activas,
            "min_sl_pips":            sl_nuevo,
            "adx_max_rsi_bollinger":  adx_nuevo,
            "calibrado_en":           datetime.now(timezone.utc).isoformat(),
            "calibrado_por":          "audit_agent_calibrar",
            "wr_global_calibracion":  round(wr_global, 3),
            "trades_calibracion":     n_total,
        })

        try:
            with open(PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False, indent=2)
            logger.info(
                f"[AuditAgent.calibrar] ✓ Guardado — "
                f"pares={pares_activos_new} | "
                f"strats={strats_activas_new} | "
                f"adx={adx_nuevo} | sl_pips={sl_nuevo} | "
                f"sesiones={sesiones_activas} | "
                f"WR={wr_global:.1%} ({wins_total}/{n_total})"
            )
        except Exception as exc:
            logger.error(f"[AuditAgent.calibrar] Error guardando: {exc}")

    # ════════════════════════════════════════════════════════════════
    # FIN DE SEMANA — ANÁLISIS Y CALIBRACIÓN
    # ════════════════════════════════════════════════════════════════

    async def notificar_sesion_abierta(self):
        """07:00 UTC Lun-Vie — London abre, el bot empieza a buscar trades."""
        p     = self._params
        pares = [x.replace("_", "/") for x in p.get("pares_activos", [])]
        fecha = datetime.now(timezone.utc).strftime("%a %d %b")
        sep   = "─" * 30
        msg   = (
            f"🟢 <b>SESIÓN ABIERTA — LONDON</b>\n"
            f"{sep}\n"
            f"📅 {fecha} | 07:00 UTC\n"
            f"📊 Pares:       {', '.join(pares)}\n"
            f"⚙️ ADX mín:     {p.get('adx_min_operar', 23)} | "
            f"RR: {p.get('rr_ratio', 2.0)} | "
            f"Timeout: {p.get('max_trade_hours', 12)}h\n"
            f"🧠 Motor:       Briefing (chat) → Decisor (reasoner)\n"
            f"🕐 Cierre:      17:00 UTC (Overlap)"
        )
        await self._enviar(msg)

    async def notificar_sesion_cerrada(self):
        """17:00 UTC Lun-Vie — Overlap cierra, resumir el dia."""
        trades = self._cargar_trades_hoy()
        wins   = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = len(trades) - wins
        pnl    = sum(t.get("pnl", 0) for t in trades)
        wr     = wins / len(trades) if trades else 0
        snap   = self._risk.snapshot() if self._risk else {}
        cap    = snap.get("capital", 0)

        if not trades:
            resumen = "Sin trades hoy."
        elif wr >= 0.50:
            resumen = "✅ Buen día — WR por encima del 50%"
        elif wr >= 0.333:
            resumen = "📊 Día rentable — WR sobre break-even"
        else:
            resumen = "⚠️ WR bajo break-even — revisar mañana"

        sep = "─" * 30
        msg = (
            f"⚫ <b>SESIÓN CERRADA</b>\n"
            f"{sep}\n"
            f"⏰ 17:00 UTC — Overlap cerrado\n"
            f"{sep}\n"
            f"<b>RESUMEN DEL DÍA</b>\n"
            f"Trades:     {len(trades)} ({wins}✅ / {losses}❌)\n"
            f"WR:         <b>{wr:.1%}</b>\n"
            f"PnL:        <b>${pnl:+.4f}</b>\n"
            f"Capital:    <b>${cap:.2f}</b>\n"
            f"{sep}\n"
            f"{resumen}\n"
            f"⏭ Próxima sesión: mañana 07:00 UTC"
        )
        await self._enviar(msg)

    async def run(self):
        """Loop del AuditAgent — tareas programadas + Telegram."""
        self._running = True

        # Iniciar bot Telegram
        await self.iniciar_telegram()

        # ── Sesión London+Overlap: notificaciones de apertura y cierre ────────
        schedule.every().monday.at("07:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_abierta()))
        schedule.every().tuesday.at("07:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_abierta()))
        schedule.every().wednesday.at("07:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_abierta()))
        schedule.every().thursday.at("07:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_abierta()))
        schedule.every().friday.at("07:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_abierta()))

        schedule.every().monday.at("17:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_cerrada()))
        schedule.every().tuesday.at("17:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_cerrada()))
        schedule.every().wednesday.at("17:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_cerrada()))
        schedule.every().thursday.at("17:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_cerrada()))
        schedule.every().friday.at("17:00").do(
            lambda: asyncio.create_task(self.notificar_sesion_cerrada()))

        # ── Estado periódico cada 4 horas (solo en horario activo) ───────────
        schedule.every(4).hours.do(
            lambda: asyncio.create_task(self._notificar_estado_si_activo())
        )

        # ── Fin de semana ─────────────────────────────────────────────────────
        schedule.every().saturday.at("00:00").do(
            lambda: asyncio.create_task(self._analisis_semanal())
        )
        schedule.every().sunday.at("20:00").do(
            lambda: asyncio.create_task(self._preparar_apertura())
        )
        schedule.every().sunday.at("22:00").do(
            lambda: asyncio.create_task(self._notificar_apertura())
        )

        logger.info("AuditAgent: loop iniciado (schedule London+Overlap + Telegram)")
        while self._running:
            schedule.run_pending()
            await asyncio.sleep(60)

    async def _notificar_estado_si_activo(self):
        """Solo envía estado periódico durante sesión London+Overlap (7-17 UTC)."""
        h = datetime.now(timezone.utc).hour
        if 7 <= h < 17:
            await self.notificar_estado()

    async def _analisis_semanal(self):
        """Reporte semanal con métricas matemáticas puras (sin DeepSeek)."""
        logger.info("AuditAgent: iniciando análisis semanal")
        await self._enviar("🔬 <b>ANÁLISIS SEMANAL</b>\nCalculando métricas...")

        trades = self._cargar_trades_semana()
        if not trades or len(trades) < 3:
            await self._enviar(
                "📋 <b>REPORTE SEMANAL</b>\n"
                "Sin suficientes trades esta semana (mínimo 3).\n"
                "El sistema sigue activo y monitoreando."
            )
            return

        metricas = self._calcular_metricas(trades)
        analisis = self._analisis_matematico(trades, metricas)
        await self._reporte_semanal_telegram(metricas, analisis)

    def _analisis_matematico(self, trades: list, metricas: dict) -> dict:
        """Análisis y recomendaciones basadas en matemática pura, sin LLM."""
        wr  = metricas.get("wr", 0)
        pf  = metricas.get("pf") or 0
        n   = metricas.get("n", 0)
        rr  = self._params.get("rr_ratio", 2.0)
        breakeven_wr = 1 / (1 + rr)

        # Por estrategia
        por_strat = {}
        for t in trades:
            s = t.get("estrategia", "?")
            if s not in por_strat:
                por_strat[s] = {"ops": 0, "wins": 0, "pnl": 0.0}
            por_strat[s]["ops"] += 1
            por_strat[s]["pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                por_strat[s]["wins"] += 1

        # Diagnóstico automático
        if n < 10:
            accion = f"Muestra pequeña ({n} trades) — continuar acumulando datos antes de ajustar."
        elif wr >= breakeven_wr + 0.05:
            accion = f"WR {wr:.1%} sobre breakeven ({breakeven_wr:.1%}) — sistema funcionando. Mantener config."
        elif wr >= breakeven_wr:
            accion = f"WR {wr:.1%} cerca del breakeven ({breakeven_wr:.1%}) — monitorear próxima semana."
        else:
            accion = f"WR {wr:.1%} bajo breakeven ({breakeven_wr:.1%}) — revisar filtro H4 y ADX."

        # Estrategia con peor desempeño
        peor = min(por_strat.items(),
                   key=lambda x: x[1]["pnl"] / max(x[1]["ops"], 1),
                   default=(None, {}))
        strat_problema = peor[0] if peor[0] and peor[1].get("pnl", 0) < 0 else None

        return {
            "accion_inmediata": accion,
            "sugerencias": [],
            "estrategia_problema": strat_problema,
        }

    def _calcular_metricas(self, trades: list) -> dict:
        """Calcula métricas con QuantStats."""
        try:
            import quantstats as qs
            import pandas as pd

            pnls    = [t.get("pnl", 0) for t in trades if "pnl" in t]
            cap_ini = 200.0
            returns = pd.Series(
                [p / cap_ini for p in pnls],
                index=pd.to_datetime([t.get("opened_at", "")
                                      for t in trades if "pnl" in t])
            ).dropna()

            if len(returns) < 3:
                raise ValueError("insuficiente")

            return {
                "sharpe":  round(qs.stats.sharpe(returns), 3),
                "sortino": round(qs.stats.sortino(returns), 3),
                "max_dd":  round(qs.stats.max_drawdown(returns), 4),
                "wr":      round(qs.stats.win_rate(returns), 3),
                "pf":      round(qs.stats.profit_factor(returns), 3),
                "pnl_tot": round(sum(pnls), 4),
                "n":       len(trades),
            }
        except Exception:
            wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
            pnl  = sum(t.get("pnl", 0) for t in trades)
            return {
                "wr":      round(wins / len(trades), 3) if trades else 0,
                "pnl_tot": round(pnl, 4),
                "n":       len(trades),
                "sharpe":  None, "sortino": None, "max_dd": None, "pf": None,
            }


    async def _reporte_semanal_telegram(self, metricas: dict, analisis: dict):
        """Envía el reporte semanal por Telegram."""
        m   = metricas
        wr  = m.get("wr", 0)
        pf  = m.get("pf")
        sh  = m.get("sharpe")
        dd  = m.get("max_dd")
        pnl = m.get("pnl_tot", 0)
        n   = m.get("n", 0)

        msg = (
            f"📋 <b>REPORTE SEMANAL</b>\n"
            f"{'━'*28}\n"
            f"<b>📊 RENDIMIENTO</b>\n"
            f"Trades:   {n}\n"
            f"WR:       <b>{wr:.1%}</b>\n"
        )
        if pf:    msg += f"PF:       <b>{pf:.2f}</b>\n"
        if sh:    msg += f"Sharpe:   <b>{sh:.2f}</b>\n"
        if dd:    msg += f"Max DD:   <b>{dd:.1%}</b>\n"
        msg += f"PnL:      <b>${pnl:+.4f}</b>\n\n"

        msg += f"<b>\U0001f527 AN\u00c1LISIS</b>\n{analisis.get('accion_inmediata','N/A')}\n"

        sugs = analisis.get("sugerencias", [])
        if sugs:
            msg += "\n<b>Cambios propuestos:</b>\n"
            for s in sugs[:2]:
                msg += f"\u2022 {s.get('param')}: {s.get('actual')} \u2192 {s.get('propuesto')} ({s.get('impacto','')})\n"

        await self._enviar(msg)

    # \u2550 UTILIDADES \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

    async def notificar_break_even(self, trade_id, par, dir_, entry, new_sl, precio, sl_dist):
        pip = 0.01 if "JPY" in par else 0.0001
        ganancia_pip = abs(precio - entry) / pip
        sl_dist_pip  = sl_dist / pip
        hora = datetime.now(timezone.utc).strftime("%H:%M UTC")
        lineas = [
            "\U0001f512 <b>BREAK-EVEN ACTIVADO</b>",
            f"Par:      {par.replace('_', '/')}",
            f"Hora:     {hora}",
            f"Dir:      {dir_.upper()}",
            f"Entry:    {entry:.5f}",
            f"SL nuevo: {new_sl:.5f} (entry)",
            f"Precio:   {precio:.5f} (+{ganancia_pip:.1f} pips)",
            f"1R dist:  {sl_dist_pip:.1f} pips",
            "Riesgo:   $0.00 \u2705",
            f"ID:       {trade_id}",
        ]
        await self._enviar("\n".join(lineas))

    # \u2500\u2500 ENV\u00cdO TELEGRAM \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    async def _enviar(self, texto: str) -> bool:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.debug(f"Telegram desactivado: {texto[:50]}")
            return False
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       texto,
                "parse_mode": "HTML",
            }
            resp = await self._http.post(url, json=data)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    def _cargar_todos_los_trades(self) -> list:
        candidatos = [
            TRADES_LOG,
            TRADES_LOG.parent.parent / "data" / "trades" / "trades_log.json",
        ]
        mejor = []
        for path in candidatos:
            try:
                if path.exists() and path.stat().st_size > 2:
                    trades = [t for t in json.loads(path.read_text()) if "pnl" in t]
                    if len(trades) > len(mejor):
                        mejor = trades
            except Exception:
                pass
        return mejor

    def _cargar_trades_hoy(self) -> list:
        """Trades del d\u00eda actual (UTC)."""
        try:
            hoy   = datetime.now(timezone.utc).date().isoformat()
            todos = json.loads(TRADES_LOG.read_text())
            return [
                t for t in todos
                if str(t.get("opened_at", t.get("timestamp", "")))[:10] == hoy
                and "pnl" in t
            ]
        except Exception:
            return []

    def _cargar_trades_recientes(self, n: int = 20) -> list:
        try:
            trades = json.loads(TRADES_LOG.read_text())
            return [t for t in trades if "pnl" in t][-n:]
        except Exception:
            return []

    def _cargar_trades_semana(self) -> list:
        try:
            ahora    = datetime.now(timezone.utc)
            semana   = ahora.isocalendar()[1]
            anio     = ahora.year
            todos    = json.loads(TRADES_LOG.read_text())
            resultado = []
            for t in todos:
                try:
                    ts = datetime.fromisoformat(
                        t.get("opened_at", "").replace("Z", "+00:00")
                    )
                    if ts.isocalendar()[1] == semana and ts.year == anio:
                        resultado.append(t)
                except Exception:
                    pass
            return resultado
        except Exception:
            return []

    def reload_params(self, new_params: dict) -> None:
        """Callback de ParamsWatcher — actualiza parámetros en caliente."""
        self._params = new_params
        logger.info(
            f"AuditAgent reload_params: "
            f"activas={new_params.get('estrategias_activas', [])} | "
            f"min_conf={new_params.get('min_confidence')}"
        )

    def stop(self):
        self._running = False
        # NOTA: el apagado real de Telegram es async (ashutdown), ejecutado por
        # main.py. No crear tasks aquí: la task huérfana + el doble stop()
        # causaban "RuntimeError: This Application is not running!".
        logger.info("AuditAgent detenido")

    async def ashutdown(self):
        """Apagado limpio de python-telegram-bot: updater → app → shutdown.

        Idempotente: main.py lo llama una vez en el finally; llamadas
        repetidas no hacen nada.
        """
        if self._tg_stopped:
            return
        self._tg_stopped = True
        app, self._app = self._app, None
        if app is None:
            return
        try:
            updater = getattr(app, "updater", None)
            if updater is not None and getattr(updater, "running", False):
                await updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("AuditAgent: Telegram apagado limpiamente")
        except RuntimeError as e:
            # p.ej. "This Application is not running!" si ya se detuvo
            logger.debug(f"AuditAgent: Telegram ya estaba detenido ({e})")
        except Exception as e:
            logger.warning(f"AuditAgent: error apagando Telegram: {e}")
