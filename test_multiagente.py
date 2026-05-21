#!/usr/bin/env python3
"""
test_multiagente.py — Validación de la arquitectura multi-agente v13

Ejecutar en Windows (donde DeepSeek es alcanzable):
    python test_multiagente.py

Evalúa los últimos 5 candles de cada par activo usando los 3 agentes
en paralelo + agente decisor. Imprime el razonamiento completo de cada
agente para que puedas ver cómo "piensan".
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Setup paths ──────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

DEEPSEEK_KEY      = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_FAST        = "deepseek-chat"    # V3 — rápido, económico
MODEL_DEEP        = "deepseek-reasoner" # R1 — razonamiento profundo

if not DEEPSEEK_KEY:
    print("ERROR: DEEPSEEK_API_KEY no encontrado en .env")
    sys.exit(1)

from openai import OpenAI
DS = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)

# ── Cargar datos históricos ──────────────────────────────────────────────────
HIST_DIR = BASE / "data" / "historical"
PARAMS   = json.loads((BASE / "data" / "calibration" / "strategy_params.json").read_text())
PARES    = PARAMS.get("pares_activos", ["EUR_USD", "GBP_USD", "USD_JPY"])

# ── Helpers de indicadores (standalone, sin market_agent) ───────────────────
import numpy as np
import pandas as pd

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    n = len(c)
    P = 14

    tr = np.zeros(n)
    tr[1:] = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))

    atr = np.full(n, np.nan)
    if n > P:
        atr[P] = tr[1:P+1].mean()
        for i in range(P+1, n):
            atr[i] = (atr[i-1]*(P-1) + tr[i]) / P

    def ema(s, n):
        r = np.full(len(s), np.nan)
        r[n-1] = np.nanmean(s[:n])
        k = 2/(n+1)
        for i in range(n, len(s)):
            r[i] = s[i]*k + r[i-1]*(1-k)
        return r

    ema20 = ema(c, 20); ema50 = ema(c, 50)
    e12 = ema(c, 12); e26 = ema(c, 26)
    ml  = e12 - e26
    macd_dif = ml - ema(np.nan_to_num(ml), 9)

    delta = np.diff(c, prepend=c[0])
    g = np.where(delta>0, delta, 0.0); ls = np.where(delta<0, -delta, 0.0)
    ag = np.full(n, np.nan); al = np.full(n, np.nan)
    if n > P:
        ag[P] = g[1:P+1].mean(); al[P] = ls[1:P+1].mean()
        for i in range(P+1, n):
            ag[i] = (ag[i-1]*(P-1)+g[i])/P; al[i] = (al[i-1]*(P-1)+ls[i])/P
    rsi = 100 - 100/(1+np.where(al==0, 100, np.where(np.isnan(ag)|np.isnan(al), np.nan, ag/al)))

    pdm = np.zeros(n); mdm = np.zeros(n)
    pdm[1:] = np.where((h[1:]-h[:-1])>(l[:-1]-l[1:]),np.maximum(h[1:]-h[:-1],0),0)
    mdm[1:] = np.where((l[:-1]-l[1:])>(h[1:]-h[:-1]),np.maximum(l[:-1]-l[1:],0),0)
    s_tr = np.full(n, np.nan); s_pd = np.full(n, np.nan); s_md = np.full(n, np.nan)
    if n > P:
        s_tr[P]=tr[1:P+1].sum(); s_pd[P]=pdm[1:P+1].sum(); s_md[P]=mdm[1:P+1].sum()
        for i in range(P+1, n):
            s_tr[i]=s_tr[i-1]-s_tr[i-1]/P+tr[i]
            s_pd[i]=s_pd[i-1]-s_pd[i-1]/P+pdm[i]
            s_md[i]=s_md[i-1]-s_md[i-1]/P+mdm[i]
    pdi = np.where(s_tr>0, s_pd/s_tr*100, 0.0)
    mdi = np.where(s_tr>0, s_md/s_tr*100, 0.0)
    dx  = np.where((pdi+mdi)>0, abs(pdi-mdi)/(pdi+mdi)*100, 0.0)
    adx = np.full(n, np.nan)
    if n > 2*P:
        adx[2*P-1] = dx[P:2*P].mean()
        for i in range(2*P, n):
            adx[i] = (adx[i-1]*(P-1)+dx[i])/P

    bb_mid = np.full(n, np.nan); bb_std = np.full(n, np.nan)
    for i in range(20, n):
        bb_mid[i] = c[i-20:i].mean(); bb_std[i] = c[i-20:i].std()

    df = df.copy()
    df["ATR_14"]  = atr;  df["EMA_20"] = ema20; df["EMA_50"] = ema50
    df["MACD_DIF"]= macd_dif; df["RSI_14"] = rsi; df["ADX_14"] = adx
    df["BBL_20"]  = bb_mid - 2*bb_std; df["BBU_20"] = bb_mid + 2*bb_std
    df["Close"]   = df["close"]; df["Open"] = df["open"]
    df["High"]    = df["high"];  df["Low"]  = df["low"]
    return df

# ── Prompts ──────────────────────────────────────────────────────────────────
SYS_TECNICO = """Eres un analista técnico experto en forex M15.
Analiza las velas e indicadores y produce un resumen de 3-5 oraciones:
qué patrón ves, dirección dominante, confluencia de indicadores,
y si el setup tiene calidad para una entrada. Sé específico y objetivo."""

SYS_REGIMEN = """Eres un analista de régimen de mercado en forex M15.
Determina si el mercado está en TENDENCIA, RANGO o TRANSICIÓN, y si
la volatilidad favorece operar. Responde en 2-3 oraciones concretas."""

SYS_DECISION = """Eres un trader de forex profesional y conservador.
Solo aprueba setups de ALTA PROBABILIDAD con al menos 2 factores de confluencia.
La inacción es válida — si hay duda, NO operes.
Responde EXCLUSIVAMENTE con JSON válido:
{"trade": true/false, "dir": "long"/"short"/null, "conf": 0.0-1.0, "razon": "máx 25 palabras"}"""

# ── Llamadas a DeepSeek ──────────────────────────────────────────────────────

def llamar_ds(system: str, user: str, model=MODEL_FAST, max_tokens=250, json_mode=False) -> str:
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.2,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["temperature"] = 0.1
    resp = DS.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()

async def agente_tecnico(par: str, df: pd.DataFrame) -> str:
    u = df.iloc[-1]
    velas = []
    for _, row in df.tail(15).iterrows():
        ts = str(row.get("timestamp", ""))[:16]
        velas.append(f"{ts}  O:{row['open']:.5f}  H:{row['high']:.5f}  "
                     f"L:{row['low']:.5f}  C:{row['close']:.5f}")
    prompt = (
        f"Par: {par} — últimas 15 velas M15:\n" + "\n".join(velas) + "\n\n"
        f"Indicadores:\n"
        f"  RSI={u.get('RSI_14',50):.1f}  ATR={u.get('ATR_14',0):.5f}  "
        f"ADX={u.get('ADX_14',0):.1f}\n"
        f"  EMA20={u.get('EMA_20',0):.5f}  EMA50={u.get('EMA_50',0):.5f}  "
        f"MACD_hist={u.get('MACD_DIF',0):.5f}\n"
        f"  BB_low={u.get('BBL_20',0):.5f}  BB_high={u.get('BBU_20',0):.5f}\n\n"
        f"¿Qué patrón o setup técnico ves? ¿Hacia dónde apuntan los indicadores?"
    )
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: llamar_ds(SYS_TECNICO, prompt, max_tokens=220))

async def agente_regimen(par: str, df: pd.DataFrame) -> str:
    u = df.iloc[-1]
    cierres = [f"{x:.5f}" for x in df["close"].tail(10).tolist()]
    atrs    = [f"{x:.5f}" for x in df["ATR_14"].dropna().tail(5).tolist()]
    ema20 = u.get("EMA_20", 0); ema50 = u.get("EMA_50", 0)
    prompt = (
        f"Par: {par} — Régimen M15\n"
        f"ADX={u.get('ADX_14',0):.1f} | EMA20={'sobre' if ema20>ema50 else 'bajo'} EMA50 "
        f"(spread={abs(ema20-ema50):.5f}) | RSI={u.get('RSI_14',50):.1f}\n"
        f"Últimos 10 cierres: {', '.join(cierres)}\n"
        f"Últimos 5 ATR: {', '.join(atrs)}\n\n"
        f"¿Tendencia, rango o transición? ¿Favorece operar ahora?"
    )
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: llamar_ds(SYS_REGIMEN, prompt, max_tokens=150))

async def agente_decision(par: str, df: pd.DataFrame,
                           tecnico: str, regimen: str) -> dict:
    u      = df.iloc[-1]
    precio = float(u.get("close", 0))
    atr    = float(u.get("ATR_14", 0) or 0)
    adx    = float(u.get("ADX_14", 0) or 0)
    prompt = (
        f"=== DECISIÓN: {par} ===\n"
        f"Precio={precio:.5f} | ATR={atr:.5f} | ADX={adx:.1f} | RR={PARAMS.get('rr_ratio',2.0)}\n\n"
        f"ANÁLISIS TÉCNICO:\n{tecnico}\n\n"
        f"RÉGIMEN DE MERCADO:\n{regimen}\n\n"
        f"¿Debo abrir operación en {par} ahora? Responde con JSON."
    )
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        None, lambda: llamar_ds(SYS_DECISION, prompt, max_tokens=120, json_mode=True)
    )
    try:
        return json.loads(raw)
    except Exception:
        return {"trade": False, "dir": None, "conf": 0.0, "razon": f"JSON error: {raw[:50]}"}

# ── Evaluar un par ───────────────────────────────────────────────────────────

async def evaluar_par(par: str) -> dict:
    t0 = time.time()
    # Cargar datos
    path = HIST_DIR / f"{par}_M15.json"
    raw  = json.loads(path.read_text())
    candles = raw if isinstance(raw, list) else raw.get("candles", raw.get("data", []))
    df = pd.DataFrame(candles[-100:])   # últimas 100 velas para indicadores
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ["open","high","low","close"]: df[col] = pd.to_numeric(df[col])
    df = compute_indicators(df)

    # Pre-filtro ADX
    adx_val = float(df["ADX_14"].iloc[-1] or 0)
    adx_min = float(PARAMS.get("adx_min_operar", 14))
    if adx_val < adx_min:
        return {"par": par, "skip": f"ADX={adx_val:.1f} < {adx_min} (mercado plano)"}

    # Lanzar 3 agentes en paralelo
    print(f"\n  [{par}] Lanzando agentes técnico + régimen en paralelo...")
    tecnico_txt, regimen_txt = await asyncio.gather(
        agente_tecnico(par, df),
        agente_regimen(par, df),
    )

    # Decisor
    print(f"  [{par}] Decisor procesando contexto...")
    decision = await agente_decision(par, df, tecnico_txt, regimen_txt)

    elapsed = time.time() - t0
    return {
        "par":       par,
        "precio":    float(df["close"].iloc[-1]),
        "adx":       adx_val,
        "tecnico":   tecnico_txt,
        "regimen":   regimen_txt,
        "decision":  decision,
        "tiempo_s":  round(elapsed, 1),
    }

# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("="*65)
    print("  TEST MULTI-AGENTE v13 — DeepSeek como motor de decisión")
    print(f"  Modelos: {MODEL_FAST} (contexto) | {MODEL_FAST} (decisión)")
    print(f"  Pares: {', '.join(PARES)}")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*65)

    resultados = []
    for par in PARES:
        print(f"\n{'─'*65}")
        print(f"  PAR: {par}")
        print(f"{'─'*65}")
        try:
            r = await evaluar_par(par)
            resultados.append(r)

            if r.get("skip"):
                print(f"  SKIP: {r['skip']}")
                continue

            print(f"\n  Precio actual: {r['precio']:.5f}  |  ADX: {r['adx']:.1f}")
            print(f"\n  🔬 ANÁLISIS TÉCNICO ({MODEL_FAST}):")
            for linea in r["tecnico"].split("\n"):
                print(f"     {linea}")
            print(f"\n  📊 RÉGIMEN DE MERCADO ({MODEL_FAST}):")
            for linea in r["regimen"].split("\n"):
                print(f"     {linea}")

            d = r["decision"]
            trade = d.get("trade", False)
            print(f"\n  {'✅ TRADE' if trade else '⛔ NO TRADE'} — {d.get('razon','')}")
            if trade:
                print(f"  Dirección: {d.get('dir','?').upper()}  |  "
                      f"Confianza: {d.get('conf',0):.0%}")
            print(f"\n  Tiempo total: {r['tiempo_s']}s")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()

    # Resumen
    print(f"\n{'='*65}")
    print("  RESUMEN")
    print(f"{'='*65}")
    trades = [r for r in resultados if r.get("decision",{}).get("trade")]
    skips  = [r for r in resultados if r.get("skip")]
    print(f"  Pares evaluados: {len(resultados)}")
    print(f"  Trades aprobados: {len(trades)}")
    print(f"  Saltados (ADX bajo): {len(skips)}")
    if trades:
        print(f"\n  Trades detectados:")
        for r in trades:
            d = r["decision"]
            print(f"    {r['par']}: {d.get('dir','?').upper()} "
                  f"conf={d.get('conf',0):.0%} — {d.get('razon','')}")
    print()

if __name__ == "__main__":
    asyncio.run(main())
