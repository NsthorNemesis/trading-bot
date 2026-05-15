"""
database.py — Capa de datos con DuckDB
=======================================
Almacena velas OHLCV de múltiples pares y timeframes.
Sin servidor — archivo local, consultas analíticas en ms.
"""
import duckdb
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "candles.duckdb"


def get_conn() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def init_db():
    """Crea tablas si no existen."""
    with get_conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                ts        TIMESTAMPTZ NOT NULL,
                pair      VARCHAR     NOT NULL,
                timeframe VARCHAR     NOT NULL,
                open      DOUBLE      NOT NULL,
                high      DOUBLE      NOT NULL,
                low       DOUBLE      NOT NULL,
                close     DOUBLE      NOT NULL,
                volume    BIGINT      DEFAULT 0,
                PRIMARY KEY (ts, pair, timeframe)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_pair_tf
            ON candles (pair, timeframe, ts)
        """)


def insert_candles(rows: List[Dict], pair: str, timeframe: str = "M15"):
    """
    Inserta o reemplaza velas en bulk.
    rows: lista de dicts con keys: timestamp (str ISO), open, high, low, close, volume
    """
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["ts"]        = pd.to_datetime(df["timestamp"], utc=True)
    df["pair"]      = pair
    df["timeframe"] = timeframe
    df = df[["ts", "pair", "timeframe", "open", "high", "low", "close", "volume"]]
    df = df.drop_duplicates(subset=["ts", "pair", "timeframe"])

    with get_conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO candles
            SELECT * FROM df
        """)
    return len(df)


def get_candles(
    pair: str,
    start: datetime,
    end: datetime,
    timeframe: str = "M15",
) -> pd.DataFrame:
    """Retorna DataFrame con velas del período solicitado."""
    with get_conn() as con:
        df = con.execute("""
            SELECT ts, open, high, low, close, volume
            FROM candles
            WHERE pair = ? AND timeframe = ?
              AND ts >= ? AND ts < ?
            ORDER BY ts
        """, [pair, timeframe, start, end]).df()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def get_coverage() -> List[Dict]:
    """Retorna resumen de cobertura de datos por par/timeframe."""
    with get_conn() as con:
        rows = con.execute("""
            SELECT
                pair,
                timeframe,
                COUNT(*)                           AS velas,
                MIN(ts)::DATE                      AS desde,
                MAX(ts)::DATE                      AS hasta,
                DATEDIFF('day', MIN(ts), MAX(ts))  AS dias
            FROM candles
            GROUP BY pair, timeframe
            ORDER BY pair, timeframe
        """).fetchall()
    return [
        {"pair": r[0], "timeframe": r[1], "velas": r[2],
         "desde": str(r[3]), "hasta": str(r[4]), "dias": r[5]}
        for r in rows
    ]


def get_db_size_mb() -> float:
    return round(DB_PATH.stat().st_size / 1_048_576, 1) if DB_PATH.exists() else 0.0
