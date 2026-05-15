#!/usr/bin/env bash
# ============================================================
# deploy_sabado.sh — Deploy semanal 2026-05-17
# Config óptima: Top4|EUR+AUD|SL2× (WR=38%, ROI/DD=3.69)
#
# Ejecutar en VPS:  bash scripts/deploy_sabado.sh
# ============================================================
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$BOT_DIR/logs/deploy_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$BOT_DIR/logs"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== DEPLOY SEMANAL 2026-05-17 ==="
log "Directorio: $BOT_DIR"

# ── 1. RECALIBRACIÓN (dry-run) ─────────────────────────────────────────────────
log ""
log "── PASO 1: Recalibración semanal (dry-run) ──"
cd "$BOT_DIR"

if python scripts/recalibrar_semanal.py --dry-run 2>&1 | tee -a "$LOG"; then
    log "✅ Dry-run OK"
else
    log "⚠  Dry-run con advertencias — revisar log antes de continuar"
    read -rp "¿Continuar de todas formas? [s/N] " resp
    [[ "$resp" =~ ^[sS]$ ]] || { log "Deploy cancelado."; exit 1; }
fi

# ── 2. RECALIBRACIÓN (real) ────────────────────────────────────────────────────
log ""
log "── PASO 2: Recalibración real ──"
python scripts/recalibrar_semanal.py 2>&1 | tee -a "$LOG"
log "✅ Recalibración completada"

# ── 3. VERIFICAR strategy_params.json ─────────────────────────────────────────
log ""
log "── PASO 3: Verificar configuración ──"
python - <<'PY' | tee -a "$LOG"
import json, sys
p = json.load(open("data/calibration/strategy_params.json"))
print(f"  Estrategias activas : {p['estrategias_activas']}")
print(f"  Pares activos       : {p['pares_activos']}")
print(f"  sl_atr_mult         : {p['sl_atr_mult']}")
print(f"  rsi_low/rsi_high    : {p.get('rsi_low',32)}/{p.get('rsi_high',68)}")
print(f"  rr_ratio            : {p['rr_ratio']}")
assert p['sl_atr_mult'] == 2.0,        "⛔ sl_atr_mult debe ser 2.0"
assert "EMA_Crossover" in p['estrategias_activas'], "⛔ EMA_Crossover debe estar activa"
assert "GBP_USD" not in p['pares_activos'], "⛔ GBP_USD debe estar pausado"
print("  ✅ Validación OK")
PY

# ── 4. GIT COMMIT + PUSH ───────────────────────────────────────────────────────
log ""
log "── PASO 4: Git commit & push ──"
git -C "$BOT_DIR" add \
    data/calibration/strategy_params.json \
    agents/risk_execution_agent/risk_execution_agent.py \
    strategies/ema_crossover.py \
    strategies/hammer.py \
    strategies/doji.py \
    agents/signal_agent/ \
    2>/dev/null || true

TIMESTAMP=$(date '+%Y-%m-%d')
git -C "$BOT_DIR" commit -m "deploy($TIMESTAMP): Top4|EUR+AUD|SL2× — WR=38% ROI/DD=3.69" \
    2>&1 | tee -a "$LOG" || log "Nada nuevo para commitear"

git -C "$BOT_DIR" push 2>&1 | tee -a "$LOG" || log "⚠  Push falló — verificar remote"

# ── 5. REINICIO DEL BOT ────────────────────────────────────────────────────────
log ""
log "── PASO 5: Reinicio del bot ──"

# Detener proceso actual
if pgrep -f "main.py" > /dev/null; then
    log "Deteniendo bot actual..."
    pkill -f "main.py" || true
    sleep 3
fi

# Arrancar con nohup
log "Arrancando bot..."
nohup python "$BOT_DIR/main.py" >> "$BOT_DIR/logs/bot.log" 2>&1 &
BOT_PID=$!
sleep 5

if ps -p "$BOT_PID" > /dev/null 2>&1; then
    log "✅ Bot arrancado — PID=$BOT_PID"
else
    log "⛔ Bot no arrancó — revisar logs/bot.log"
    tail -30 "$BOT_DIR/logs/bot.log" | tee -a "$LOG"
    exit 1
fi

# ── 6. HEALTHCHECK ────────────────────────────────────────────────────────────
log ""
log "── PASO 6: Healthcheck (60 s) ──"
sleep 60

ERRORS=$(grep -c "ERROR\|CRITICAL\|Traceback" "$BOT_DIR/logs/bot.log" 2>/dev/null || echo 0)
if [[ "$ERRORS" -gt 0 ]]; then
    log "⚠  Se detectaron $ERRORS errores en bot.log — revisar:"
    grep -E "ERROR|CRITICAL|Traceback" "$BOT_DIR/logs/bot.log" | tail -10 | tee -a "$LOG"
else
    log "✅ Healthcheck OK — sin errores críticos"
fi

# ── 7. RESUMEN FINAL ──────────────────────────────────────────────────────────
log ""
log "══════════════════════════════════════════════════"
log "  DEPLOY COMPLETADO — $(date '+%Y-%m-%d %H:%M:%S')"
log "  Config: Top4|EUR+AUD|SL2×"
log "  Estrategias: RSI_Bollinger, EMA_Crossover, Hammer, Doji"
log "  Pares: EUR_USD, AUD_USD"
log "  SL mult: 2.0× ATR | RR: 2.0 | Riesgo: 1.5%"
log "  Log completo: $LOG"
log "══════════════════════════════════════════════════"
