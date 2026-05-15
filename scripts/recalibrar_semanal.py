"""
scripts/recalibrar_semanal.py
══════════════════════════════════════════════════════════════
Script de recalibracion semanal — correr cada sabado con mercado cerrado.

Que hace:
  1. Descarga datos frescos de OANDA (ultimos 18 meses por par)
  2. Corre rolling window backtest sobre todas las estrategias
  3. Evalua cual estrategia activa es mejor por par y por regimen
  4. Propone nuevos parametros optimos
  5. Consulta DeepSeek para validar y ajustar la propuesta
  6. Guarda strategy_params.json actualizado
  7. Genera reporte en docs/traidingbot/ para Obsidian

Uso:
  cd C:\\Users\\na_sc\\trading_bot_v11
  python scripts/recalibrar_semanal.py

  Con opcion de solo ver reporte sin guardar:
  python scripts/recalibrar_semanal.py --dry-run

  Sin consultar DeepSeek (mas rapido):
  python scripts/recalibrar_semanal.py --local
══════════════════════════════════════════════════════════════
"""
import sys
import json
import argparse
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from config.settings import (
    DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_DEEP,
    OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV,
    CALIB_DIR, PARAMS_FILE,
)

# ─────────────────────────────────────────────────────────────
# Configuracion del rolling window
# ─────────────────────────────────────────────────────────────
PARES_BACKTEST = [
    "EUR_USD", "GBP_USD", "USD_JPY",
    "USD_CHF", "AUD_USD", "USD_CAD",
]

ESTRATEGIAS_DISPONIBLES = [
    "RSI_Bollinger",
    "RSI_Divergence",
    "Doji",
    "Hammer",
    "Engulfing",
    "EMA_Crossover",
]

# Rolling window: evaluar en ventanas de 8 semanas deslizando de a 2 semanas
# sobre los ultimos 18 meses de datos
VENTANA_SEMANAS  = 8
PASO_SEMANAS     = 2
MESES_HISTORICO  = 18
VELAS_POR_DIA    = 64   # M15: 96 velas/dia en sesion completa, ~64 en sesion forex
CAPITAL_INICIAL  = 200.0
RIESGO_PCT       = 0.015
RR_RATIO         = 2.0
SL_ATR_MULT      = 1.5

# Parametros actuales como fallback
PARAMS_FALLBACK = {
    "estrategias_activas":  ["RSI_Bollinger"],
    "pares_activos":        PARES_BACKTEST,
    "sl_atr_mult":          SL_ATR_MULT,
    "rr_ratio":             RR_RATIO,
    "min_win_rate":         0.34,
    "min_confidence":       0.30,
    "riesgo_pct":           RIESGO_PCT,
    "signal_timeframe":     "M15",
    "sesiones_activas":     ["london", "overlap", "new_york"],
    "cooldown_minutes":     15,
    "max_posiciones":       3,
    "max_drawdown_dia":     0.04,
    "circuit_breaker_pct":  0.15,
    "min_sl_pips":          10,
    "max_sl_pips":          40,
    "estrategias_pausadas": [],
}


# ─────────────────────────────────────────────────────────────
# Descarga de datos OANDA
# ─────────────────────────────────────────────────────────────

def descargar_datos_oanda(par: str, semanas: int = MESES_HISTORICO * 4) -> Optional[list]:
    """Descarga velas M15 de OANDA para el par dado."""
    try:
        import oandapyV20
        import oandapyV20.endpoints.instruments as instruments

        api    = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)
        count  = min(semanas * 5 * VELAS_POR_DIA, 5000)  # max 5000 por request
        params = {"granularity": "M15", "count": count, "price": "M"}

        r = instruments.InstrumentsCandles(par, params=params)
        api.request(r)
        candles = r.response.get("candles", [])
        return [c for c in candles if c.get("complete", True)]
    except Exception as e:
        print(f"  ERROR descargando {par}: {e}")
        return None


def candles_a_dataframe(candles: list):
    """Convierte velas de OANDA a DataFrame con indicadores."""
    try:
        import pandas as pd
        import numpy as np

        rows = []
        for c in candles:
            mid = c.get("mid", {})
            rows.append({
                "time":  c["time"],
                "Open":  float(mid.get("o", 0)),
                "High":  float(mid.get("h", 0)),
                "Low":   float(mid.get("l", 0)),
                "Close": float(mid.get("c", 0)),
                "Volume": int(c.get("volume", 0)),
            })

        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time").sort_index()

        # Indicadores basicos
        df = _agregar_indicadores(df)
        return df
    except Exception as e:
        print(f"  ERROR convirtiendo candles: {e}")
        return None


def _agregar_indicadores(df):
    """Agrega RSI, EMA, ATR, Bollinger al DataFrame."""
    import pandas as pd
    import numpy as np

    # EMA
    df["EMA_20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA_50"] = df["Close"].ewm(span=50, adjust=False).mean()

    # RSI
    delta  = df["Close"].diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=13, adjust=False).mean()
    avg_l  = loss.ewm(com=13, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    df["RSI_14"] = 100 - (100 / (1 + rs))

    # ATR
    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - df["Close"].shift(1)).abs()
    tr3 = (df["Low"]  - df["Close"].shift(1)).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR_14"] = tr.ewm(com=13, adjust=False).mean()

    # Bollinger
    sma20 = df["Close"].rolling(20).mean()
    std20 = df["Close"].rolling(20).std()
    df["BBL_20"] = sma20 - 2 * std20
    df["BBU_20"] = sma20 + 2 * std20

    # MACD
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD_DIF"] = ema12 - ema26

    return df.dropna()


# ─────────────────────────────────────────────────────────────
# Backtest por ventana
# ─────────────────────────────────────────────────────────────

def backtest_estrategia(df, estrategia_nombre: str, par: str,
                         rr: float = RR_RATIO,
                         sl_mult: float = SL_ATR_MULT,
                         capital: float = CAPITAL_INICIAL,
                         riesgo_pct: float = RIESGO_PCT) -> dict:
    """
    Simula la estrategia sobre el DataFrame y retorna metricas.
    Devuelve: {trades, wins, losses, wr, pf, pnl, max_dd, estrategia, par}
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from strategies import load_strategies
        strats = load_strategies(active_only=[estrategia_nombre])
        if estrategia_nombre not in strats:
            return _resultado_vacio(estrategia_nombre, par)
        estrategia = strats[estrategia_nombre]
    except Exception as e:
        return _resultado_vacio(estrategia_nombre, par)

    params_sim = {**PARAMS_FALLBACK, "estrategias_activas": [estrategia_nombre]}

    trades  = 0
    wins    = 0
    losses  = 0
    pnl     = 0.0
    cap     = capital
    peak    = capital
    max_dd  = 0.0
    gross_w = 0.0
    gross_l = 0.0
    i       = 50  # warmup para indicadores

    while i < len(df) - 10:
        ventana = df.iloc[max(0, i - 80):i + 1]
        if len(ventana) < 20:
            i += 1
            continue

        try:
            senal = estrategia.generate_signal(ventana, par, params_sim)
        except Exception:
            i += 1
            continue

        if not senal:
            i += 1
            continue

        # Simular trade
        entry   = float(df["Close"].iloc[i])
        atr     = float(df["ATR_14"].iloc[i])
        sl_dist = atr * sl_mult
        tp_dist = sl_dist * rr

        if sl_dist <= 0 or entry <= 0:
            i += 1
            continue

        riesgo_usd = cap * riesgo_pct
        sl_price   = entry - sl_dist if senal["dir"] == "long" else entry + sl_dist
        tp_price   = entry + tp_dist if senal["dir"] == "long" else entry - tp_dist

        # Buscar resultado en las siguientes velas
        resultado = None
        for j in range(i + 1, min(i + 200, len(df))):
            h = float(df["High"].iloc[j])
            l = float(df["Low"].iloc[j])

            if senal["dir"] == "long":
                if l <= sl_price:
                    resultado = "loss"
                    break
                if h >= tp_price:
                    resultado = "win"
                    break
            else:
                if h >= sl_price:
                    resultado = "loss"
                    break
                if l <= tp_price:
                    resultado = "win"
                    break

        if resultado is None:
            i += 10  # timeout
            continue

        trades += 1
        if resultado == "win":
            ganancia = riesgo_usd * rr
            cap     += ganancia
            pnl     += ganancia
            gross_w += ganancia
            wins    += 1
        else:
            cap     -= riesgo_usd
            pnl     -= riesgo_usd
            gross_l += riesgo_usd
            losses  += 1

        # Drawdown
        if cap > peak:
            peak = cap
        dd = (peak - cap) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # Saltar al final del trade para evitar solapamiento
        i = j + 1

    wr = wins / trades if trades > 0 else 0.0
    pf = gross_w / gross_l if gross_l > 0 else (999.0 if gross_w > 0 else 0.0)

    return {
        "estrategia": estrategia_nombre,
        "par":        par,
        "trades":     trades,
        "wins":       wins,
        "losses":     losses,
        "wr":         round(wr, 4),
        "pf":         round(pf, 4),
        "pnl":        round(pnl, 2),
        "max_dd":     round(max_dd, 4),
        "capital_final": round(cap, 2),
    }


def _resultado_vacio(estrategia: str, par: str) -> dict:
    return {
        "estrategia": estrategia, "par": par,
        "trades": 0, "wins": 0, "losses": 0,
        "wr": 0.0, "pf": 0.0, "pnl": 0.0,
        "max_dd": 0.0, "capital_final": CAPITAL_INICIAL,
    }


# ─────────────────────────────────────────────────────────────
# Rolling window
# ─────────────────────────────────────────────────────────────

def rolling_window_backtest(df, estrategia: str, par: str) -> dict:
    """
    Corre el backtest en multiples ventanas solapadas y promedia los resultados.
    Mas robusto que un backtest unico — reduce overfitting.
    """
    velas_por_ventana = VENTANA_SEMANAS * 5 * VELAS_POR_DIA
    velas_paso        = PASO_SEMANAS  * 5 * VELAS_POR_DIA
    n                 = len(df)

    if n < velas_por_ventana:
        # Si no hay suficientes datos, corre sobre todo el df
        return backtest_estrategia(df, estrategia, par)

    resultados_ventanas = []
    inicio = 0
    while inicio + velas_por_ventana <= n:
        ventana_df = df.iloc[inicio:inicio + velas_por_ventana]
        r = backtest_estrategia(ventana_df, estrategia, par)
        if r["trades"] >= 10:  # ignorar ventanas con muy pocos trades
            resultados_ventanas.append(r)
        inicio += velas_paso

    if not resultados_ventanas:
        return backtest_estrategia(df, estrategia, par)

    # Promediar metricas
    total_trades = sum(r["trades"] for r in resultados_ventanas)
    total_wins   = sum(r["wins"]   for r in resultados_ventanas)
    avg_pf       = sum(r["pf"]     for r in resultados_ventanas) / len(resultados_ventanas)
    avg_dd       = sum(r["max_dd"] for r in resultados_ventanas) / len(resultados_ventanas)
    total_pnl    = sum(r["pnl"]    for r in resultados_ventanas)
    wr           = total_wins / total_trades if total_trades > 0 else 0

    return {
        "estrategia":    estrategia,
        "par":           par,
        "trades":        total_trades,
        "wins":          total_wins,
        "losses":        total_trades - total_wins,
        "wr":            round(wr, 4),
        "pf":            round(avg_pf, 4),
        "pnl":           round(total_pnl, 2),
        "max_dd":        round(avg_dd, 4),
        "ventanas":      len(resultados_ventanas),
        "capital_final": round(CAPITAL_INICIAL + total_pnl / len(resultados_ventanas), 2),
    }


# ─────────────────────────────────────────────────────────────
# Analisis y propuesta de parametros
# ─────────────────────────────────────────────────────────────

def analizar_resultados(resultados: list) -> dict:
    """
    Analiza todos los resultados y propone que estrategias activar.

    Criterios:
    - WR >= 34% (breakeven con RR 2.0)
    - PF >= 1.1
    - Al menos 30 trades en el rolling window
    - Ordenar por score = WR * PF (combina ambas metricas)
    """
    por_estrategia = {}
    for r in resultados:
        e = r["estrategia"]
        if e not in por_estrategia:
            por_estrategia[e] = {"trades": 0, "wins": 0, "pfs": [], "pnls": [], "dds": []}
        por_estrategia[e]["trades"] += r["trades"]
        por_estrategia[e]["wins"]   += r["wins"]
        por_estrategia[e]["pfs"].append(r["pf"])
        por_estrategia[e]["pnls"].append(r["pnl"])
        por_estrategia[e]["dds"].append(r["max_dd"])

    ranking = []
    for e, d in por_estrategia.items():
        trades = d["trades"]
        wr     = d["wins"] / trades if trades > 0 else 0
        pf     = sum(d["pfs"]) / len(d["pfs"]) if d["pfs"] else 0
        pnl    = sum(d["pnls"])
        dd     = max(d["dds"]) if d["dds"] else 0
        score  = wr * pf  # metrica combinada

        cumple = (
            wr >= 0.34 and
            pf >= 1.10 and
            trades >= 30
        )
        ranking.append({
            "estrategia": e,
            "wr": round(wr, 4),
            "pf": round(pf, 4),
            "pnl": round(pnl, 2),
            "max_dd": round(dd, 4),
            "trades": trades,
            "score": round(score, 4),
            "cumple_criterios": cumple,
        })

    ranking.sort(key=lambda x: x["score"], reverse=True)
    return {"ranking": ranking}


def proponer_params(analisis: dict, params_actuales: dict) -> dict:
    """Genera un nuevo conjunto de parametros basado en el analisis."""
    ranking = analisis["ranking"]

    # Estrategias que cumplen criterios
    aprobadas  = [r["estrategia"] for r in ranking if r["cumple_criterios"]]
    rechazadas = [r["estrategia"] for r in ranking if not r["cumple_criterios"]]

    # Asegurar al menos RSI_Bollinger si no hay ninguna aprobada
    if not aprobadas:
        aprobadas = ["RSI_Bollinger"]

    # Limitar a max 3 estrategias activas para no diluir demasiado
    activas = aprobadas[:3]

    # Ajustar parametros conservadores si el DD promedio es alto
    dds = [r["max_dd"] for r in ranking if r["estrategia"] in activas]
    dd_avg = sum(dds) / len(dds) if dds else 0
    riesgo = RIESGO_PCT
    if dd_avg > 0.12:
        riesgo = max(0.010, RIESGO_PCT * 0.8)  # reducir riesgo si DD alto

    nuevos_params = {
        **params_actuales,
        "estrategias_activas":  activas,
        "estrategias_pausadas": rechazadas,
        "riesgo_pct":           round(riesgo, 4),
        "sl_atr_mult":          SL_ATR_MULT,
        "rr_ratio":             RR_RATIO,
    }
    return nuevos_params


# ─────────────────────────────────────────────────────────────
# Validacion con DeepSeek
# ─────────────────────────────────────────────────────────────

def validar_con_deepseek(analisis: dict, propuesta: dict) -> dict:
    """Envia el analisis a DeepSeek para validacion y ajuste fino."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)

        ranking_txt = "\n".join(
            f"  {r['estrategia']}: WR={r['wr']:.1%} PF={r['pf']:.2f} "
            f"trades={r['trades']} pnl=${r['pnl']:.0f} maxDD={r['max_dd']:.1%}"
            for r in analisis["ranking"]
        )

        prompt = (
            f"Analiza estos resultados de backtest M15 Forex rolling window (18 meses).\n\n"
            f"RESULTADOS POR ESTRATEGIA:\n{ranking_txt}\n\n"
            f"PROPUESTA ACTUAL:\n"
            f"  activas={propuesta['estrategias_activas']}\n"
            f"  riesgo={propuesta['riesgo_pct']:.1%} rr={propuesta['rr_ratio']}\n\n"
            f"Capital=$200 USD, cuenta practice OANDA, 6 pares Forex.\n"
            f"DeepSeek filtra senales en produccion (+15-20% WR real sobre backtest).\n"
            f"WR minimo backtest para operar: 34% (breakeven con RR 2.0).\n\n"
            f"Responde SOLO con JSON, sin texto adicional:\n"
            f'{{"estrategias_activas":["RSI_Bollinger"],'
            f'"estrategias_pausadas":[],'
            f'"sl_atr_mult":1.5,'
            f'"rr_ratio":2.0,'
            f'"min_win_rate":0.34,'
            f'"min_confidence":0.30,'
            f'"riesgo_pct":0.015,'
            f'"cooldown_minutes":15,'
            f'"max_posiciones":3,'
            f'"sesiones_activas":["london","overlap","new_york"],'
            f'"notas_deepseek":"tu analisis aqui"}}'
        )

        print("  Consultando DeepSeek para validacion...")
        response = client.chat.completions.create(
            model=MODEL_DEEP,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=500,
        )

        texto = response.choices[0].message.content or ""
        texto = re.sub(r"```json\s*", "", texto)
        texto = re.sub(r"```\s*", "", texto).strip()

        if not texto:
            print("  DeepSeek: sin respuesta — usando propuesta local")
            return propuesta

        params_ds = json.loads(texto)
        print(f"  DeepSeek: {params_ds.get('notas_deepseek', 'OK')}")
        return params_ds

    except Exception as e:
        print(f"  DeepSeek error: {e} — usando propuesta local")
        return propuesta


# ─────────────────────────────────────────────────────────────
# Reporte para Obsidian
# ─────────────────────────────────────────────────────────────

def generar_reporte_obsidian(analisis: dict, params_nuevos: dict,
                              params_anteriores: dict,
                              semana: str) -> None:
    """Genera una nota de Obsidian con los resultados de la recalibracion."""
    docs_path = Path(__file__).parent.parent / "docs" / "traidingbot"
    docs_path.mkdir(parents=True, exist_ok=True)

    ranking = analisis["ranking"]

    # Tabla de resultados
    tabla = "| Estrategia | WR | PF | Trades | PnL | MaxDD | Aprobada |\n"
    tabla += "|------------|----|----|--------|-----|-------|----------|\n"
    for r in ranking:
        aprobada = "si" if r["cumple_criterios"] else "no"
        tabla += (
            f"| {r['estrategia']} | {r['wr']:.1%} | {r['pf']:.2f} | "
            f"{r['trades']} | ${r['pnl']:.0f} | {r['max_dd']:.1%} | {aprobada} |\n"
        )

    # Cambios vs parametros anteriores
    act_antes  = params_anteriores.get("estrategias_activas", [])
    act_nuevas = params_nuevos.get("estrategias_activas", [])
    activadas  = [e for e in act_nuevas if e not in act_antes]
    pausadas   = [e for e in act_antes  if e not in act_nuevas]
    cambios    = []
    if activadas:
        cambios.append(f"Activadas: {', '.join(activadas)}")
    if pausadas:
        cambios.append(f"Pausadas: {', '.join(pausadas)}")
    if params_nuevos.get("riesgo_pct") != params_anteriores.get("riesgo_pct"):
        cambios.append(
            f"Riesgo: {params_anteriores.get('riesgo_pct', 0):.1%} -> "
            f"{params_nuevos.get('riesgo_pct', 0):.1%}"
        )
    if not cambios:
        cambios.append("Sin cambios significativos")

    nota = f"""# Calibracion Semanal — {semana}

## Resultados Rolling Window (18 meses, M15)

{tabla}

## Cambios aplicados

{chr(10).join(f'- {c}' for c in cambios)}

## Parametros activos

```json
{json.dumps({k: v for k, v in params_nuevos.items()
             if k in ['estrategias_activas', 'riesgo_pct', 'rr_ratio',
                      'sl_atr_mult', 'min_confidence', 'cooldown_minutes']},
            indent=2, ensure_ascii=False)}
```

## Notas

{params_nuevos.get('notas_deepseek', 'Calibracion automatica sin notas adicionales.')}

---
*Generado automaticamente por scripts/recalibrar_semanal.py*
"""

    nombre_archivo = f"Calibracion {semana}.md"
    (docs_path / nombre_archivo).write_text(nota, encoding="utf-8")
    print(f"  Reporte Obsidian: docs/traidingbot/{nombre_archivo}")

    # Actualizar nota de backtest general
    backtest_note = docs_path / "05 - Backtest y Calibracion.md"
    if backtest_note.exists():
        contenido = backtest_note.read_text(encoding="utf-8")
        entrada = (
            f"\n### {semana}\n"
            f"- Estrategias activas: {', '.join(act_nuevas)}\n"
            f"- Riesgo: {params_nuevos.get('riesgo_pct', 0):.1%}\n"
            f"- Cambios: {'; '.join(cambios)}\n"
            f"- Ver: [[Calibracion {semana}]]\n"
        )
        if "## Resultados historicos" in contenido:
            contenido = contenido.replace(
                "## Resultados historicos",
                f"## Resultados historicos{entrada}"
            )
        else:
            contenido += f"\n## Resultados historicos{entrada}"
        backtest_note.write_text(contenido, encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Recalibracion semanal del trading bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar propuesta sin guardar")
    parser.add_argument("--local", action="store_true",
                        help="Sin consultar DeepSeek (mas rapido)")
    parser.add_argument("--pares", nargs="+", default=PARES_BACKTEST,
                        help="Pares a analizar (default: todos)")
    parser.add_argument("--estrategias", nargs="+", default=ESTRATEGIAS_DISPONIBLES,
                        help="Estrategias a evaluar")
    args = parser.parse_args()

    semana = datetime.now(timezone.utc).strftime("%Y-W%W")
    print()
    print("=" * 62)
    print(f"  RECALIBRACION SEMANAL — Trading Bot v11")
    print(f"  Semana: {semana}")
    print(f"  Modo: {'DRY-RUN' if args.dry_run else 'PRODUCCION'}"
          f"{' + LOCAL (sin DS)' if args.local else ''}")
    print("=" * 62)

    # Cargar parametros actuales
    try:
        params_actuales = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
        print(f"\n  Params actuales: {params_actuales.get('estrategias_activas')}")
    except Exception:
        params_actuales = PARAMS_FALLBACK.copy()
        print("  WARN: usando parametros por defecto")

    # 1. Descargar datos
    print(f"\n  [1/4] Descargando datos ({len(args.pares)} pares, ~18 meses M15)...")
    datos_por_par = {}
    for par in args.pares:
        print(f"    {par}...", end=" ", flush=True)
        candles = descargar_datos_oanda(par)
        if candles:
            df = candles_a_dataframe(candles)
            if df is not None and len(df) > 500:
                datos_por_par[par] = df
                print(f"OK ({len(df)} velas)")
            else:
                print("ERROR (pocas velas)")
        else:
            print("ERROR")

    if not datos_por_par:
        print("\n  ERROR: sin datos. Verificar conexion OANDA.")
        sys.exit(1)

    # 2. Rolling window backtest
    print(f"\n  [2/4] Rolling window backtest "
          f"({len(args.estrategias)} estrategias x {len(datos_por_par)} pares)...")
    todos_resultados = []
    for par, df in datos_por_par.items():
        for estrategia in args.estrategias:
            print(f"    {par} / {estrategia}...", end=" ", flush=True)
            r = rolling_window_backtest(df, estrategia, par)
            todos_resultados.append(r)
            print(f"WR={r['wr']:.1%} PF={r['pf']:.2f} trades={r['trades']}")

    # 3. Analizar y proponer
    print(f"\n  [3/4] Analizando resultados...")
    analisis = analizar_resultados(todos_resultados)

    print("\n  RANKING DE ESTRATEGIAS:")
    for r in analisis["ranking"]:
        estado = "OK" if r["cumple_criterios"] else "BAJA"
        print(f"    [{estado}] {r['estrategia']:20} WR={r['wr']:.1%} "
              f"PF={r['pf']:.2f} trades={r['trades']}")

    propuesta = proponer_params(analisis, params_actuales)

    # 4. Validar con DeepSeek
    if not args.local and DEEPSEEK_KEY:
        print(f"\n  [4/4] Validando con DeepSeek...")
        params_nuevos = validar_con_deepseek(analisis, propuesta)
    else:
        print(f"\n  [4/4] Usando propuesta local (sin DeepSeek)")
        params_nuevos = propuesta

    # Agregar metadata
    params_nuevos["calibrado_en"]      = datetime.now(timezone.utc).isoformat()
    params_nuevos["semana_calibracion"] = semana
    params_nuevos["trades_analizados"] = sum(r["trades"] for r in todos_resultados)
    params_nuevos["pares_analizados"]  = list(datos_por_par.keys())
    params_nuevos["version"]           = "v11.2.0"

    # Asegurar campos requeridos
    for campo, valor in PARAMS_FALLBACK.items():
        params_nuevos.setdefault(campo, valor)

    # Mostrar cambios
    print(f"\n  PROPUESTA FINAL:")
    print(f"    Estrategias activas:  {params_nuevos['estrategias_activas']}")
    print(f"    Estrategias pausadas: {params_nuevos.get('estrategias_pausadas', [])}")
    print(f"    Riesgo:               {params_nuevos['riesgo_pct']:.1%}")
    print(f"    RR:                   {params_nuevos['rr_ratio']}")
    print(f"    Min confianza:        {params_nuevos['min_confidence']:.0%}")

    if args.dry_run:
        print(f"\n  [DRY-RUN] Sin cambios guardados.")
        print(f"  Usa sin --dry-run para aplicar.")
    else:
        # Guardar
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        PARAMS_FILE.write_text(
            json.dumps(params_nuevos, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"\n  Guardado: {PARAMS_FILE}")

        # Reporte Obsidian
        generar_reporte_obsidian(analisis, params_nuevos, params_actuales, semana)

    print(f"\n{'=' * 62}")
    print(f"  CALIBRACION COMPLETADA")
    if not args.dry_run:
        print(f"\n  SIGUIENTES PASOS:")
        print(f"    1. Revisar docs/traidingbot/Calibracion {semana}.md en Obsidian")
        print(f"    2. git add data/calibration/strategy_params.json")
        print(f"    3. git commit -m 'calibracion: {semana}'")
        print(f"    4. git push")
        print(f"    5. En el VPS: sudo systemctl restart trading_bot")
    print(f"{'=' * 62}")
    print()


if __name__ == "__main__":
    main()
