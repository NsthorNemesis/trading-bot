"""
monitor_v2.py — Trading Bot v11
Monitor mejorado con 3 modos:
  python monitor_v2.py           -> dashboard (actualiza cada 10s)
  python monitor_v2.py --logs    -> logs en tiempo real (como tail -f)
  python monitor_v2.py --senales -> solo senales y ordenes
"""
import sys, json, time, os
from pathlib import Path
from datetime import datetime
from collections import deque

# Colores
V  = lambda t: f"\033[92m{t}\033[0m"
R  = lambda t: f"\033[91m{t}\033[0m"
A  = lambda t: f"\033[93m{t}\033[0m"
C  = lambda t: f"\033[96m{t}\033[0m"
N  = lambda t: f"\033[1m{t}\033[0m"
G  = lambda t: f"\033[90m{t}\033[0m"
M  = lambda t: f"\033[95m{t}\033[0m"

BASE   = Path(r"C:\Users\na_sc\trading_bot_v11")
LOG    = BASE / "logs" / "trading_bot.log"
TRADES = BASE / "logs" / "trades.json"
PARAMS = BASE / "data" / "calibration" / "strategy_params.json"
HIST   = BASE / "data" / "historical"

_logpos = 0
_buf    = deque(maxlen=300)

def cls(): os.system("cls" if os.name == "nt" else "clear")

def leer_json(p, d=None):
    try:
        p = Path(p)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        pass
    return d

def cargar_log():
    global _buf
    try:
        if LOG.exists():
            lines = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
            _buf  = deque(lines, maxlen=300)
            return lines
    except Exception:
        pass
    return []

def nuevas():
    global _logpos, _buf
    result = []
    try:
        if LOG.exists():
            lines = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
            if len(lines) > _logpos:
                result   = lines[_logpos:]
                _logpos  = len(lines)
                for l in result:
                    _buf.append(l)
    except Exception:
        pass
    return result

def hora_log(l):
    try: return l[11:19]
    except: return "??:??:??"

def sesion():
    h = datetime.utcnow().hour
    if 7  <= h < 13: return V("LONDON")
    if 13 <= h < 17: return V("OVERLAP")
    if 17 <= h < 22: return V("NEW YORK")
    return A("ASIA")

def colorear(l):
    lo = l.lower()
    if "error" in lo:         return R, "ERR"
    if "warning" in lo:       return A, "WRN"
    if "senal" in lo or "señal" in lo: return V, "SIG"
    if "orden" in lo or "ordercreate" in lo: return C, "ORD"
    if "market_agent" in lo:  return lambda t: f"\033[94m{t}\033[0m", "MKT"
    if "signal_agent" in lo:  return M, "SIG"
    if "risk_agent" in lo:    return C, "RSK"
    if "audit_agent" in lo:   return A, "AUD"
    if "telegram" in lo or "httpx" in lo: return G, "TLG"
    return G, "   "

SPAM = ["getUpdates", "deleteWebhook", "getMe"]

def sin_spam(lines):
    return [l for l in lines if not any(s in l for s in SPAM)]


# ── MODO 1: DASHBOARD ─────────────────────────────────────────────────────────

def dashboard():
    cargar_log()
    todas = list(_buf)

    cls()
    ahora    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cerrado  = datetime.utcnow().weekday() >= 5
    print(N("═"*72))
    print(N(f"  TRADING BOT v11 — MONITOR  {ahora}"))
    print(N("═"*72))
    merc = A("CERRADO (fin de semana)") if cerrado else V("ABIERTO")
    print(f"  Mercado: {merc}  |  Sesion: {sesion()}")

    # MARKET AGENT
    print(f"\n{C('▌ MARKET AGENT')} {G('datos + indicadores')}")
    print(f"  {'Par':<10} {'Velas':>7}  {'Ultima vela':>17}  {'Edad':>7}  Estado")
    print(f"  {'─'*60}")
    total = 0
    for par in ["EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD"]:
        arch = HIST / f"{par}_M1.json"
        if not arch.exists():
            print(f"  {par.replace('_','/'):<10}  {A('sin datos')}")
            continue
        try:
            v  = json.loads(arch.read_text())
            n  = len(v)
            total += n
            ts = v[-1]["timestamp"] if v else "?"
            tf = ts[:16].replace("T"," ")
            dt = datetime.fromisoformat(ts.replace("Z","+00:00")).replace(tzinfo=None)
            s  = (datetime.utcnow() - dt).total_seconds()
            if s < 120:   es, ec = V("● FRESCO"),  V(f"{s:.0f}s")
            elif s < 300: es, ec = A("● RECIENTE"), A(f"{s:.0f}s")
            else:
                m = s/60
                es = G("● CERRADO") if cerrado else R("● VIEJO")
                ec = G(f"{m:.0f}m")
            print(f"  {par.replace('_','/'):<10} {n:>7,}  {tf:>17}  {ec:>7}  {es}")
        except Exception as e:
            print(f"  {par:<10} {R(str(e)[:35])}")
    print(f"  {'─'*60}")
    print(f"  Total en buffer: {N(f'{total:,}')} velas")

    # SIGNAL AGENT
    p = leer_json(PARAMS, {})
    print(f"\n{C('▌ SIGNAL AGENT')} {G('deteccion de senales')}")
    ests = p.get("estrategias_activas", [])
    print(f"  Estrategias: {V(', '.join(ests)) if ests else A('ninguna')}")
    conf_str = f"{p.get('min_confidence',0.3):.0%}"
    print(f"  Confianza minima: {N(conf_str)}  |  Cooldown: {p.get('cooldown_minutes',20)} min")

    senales = [l for l in todas if "senal" in l.lower() or "SEÑAL" in l]
    if senales:
        print(f"  {V('Senales detectadas:')}")
        for s in senales[-3:]:
            print(f"    {V('►')} {s[24:74] if len(s)>24 else s}")
    elif cerrado:
        print(f"  {G('Mercado cerrado — senales al abrir Dom 22:00 UTC')}")
    else:
        print(f"  {A('Evaluando pares — sin senales aun')}")

    # RISK AGENT
    print(f"\n{C('▌ RISK + EXECUTION AGENT')} {G('ordenes OANDA')}")
    print(f"  Riesgo: {N('0.5%/op')}  |  Max pos: {p.get('max_posiciones',3)}  |  SL: {p.get('sl_atr_mult',1.5)}x ATR")
    ot = [l for l in todas if "openTrades" in l]
    if ot:
        print(f"  Monitor OANDA: {V('activo')} — ultima consulta {hora_log(ot[-1])}")
    trades = leer_json(TRADES, [])
    tpnl   = [t for t in trades if "pnl" in t]
    if tpnl:
        wins = sum(1 for t in tpnl if t.get("pnl",0) > 0)
        pnl  = sum(t.get("pnl",0) for t in tpnl)
        wr   = wins / len(tpnl)
        wrc  = V(f"{wr:.1%}") if wr >= 0.5 else R(f"{wr:.1%}")
        pnlc = V(f"${pnl:+.4f}") if pnl >= 0 else R(f"${pnl:+.4f}")
        print(f"  Trades: {len(tpnl)} | WR: {wrc} | PnL: {pnlc}")
        for t in tpnl[-3:]:
            e = "✅" if t.get("pnl",0)>0 else "❌"
            print(f"    {e} {t.get('par','?').replace('_','/'):<8} {t.get('estrategia','?'):<14} ${t.get('pnl',0):+.4f}")
    else:
        print(f"  {G('Sin trades ejecutados aun — esperando primera senal')}")

    # AUDIT
    print(f"\n{C('▌ AUDIT + TELEGRAM')}")
    tg = [l for l in todas if "getUpdates" in l and "200 OK" in l]
    print(f"  Telegram: {V('ACTIVO ✓') if tg else R('SIN CONEXION')}")
    cal = p.get("calibrado_en","N/A")
    if cal != "N/A": cal = cal[:16].replace("T"," ")
    print(f"  Ultima calibracion: {cal}")
    dia = datetime.utcnow().weekday()
    if dia == 5: print(f"  {V('HOY SABADO — analisis semanal programado 00:00 UTC')}")
    elif dia == 6: print(f"  {V('HOY DOMINGO — mercado abre 22:00 UTC')}")

    # LOGS
    print(f"\n{C('▌ ACTIVIDAD RECIENTE')}")
    rel = sin_spam(list(todas))[-12:]
    for linea in rel:
        col_fn, tipo = colorear(linea)
        h = hora_log(linea)
        c = linea[24:] if len(linea)>24 else linea
        print(f"  {G(h)} {col_fn(tipo)}  {col_fn(c[:80])}")

    print(f"\n{G('─'*72)}")
    print(f"  {G('Ctrl+C salir | actualiza 10s | --logs para tiempo real | --senales para solo senales')}")


# ── MODO 2: LOGS EN TIEMPO REAL ───────────────────────────────────────────────

def modo_logs():
    global _logpos
    cargar_log()
    _logpos = len(list(_buf))

    cls()
    print(N("═"*72))
    print(N("  TRADING BOT v11 — LOGS EN TIEMPO REAL  (Ctrl+C para salir)"))
    print(N("═"*72))

    # Mostrar ultimas 20 relevantes
    rel = sin_spam(list(_buf))[-20:]
    for l in rel:
        col_fn, tipo = colorear(l)
        h = hora_log(l)
        c = l[24:] if len(l)>24 else l
        print(f"  {G(h)} {col_fn(tipo)}  {col_fn(c[:85])}")

    print(f"\n  {C('─── escuchando nuevos eventos ───')}\n")

    while True:
        try:
            ns = nuevas()
            for l in ns:
                if any(s in l for s in SPAM): continue
                col_fn, tipo = colorear(l)
                h = hora_log(l)
                c = l[24:] if len(l)>24 else l
                lo = l.lower()
                if "error" in lo:
                    print(f"  {R(h)} {R(tipo)}  {R(c[:85])}")
                elif "senal" in lo or "señal" in lo:
                    print(f"\n  {V('★')} {V(h)} {V(tipo)}  {N(V(c[:85]))}")
                    print(f"  {V('─'*68)}\n")
                elif "ordercreate" in lo or "orden" in lo:
                    print(f"\n  {C('◆')} {C(h)} {C(tipo)}  {N(C(c[:85]))}")
                    print(f"  {C('─'*68)}\n")
                elif "opentrades" in l:
                    print(f"  {G(h)} {G('CHK')}  {G('OANDA → openTrades OK')}")
                elif "warning" in lo:
                    print(f"  {A(h)} {A(tipo)}  {A(c[:85])}")
                else:
                    print(f"  {G(h)} {col_fn(tipo)}  {col_fn(c[:85])}")
            time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{V('Monitor detenido.')}")
            break


# ── MODO 3: SOLO SENALES ──────────────────────────────────────────────────────

def modo_senales():
    cargar_log()
    cls()
    print(N("═"*72))
    print(N("  TRADING BOT v11 — SEÑALES Y ORDENES  (Ctrl+C para salir)"))
    print(N("═"*72))
    print(f"  {G('Silenciando todo excepto senales, ordenes y cierres')}\n")

    KEYS = ["SEÑAL","SENAL","ORDEN","ORDERCREATE","CIERRE","GANADORA","PERDEDORA"]
    hist = [l for l in list(_buf) if any(k in l.upper() for k in KEYS)]
    if hist:
        print(f"  {C('Registros previos:')}")
        for l in hist[-8:]:
            print(f"  {V(hora_log(l))}  {V(l[24:80] if len(l)>24 else l)}")
    else:
        print(f"  {G('Sin senales previas en el log')}")

    print(f"\n  {C('─── escuchando ───')}\n")
    vistos = set()

    while True:
        try:
            ns     = nuevas()
            trades = leer_json(TRADES, [])

            for t in trades:
                tid = t.get("trade_id","")
                if tid and tid not in vistos:
                    vistos.add(tid)
                    par  = t.get("par","?").replace("_","/")
                    dir_ = t.get("dir","?").upper()
                    st   = t.get("estrategia","?")
                    en   = t.get("entry",0)
                    pnl  = t.get("pnl",None)
                    ts   = str(t.get("opened_at",""))[:16]
                    if pnl is not None:
                        e = "✅" if pnl > 0 else "❌"
                        print(f"\n  {e} CIERRE  {par} {dir_} | {st} | PnL: ${pnl:+.4f}")
                    else:
                        print(f"\n  {C('◆')} APERTURA  {par} {dir_} @ {en:.5f} | {st} | {ts}")

            for l in ns:
                if any(k in l.upper() for k in KEYS):
                    print(f"  {V('★')} {V(hora_log(l))}  {N(V(l[24:80] if len(l)>24 else l))}")

            time.sleep(2)
        except KeyboardInterrupt:
            print(f"\n{V('Monitor detenido.')}")
            break


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    modo = "dashboard"
    if len(sys.argv) > 1:
        a = sys.argv[1].lower()
        if a in ["--logs", "-l"]:   modo = "logs"
        elif a in ["--senales", "--señales", "-s"]: modo = "senales"

    if modo == "logs":
        modo_logs()
    elif modo == "senales":
        modo_senales()
    else:
        cargar_log()
        while True:
            try:
                dashboard()
                time.sleep(10)
                nuevas()
            except KeyboardInterrupt:
                print(f"\n{V('Monitor detenido.')}")
                break
            except Exception as e:
                print(f"\n{R(f'Error: {e}')}")
                time.sleep(5)
