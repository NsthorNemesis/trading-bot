"""
diagnostico_hoy.py — Simulación de señales del día actual
==========================================================
Descarga M15 de hoy desde OANDA y replica vela por vela la lógica
de RSI_Bollinger + filtro de sesión para saber si el bot debería
haber señalado y cuándo.

Uso:
  cd /root/trading_bot_v11
  python3 diagnostico_hoy.py
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pandas_ta as ta
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from dotenv import load_dotenv

load_dotenv()

OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN", "")
OANDA_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV     = os.getenv("OANDA_ENVIRONMENT", "practice")

PARES = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD"]

# Sesiones (UTC)
SESIONES = {
    "london":   (7, 12),
    "overlap":  (12, 17),
    "new_york": (13, 21),
}
SESIONES_ACTIVAS = {"london", "overlap", "new_york"}

# Parámetros RSI_Bollinger (igual que producción)
RSI_LOW   = 32
RSI_HIGH  = 68
BB_TOL    = 0.002   # 0.2% de tolerancia sobre la banda


def sesion_actual(hora_utc: int) -> str:
    for nombre, (ini, fin) in SESIONES.items():
        if ini <= hora_utc < fin:
            return nombre
    return "asian"


def descargar_m15(api, par: str, horas: int = 30) -> pd.DataFrame:
    """Descarga las últimas N horas de velas M15 para el par."""
    ahora  = datetime.now(timezone.utc)
    desde  = ahora - timedelta(hours=horas)
    params = {
        "granularity": "M15",
        "from": desde.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   ahora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": "M",
    }
    req  = instruments.InstrumentsCandles(par, params=params)
    api.request(req)
    candles = req.response.get("candles", [])
    if not candles:
        return pd.DataFrame()

    rows = []
    for c in candles:
        if c.get("complete", False):
            mid = c["mid"]
            rows.append({
                "time":  c["time"][:19],
                "Open":  float(mid["o"]),
                "High":  float(mid["h"]),
                "Low":   float(mid["l"]),
                "Close": float(mid["c"]),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    return df


def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    """Añade RSI, Bollinger Bands, ATR, EMA al DataFrame."""
    df = df.copy()
    df.ta.rsi(length=14,  append=True)
    df.ta.bbands(length=20, std=2.0, append=True)
    df.ta.atr(length=14,  append=True)
    df.ta.ema(length=20,  append=True)
    df.ta.ema(length=50,  append=True)
    return df


def revisar_señales(df: pd.DataFrame, par: str) -> list:
    """
    Recorre el DataFrame vela por vela y registra cada vez que
    RSI_Bollinger habría generado señal en sesión activa.
    """
    señales = []

    # Columnas de Bollinger (pandas_ta usa BBL_20_2.0, BBU_20_2.0)
    bbl_col = next((c for c in df.columns if c.startswith("BBL_")), None)
    bbu_col = next((c for c in df.columns if c.startswith("BBU_")), None)
    rsi_col = next((c for c in df.columns if c.startswith("RSI_")), None)

    if not bbl_col or not bbu_col or not rsi_col:
        print(f"  ⚠ {par}: columnas de indicadores no encontradas")
        return señales

    for i in range(20, len(df)):
        row   = df.iloc[i]
        ts    = df.index[i]
        hora  = ts.hour
        sesion = sesion_actual(hora)

        # Filtro de sesión
        if sesion not in SESIONES_ACTIVAS:
            continue

        rsi    = row.get(rsi_col, 50.0)
        precio = row["Close"]
        bbl    = row.get(bbl_col, 0.0)
        bbu    = row.get(bbu_col, 0.0)

        if pd.isna(rsi) or pd.isna(bbl) or pd.isna(bbu):
            continue

        en_bbl = precio < bbl * (1 + BB_TOL) if bbl > 0 else False
        en_bbu = precio > bbu * (1 - BB_TOL) if bbu > 0 else False

        if rsi < RSI_LOW and en_bbl:
            señales.append({
                "par": par, "dir": "LONG", "ts": ts,
                "sesion": sesion, "rsi": round(rsi, 1),
                "precio": precio, "bbl": round(bbl, 5),
            })
        elif rsi > RSI_HIGH and en_bbu:
            señales.append({
                "par": par, "dir": "SHORT", "ts": ts,
                "sesion": sesion, "rsi": round(rsi, 1),
                "precio": precio, "bbu": round(bbu, 5),
            })

    return señales


def main():
    if not OANDA_TOKEN:
        print("ERROR: OANDA_ACCESS_TOKEN no encontrado en .env")
        sys.exit(1)

    api = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)

    ahora = datetime.now(timezone.utc)
    print("=" * 62)
    print(f"  DIAGNÓSTICO DE SEÑALES — {ahora.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Estrategia: RSI_Bollinger (RSI<{RSI_LOW}/>{RSI_HIGH} + BB)")
    print(f"  Sesiones:   London / Overlap / New York")
    print(f"  Pares:      {', '.join(PARES)}")
    print("=" * 62)

    todas_las_señales = []

    for par in PARES:
        print(f"\n▶ {par}...")
        df = descargar_m15(api, par, horas=30)
        if df.empty:
            print(f"  Sin datos")
            continue

        df = calcular_indicadores(df)
        señales = revisar_señales(df, par)

        # Mostrar últimos valores para contexto
        ult = df.iloc[-1]
        rsi_col = next((c for c in df.columns if c.startswith("RSI_")), "RSI_14")
        bbl_col = next((c for c in df.columns if c.startswith("BBL_")), "BBL")
        bbu_col = next((c for c in df.columns if c.startswith("BBU_")), "BBU")
        rsi_now = ult.get(rsi_col, float("nan"))
        bbl_now = ult.get(bbl_col, float("nan"))
        bbu_now = ult.get(bbu_col, float("nan"))
        precio  = ult["Close"]
        sesion_now = sesion_actual(ahora.hour)

        dist_bbl = (precio - bbl_now) / bbl_now * 100 if bbl_now > 0 else float("nan")
        dist_bbu = (bbu_now - precio) / bbu_now * 100 if bbu_now > 0 else float("nan")

        print(f"  Última vela ({df.index[-1].strftime('%H:%M')} UTC)")
        print(f"    RSI={rsi_now:.1f}  Precio={precio:.5f}")
        print(f"    BBL={bbl_now:.5f} ({dist_bbl:+.2f}% desde precio)")
        print(f"    BBU={bbu_now:.5f} ({dist_bbu:+.2f}% desde precio)")
        print(f"    Sesión actual: {sesion_now}")

        if señales:
            print(f"  ✅ {len(señales)} señal(es) detectada(s) hoy:")
            for s in señales:
                nivel = s.get("bbl", s.get("bbu", 0))
                print(
                    f"    {s['ts'].strftime('%H:%M UTC')}  "
                    f"{s['dir']:5s}  RSI={s['rsi']}  "
                    f"precio={s['precio']:.5f}  "
                    f"BB={'BBL' if s['dir']=='LONG' else 'BBU'}={nivel:.5f}  "
                    f"[{s['sesion']}]"
                )
            todas_las_señales.extend(señales)
        else:
            print(f"  — Sin señales RSI_Bollinger en sesiones activas")

    # Resumen final
    print("\n" + "=" * 62)
    print(f"  RESUMEN: {len(todas_las_señales)} señal(es) totales en {ahora.strftime('%Y-%m-%d')}")
    if todas_las_señales:
        por_par  = {}
        for s in todas_las_señales:
            por_par[s["par"]] = por_par.get(s["par"], 0) + 1
        for p, n in sorted(por_par.items(), key=lambda x: -x[1]):
            print(f"    {p}: {n} señal(es)")
    else:
        print("  El mercado de hoy no generó condiciones RSI_Bollinger.")
        print("  Posibles razones:")
        print("    - RSI en zona neutral (30-70) durante todo el día")
        print("    - Precio no tocó las bandas de Bollinger")
        print("    - Mercado en tendencia (mean-reversion no aplica)")
    print("=" * 62)


if __name__ == "__main__":
    main()
