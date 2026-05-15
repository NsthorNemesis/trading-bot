"""
sim_hoy.py — Simulación de PnL real del día actual
====================================================
1. Descarga M15 de hoy desde OANDA
2. Aplica RSI_Bollinger + filtros reales del bot (cooldown, max_pos, correlación)
3. Para cada trade ejecutado, busca si el precio tocó TP o SL en las velas siguientes
4. Calcula PnL real y capital final

Uso:
  cd /root/trading_bot_v11 && python3 sim_hoy.py
"""
import os, sys
from datetime import datetime, timezone, timedelta
import pandas as pd
import pandas_ta as ta
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from dotenv import load_dotenv

load_dotenv()

OANDA_TOKEN = os.getenv("OANDA_ACCESS_TOKEN", "")
OANDA_ENV   = os.getenv("OANDA_ENVIRONMENT", "practice")

PARES = ["EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD"]

CORRELADOS = {
    "EUR_USD": ["GBP_USD"], "GBP_USD": ["EUR_USD"],
    "USD_JPY": ["USD_CHF"], "USD_CHF": ["USD_JPY"],
}

# Parámetros del bot (igual que producción)
CAPITAL_INI    = 200.0
RIESGO_PCT     = 0.015       # 1.5%
SL_ATR_MULT    = 1.5
RR             = 2.0
MIN_SL_PIPS    = 10
MAX_POSICIONES = 3
COOLDOWN_MIN   = 15
RSI_LOW        = 32
RSI_HIGH       = 68
BB_TOL         = 0.002
SESIONES_ACTIVAS = {"london","overlap","new_york"}
SESIONES = {"london":(7,12),"overlap":(12,17),"new_york":(13,21)}

def sesion(h):
    for n,(i,f) in SESIONES.items():
        if i<=h<f: return n
    return "asian"

def pip_size(par):
    return 0.01 if "JPY" in par else 0.0001

def descargar_m15(api, par, horas=36):
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(hours=horas)
    params = {
        "granularity": "M15",
        "from": desde.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   ahora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": "M",
    }
    req = instruments.InstrumentsCandles(par, params=params)
    api.request(req)
    rows = []
    for c in req.response.get("candles", []):
        mid = c["mid"]
        rows.append({
            "time":     c["time"][:19],
            "Open":     float(mid["o"]),
            "High":     float(mid["h"]),
            "Low":      float(mid["l"]),
            "Close":    float(mid["c"]),
            "complete": c.get("complete", False),
        })
    df = pd.DataFrame(rows)
    if df.empty: return df
    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    return df

def agregar_indicadores(df):
    df = df.copy()
    df.ta.rsi(length=14, append=True)
    df.ta.bbands(length=20, std=2.0, append=True)
    df.ta.atr(length=14, append=True)
    return df

def simular_resultado(df, idx_entry, dir_, sl, tp):
    """
    Busca en las velas posteriores cuál se toca primero: SL o TP.
    Retorna: ('TP'|'SL'|'PENDIENTE', pnl_pips)
    """
    pip = pip_size("JPY") if False else 1  # no usado aquí directamente
    for i in range(idx_entry + 1, len(df)):
        high = df.iloc[i]["High"]
        low  = df.iloc[i]["Low"]
        if dir_ == "long":
            if low <= sl:   return "SL", sl
            if high >= tp:  return "TP", tp
        else:
            if high >= sl:  return "SL", sl
            if low <= tp:   return "TP", tp
    return "PENDIENTE", df.iloc[-1]["Close"]

def main():
    if not OANDA_TOKEN:
        print("ERROR: OANDA_ACCESS_TOKEN no encontrado"); sys.exit(1)

    api    = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)
    ahora  = datetime.now(timezone.utc)

    print("=" * 66)
    print(f"  SIMULACIÓN DE TRADING — {ahora.strftime('%Y-%m-%d')} (datos reales OANDA)")
    print(f"  Capital inicial: ${CAPITAL_INI:.2f} | Riesgo: {RIESGO_PCT*100:.1f}% | RR: {RR}")
    print("=" * 66)

    # Descargar y procesar todos los pares
    dfs = {}
    for par in PARES:
        df = descargar_m15(api, par)
        if not df.empty:
            dfs[par] = agregar_indicadores(df)

    capital      = CAPITAL_INI
    trades       = []
    pos_abiertas = {}   # par → info del trade abierto
    cooldowns    = {}   # par → datetime de último trade

    # Unificar todos los timestamps y ordenarlos
    todos_ts = sorted(set(
        ts for df in dfs.values() for ts in df.index
    ))

    # Simular vela por vela en orden cronológico
    for ts in todos_ts:
        hora = ts.hour
        if sesion(hora) not in SESIONES_ACTIVAS:
            continue

        # 1. Cerrar posiciones que tocaron SL/TP en esta vela
        cerrados = []
        for par, pos in list(pos_abiertas.items()):
            if par not in dfs or ts not in dfs[par].index:
                continue
            row  = dfs[par].loc[ts]
            high = row["High"]
            low  = row["Low"]
            sl, tp, dir_ = pos["sl"], pos["tp"], pos["dir"]
            if dir_ == "long":
                if low <= sl:
                    resultado = "SL"; exit_p = sl
                elif high >= tp:
                    resultado = "TP"; exit_p = tp
                else:
                    continue
            else:
                if high >= sl:
                    resultado = "SL"; exit_p = sl
                elif low <= tp:
                    resultado = "TP"; exit_p = tp
                else:
                    continue

            pip = pip_size(par)
            if dir_ == "long":
                pnl_pips = (exit_p - pos["entry"]) / pip
            else:
                pnl_pips = (pos["entry"] - exit_p) / pip

            risk_usd = capital * RIESGO_PCT
            sl_pips  = abs(pos["entry"] - sl) / pip
            sl_pips  = max(sl_pips, MIN_SL_PIPS)
            pnl_usd  = pnl_pips / sl_pips * risk_usd

            capital += pnl_usd
            trades[-1].update({
                "resultado": resultado,
                "exit_ts":   ts,
                "exit_px":   exit_p,
                "pnl_usd":   round(pnl_usd, 4),
                "capital_post": round(capital, 2),
            })
            cerrados.append(par)

        for par in cerrados:
            del pos_abiertas[par]

        # 2. Buscar nuevas señales
        if len(pos_abiertas) >= MAX_POSICIONES:
            continue

        for par in PARES:
            if par not in dfs or ts not in dfs[par].index:
                continue

            # Cooldown
            ult = cooldowns.get(par)
            if ult and (ts - ult).total_seconds() / 60 < COOLDOWN_MIN:
                continue

            # Max posiciones
            if len(pos_abiertas) >= MAX_POSICIONES:
                break

            # Ya tiene posición abierta en este par
            if par in pos_abiertas:
                continue

            df  = dfs[par]
            idx = df.index.get_loc(ts)
            if idx < 20:
                continue

            row = df.iloc[idx]
            rsi_col = next((c for c in df.columns if c.startswith("RSI_")), None)
            bbl_col = next((c for c in df.columns if c.startswith("BBL_")), None)
            bbu_col = next((c for c in df.columns if c.startswith("BBU_")), None)
            atr_col = next((c for c in df.columns if c.startswith("ATRr_") or c.startswith("ATR")), None)

            if not all([rsi_col, bbl_col, bbu_col, atr_col]):
                continue

            rsi, bbl, bbu, atr, precio = (
                row[rsi_col], row[bbl_col], row[bbu_col], row[atr_col], row["Close"]
            )

            if any(pd.isna(x) for x in [rsi, bbl, bbu, atr]):
                continue

            # Señal RSI_Bollinger
            dir_ = None
            if rsi < RSI_LOW and precio < bbl * (1 + BB_TOL):
                dir_ = "long"
            elif rsi > RSI_HIGH and precio > bbu * (1 - BB_TOL):
                dir_ = "short"

            if not dir_:
                continue

            # Filtro correlación
            rel = CORRELADOS.get(par, [])
            bloqueado = False
            for r in rel:
                if r in pos_abiertas and pos_abiertas[r]["dir"] == dir_:
                    bloqueado = True
                    break
            if bloqueado:
                continue

            # Calcular SL/TP
            pip     = pip_size(par)
            sl_dist = max(atr * SL_ATR_MULT, MIN_SL_PIPS * pip)
            if dir_ == "long":
                sl = precio - sl_dist
                tp = precio + sl_dist * RR
            else:
                sl = precio + sl_dist
                tp = precio - sl_dist * RR

            # Calcular tamaño
            sl_pips  = max(sl_dist / pip, MIN_SL_PIPS)
            risk_usd = capital * RIESGO_PCT
            units    = int(risk_usd / (sl_pips * pip))
            units    = max(100, min(units, 2000))

            # Registrar trade
            trade = {
                "par":     par,
                "dir":     dir_,
                "ts":      ts,
                "entry":   precio,
                "sl":      round(sl, 5),
                "tp":      round(tp, 5),
                "sl_pips": round(sl_pips, 1),
                "rsi":     round(rsi, 1),
                "sesion":  sesion(hora),
                "resultado": "PENDIENTE",
                "pnl_usd":   0.0,
            }
            trades.append(trade)
            pos_abiertas[par] = {"dir": dir_, "entry": precio, "sl": sl, "tp": tp}
            cooldowns[par] = ts

    # Imprimir resultados
    print(f"\n{'─'*66}")
    print(f"  {'HORA':5}  {'PAR':8}  {'DIR':5}  {'RSI':5}  {'ENTRY':10}  {'SL_PIPS':8}  {'RESULT':8}  {'PnL $':>8}")
    print(f"{'─'*66}")

    for t in trades:
        pnl_str = f"${t['pnl_usd']:+.2f}" if t["resultado"] != "PENDIENTE" else "  vivo"
        print(
            f"  {t['ts'].strftime('%H:%M'):5}  {t['par']:8}  {t['dir']:5}  "
            f"{t['rsi']:5.1f}  {t['entry']:10.5f}  {t['sl_pips']:8.1f}  "
            f"{t['resultado']:8}  {pnl_str:>8}"
        )

    # Resumen
    cerrados_t  = [t for t in trades if t["resultado"] != "PENDIENTE"]
    pendientes  = [t for t in trades if t["resultado"] == "PENDIENTE"]
    ganadores   = [t for t in cerrados_t if t["resultado"] == "TP"]
    perdedores  = [t for t in cerrados_t if t["resultado"] == "SL"]
    pnl_total   = sum(t["pnl_usd"] for t in cerrados_t)
    wr          = len(ganadores) / len(cerrados_t) * 100 if cerrados_t else 0

    print(f"\n{'='*66}")
    print(f"  RESUMEN DEL DÍA — {ahora.strftime('%Y-%m-%d')}")
    print(f"{'─'*66}")
    print(f"  Trades ejecutados:  {len(trades)}")
    print(f"  Cerrados:           {len(cerrados_t)}  ({len(ganadores)} TP / {len(perdedores)} SL)")
    print(f"  Pendientes:         {len(pendientes)}")
    print(f"  Win Rate:           {wr:.1f}%")
    print(f"  PnL del día:        ${pnl_total:+.2f}")
    print(f"  Capital inicial:    ${CAPITAL_INI:.2f}")
    print(f"  Capital final:      ${capital:.2f}")
    print(f"  Retorno del día:    {(capital-CAPITAL_INI)/CAPITAL_INI*100:+.2f}%")
    if pendientes:
        print(f"\n  Posiciones aún vivas (no cerraron SL/TP hoy):")
        for t in pendientes:
            print(f"    {t['ts'].strftime('%H:%M')}  {t['par']}  {t['dir'].upper()}  entry={t['entry']:.5f}")
    print(f"{'='*66}\n")

if __name__ == "__main__":
    main()
