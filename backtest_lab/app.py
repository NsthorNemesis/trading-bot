"""
app.py — Backtest Lab Web App
==============================
Lanza en: python app.py  →  http://localhost:5050
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

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
    py_files = [f for f in BOT_ROOT.rglob("*.py")
                if "venv" not in str(f) and "__pycache__" not in str(f)
                and "backups" not in str(f) and "backtest_lab" not in str(f)]
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

    # Bug hot-reload en VPS (strategies vacías)
    out2, _ = _ssh(
        "grep -c 'Estrategias cargadas: \\[\\]' "
        "/root/trading_bot_v11/logs/trading_bot.log 2>/dev/null || echo 0"
    )
    try:
        n = int(out2.strip().split()[0])
        if n > 0:
            res.append(_chk(3, "Bug hot-reload en VPS", "fail",
                f"Detectados {n} reload(s) con strategies vacías en el log — "
                "bot lleva tiempo sin generar señales",
                "Reiniciar: systemctl restart trading_bot"))
        else:
            res.append(_chk(3, "Bug hot-reload en VPS", "pass",
                "No se detectaron reloads con strategies vacías"))
    except:
        res.append(_chk(3, "Bug hot-reload en VPS", "warn",
            f"No se pudo verificar: {out2}"))

    # Errores en log
    out3, _ = _ssh(
        "grep -c '\\[ERROR\\]' /root/trading_bot_v11/logs/trading_bot.log 2>/dev/null || echo 0"
    )
    try:
        n = int(out3.strip().split()[0])
        status = "pass" if n == 0 else ("warn" if n < 20 else "fail")
        res.append(_chk(3, "Errores en log VPS", status,
            f"{n} líneas de ERROR en el log total"))
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
    elif not vps_commit or "error" in vps_commit.lower():
        res.append(_chk(3, "Git sincronizado local↔VPS", "warn",
            "No se pudo leer el commit del VPS"))
    else:
        res.append(_chk(3, "Git sincronizado local↔VPS", "warn",
            f"Local: {local_commit} | VPS: {vps_commit} — desincronizados",
            "ssh VPS + git fetch origin && git reset --hard origin/master"))

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
    tg = env_vars.get("TELEGRAM_TOKEN","")
    if len(tg) > 10:
        res.append(_chk(4, "Telegram token", "pass",
            f"Token configurado ({tg[:8]}***)"))
    else:
        res.append(_chk(4, "Telegram token", "warn",
            "TELEGRAM_TOKEN no encontrado — sin notificaciones"))

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


if __name__ == "__main__":
    init_db()
    print("\n" + "="*55)
    print("  BACKTEST LAB — Trading Bot v11")
    print("  http://localhost:5050")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
