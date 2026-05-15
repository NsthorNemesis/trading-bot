"""
sim_semana.py â€” Backtest de la Ãºltima semana de mercado (M1)
================================================================
Reproduce fielmente el SignalAgent en modo fallback (sin DeepSeek):
- Indicadores idÃ©nticos al MarketAgent (libreria 'ta' o fallback numpy)
- Pre-filtro idÃ©ntico al SignalAgent._prefiltro_tecnico
- Reglas: cooldown, sesiones, max posiciones, SL/TP por ATR, RR, riesgo

Resultado: piso del sistema (sin filtro DeepSeek).
La nota de calibraciÃ³n indica que DeepSeek sube WR de ~33% a ~55%.
================================================================
Uso:
    python sim_semana.py
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Intento de usar 'ta' (igual que el sistema). Fallback a numpy si no estÃ¡.
try:
    from ta.volatility import AverageTrueRange, BollingerBands
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator
    HAS_TA = True
except ImportError:
    HAS_TA = False

ROOT     = Path(__file__).parent
HIST     = ROOT / "data" / "historical"
PARAMS_F = ROOT / "data" / "calibration" / "strategy_params.json"
OUT_DIR  = ROOT / "data" / "backtesting"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL_INICIAL = 200.0


# â”€â”€ INDICADORES â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _atr_numpy(h, l, c, n=14):
    pc = c.shift(1)
    tr = np.maximum.reduce([h - l, (h - pc).abs(), (l - pc).abs()])
    tr = pd.Series(tr, index=c.index).fillna(0)
    # Wilder smoothing (igual que ta.AverageTrueRange)
    atr = tr.ewm(alpha=1/n, adjust=False).mean()
    return atr

def _rsi_numpy(c, n=14):
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def _ema_numpy(c, n):
    return c.ewm(span=n, adjust=False).mean()

def _bb_numpy(c, n=20, k=2):
    ma = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    return ma - k*sd, ma + k*sd

def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    """Replica MarketAgent.get_df exactamente."""
    df = df.copy()
    df = df.rename(columns={
        "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = df[c].astype(float)

    if HAS_TA:
        df["ATR_14"] = AverageTrueRange(
            df["High"], df["Low"], df["Close"], window=14, fillna=True
        ).average_true_range()
        df["RSI_14"] = RSIIndicator(df["Close"], window=14, fillna=True).rsi()
        df["EMA_9"]  = EMAIndicator(df["Close"], window=9,  fillna=True).ema_indicator()
        df["EMA_20"] = EMAIndicator(df["Close"], window=20, fillna=True).ema_indicator()
        df["EMA_50"] = EMAIndicator(df["Close"], window=50, fillna=True).ema_indicator()
        bb = BollingerBands(df["Close"], window=20, window_dev=2, fillna=True)
        df["BBL_20"] = bb.bollinger_lband()
        df["BBU_20"] = bb.bollinger_hband()
    else:
        df["ATR_14"] = _atr_numpy(df["High"], df["Low"], df["Close"], 14)
        df["RSI_14"] = _rsi_numpy(df["Close"], 14)
        df["EMA_9"]  = _ema_numpy(df["Close"], 9)
        df["EMA_20"] = _ema_numpy(df["Close"], 20)
        df["EMA_50"] = _ema_numpy(df["Close"], 50)
        bbl, bbu = _bb_numpy(df["Close"], 20, 2)
        df["BBL_20"], df["BBU_20"] = bbl.fillna(df["Close"]), bbu.fillna(df["Close"])

    # Patrones de velas â€” fÃ³rmula idÃ©ntica a MarketAgent
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    cuerpo = (c - o).abs()
    rango  = h - l
    sombra_inf = pd.concat([o, c], axis=1).min(axis=1) - l
    sombra_sup = h - pd.concat([o, c], axis=1).max(axis=1)

    df["CDL_HAMMER"] = (
        (cuerpo > 0) &
        (sombra_inf >= 2 * cuerpo) &
        (sombra_sup <= cuerpo * 0.5)
    ).astype(int)

    df["CDL_DOJI"] = (
        (rango > 0) &
        (cuerpo / rango.replace(0, 1) < 0.10)
    ).astype(int)

    df["CDL_ENGULF_BULL"] = (
        (c > o) &
        (c.shift(1) < o.shift(1)) &
        (c > o.shift(1)) &
        (o < c.shift(1)) &
        (cuerpo > cuerpo.shift(1) * 1.05)
    ).astype(int)

    df["CDL_ENGULF_BEAR"] = (
        (c < o) &
        (c.shift(1) > o.shift(1)) &
        (c < o.shift(1)) &
        (o > c.shift(1)) &
        (cuerpo > cuerpo.shift(1) * 1.05)
    ).astype(int)

    return df


# â”€â”€ SESIÃ“N â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def sesion_de(ts: pd.Timestamp) -> str:
    h = ts.hour
    if 7  <= h < 13: return "london"
    if 13 <= h < 17: return "overlap"
    if 17 <= h < 22: return "new_york"
    return "asia"


# â”€â”€ PRE-FILTRO (replica SignalAgent) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def evaluar_senal(row: pd.Series, estrategias_activas: set) -> dict | None:
    """Devuelve dict de seÃ±al o None â€” rÃ©plica de SignalAgent._prefiltro_tecnico."""
    rsi   = row["RSI_14"]
    bbl   = row["BBL_20"]
    bbu   = row["BBU_20"]
    ema20 = row["EMA_20"]
    ema50 = row["EMA_50"]
    precio = row["Close"]

    tendencia = "up" if ema20 > ema50 else "down"

    hay_hammer = bool(row.get("CDL_HAMMER", 0))
    hay_doji   = bool(row.get("CDL_DOJI", 0))
    hay_engulf = bool(row.get("CDL_ENGULF_BULL", 0) or row.get("CDL_ENGULF_BEAR", 0))

    rsi_sobre = rsi > 68
    rsi_bajo  = rsi < 32
    precio_bbl = precio < bbl * 1.002
    precio_bbu = precio > bbu * 0.998

    senal_long = (
        (hay_hammer and tendencia == "down") or
        (hay_doji   and rsi_bajo) or
        (rsi_bajo   and precio_bbl) or
        (hay_engulf and tendencia == "up")
    )
    senal_short = (
        (hay_doji   and rsi_sobre) or
        (rsi_sobre  and precio_bbu)
    )

    if not senal_long and not senal_short:
        return None

    if senal_long:
        dir_ = "long"
        if hay_hammer:    estrat = "Hammer"
        elif hay_doji:    estrat = "Doji"
        elif hay_engulf:  estrat = "Engulfing"
        elif rsi_bajo:    estrat = "RSI_Bollinger" if precio_bbl else "RSI_Divergence"
        else:             estrat = "RSI_Divergence"
    else:
        dir_  = "short"
        estrat = "Doji" if hay_doji else "RSI_Bollinger"

    if estrat not in estrategias_activas:
        return None

    return {
        "dir": dir_,
        "estrategia": estrat,
        "entry": precio,
        "atr": row["ATR_14"],
        "rsi": rsi,
        "tendencia": tendencia,
    }


# â”€â”€ SL/TP (replica RiskExecutionAgent) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def pip_size(par: str) -> float:
    return 0.01 if par.endswith("JPY") else 0.0001

def calcular_sl_tp(par: str, entry: float, dir_: str, atr: float, P: dict):
    pip = pip_size(par)
    sl_atr_pips = (atr * P["sl_atr_mult"]) / pip
    sl_pips = max(P["min_sl_pips"], min(sl_atr_pips, P["max_sl_pips"]))
    tp_pips = sl_pips * P["rr_ratio"]
    if dir_ == "long":
        sl = entry - sl_pips * pip
        tp = entry + tp_pips * pip
    else:
        sl = entry + sl_pips * pip
        tp = entry - tp_pips * pip
    return sl, tp, sl_pips, tp_pips


# â”€â”€ BACKTEST â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def correr_backtest():
    raw = PARAMS_F.read_bytes()
    try:
        P = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        P = json.loads(raw.decode("latin-1"))
    print("=" * 78)
    print(f"  BACKTEST SEMANAL â€” calibraciÃ³n {P['calibrado_en']}")
    print(f"  Capital inicial: ${CAPITAL_INICIAL}  |  Riesgo {P['riesgo_pct']*100}%/op")
    print(f"  Estrategias activas: {P['estrategias_activas']}")
    print(f"  Pares: {P['pares_activos']}")
    print(f"  ta installed: {HAS_TA}")
    print("=" * 78)

    estrategias_activas = set(P["estrategias_activas"])
    sesiones_activas    = set(P["sesiones_activas"])
    cooldown_min        = P["cooldown_minutes"]
    max_pos             = P["max_posiciones"]
    riesgo              = P["riesgo_pct"]

    # Cargar datos por par
    datos = {}
    for par in P["pares_activos"]:
        f = HIST / f"{par}_M1.json"
        velas = json.loads(f.read_text())
        df = pd.DataFrame(velas)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = calcular_indicadores(df)
        datos[par] = df
        print(f"  {par}: {len(df)} velas  "
              f"({df['timestamp'].iloc[0]} â†’ {df['timestamp'].iloc[-1]})")

    # â”€â”€ Walk-forward sobre lÃ­nea de tiempo unificada â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Estrategia: recorro las velas por par; cada apertura tiene entry/SL/TP
    # y la simulo recorriendo velas siguientes hasta que toque uno u otro.
    capital = CAPITAL_INICIAL
    trades  = []
    cooldown_ts = {}              # par â†’ ts Ãºltima seÃ±al
    pos_abiertas = {}             # par â†’ trade dict (max 1 por par)

    # Recorrer cada par independientemente; max_posiciones se respeta global
    # con un orden cronolÃ³gico unificado.
    eventos = []
    for par, df in datos.items():
        for i in range(50, len(df) - 1):     # margen para indicadores y entry t+1
            eventos.append((df["timestamp"].iloc[i], par, i))
    eventos.sort(key=lambda x: x[0])

    abiertas_global = []  # lista de (par, entry_idx) â€” para chequear max 3
    eq_curve = [(eventos[0][0], CAPITAL_INICIAL)] if eventos else []

    for ts, par, i in eventos:
        df = datos[par]
        row = df.iloc[i]

        # 1) Resolver posiciones abiertas que toquen SL/TP en esta vela
        cerradas = []
        for j, abierta in enumerate(abiertas_global):
            if abierta["par"] != par:
                continue
            if i <= abierta["idx_entry"]:
                continue
            high = row["High"]; low = row["Low"]
            sl, tp = abierta["sl"], abierta["tp"]
            golpe_sl = (low <= sl) if abierta["dir"] == "long" else (high >= sl)
            golpe_tp = (high >= tp) if abierta["dir"] == "long" else (low <= tp)
            if golpe_sl and golpe_tp:
                # Mismo bar â€” conservador: asume SL primero
                resultado = "loss"; exit_px = sl
            elif golpe_sl:
                resultado = "loss"; exit_px = sl
            elif golpe_tp:
                resultado = "win"; exit_px = tp
            else:
                continue

            pnl_dolares = abierta["riesgo_dolar"] * (
                P["rr_ratio"] if resultado == "win" else -1.0
            )
            capital += pnl_dolares
            trades.append({
                **abierta,
                "exit_ts": ts,
                "exit_px": exit_px,
                "resultado": resultado,
                "pnl": pnl_dolares,
                "capital_post": capital,
            })
            eq_curve.append((ts, capital))
            cerradas.append(j)

        # Quitar cerradas (en reversa para no romper indices)
        for j in reversed(cerradas):
            abiertas_global.pop(j)

        # 2) Â¿Puedo abrir nueva posiciÃ³n?
        if len(abiertas_global) >= max_pos:
            continue
        # cooldown
        ult = cooldown_ts.get(par)
        if ult and (ts - ult).total_seconds() < cooldown_min * 60:
            continue
        # ya hay una posiciÃ³n en este par
        if any(a["par"] == par for a in abiertas_global):
            continue
        # sesiÃ³n activa
        if sesion_de(ts) not in sesiones_activas:
            continue

        sen = evaluar_senal(row, estrategias_activas)
        if not sen:
            continue

        # Entry en open de la siguiente vela (mÃ¡s realista que close de t)
        next_row = df.iloc[i + 1]
        entry_px = float(next_row["Open"])
        sl, tp, sl_pips, tp_pips = calcular_sl_tp(
            par, entry_px, sen["dir"], sen["atr"], P
        )
        riesgo_dolar = capital * riesgo

        abiertas_global.append({
            "par": par,
            "dir": sen["dir"],
            "estrategia": sen["estrategia"],
            "entry_ts": next_row["timestamp"],
            "entry_px": entry_px,
            "idx_entry": i + 1,
            "sl": sl, "tp": tp,
            "sl_pips": sl_pips, "tp_pips": tp_pips,
            "riesgo_dolar": riesgo_dolar,
            "sesion": sesion_de(ts),
            "rsi_at_entry": float(sen["rsi"]),
            "atr_at_entry": float(sen["atr"]),
        })
        cooldown_ts[par] = ts

    # Cerrar posiciones abiertas al final (mark-to-market al Ãºltimo close)
    for ab in abiertas_global:
        df = datos[ab["par"]]
        last = df.iloc[-1]
        exit_px = float(last["Close"])
        pip = pip_size(ab["par"])
        if ab["dir"] == "long":
            pips = (exit_px - ab["entry_px"]) / pip
        else:
            pips = (ab["entry_px"] - exit_px) / pip
        # PnL proporcional: pips/sl_pips * riesgo
        pnl = (pips / ab["sl_pips"]) * ab["riesgo_dolar"]
        capital += pnl
        trades.append({
            **ab,
            "exit_ts": last["timestamp"],
            "exit_px": exit_px,
            "resultado": "open_at_end",
            "pnl": pnl,
            "capital_post": capital,
        })
        eq_curve.append((last["timestamp"], capital))

    return P, datos, trades, eq_curve, capital


# â”€â”€ MÃ‰TRICAS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def imprimir_metricas(P, datos, trades, eq_curve, capital_final):
    print("\n" + "=" * 78)
    print("  RESULTADOS")
    print("=" * 78)

    n = len(trades)
    if n == 0:
        print("  Sin trades en el periodo.")
        return

    wins   = [t for t in trades if t["resultado"] == "win"]
    losses = [t for t in trades if t["resultado"] == "loss"]
    abiertos = [t for t in trades if t["resultado"] == "open_at_end"]

    pnl_total = sum(t["pnl"] for t in trades)
    wr = len(wins) / max(1, len(wins) + len(losses))
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # Drawdown sobre eq_curve
    eq = pd.DataFrame(eq_curve, columns=["ts", "eq"]).drop_duplicates("ts", keep="last")
    eq = eq.sort_values("ts").reset_index(drop=True)
    eq["peak"] = eq["eq"].cummax()
    eq["dd"]   = (eq["peak"] - eq["eq"]) / eq["peak"]
    max_dd_pct = eq["dd"].max() * 100 if len(eq) else 0

    expectancy = pnl_total / n

    print(f"  Capital inicial : ${CAPITAL_INICIAL:.2f}")
    print(f"  Capital final   : ${capital_final:.2f}")
    print(f"  P&L total       : ${pnl_total:+.2f}  ({pnl_total/CAPITAL_INICIAL*100:+.2f}%)")
    print(f"  Trades cerrados : {len(wins) + len(losses)}  "
          f"(W:{len(wins)}  L:{len(losses)}  Open:{len(abiertos)})")
    print(f"  Win rate        : {wr*100:.1f}%")
    print(f"  Profit Factor   : {pf:.2f}")
    print(f"  Expectancy      : ${expectancy:+.3f}/trade")
    print(f"  Max drawdown    : {max_dd_pct:.2f}%")

    # Por estrategia
    print("\n  Por estrategia:")
    print(f"    {'estrategia':<16} {'n':>4} {'WR':>7} {'PF':>7} {'PnL':>9}")
    by_estrat = defaultdict(list)
    for t in trades: by_estrat[t["estrategia"]].append(t)
    for est, lst in by_estrat.items():
        w = [x for x in lst if x["resultado"] == "win"]
        l = [x for x in lst if x["resultado"] == "loss"]
        gw = sum(x["pnl"] for x in w)
        gl = abs(sum(x["pnl"] for x in l))
        pf_e = (gw/gl) if gl > 0 else float("inf")
        wr_e = len(w)/max(1, len(w)+len(l))
        pnl_e = sum(x["pnl"] for x in lst)
        print(f"    {est:<16} {len(lst):>4} {wr_e*100:>6.1f}% "
              f"{pf_e:>7.2f} ${pnl_e:>+8.2f}")

    # Por par
    print("\n  Por par:")
    print(f"    {'par':<10} {'n':>4} {'WR':>7} {'PnL':>9}")
    by_par = defaultdict(list)
    for t in trades: by_par[t["par"]].append(t)
    for par, lst in by_par.items():
        w = [x for x in lst if x["resultado"] == "win"]
        l = [x for x in lst if x["resultado"] == "loss"]
        wr_p = len(w)/max(1, len(w)+len(l))
        pnl_p = sum(x["pnl"] for x in lst)
        print(f"    {par:<10} {len(lst):>4} {wr_p*100:>6.1f}% ${pnl_p:>+8.2f}")

    # Por sesiÃ³n
    print("\n  Por sesiÃ³n:")
    print(f"    {'sesion':<10} {'n':>4} {'WR':>7} {'PnL':>9}")
    by_ses = defaultdict(list)
    for t in trades: by_ses[t["sesion"]].append(t)
    for ses, lst in by_ses.items():
        w = [x for x in lst if x["resultado"] == "win"]
        l = [x for x in lst if x["resultado"] == "loss"]
        wr_s = len(w)/max(1, len(w)+len(l))
        pnl_s = sum(x["pnl"] for x in lst)
        print(f"    {ses:<10} {len(lst):>4} {wr_s*100:>6.1f}% ${pnl_s:>+8.2f}")

    # Comparar contra umbrales live
    print("\n  Umbrales para LIVE (4 semanas paper):")
    print(f"    WR â‰¥ 55%       : {'OK' if wr >= 0.55 else 'NO'}  ({wr*100:.1f}%)")
    print(f"    PF â‰¥ 1.4       : {'OK' if pf >= 1.4 else 'NO'}  ({pf:.2f})")
    print(f"    MaxDD < 6%     : {'OK' if max_dd_pct < 6 else 'NO'}  ({max_dd_pct:.2f}%)")
    print("\n  NOTA: este backtest NO incluye filtro DeepSeek. Es el piso")
    print("        del sistema. La calibraciÃ³n estima que DeepSeek sube")
    print("        WR a ~52-58% al filtrar seÃ±ales dÃ©biles.")

    # Persistir
    out = {
        "calibrado_en":      P["calibrado_en"],
        "rango": {
            "desde": str(min(d["timestamp"].iloc[0] for d in datos.values())),
            "hasta": str(max(d["timestamp"].iloc[-1] for d in datos.values())),
        },
        "capital_inicial":   CAPITAL_INICIAL,
        "capital_final":     capital_final,
        "trades_total":      n,
        "wins":              len(wins),
        "losses":            len(losses),
        "abiertos_al_final": len(abiertos),
        "win_rate":          wr,
        "profit_factor":     pf if pf != float("inf") else None,
        "expectancy":        expectancy,
        "max_drawdown_pct":  max_dd_pct,
        "pnl_total":         pnl_total,
        "trades":            [
            {**{k: (v.isoformat() if isinstance(v, pd.Timestamp) else v)
                for k, v in t.items()}}
            for t in trades
        ],
    }
    out_f = OUT_DIR / "resultados_semana.json"
    out_f.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\n  Resultado guardado en: {out_f}")


# â”€â”€ MAIN â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    P, datos, trades, eq_curve, capital_final = correr_backtest()
    imprimir_metricas(P, datos, trades, eq_curve, capital_final)

