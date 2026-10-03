#!/usr/bin/env bash
set -euo pipefail
BOT_DIR="/root/trading_bot_v11"
cd "$BOT_DIR"
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Trading Bot v11 — Restart seguro               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "[1/4] Limpiando null bytes..."
python3 scripts/fix_nullbytes.py 2>/dev/null && echo "      ✓ OK" || echo "      ⚠ omitiendo"
echo "[2/4] Verificando sintaxis..."
ALL_OK=true
for f in agents/signal_agent/signal_agent.py agents/signal_agent/strategies.py agents/audit_agent/audit_agent.py agents/risk_execution_agent/risk_execution_agent.py webapp/main.py; do
    python3 -c "
import ast,sys
try:
    ast.parse(open('$f','rb').read().replace(b'\x00',b''))
    print('  ✓ $f')
except SyntaxError as e:
    print(f'  ✗ $f  línea {e.lineno}: {e.msg}')
    sys.exit(1)
except FileNotFoundError:
    print('  ⚠ $f no encontrado')
" || ALL_OK=false
done
[ "$ALL_OK" = false ] && echo "✗ Errores — abortando" && exit 1
echo "      ✓ Sintaxis OK"
echo "[3/4] Reiniciando servicios..."
systemctl restart trading_bot.service && echo "      ✓ trading_bot OK"
sleep 3
systemctl daemon-reload && systemctl restart webapp.service && echo "      ✓ webapp OK"
sleep 2
echo "[4/4] Estado:"
systemctl is-active trading_bot.service && echo "  trading_bot: RUNNING" || echo "  trading_bot: FAILED"
systemctl is-active webapp.service && echo "  webapp:      RUNNING" || echo "  webapp:      FAILED"
echo ""
tail -5 "$BOT_DIR/logs/trading_bot.log" 2>/dev/null | sed 's/^/  /' || true
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Listo — https://nsthor.duckdns.org             ║"
echo "╚══════════════════════════════════════════════════╝"
