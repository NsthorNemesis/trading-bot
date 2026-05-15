"""
downloader.py — Descarga e importación de datos históricos
===========================================================
Fuentes soportadas:
  1. OANDA v20 API   — datos recientes (práctica: ~2 años M15)
  2. Histdata.com    — CSVs gratuitos 10+ años M1 → resampleados a M15
  3. CSV genérico    — cualquier CSV con columnas OHLCV
"""
import os
import time
import zipfile
import logging
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import List, Optional

import pandas as pd

from database import insert_candles, init_db

logger = logging.getLogger("downloader")

PARES_OANDA = {
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
    "AUD_USD", "USD_CAD", "NZD_USD", "EUR_GBP",
}

# ── OANDA ─────────────────────────────────────────────────────────────────────

def descargar_oanda(
    pares: List[str],
    token: str,
    account_id: str,
    environment: str = "practice",
    desde: Optional[datetime] = None,
    hasta: Optional[datetime] = None,
    timeframe: str = "M15",
    verbose: bool = True,
) -> dict:
    """
    Descarga velas de OANDA y las guarda en DuckDB.
    Retorna dict con resumen por par: {par: velas_insertadas}.
    """
    try:
        import oandapyV20
        import oandapyV20.endpoints.instruments as instruments
    except ImportError:
        raise ImportError("Instala oandapyV20: pip install oandapyV20")

    client = oandapyV20.API(access_token=token, environment=environment)
    init_db()

    hasta  = hasta  or datetime.now(timezone.utc)
    desde  = desde  or (hasta - timedelta(days=730))  # 2 años por defecto
    bloque = timedelta(weeks=8)
    resultado = {}

    for par in pares:
        if verbose:
            print(f"  {par}: descargando {desde.date()} → {hasta.date()}...", end="", flush=True)
        all_rows = []
        cur = desde
        while cur < hasta:
            fin = min(cur + bloque, hasta)
            params = {
                "granularity": timeframe,
                "from":  cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":    fin.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "price": "M",
            }
            try:
                ep  = instruments.InstrumentsCandles(instrument=par, params=params)
                rv  = client.request(ep)
                for c in rv.get("candles", []):
                    if not c.get("complete", True):
                        continue
                    mid = c.get("mid", {})
                    all_rows.append({
                        "timestamp": c["time"],
                        "open":   float(mid.get("o", 0)),
                        "high":   float(mid.get("h", 0)),
                        "low":    float(mid.get("l", 0)),
                        "close":  float(mid.get("c", 0)),
                        "volume": int(c.get("volume", 0)),
                    })
                if verbose:
                    print(".", end="", flush=True)
            except Exception as e:
                logger.warning(f"{par} bloque {cur.date()}: {e}")
            cur = fin
            time.sleep(0.3)

        n = insert_candles(all_rows, par, timeframe)
        resultado[par] = n
        if verbose:
            print(f" → {n:,} velas ✅")

    return resultado


# ── Histdata.com ───────────────────────────────────────────────────────────────

HISTDATA_PARES = {
    "EUR_USD": "EURUSD",
    "GBP_USD": "GBPUSD",
    "USD_JPY": "USDJPY",
    "USD_CHF": "USDCHF",
    "AUD_USD": "AUDUSD",
    "USD_CAD": "USDCAD",
    "NZD_USD": "NZDUSD",
    "EUR_GBP": "EURGBP",
}


def importar_histdata_csv(
    csv_path: str | Path,
    par: str,
    timeframe_destino: str = "M15",
    verbose: bool = True,
) -> int:
    """
    Importa un CSV de Histdata.com (formato M1) y lo resamplea a M15.

    Formato Histdata M1:
      20150103 170100;1.20350;1.20380;1.20310;1.20340;0

    Parámetros:
        csv_path:          ruta al archivo CSV o ZIP descargado de histdata.com
        par:               nombre OANDA del par, ej: "EUR_USD"
        timeframe_destino: "M15", "H1", "H4", "D1"
        verbose:           imprimir progreso
    """
    init_db()
    csv_path = Path(csv_path)

    # Descomprimir ZIP si es necesario
    contenido = None
    if csv_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(csv_path) as zf:
            csvs = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csvs:
                raise ValueError(f"ZIP sin archivos CSV: {csv_path}")
            contenido = zf.read(csvs[0]).decode("utf-8", errors="replace")
    else:
        contenido = csv_path.read_text(encoding="utf-8", errors="replace")

    # Parsear CSV — Histdata usa ";" como separador
    df = pd.read_csv(
        StringIO(contenido),
        sep=";",
        header=None,
        names=["datetime_str", "open", "high", "low", "close", "volume"],
        dtype={"open": float, "high": float, "low": float, "close": float},
    )

    # Parsear timestamp — formato "20150103 170100"
    df["ts"] = pd.to_datetime(
        df["datetime_str"].astype(str),
        format="%Y%m%d %H%M%S",
        utc=True,
        errors="coerce",
    )
    df = df.dropna(subset=["ts"])
    df = df.sort_values("ts").set_index("ts")

    if verbose:
        print(f"  {par}: {len(df):,} velas M1 leídas ({df.index[0].date()} → {df.index[-1].date()})")

    # Resamplear a timeframe destino
    freq_map = {"M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}
    freq = freq_map.get(timeframe_destino, "15min")

    df_resampled = df[["open", "high", "low", "close", "volume"]].resample(freq).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()

    if verbose:
        print(f"  {par}: {len(df_resampled):,} velas {timeframe_destino} tras resample")

    # Convertir a formato para insertar
    rows = []
    for ts, row in df_resampled.iterrows():
        rows.append({
            "timestamp": ts.isoformat(),
            "open":   float(row["open"]),
            "high":   float(row["high"]),
            "low":    float(row["low"]),
            "close":  float(row["close"]),
            "volume": int(row["volume"]) if pd.notna(row["volume"]) else 0,
        })

    n = insert_candles(rows, par, timeframe_destino)
    if verbose:
        print(f"  {par}: {n:,} velas guardadas en DB ✅")
    return n


def importar_carpeta_histdata(
    carpeta: str | Path,
    timeframe_destino: str = "M15",
    verbose: bool = True,
) -> dict:
    """
    Importa todos los CSV/ZIP de una carpeta.
    Detecta el par por el nombre del archivo (ej: DAT_ASCII_EURUSD_M1_2020.csv).
    """
    carpeta = Path(carpeta)
    resultado = {}

    for f in sorted(carpeta.glob("*.csv")) + sorted(carpeta.glob("*.zip")):
        # Detectar par por nombre de archivo
        nombre = f.stem.upper()
        par_detectado = None
        for par_oanda, par_hist in HISTDATA_PARES.items():
            if par_hist in nombre:
                par_detectado = par_oanda
                break
        if not par_detectado:
            if verbose:
                print(f"  Saltando {f.name} — par no reconocido")
            continue
        if verbose:
            print(f"\n  Importando: {f.name} → {par_detectado}")
        try:
            n = importar_histdata_csv(f, par_detectado, timeframe_destino, verbose)
            resultado[f.name] = {"par": par_detectado, "velas": n, "ok": True}
        except Exception as e:
            logger.error(f"Error importando {f.name}: {e}")
            resultado[f.name] = {"par": par_detectado, "velas": 0, "ok": False, "error": str(e)}

    return resultado


# ── CSV genérico ──────────────────────────────────────────────────────────────

def importar_csv_generico(
    csv_path: str | Path,
    par: str,
    timeframe: str = "M15",
    col_ts: str = "timestamp",
    col_open: str = "open",
    col_high: str = "high",
    col_low: str = "low",
    col_close: str = "close",
    col_volume: str = "volume",
    sep: str = ",",
    verbose: bool = True,
) -> int:
    """
    Importa cualquier CSV con columnas OHLCV de nomenclatura configurable.
    """
    init_db()
    df = pd.read_csv(csv_path, sep=sep)
    df["ts"] = pd.to_datetime(df[col_ts], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")

    rows = [{
        "timestamp": row["ts"].isoformat(),
        "open":   float(row[col_open]),
        "high":   float(row[col_high]),
        "low":    float(row[col_low]),
        "close":  float(row[col_close]),
        "volume": int(row.get(col_volume, 0)) if col_volume in df.columns else 0,
    } for _, row in df.iterrows()]

    n = insert_candles(rows, par, timeframe)
    if verbose:
        print(f"  {par}: {n:,} velas importadas desde CSV ✅")
    return n
