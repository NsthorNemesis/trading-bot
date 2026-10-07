"""
backtest_semanal.py — Corre backtest_26w_v3.py y sube el resultado al VPS.
Diseñado para ejecutarse automáticamente cada domingo.
Uso manual: python scripts/backtest_semanal.py
"""
import json, subprocess, sys, pathlib, datetime

ROOT    = pathlib.Path(__file__).parent.parent
RESULT  = ROOT / "data" / "backtesting" / "backtest_semanal.json"
VPS     = "root@24.199.87.217"
VPS_DST = "/root/trading_bot_v11/data/backtesting/backtest_semanal.json"
SSH_KEY = pathlib.Path.home() / ".ssh" / "id_ed25519"

def run():
    params = json.loads((ROOT / "data" / "calibration" / "strategy_params.json").read_text())
    pares  = ",".join(params.get("pares_activos", ["EUR_USD","USD_CAD","AUD_USD"]))
    fecha  = datetime.datetime.now().strftime("%Y-%m-%d")

    print(f"[{fecha}] Corriendo backtest 26 semanas — pares: {pares}")

    # Usar datos _jun2.json si existen (más completos), si no los normales
    data_dir = ROOT / "data" / "historical"
    usa_full = all((data_dir / f"{p.replace(',','_')}_M15_jun2.json").exists()
                   for p in pares.split(","))

    script = ROOT / "scripts" / "backtest_26w_v3.py"
    if usa_full:
        # Parchar temporalmente para usar _full
        tmp = ROOT / "scripts" / "_bt_v3_tmp.py"
        tmp.write_text(script.read_text().replace("_M15.json", "_M15_jun2.json"))
        script_run = tmp
    else:
        script_run = script

    try:
        r = subprocess.run(
            [sys.executable, str(script_run),
             "--semanas", "26", "--pares", pares,
             "--output", str(RESULT)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300
        )
        if r.returncode != 0:
            print("ERROR en backtest:\n", r.stderr[-500:])
            return False
        print(r.stdout[-800:])
    finally:
        if usa_full and tmp.exists():
            tmp.unlink()

    # Agregar fecha al resultado
    if RESULT.exists():
        data = json.loads(RESULT.read_text())
        data["fecha_ejecucion"] = fecha
        RESULT.write_text(json.dumps(data, indent=2, default=str))
        print(f"Resultado guardado en {RESULT}")

    # Subir al VPS
    if SSH_KEY.exists():
        print("Subiendo resultado al VPS...")
        r2 = subprocess.run(
            ["scp", "-i", str(SSH_KEY), str(RESULT), f"{VPS}:{VPS_DST}"],
            capture_output=True, text=True
        )
        if r2.returncode == 0:
            print("✓ Resultado subido al VPS")
        else:
            print(f"⚠ No se pudo subir al VPS: {r2.stderr}")
    else:
        print(f"⚠ SSH key no encontrada en {SSH_KEY} — resultado solo local")

    return True

if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
