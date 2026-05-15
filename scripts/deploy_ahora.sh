#!/usr/bin/env bash
# ============================================================
# deploy_ahora.sh — Deploy inmediato en VPS (paper trading)
# Config: EMA_Crossover + Hammer + Doji | EUR_USD + AUD_USD
# Ejecutar en VPS: bash ~/trading_bot_v11/scripts/deploy_ahora.sh
# ============================================================
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$BOT_DIR/logs/deploy_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$BOT_DIR/logs"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== DEPLOY INMEDIATO — $(date '+%Y-%m-%d %H:%M:%S') ==="
log "Config: EMA_Crossover + Hammer + Doji | EUR_USD + AUD_USD | SL=2.0x"

# ── 1. Git pull ────────────────────────────────────────────────
log ""
log "── Paso 1: git pull ──"
cd "$BOT_DIR"
git pull 2>&1 | tee -a "$LOG"
log "OK"

# ── 2. Verificar config ────────────────────────────────────────
log ""
log "── Paso 2: Verificar strategy_params.json ──"
python3 - <<'PY' | tee -a "$LOG"
import json, sys
p = json.load(open("data/calibration/strategy_params.json"))
print(f"  Estrategias activas : {p['estrategias_activas']}")
print(f"  Pares activos       : {p['pares_activos']}")
print(f"  sl_atr_mult         : {p['sl_atr_mult']}")
ok = True
if "EMA_Crossover" not in p["estrategias_activas"]: print("  ERROR: EMA_Crossover no activa"); ok=False
if "RSI_Bollinger" in p["estrategias_activas"]:     print("  ERROR: RSI_Bollinger debe estar pausada"); ok=False
if "GBP_USD" in p["pares_activos"]:                 print("  ERROR: GBP_USD debe estar pausado"); ok=False
if p["sl_atr_mult"] != 2.0:                         print("  ERROR: sl_atr_mult debe ser 2.0"); ok=False
if ok: print("  OK — config validada")
else: sys.exit(1)
PY

# ── 3. Detener bot actual ──────────────────────────────────────
log ""
log "── Paso 3: Detener bot actual ──"
if systemctl is-active --quiet trading_bot 2>/dev/null; then
    sudo systemctl stop trading_bot
    sleep 2
    log "Bot detenido via systemctl"
elif pgrep -f "main.py" > /dev/null 2>&1; then
    pkill -f "main.py" || true
    sleep 3
    log "Bot detenido via pkill"
else
    log "Bot no estaba corriendo"
fi

# ── 4. Arrancar con nueva config ───────────────────────────────
log ""
log "── Paso 4: Arrancar bot ──"
if systemctl list-unit-files trading_bot.service &>/dev/null; then
    sudo systemctl start trading_bot
    sleep 4
    if systemctl is-active --quiet trading_bot; then
        log "OK — bot arrancado via systemctl"
    else
        log "ERROR — fallo en systemctl start; revisar: journalctl -u trading_bot -n 30"
        exit 1
    fi
else
    nohup python3 "$BOT_DIR/main.py" >> "$BOT_DIR/logs/bot.log" 2>&1 &
    sleep 5
    if pgrep -f "main.py" > /dev/null; then
        log "OK — bot arrancado via nohup (PID=$(pgrep -f main.py))"
    else
        log "ERROR — bot no arrancó; revisar logs/bot.log"
        tail -20 "$BOT_DIR/logs/bot.log"
        exit 1
    fi
fi

# ── 5. Healthcheck rápido (30s) ────────────────────────────────
log ""
log "── Paso 5: Healthcheck (30s) ──"
sleep 30
ERRORS=$(grep -c "ERROR\|CRITICAL\|Traceback" "$BOT_DIR/logs/bot.log" 2>/dev/null || echo 0)
if [[ "$ERRORS" -gt 0 ]]; then
    log "ATENCION: $ERRORS errores en log — revisar:"
    grep -E "ERROR|CRITICAL|Traceback" "$BOT_DIR/logs/bot.log" | tail -10 | tee -a "$LOG"
else
    log "Healthcheck OK — sin errores criticos"
fi

# ── 6. Resumen ─────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════"
log "  DEPLOY COMPLETADO — $(date '+%H:%M:%S')"
log "  Estrategias: EMA_Crossover, Hammer, Doji"
log "  RSI_Bollinger: PAUSADA"
log "  Pares: EUR_USD, AUD_USD"
log "  SL: 2.0x ATR | RR: 2.0 | Riesgo: 1.5%"
log "  Log: $LOG"
log "═══════════════════════════════════════════════════"
