"""
app.py — Backtest Lab Web App
==============================
Lanza en: python app.py  →  http://localhost:5050
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for, Response, stream_with_context

from database import get_candles, get_coverage, get_db_size_mb, init_db
from downloader import descargar_oanda, importar_carpeta_histdata, importar_histdata_csv
from engine import BacktestConfig, run_backtest

app = Flask(__name__)
ROOT = Path(__file__).parent

PARES_DISPONIBLES = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
    "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP",
]
ESTRATEGIAS_DISPONIBLES = [
    "RSI_Bollinger", "EMA_Crossover", "RSI_Divergence", "Hammer", "Doji",
]
SESIONES_DISPONIBLES = ["london", "overlap", "new_york"]


# ── Inicialización ─────────────────────────────────────────────────────────────

@app.before_request
def _init():
    init_db()


# ── Rutas principales ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    coverage = get_coverage()
    db_size  = get_db_size_mb()
    return render_template(
        "index.html",
        pares=PARES_DISPONIBLES,
        estrategias=ESTRATEGIAS_DISPONIBLES,
        sesiones=SESIONES_DISPONIBLES,
        coverage=coverage,
        db_size=db_size,
    )


@app.route("/run", methods=["POST"])
def run():
    """Ejecuta el backtest con los parámetros del formulario."""
    form = request.form

    # Parámetros del formulario
    pares_sel     = form.getlist("pares")         or ["EUR_USD", "AUD_USD"]
    estrats_sel   = form.getlist("estrategias")   or ["RSI_Bollinger"]
    sesiones_sel  = form.getlist("sesiones")      or ["london", "new_york"]
    fecha_inicio  = form.get("fecha_inicio", "2023-01-01")
    fecha_fin     = form.get("fecha_fin",    "2024-12-31")
    capital_ini   = float(form.get("capital",     200))
    riesgo_pct    = float(form.get("riesgo",      1.5)) / 100
    sl_atr_mult   = float(form.get("sl_mult",     2.0))
    rr_ratio      = float(form.get("rr",          2.0))
    cooldown_m    = int(form.get("cooldown",       240))
    max_pos       = int(form.get("max_pos",          6))
    max_pos_par   = int(form.get("max_pos_par",      2))
    min_conf      = float(form.get("min_conf",    0.40))
    usar_ds       = form.get("usar_ds") == "on"

    start = datetime.fromisoformat(fecha_inicio).replace(tzinfo=timezone.utc)
    end   = datetime.fromisoformat(fecha_fin).replace(hour=23, minute=59, tzinfo=timezone.utc)

    # Cargar datos desde DuckDB
    dfs = {}
    for par in pares_sel:
        df = get_candles(par, start, end, "M15")
        if not df.empty:
            dfs[par] = df

    if not dfs:
        return render_template("results.html",
            error="Sin datos para el período seleccionado. Importa datos primero.",
            pares=pares_sel, fecha_inicio=fecha_inicio, fecha_fin=fecha_fin,
        )

    cfg = BacktestConfig(
        capital_ini   = capital_ini,
        riesgo_pct    = riesgo_pct,
        sl_atr_mult   = sl_atr_mult,
        rr_ratio      = rr_ratio,
        cooldown_mins = cooldown_m,
        max_pos       = max_pos,
        max_pos_par   = max_pos_par,
        min_conf      = min_conf,
        sesiones      = set(sesiones_sel),
        estrategias   = set(estrats_sel),
        usar_ds       = usar_ds,
        seed          = 42,
    )

    result = run_backtest(dfs, cfg, start, end)
    result["fecha_inicio"] = fecha_inicio
    result["fecha_fin"]    = fecha_fin
    result["pares_sel"]    = pares_sel
    result["estrats_sel"]  = estrats_sel

    # Equity curve para Chart.js
    equity_labels = [p["ts"] or fecha_inicio for p in result["equity_curve"]]
    equity_values = [p["capital"] for p in result["equity_curve"]]

    return render_template(
        "results.html",
        r          = result,
        eq_labels  = json.dumps(equity_labels),
        eq_values  = json.dumps(equity_values),
        trades     = result["trades_log"][:500],   # máx 500 en tabla
        breakeven  = round(1 / (1 + rr_ratio) * 100, 1),
    )


# ── Datos ──────────────────────────────────────────────────────────────────────

@app.route("/datos")
def datos():
    coverage = get_coverage()
    db_size  = get_db_size_mb()
    return render_template("datos.html",
        coverage=coverage, db_size=db_size,
        pares=PARES_DISPONIBLES,
    )


@app.route("/api/descargar-oanda", methods=["POST"])
def api_descargar_oanda():
    data        = request.json or {}
    pares       = data.get("pares", ["EUR_USD", "AUD_USD"])
    token       = data.get("token") or os.getenv("OANDA_ACCESS_TOKEN", "")
    account_id  = data.get("account_id") or os.getenv("OANDA_ACCOUNT_ID", "")
    environment = data.get("environment", "practice")
    desde_str   = data.get("desde")
    hasta_str   = data.get("hasta")

    if not token:
        return jsonify({"ok": False, "error": "Token OANDA requerido"}), 400

    desde = datetime.fromisoformat(desde_str).replace(tzinfo=timezone.utc) if desde_str else None
    hasta = datetime.fromisoformat(hasta_str).replace(tzinfo=timezone.utc) if hasta_str else None

    try:
        res = descargar_oanda(pares, token, account_id, environment, desde, hasta, verbose=False)
        return jsonify({"ok": True, "resultado": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/importar-histdata", methods=["POST"])
def api_importar_histdata():
    if "archivo" not in request.files:
        return jsonify({"ok": False, "error": "No se recibió archivo"}), 400

    archivo  = request.files["archivo"]
    par      = request.form.get("par", "EUR_USD")
    tmp_path = ROOT / "data" / "uploads" / archivo.filename
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    archivo.save(tmp_path)

    try:
        n = importar_histdata_csv(tmp_path, par, verbose=False)
        tmp_path.unlink(missing_ok=True)
        return jsonify({"ok": True, "velas": n, "par": par})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/coverage")
def api_coverage():
    return jsonify(get_coverage())


# ── Health Check ───────────────────────────────────────────────────────────────

import py_compile, subprocess, sys as _sys, urllib.request as _urllib

BOT_ROOT = ROOT.parent
VPS_HOST = "root@24.199.87.217"

def _chk(level, name, status, detail="", fix=""):
    return {"level": level, "name": name, "status": status, "detail": detail, "fix": fix}

def _ssh(cmd, timeout=8):
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", VPS_HOST, cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return (r.stdout + r.stderr).strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "Timeout conectando al VPS", -1
    except Exception as e:
        return str(e), -1

def _nivel1():
    res = []

    # Sintaxis Python
    _EXCLUDE = {"venv", "__pycache__", "backups", "backtest_lab"}
    py_files = [f for f in BOT_ROOT.rglob("*.py")
                if not any(part in _EXCLUDE for part in f.parts)]
    errors = []
    for f in py_files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{f.relative_to(BOT_ROOT)}: {str(e)[:120]}")
    if errors:
        res.append(_chk(1, "Sintaxis Python", "fail",
            f"{len(errors)} archivo(s) con errores:\n" + "\n".join(errors),
            "Corregir los SyntaxError indicados y hacer git push"))
    else:
        res.append(_chk(1, "Sintaxis Python", "pass",
            f"{len(py_files)} archivos .py verificados — sin errores de sintaxis"))

    # Archivos críticos
    critical = {
        "config/settings.py":                       BOT_ROOT / "config" / "settings.py",
        ".env":                                      BOT_ROOT / ".env",
        "data/calibration/strategy_params.json":    BOT_ROOT / "data" / "calibration" / "strategy_params.json",
        "main.py":                                   BOT_ROOT / "main.py",
        "trading_bot.service":                       BOT_ROOT / "trading_bot.service",
    }
    missing = [name for name, path in critical.items() if not path.exists()]
    if missing:
        res.append(_chk(1, "Archivos críticos", "fail",
            "Faltan: " + ", ".join(missing),
            "Crear o commitear los archivos faltantes"))
    else:
        res.append(_chk(1, "Archivos críticos", "pass",
            "Todos los archivos críticos presentes (" + ", ".join(critical.keys()) + ")"))

    # strategy_params.json válido
    params_file = BOT_ROOT / "data" / "calibration" / "strategy_params.json"
    required = ["estrategias_activas","pares_activos","cooldown_minutes",
                "riesgo_pct","max_posiciones","rr_ratio"]
    try:
        params = json.loads(params_file.read_text(encoding="utf-8"))
        missing_f = [f for f in required if f not in params]
        if missing_f:
            res.append(_chk(1, "strategy_params.json", "warn",
                f"Campos faltantes: {missing_f}"))
        else:
            res.append(_chk(1, "strategy_params.json", "pass",
                f"Estrategias: {params['estrategias_activas']} | "
                f"Pares: {params['pares_activos']} | "
                f"Cooldown: {params['cooldown_minutes']}min | "
                f"Riesgo: {params['riesgo_pct']*100:.1f}%"))
    except Exception as e:
        res.append(_chk(1, "strategy_params.json", "fail", f"JSON inválido: {e}",
            "Verificar y corregir el JSON"))

    # Git status
    try:
        dirty = subprocess.run(["git","status","--short"], cwd=BOT_ROOT,
            capture_output=True, text=True, timeout=5).stdout.strip()
        commit = subprocess.run(["git","log","--oneline","-1"], cwd=BOT_ROOT,
            capture_output=True, text=True, timeout=5).stdout.strip()
        if dirty:
            res.append(_chk(1, "Git status", "warn",
                f"Cambios sin commitear:\n{dirty}\nÚltimo commit: {commit}",
                "git add + git commit + git push origin master"))
        else:
            res.append(_chk(1, "Git status", "pass", f"Working tree limpio | {commit}"))
    except Exception as e:
        res.append(_chk(1, "Git status", "warn", f"No se pudo verificar: {e}"))

    # ── v14: suscribir_señal presente en signal_agent ──────────────────────
    signal_file = BOT_ROOT / "agents" / "signal_agent" / "signal_agent.py"
    try:
        sig_src = signal_file.read_text(encoding="utf-8")
        if "def suscribir_señal" not in sig_src:
            res.append(_chk(1, "v14 — suscribir_señal", "fail",
                "El método suscribir_señal() NO existe en signal_agent.py.\n"
                "El risk_agent se suscribe en el arranque con self._signal.suscribir_señal(...)\n"
                "Sin este método: AttributeError silencioso → 0 trades ejecutados.",
                "Agregar def suscribir_señal(self, cb): self._suscriptores_senal.append(cb)"))
        else:
            # También verificar que el loop lo llama
            if "await cb(senal)" in sig_src or "await cb(" in sig_src:
                res.append(_chk(1, "v14 — suscribir_señal", "pass",
                    "suscribir_señal() presente y el loop notifica a los suscriptores"))
            else:
                res.append(_chk(1, "v14 — suscribir_señal", "warn",
                    "suscribir_señal() existe pero el loop run() no llama a los callbacks",
                    "Agregar 'for cb in self._suscriptores_senal: await cb(senal)' en run()"))
    except Exception as e:
        res.append(_chk(1, "v14 — suscribir_señal", "warn", f"No se pudo leer signal_agent: {e}"))

    # ── v14: arquitectura briefing (no tecnico/regimen separados) ──────────
    try:
        sig_src = sig_src if 'sig_src' in dir() else signal_file.read_text(encoding="utf-8")
        has_briefing  = "_agente_briefing" in sig_src
        has_old_tecnico = "_agente_tecnico" in sig_src
        has_old_regimen = "_agente_regimen" in sig_src
        has_reasoner  = "deepseek-reasoner" in sig_src or "MODEL_DEEP" in sig_src
        if not has_briefing:
            res.append(_chk(1, "v14 — arquitectura briefing", "fail",
                "_agente_briefing() no encontrado — signal_agent puede ser v13 o anterior",
                "Restaurar signal_agent.py desde backup checkpoint_v14_BASE_EXPANSION"))
        elif has_old_tecnico or has_old_regimen:
            res.append(_chk(1, "v14 — arquitectura briefing", "warn",
                f"_agente_briefing presente pero también quedan métodos v13: "
                f"{'_agente_tecnico ' if has_old_tecnico else ''}"
                f"{'_agente_regimen' if has_old_regimen else ''}"))
        else:
            reasoner_txt = " | decisor=deepseek-reasoner" if has_reasoner else " | ⚠ MODEL_DEEP no detectado"
            res.append(_chk(1, "v14 — arquitectura briefing", "pass",
                f"Arquitectura v14 correcta: _agente_briefing + _agente_decision{reasoner_txt}"))
    except Exception as e:
        res.append(_chk(1, "v14 — arquitectura briefing", "warn", f"No se pudo verificar: {e}"))

    return res

def _nivel2():
    res = []
    py = _sys.executable

    # Import de agentes
    script = f"""
import sys, os
sys.path.insert(0, r'{BOT_ROOT}')
os.chdir(r'{BOT_ROOT}')
errors = []
for a in ['agents.market_agent.market_agent','agents.signal_agent.signal_agent',
          'agents.risk_execution_agent.risk_execution_agent','agents.audit_agent.audit_agent']:
    try:
        __import__(a)
    except Exception as e:
        errors.append(f'{{a}}: {{type(e).__name__}}: {{e}}')
if errors:
    print('FAIL:' + '||'.join(errors))
else:
    print('PASS:todos los agentes importan correctamente')
"""
    try:
        r = subprocess.run([py, "-c", script], capture_output=True, text=True,
                           timeout=20, cwd=str(BOT_ROOT))
        out = (r.stdout + r.stderr).strip()
        if "PASS:" in out:
            res.append(_chk(2, "Import de agentes", "pass", out.split("PASS:",1)[1]))
        else:
            detail = out.split("FAIL:",1)[1] if "FAIL:" in out else out
            res.append(_chk(2, "Import de agentes", "fail",
                "\n".join(detail.split("||")),
                "Corregir SyntaxErrors — ver Nivel 1"))
    except Exception as e:
        res.append(_chk(2, "Import de agentes", "warn", f"No se pudo verificar: {e}"))

    # Carga de estrategias
    params_path = str(BOT_ROOT / "data" / "calibration" / "strategy_params.json")
    script2 = f"""
import sys, json
sys.path.insert(0, r'{BOT_ROOT}')
try:
    from strategies import load_strategies
    params = json.loads(open(r'{params_path}').read())
    activas = params.get('estrategias_activas', [])
    s = load_strategies(active_only=activas)
    keys = list(s.keys())
    if not keys:
        print(f'FAIL:load_strategies devolvió vacío con activas={{activas}}')
    else:
        print(f'PASS:{{keys}}')
except Exception as e:
    print(f'FAIL:{{type(e).__name__}}: {{e}}')
"""
    try:
        r = subprocess.run([py, "-c", script2], capture_output=True, text=True,
                           timeout=15, cwd=str(BOT_ROOT))
        out = (r.stdout + r.stderr).strip()
        if "PASS:" in out:
            res.append(_chk(2, "Carga de estrategias", "pass",
                f"Estrategias cargadas: {out.split('PASS:',1)[1]}"))
        else:
            detail = out.split("FAIL:",1)[1] if "FAIL:" in out else out
            res.append(_chk(2, "Carga de estrategias", "fail", detail,
                "Revisar strategies/__init__.py"))
    except Exception as e:
        res.append(_chk(2, "Carga de estrategias", "warn", f"No se pudo verificar: {e}"))

    # ── v14: MODEL_DEEP = deepseek-reasoner en settings ───────────────────
    settings_file = BOT_ROOT / "config" / "settings.py"
    try:
        cfg_src = settings_file.read_text(encoding="utf-8")
        if "deepseek-reasoner" in cfg_src:
            # Extraer la línea exacta
            for line in cfg_src.splitlines():
                if "deepseek-reasoner" in line:
                    res.append(_chk(2, "v14 — MODEL_DEEP (deepseek-reasoner)", "pass",
                        f"Configurado correctamente: {line.strip()}"))
                    break
        else:
            res.append(_chk(2, "v14 — MODEL_DEEP (deepseek-reasoner)", "fail",
                "MODEL_DEEP no apunta a 'deepseek-reasoner' en config/settings.py\n"
                "El decisor usará deepseek-chat en lugar de razonamiento profundo.",
                "Añadir MODEL_DEEP = 'deepseek-reasoner' en config/settings.py"))
    except Exception as e:
        res.append(_chk(2, "v14 — MODEL_DEEP (deepseek-reasoner)", "warn",
            f"No se pudo verificar settings.py: {e}"))

    # ── v14: canal señal→riesgo (_suscriptores_senal inicializado) ─────────
    try:
        sig_src_l = (BOT_ROOT / "agents" / "signal_agent" / "signal_agent.py"
                     ).read_text(encoding="utf-8")
        risk_src  = (BOT_ROOT / "agents" / "risk_execution_agent" /
                     "risk_execution_agent.py").read_text(encoding="utf-8")
        has_init   = "_suscriptores_senal" in sig_src_l
        has_method = "def suscribir_señal" in sig_src_l
        has_call   = "suscribir_señal" in risk_src
        if has_init and has_method and has_call:
            res.append(_chk(2, "v14 — canal señal→riesgo", "pass",
                "signal_agent inicializa _suscriptores_senal, expone suscribir_señal() "
                "y risk_execution_agent la llama al arranque"))
        else:
            missing = []
            if not has_init:   missing.append("_suscriptores_senal no inicializado en __init__")
            if not has_method: missing.append("def suscribir_señal() ausente")
            if not has_call:   missing.append("risk_agent no llama suscribir_señal()")
            res.append(_chk(2, "v14 — canal señal→riesgo", "fail",
                "El canal de señales está roto:\n" + "\n".join(missing),
                "Ver signal_agent.py — agregar suscribir_señal() y llamarla desde risk_agent"))
    except Exception as e:
        res.append(_chk(2, "v14 — canal señal→riesgo", "warn",
            f"No se pudo verificar: {e}"))

    # Simulación hot-reload (el bug de ayer)
    script3 = f"""
import sys, json
sys.path.insert(0, r'{BOT_ROOT}')
try:
    from strategies import load_strategies
    params = json.loads(open(r'{params_path}').read())
    activas = params.get('estrategias_activas', [])
    s1 = load_strategies(active_only=activas)
    s2 = load_strategies(active_only=activas)
    if not s2 and activas:
        print(f'FAIL:segunda carga devolvió vacío — bug de hot-reload activo')
    elif list(s1.keys()) == list(s2.keys()):
        print(f'PASS:reload consistente {{list(s2.keys())}}')
    else:
        print(f'WARN:carga1={{list(s1.keys())}} vs carga2={{list(s2.keys())}}')
except Exception as e:
    print(f'FAIL:{{e}}')
"""
    try:
        r = subprocess.run([py, "-c", script3], capture_output=True, text=True,
                           timeout=15, cwd=str(BOT_ROOT))
        out = (r.stdout + r.stderr).strip()
        if "PASS:" in out:
            res.append(_chk(2, "Simulación hot-reload", "pass", out.split("PASS:",1)[1]))
        elif "WARN:" in out:
            res.append(_chk(2, "Simulación hot-reload", "warn", out.split("WARN:",1)[1]))
        else:
            detail = out.split("FAIL:",1)[1] if "FAIL:" in out else out
            res.append(_chk(2, "Simulación hot-reload", "fail", detail,
                "Bug confirmado: strategies se vacía en reload — fix en signal_agent.py"))
    except Exception as e:
        res.append(_chk(2, "Simulación hot-reload", "warn", f"No se pudo verificar: {e}"))

    return res

def _nivel3():
    res = []

    # Estado del servicio
    out, code = _ssh("systemctl is-active trading_bot 2>&1 && "
                     "systemctl show trading_bot --no-pager "
                     "--property=ActiveState,MainPID,ExecMainStartTimestamp 2>&1 | head -4")
    if "active" in out and code == 0:
        res.append(_chk(3, "Servicio VPS (systemd)", "pass", out[:300]))
    elif code == -1:
        res.append(_chk(3, "Servicio VPS (systemd)", "warn",
            "No se pudo conectar al VPS: " + out[:200],
            "Verificar conectividad SSH: ssh root@" + VPS_HOST.split("@",1)[-1]))
    else:
        res.append(_chk(3, "Servicio VPS (systemd)", "fail", out[:300],
            "ssh root@24.199.87.217 'systemctl restart trading_bot'"))

    # Bug hot-reload en VPS (strategies vacías) — solo últimas 2000 líneas
    out2, _ = _ssh(
        "tail -2000 /root/trading_bot_v11/logs/trading_bot.log 2>/dev/null "
        "| grep -c 'Estrategias cargadas: \\[\\]' || echo 0"
    )
    try:
        n = int(out2.strip().split()[0])
        if n > 0:
            res.append(_chk(3, "Bug hot-reload en VPS", "fail",
                f"Detectados {n} reload(s) con strategies vacías en sesión reciente — "
                "bot sin señales activas",
                "Reiniciar: systemctl restart trading_bot"))
        else:
            res.append(_chk(3, "Bug hot-reload en VPS", "pass",
                "Sin reloads vacíos en sesión reciente — estrategias estables"))
    except:
        res.append(_chk(3, "Bug hot-reload en VPS", "warn",
            f"No se pudo verificar: {out2}"))

    # Errores en log — desde el último arranque, excluyendo errores externos normales
    out3, _ = _ssh(
        "awk '/SignalAgent v14/{count=0; in_s=1} "
        "in_s && /\\[ERROR\\]/ && !/telegram/ && !/Stream error/ && !/Response ended prematurely/ && !/oandapyV20/ && !/401/ && !/DeepSeek fall/ && !/tradeReduced/ && !/tradesClosed/ && !/DOCTYPE html/ && !/Unable to service/ && !/Fill: {}/ && !/asyncio: Task/{count++} "
        "END{print count+0}' "
        "/root/trading_bot_v11/logs/trading_bot.log 2>/dev/null || echo 0"
    )
    try:
        n = int(out3.strip().split()[0])
        status = "pass" if n == 0 else ("warn" if n < 5 else "fail")
        res.append(_chk(3, "Errores en log VPS", status,
            f"{n} errores de bot desde el último arranque (excluye errores Telegram/API externos)"))
    except:
        res.append(_chk(3, "Errores en log VPS", "warn", f"No se pudo contar: {out3}"))

    # Actividad reciente
    out4, _ = _ssh(
        "tail -200 /root/trading_bot_v11/logs/trading_bot.log | "
        "grep 'oandapyV20' | tail -1 | cut -c1-30"
    )
    if out4.strip():
        res.append(_chk(3, "Actividad reciente OANDA", "pass",
            f"Última llamada OANDA: {out4.strip()}"))
    else:
        res.append(_chk(3, "Actividad reciente OANDA", "fail",
            "No hay actividad reciente detectable",
            "Revisar logs del VPS manualmente"))

    # Git sync VPS vs local
    local_commit = subprocess.run(["git","log","--oneline","-1"], cwd=BOT_ROOT,
        capture_output=True, text=True, timeout=5).stdout.strip()[:10]
    out5, _ = _ssh("cd /root/trading_bot_v11 && git log --oneline -1 2>/dev/null | cut -c1-10")
    vps_commit = out5.strip()[:10]
    if local_commit and vps_commit and local_commit == vps_commit:
        res.append(_chk(3, "Git sincronizado local↔VPS", "pass",
            f"Ambos en commit {local_commit}"))
    elif not vps_commit or "error" in vps_commit.lower() or "timeout" in vps_commit.lower():
        res.append(_chk(3, "Git sincronizado local↔VPS", "warn",
            "No se pudo conectar al VPS para verificar — git sync desconocido"))
    else:
        res.append(_chk(3, "Git sincronizado local↔VPS", "warn",
            f"Local: {local_commit} | VPS: {vps_commit} — desincronizados",
            "ssh VPS + git fetch origin && git reset --hard origin/master"))

    # ── v14: SignalAgent v14 arrancó en el VPS ─────────────────────────────
    LOG = "/root/trading_bot_v11/logs/trading_bot.log"
    out6, _ = _ssh(f"grep 'SignalAgent v14' {LOG} 2>/dev/null | tail -1")
    if "v14" in out6 and "briefing=" in out6:
        res.append(_chk(3, "v14 — SignalAgent v14 en VPS", "pass", out6.strip()[:200]))
    elif out6.strip():
        res.append(_chk(3, "v14 — SignalAgent v14 en VPS", "warn",
            f"Línea v14 encontrada pero incompleta: {out6.strip()[:120]}"))
    else:
        res.append(_chk(3, "v14 — SignalAgent v14 en VPS", "fail",
            "No se encontró 'SignalAgent v14' en el log — el VPS puede estar corriendo v13 u anterior",
            "Ejecutar deploy_vps.ps1 para subir el signal_agent.py v14"))

    # ── v14: suscribir_señal registrada en VPS (sin AttributeError) ────────
    out7, _ = _ssh(
        f"grep -c 'suscribir_señal\\|AttributeError.*suscrib' {LOG} 2>/dev/null || echo 0"
    )
    err_out, _ = _ssh(
        f"grep 'AttributeError.*suscrib\\|has no attribute.*suscrib' {LOG} 2>/dev/null | tail -3"
    )
    if err_out.strip():
        res.append(_chk(3, "v14 — sin AttributeError suscribir_señal", "fail",
            f"¡Detectado el bug! risk_agent falló al suscribirse:\n{err_out.strip()[:300]}\n"
            "→ 0 trades ejecutados aunque el bot esté corriendo",
            "Desplegar signal_agent.py con suscribir_señal() y reiniciar"))
    else:
        res.append(_chk(3, "v14 — sin AttributeError suscribir_señal", "pass",
            "Sin errores de suscripción en el log — canal señal→riesgo operativo"))

    # ── v14: actividad del decisor (deepseek-reasoner) ─────────────────────
    out8, _ = _ssh(f"grep 'Decisor-R1' {LOG} 2>/dev/null | tail -2")
    if out8.strip():
        last_line = out8.strip().splitlines()[-1]
        res.append(_chk(3, "v14 — Decisor-R1 (deepseek-reasoner)", "pass",
            f"Última actividad del reasoner:\n{last_line.strip()[:200]}"))
    else:
        out8b, _ = _ssh(f"grep 'agente_decision\\|_agente_briefing\\|briefing.*riesgo' {LOG} 2>/dev/null | tail -1")
        if out8b.strip():
            res.append(_chk(3, "v14 — Decisor-R1 (deepseek-reasoner)", "warn",
                f"Sin líneas 'Decisor-R1' aún — bot activo pero fuera de sesión o ADX bajo\n{out8b.strip()[:160]}"))
        else:
            res.append(_chk(3, "v14 — Decisor-R1 (deepseek-reasoner)", "warn",
                "Sin actividad del decisor en el log — esperar apertura de sesión London (07:00 UTC)"))

    # ── v14: sesión activa según strategy_params.json en VPS ───────────────
    out9, _ = _ssh(
        "python3 -c \""
        "import json; p=json.load(open('/root/trading_bot_v11/data/calibration/strategy_params.json'));"
        "print('activas:', p.get('sesiones_activas',[])); "
        "print('timeout:', p.get('max_trade_hours','?'), 'h'); "
        "print('adx_min:', p.get('adx_min_operar','?'))"
        "\" 2>&1"
    )
    if "activas:" in out9:
        res.append(_chk(3, "v14 — strategy_params.json en VPS", "pass", out9.strip()))
    else:
        res.append(_chk(3, "v14 — strategy_params.json en VPS", "warn",
            f"No se pudo leer params del VPS: {out9[:120]}",
            "Verificar que data/calibration/strategy_params.json existe en el VPS"))

    return res

def _nivel4():
    res = []
    env_path = BOT_ROOT / ".env"
    env_vars = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip()

    # OANDA API
    token   = env_vars.get("OANDA_ACCESS_TOKEN","")
    account = env_vars.get("OANDA_ACCOUNT_ID","")
    if not token:
        res.append(_chk(4, "OANDA API", "warn",
            "OANDA_ACCESS_TOKEN no encontrado en .env",
            "Añadir OANDA_ACCESS_TOKEN al archivo .env"))
    else:
        try:
            req = _urllib.Request(
                f"https://api-fxpractice.oanda.com/v3/accounts/{account}",
                headers={"Authorization": f"Bearer {token}"}
            )
            with _urllib.urlopen(req, timeout=6) as resp:
                res.append(_chk(4, "OANDA API", "pass",
                    f"Conectado — cuenta {account} — HTTP {resp.status}"))
        except Exception as e:
            res.append(_chk(4, "OANDA API", "fail", f"Error: {e}",
                "Verificar OANDA_ACCESS_TOKEN en .env"))

    # DeepSeek key
    ds_key = env_vars.get("DEEPSEEK_API_KEY","")
    if len(ds_key) > 10:
        res.append(_chk(4, "DeepSeek API key", "pass",
            f"Clave configurada ({ds_key[:6]}***)"))
    else:
        res.append(_chk(4, "DeepSeek API key", "warn",
            "DEEPSEEK_API_KEY no encontrado o vacío en .env",
            "El bot opera solo con indicadores — sin filtro IA"))

    # Telegram
    tg = env_vars.get("TELEGRAM_BOT_TOKEN","")
    if len(tg) > 10:
        res.append(_chk(4, "Telegram token", "pass",
            f"Token configurado ({tg[:8]}***)"))
    else:
        res.append(_chk(4, "Telegram token", "warn",
            "TELEGRAM_BOT_TOKEN no encontrado en .env — sin notificaciones"))

    return res

@app.route("/health")
def health_page():
    return render_template("health.html")

@app.route("/health/run")
def health_run():
    checks = _nivel1() + _nivel2() + _nivel3() + _nivel4()
    passed = sum(1 for c in checks if c["status"] == "pass")
    warned = sum(1 for c in checks if c["status"] == "warn")
    failed = sum(1 for c in checks if c["status"] == "fail")
    overall = "fail" if failed else ("warn" if warned else "pass")
    return jsonify({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "overall": overall,
        "summary": {"passed": passed, "warned": warned, "failed": failed},
        "checks": checks
    })


# ── Monitor en Vivo ────────────────────────────────────────────────────────────
import re as _re

def _parse_log_ts(line):
    m = _re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line or "")
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    return None

def _time_ago(ts):
    if ts is None:
        return "—"
    secs = int((datetime.utcnow() - ts).total_seconds())
    if secs < 0:    return "ahora"
    if secs < 60:   return f"hace {secs}s"
    if secs < 3600: return f"hace {secs//60}m {secs%60}s"
    if secs < 86400:return f"hace {secs//3600}h {(secs%3600)//60}m"
    return f"hace {secs//86400}d"

def _monitor_fetch():
    LOG = "/root/trading_bot_v11/logs/trading_bot.log"
    cmd = (
        "echo '|||SVC|||'; systemctl is-active trading_bot 2>&1; "
        f"echo '|||STRAT|||'; grep 'Estrategias cargadas' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||WATCH|||'; grep 'monitoreando strategy_params' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||OANDA|||'; grep 'oandapyV20' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||CICLO|||'; grep 'Parametros:' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||TRADE|||'; grep 'TRADE OK\\|TP.*PnL\\|cerrado.*TP\\|cerrado.*SL' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||ERR|||'; tail -200 {LOG} 2>/dev/null | grep '\\[ERROR\\]' | grep -cv 'telegram' || echo 0; "
        f"echo '|||DS|||'; grep -i 'deepseek\\|DeepSeek' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||V14|||'; grep 'SignalAgent v14' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||BRIEFING|||'; grep 'lanzando briefing\\|AgenteBriefing\\|_agente_briefing' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||REASONER|||'; grep 'Decisor-R1' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||SESION|||'; grep 'sesion_abierta\\|Apertura sesion\\|sesion.*london\\|sesion.*overlap' {LOG} 2>/dev/null | tail -1; "
        f"echo '|||SUSCRIP|||'; grep -c 'AttributeError.*suscrib\\|has no attribute.*suscrib' {LOG} 2>/dev/null || echo 0"
    )
    raw, rc = _ssh(cmd, timeout=12)

    sections = {}
    current = "_pre"
    for line in raw.splitlines():
        if line.startswith("|||") and line.endswith("|||"):
            current = line.strip("|")
            sections[current] = ""
        else:
            sections[current] = (sections.get(current, "") + "\n" + line).strip()

    items = []

    # 1. Servicio
    svc = sections.get("SVC", "").strip()
    ok  = "active" in svc and rc != -1
    items.append({"label": "Servicio activo", "icon": "bi-cpu-fill",
                  "status": "ok" if ok else ("timeout" if rc == -1 else "error"),
                  "detail": svc or "No se pudo conectar al VPS",
                  "ago": ""})

    # 2. Estrategias
    strat = sections.get("STRAT", "").strip()
    ts    = _parse_log_ts(strat)
    m     = _re.search(r"Estrategias cargadas: (.+)$", strat)
    strat_val = m.group(1) if m else (strat[-80:] if strat else "Sin datos")
    items.append({"label": "Estrategias cargadas", "icon": "bi-puzzle-fill",
                  "status": "ok" if strat and "[]" not in strat else "error",
                  "detail": strat_val, "ago": _time_ago(ts)})

    # 3. ParamsWatcher
    watch = sections.get("WATCH", "").strip()
    ts    = _parse_log_ts(watch)
    items.append({"label": "ParamsWatcher activo", "icon": "bi-eye-fill",
                  "status": "ok" if watch else "warn",
                  "detail": watch[-90:] if watch else "No detectado en el log",
                  "ago": _time_ago(ts)})

    # 4. OANDA
    oanda = sections.get("OANDA", "").strip()
    ts    = _parse_log_ts(oanda)
    ago_s = int((datetime.utcnow() - ts).total_seconds()) if ts else 9999
    oanda_d = _re.sub(r'.*performing request ', '', oanda)[:80] if oanda else "Sin datos"
    items.append({"label": "OANDA — último ping", "icon": "bi-broadcast",
                  "status": "ok" if ts and ago_s < 300 else ("warn" if ts and ago_s < 900 else "error"),
                  "detail": oanda_d, "ago": _time_ago(ts)})

    # 5. Ciclo señales
    ciclo = sections.get("CICLO", "").strip()
    ts    = _parse_log_ts(ciclo)
    ago_s = int((datetime.utcnow() - ts).total_seconds()) if ts else 9999
    ciclo_d = _re.sub(r'.*Parametros: ', '', ciclo)[:90] if ciclo else "Sin datos"
    items.append({"label": "Ciclo de señales", "icon": "bi-graph-up-arrow",
                  "status": "ok" if ts and ago_s < 1800 else ("warn" if ts and ago_s < 3600 else "error"),
                  "detail": ciclo_d, "ago": _time_ago(ts)})

    # 6. Último trade
    trade = sections.get("TRADE", "").strip()
    ts    = _parse_log_ts(trade)
    items.append({"label": "Último trade ejecutado", "icon": "bi-arrow-left-right",
                  "status": "ok" if trade else "neutral",
                  "detail": trade[-90:] if trade else "Ninguno en el log actual",
                  "ago": _time_ago(ts) if ts else "—"})

    # 7. Errores recientes
    try:
        n_err = int(sections.get("ERR", "0").strip().split()[0])
    except Exception:
        n_err = -1
    items.append({"label": "Errores recientes (bot)", "icon": "bi-shield-exclamation",
                  "status": "ok" if n_err == 0 else ("warn" if n_err < 5 else "error"),
                  "detail": f"{n_err} errores no-Telegram en últimas 200 líneas" if n_err >= 0 else "No se pudo leer",
                  "ago": ""})

    # 8. DeepSeek
    ds    = sections.get("DS", "").strip()
    ts    = _parse_log_ts(ds)
    items.append({"label": "DeepSeek", "icon": "bi-robot",
                  "status": "ok" if ds else "neutral",
                  "detail": ds[-90:] if ds else "Sin llamadas recientes en el log",
                  "ago": _time_ago(ts) if ts else "—"})

    # ── Items v14 ──────────────────────────────────────────────────────────

    # 9. SignalAgent v14 arrancó
    v14   = sections.get("V14", "").strip()
    ts    = _parse_log_ts(v14)
    items.append({"label": "v14 — SignalAgent arrancado", "icon": "bi-cpu",
                  "status": "ok" if ("briefing=" in v14 and "decisor=" in v14) else ("warn" if v14 else "error"),
                  "detail": v14[-110:] if v14 else "No se encontró 'SignalAgent v14' — puede ser v13 en el VPS",
                  "ago": _time_ago(ts) if ts else "—"})

    # 10. Briefing agent activo
    brf   = sections.get("BRIEFING", "").strip()
    ts    = _parse_log_ts(brf)
    ago_s = int((datetime.utcnow() - ts).total_seconds()) if ts else 9999
    items.append({"label": "v14 — AgenteBriefing (último ciclo)", "icon": "bi-chat-dots",
                  "status": "ok" if (ts and ago_s < 3600) else ("warn" if (ts and ago_s < 86400) else "neutral"),
                  "detail": brf[-110:] if brf else "Sin actividad de briefing — normal fuera de sesión London/Overlap",
                  "ago": _time_ago(ts) if ts else "—"})

    # 11. Decisor-R1 (deepseek-reasoner)
    r1    = sections.get("REASONER", "").strip()
    ts    = _parse_log_ts(r1)
    ago_s = int((datetime.utcnow() - ts).total_seconds()) if ts else 9999
    items.append({"label": "v14 — Decisor-R1 (deepseek-reasoner)", "icon": "bi-diagram-3",
                  "status": "ok" if (ts and ago_s < 86400) else "neutral",
                  "detail": r1[-110:] if r1 else "Sin decisiones de reasoner — normal fuera de sesión",
                  "ago": _time_ago(ts) if ts else "—"})

    # 12. Bug suscribir_señal — detección automática
    try:
        n_suscrib_err = int(sections.get("SUSCRIP", "0").strip().split()[0])
    except Exception:
        n_suscrib_err = -1
    if n_suscrib_err > 0:
        items.append({"label": "v14 — Bug suscribir_señal", "icon": "bi-exclamation-octagon-fill",
                      "status": "error",
                      "detail": f"¡CRÍTICO! Detectados {n_suscrib_err} AttributeError de suscribir_señal.\n"
                                "El risk_agent nunca recibió señales → 0 trades ejecutados.\n"
                                "Fix: desplegar signal_agent.py con suscribir_señal() y reiniciar.",
                      "ago": ""})
    else:
        items.append({"label": "v14 — Bug suscribir_señal", "icon": "bi-shield-check",
                      "status": "ok" if n_suscrib_err == 0 else "warn",
                      "detail": "Sin AttributeError de suscripción — canal señal→riesgo operativo",
                      "ago": ""})

    return items

@app.route("/monitor")
def monitor_page():
    return render_template("monitor.html")

@app.route("/monitor/data")
def monitor_data():
    items = _monitor_fetch()
    n_ok  = sum(1 for i in items if i["status"] == "ok")
    n_err = sum(1 for i in items if i["status"] == "error")
    return jsonify({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "items": items, "n_ok": n_ok, "n_err": n_err
    })


# ── Harness (Simulación con Agentes Reales) ────────────────────────────────────

@app.route("/harness")
def harness_page():
    return render_template("harness.html")

@app.route("/harness/run")
def harness_run():
    semanas     = request.args.get("semanas",     "4")
    capital     = request.args.get("capital",     "200")
    par         = request.args.get("par",         "").strip()
    estrategias = request.args.get("estrategias", "").strip()
    sesiones    = request.args.get("sesiones",    "").strip()
    riesgo_pct  = request.args.get("riesgo_pct",  "").strip()
    cooldown    = request.args.get("cooldown",    "").strip()
    adx_min     = request.args.get("adx_min",    "").strip()
    sl_atr_mult = request.args.get("sl_atr_mult","").strip()

    cmd = [_sys.executable, str(BOT_ROOT / "backtest_harness.py"),
           "--semanas", semanas, "--capital", capital]
    if par:          cmd += ["--par",         par]
    if estrategias:  cmd += ["--estrategias", estrategias]
    if sesiones:     cmd += ["--sesiones",    sesiones]
    if riesgo_pct:   cmd += ["--riesgo_pct",  riesgo_pct]
    if cooldown:     cmd += ["--cooldown",    cooldown]
    if adx_min:      cmd += ["--adx_min",     adx_min]
    if sl_atr_mult:  cmd += ["--sl_atr_mult", sl_atr_mult]

    def generate():
        cmd_str = " ".join(cmd[2:])
        yield f"data: {json.dumps({'line': 'Iniciando harness: ' + cmd_str})}\n\n"
        try:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(BOT_ROOT), bufsize=1,
                encoding="utf-8", errors="replace", env=env
            )
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if line:
                    yield f"data: {json.dumps({'line': line})}\n\n"
            proc.wait()
            # Intentar leer resultado JSON
            result_file = BOT_ROOT / "data" / "backtesting" / "backtest_real_resultado.json"
            result = {}
            if result_file.exists():
                try:
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            yield f"data: {json.dumps({'done': True, 'code': proc.returncode, 'result': result})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    )


if __name__ == "__main__":
    init_db()
    print("\n" + "="*55)
    print("  BACKTEST LAB — Trading Bot v11")
    print("  http://localhost:5050")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
