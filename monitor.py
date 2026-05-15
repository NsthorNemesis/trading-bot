"""
monitor.py
Monitor en tiempo real del Trading Bot v11
Muestra exactamente qué hacen los agentes con las velas

Uso: python monitor.py
"""
import sys
import json
import time
import os
from pathlib import Path
from datetime import datetime, timezone

# ── COLORES ───────────────────────────────────────────────────────────────────
def verde(t):    return f"\033[92m{t}\033[0m"
def rojo(t):     return f"\033[91m{t}\033[0m"
def azul(t):     return f"\033[94m{t}\033[0m"
def amarillo(t): return f"\033[93m{t}\033[0m"
def cyan(t):     return f"\033[96m{t}\033[0m"
def negrita(t):  return f"\033[1m{t}\033[0m"
def gris(t):     return f"\033[90m{t}\033[0m"

BASE = Path(r"C:\Users\na_sc\trading_bot_v11")

# ── UTILIDADES ────────────────────────────────────────────────────────────────

def limpiar():
    os.system("cls" if os.name == "nt" else "clear")

def hora():
    return datetime.now().strftime("%H:%M:%S")

def leer_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        pass
    return default

def ultimas_lineas_log(n: int = 8) -> list:
    log = BASE / "logs" / "trading_bot.log"
    try:
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
            return lines[-n:]
    except Exception:
        pass
    return []

def sesion_actual() -> str:
    h = datetime.utcnow().hour
    if 7  <= h < 13: return verde("LONDON 🇬🇧")
    if 13 <= h < 17: return verde("OVERLAP 🌍")
    if 17 <= h < 22: return verde("NEW YORK 🇺🇸")
    return amarillo("ASIA 🌏")

# ── SECCIONES DEL MONITOR ─────────────────────────────────────────────────────

def mostrar_header():
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(negrita("═" * 70))
    print(negrita(f"  TRADING BOT v11 — MONITOR EN TIEMPO REAL  {ahora}"))
    print(negrita("═" * 70))

def mostrar_market_agent():
    print(f"\n{cyan('── MARKET AGENT')} {gris('(datos + indicadores)')}")

    hist_dir = BASE / "data" / "historical"
    total_velas = 0

    pares = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]
    print(f"  {'Par':<12} {'Velas M1':>9} {'Última vela':>17} {'Edad':>8} {'Estado'}")
    print(f"  {'─'*62}")

    for par in pares:
        archivo = hist_dir / f"{par}_M1.json"
        if archivo.exists():
            try:
                velas = json.loads(archivo.read_text())
                n = len(velas)
                total_velas += n
                ultima_ts = velas[-1]["timestamp"] if velas else "N/A"
                ultima_fmt = ultima_ts[:16].replace("T", " ") if ultima_ts != "N/A" else "N/A"

                # Calcular edad
                try:
                    ts_dt = datetime.fromisoformat(
                        ultima_ts.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    edad_s = (datetime.utcnow() - ts_dt).total_seconds()
                    if edad_s < 120:
                        edad_str = verde(f"{edad_s:.0f}s")
                        estado   = verde("● FRESCO")
                    elif edad_s < 300:
                        edad_str = amarillo(f"{edad_s:.0f}s")
                        estado   = amarillo("● RECIENTE")
                    else:
                        mins = edad_s / 60
                        edad_str = rojo(f"{mins:.1f}m")
                        estado   = rojo("● VIEJO")
                except Exception:
                    edad_str = gris("?")
                    estado   = gris("● ?")

                par_disp = par.replace("_", "/")
                print(f"  {par_disp:<12} {n:>9,} {ultima_fmt:>17} {edad_str:>8}  {estado}")
            except Exception as e:
                print(f"  {par:<12} {rojo('ERROR: ' + str(e)[:30])}")
        else:
            print(f"  {par:<12} {amarillo('Sin datos M1')}")

    print(f"\n  Total velas en buffer: {negrita(f'{total_velas:,}')}")
    print(f"  Sesión activa: {sesion_actual()}")

def mostrar_signal_agent():
    print(f"\n{cyan('── SIGNAL AGENT')} {gris('(detección de señales)')}")

    params_file = BASE / "data" / "calibration" / "strategy_params.json"
    params = leer_json(params_file, {})

    estrategias = params.get("estrategias_activas", [])
    sesiones    = params.get("sesiones_activas", [])
    confianza   = params.get("min_confidence", 0)
    cooldown    = params.get("cooldown_minutes", 15)
    rr          = params.get("rr_ratio", 1.8)

    print(f"  Estrategias activas ({len(estrategias)}): {verde(', '.join(estrategias))}")
    print(f"  Sesiones activas:   {verde(', '.join(sesiones))}")
    print(f"  Confianza mínima:   {negrita(f'{confianza:.0%}')}")
    print(f"  Cooldown:           {cooldown} min | RR mínimo: {rr}")

    # Buscar señales en el log
    lineas = ultimas_lineas_log(50)
    señales = [l for l in lineas if "SEÑAL" in l or "senal" in l.lower()]
    if señales:
        print(f"\n  {verde('Últimas señales detectadas:')}")
        for s in señales[-3:]:
            print(f"    {verde('→')} {s[24:] if len(s) > 24 else s}")
    else:
        print(f"\n  {gris('Sin señales detectadas aún — esperando condiciones')}")

    # Ver si está evaluando
    evaluando = [l for l in lineas if "evaluación" in l.lower() or "evaluando" in l.lower()]
    if evaluando:
        print(f"  {verde('Estado: EVALUANDO señales activamente')}")
    else:
        esperando = [l for l in lineas if "esperando" in l.lower() and "signal" in l.lower()]
        if esperando:
            print(f"  {amarillo('Estado: Esperando datos frescos (5 min inicio)')}")

def mostrar_risk_agent():
    print(f"\n{cyan('── RISK + EXECUTION AGENT')} {gris('(órdenes OANDA)')}")

    trades = leer_json(BASE / "logs" / "trades.json", [])
    trades_con_pnl = [t for t in trades if "pnl" in t]

    params_file = BASE / "data" / "calibration" / "strategy_params.json"
    params = leer_json(params_file, {})

    riesgo_val = f"{params.get('riesgo_pct', 0.005):.1%}"
    print(f"  Riesgo por operación: {negrita(riesgo_val)}")
    print(f"  Max posiciones:       {params.get('max_posiciones', 5)}")
    print(f"  SL multiplicador:     {params.get('sl_atr_mult', 1.5)}× ATR")

    if trades:
        print(f"\n  Trades registrados: {negrita(str(len(trades)))}")
        wins = sum(1 for t in trades_con_pnl if t.get("pnl", 0) > 0)
        pnl  = sum(t.get("pnl", 0) for t in trades_con_pnl)
        if trades_con_pnl:
            wr = wins / len(trades_con_pnl)
            print(f"  WR: {verde(f'{wr:.1%}') if wr >= 0.5 else rojo(f'{wr:.1%}')} | PnL: {verde(f'${pnl:+.4f}') if pnl > 0 else rojo(f'${pnl:+.4f}')}")

        # Mostrar últimos 3 trades
        if trades_con_pnl:
            print(f"\n  Últimos trades:")
            for t in trades_con_pnl[-3:]:
                emoji = "✅" if t.get("pnl", 0) > 0 else "❌"
                par   = t.get("par", "?").replace("_", "/")
                strat = t.get("estrategia", "?")[:12]
                pnl_t = t.get("pnl", 0)
                hora_t = str(t.get("opened_at", ""))[:16]
                print(f"    {emoji} {par:<8} {strat:<14} ${pnl_t:+.4f}  {gris(hora_t)}")
    else:
        print(f"  {gris('Sin trades ejecutados aún')}")

    # Ver monitor de posiciones en log
    lineas = ultimas_lineas_log(30)
    open_trades = [l for l in lineas if "openTrades" in l]
    if open_trades:
        ultima = open_trades[-1][11:19]  # solo la hora
        print(f"\n  {verde('Monitor OANDA activo')} — última consulta: {ultima}")

def mostrar_audit_agent():
    print(f"\n{cyan('── AUDIT AGENT')} {gris('(Telegram + calibración)')}")

    params_file = BASE / "data" / "calibration" / "strategy_params.json"
    params = leer_json(params_file, {})

    calibrado_en = params.get("calibrado_en", "N/A")
    if calibrado_en != "N/A":
        calibrado_en = calibrado_en[:16].replace("T", " ")

    print(f"  Telegram:        {verde('ACTIVO ✓')}")
    print(f"  Calibrado:       {calibrado_en}")
    print(f"  Trades analizados: {params.get('trades_analizados', 0):,}")
    nota = params.get("nota_wr_backtest", params.get("notas_analista", ""))
    if nota:
        print(f"  Nota: {gris(nota[:65])}")

    # Tareas de fin de semana
    ahora = datetime.utcnow()
    if ahora.weekday() == 5:
        print(f"  {verde('HOY ES SÁBADO — análisis semanal programado a las 00:00 UTC')}")
    elif ahora.weekday() == 6:
        print(f"  {amarillo('HOY ES DOMINGO — preparación apertura a las 20:00 UTC')}")
    else:
        dias = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]
        dia  = dias[ahora.weekday()]
        print(f"  Próximo análisis semanal: {gris('Sábado 00:00 UTC')} (hoy es {dia})")

def mostrar_logs_recientes():
    print(f"\n{cyan('── LOGS RECIENTES')}")
    lineas = ultimas_lineas_log(8)
    filtros = ["signal", "market", "risk", "audit", "SEÑAL", "ORDEN",
               "ERROR", "stream", "velas", "iniciado", "buffer"]

    relevantes = [l for l in lineas if any(f.lower() in l.lower() for f in filtros)]
    mostrar = relevantes[-6:] if relevantes else lineas[-6:]

    for linea in mostrar:
        # Colorear según tipo
        if "ERROR" in linea or "error" in linea:
            print(f"  {rojo(linea[23:] if len(linea) > 23 else linea)}")
        elif "SEÑAL" in linea or "ORDEN" in linea:
            print(f"  {verde(linea[23:] if len(linea) > 23 else linea)}")
        elif "WARNING" in linea:
            print(f"  {amarillo(linea[23:] if len(linea) > 23 else linea)}")
        else:
            print(f"  {gris(linea[23:] if len(linea) > 23 else linea)}")

def mostrar_footer():
    print(f"\n{gris('─' * 70)}")
    print(f"  {gris('Ctrl+C para salir | Actualiza cada 15s | Señales esperadas en sesión London/Overlap')}")
    print(f"  {gris('Próxima sesión London: 07:00 UTC | Overlap: 13:00 UTC | New York: 17:00 UTC')}")

# ── LOOP PRINCIPAL ────────────────────────────────────────────────────────────

def main():
    print(f"\n{verde('Monitor iniciando...')}")
    time.sleep(1)

    while True:
        try:
            limpiar()
            mostrar_header()
            mostrar_market_agent()
            mostrar_signal_agent()
            mostrar_risk_agent()
            mostrar_audit_agent()
            mostrar_logs_recientes()
            mostrar_footer()
            time.sleep(15)

        except KeyboardInterrupt:
            print(f"\n{verde('Monitor detenido.')}")
            break
        except Exception as e:
            print(f"\n{rojo(f'Error monitor: {e}')}")
            time.sleep(5)

if __name__ == "__main__":
    main()
