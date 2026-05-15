"""
scripts/setup_datos.py
════════════════════════════════════════════════════════════════
Descarga 5 años de datos OANDA + backtesting + calibración inicial

Uso: python scripts/setup_datos.py
Tiempo: 15-40 minutos
════════════════════════════════════════════════════════════════
"""
import sys, os, json, time, statistics
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from config.settings import (
    OANDA_TOKEN, OANDA_ENV, PARES,
    HIST_DIR, BT_DIR, CALIB_DIR,
    DEEPSEEK_KEY, DEEPSEEK_BASE_URL, MODEL_DEEP,
    DEFAULTS,
)

import oandapyV20
from oandapyV20.contrib.factories import InstrumentsCandlesFactory

# Crear directorios
for d in [HIST_DIR, BT_DIR, CALIB_DIR]:
    d.mkdir(parents=True, exist_ok=True)

V = "\033[92m✓\033[0m"
X = "\033[91m✗\033[0m"
I = "\033[94m→\033[0m"

def log(msg, ok=True):
    sym = V if ok else X
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {sym} {msg}")


# ── PASO 1: DESCARGA ──────────────────────────────────────────────────────────

def descargar_historico():
    print("\n\033[1m── PASO 1: Descarga histórico OANDA (5 años)\033[0m")
    if not OANDA_TOKEN:
        log("OANDA_ACCESS_TOKEN no configurado en .env", ok=False)
        return False

    client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)
    ahora  = datetime.now(timezone.utc)
    desde  = (ahora - timedelta(days=365 * 5)).strftime("%Y-%m-%dT00:00:00Z")

    for par in PARES:
        for tf in ["M15", "H1"]:
            archivo = HIST_DIR / f"{par}_{tf}.json"

            if archivo.exists():
                datos = json.loads(archivo.read_text())
                if len(datos) > 200:
                    log(f"{par} {tf}: {len(datos):,} velas — ya descargado")
                    continue

            print(f"    Descargando {par} {tf}...", end="\r")
            params = {
                "from":        desde,
                "granularity": tf,
                "price":       "M",
                "count":       5000,
            }
            velas = []
            try:
                for req in InstrumentsCandlesFactory(instrument=par, params=params):
                    client.request(req)
                    for c in req.response.get("candles", []):
                        if c.get("complete", True):
                            mid = c.get("mid", {})
                            velas.append({
                                "timestamp": c["time"][:19] + "Z",
                                "open":   float(mid.get("o", 0)),
                                "high":   float(mid.get("h", 0)),
                                "low":    float(mid.get("l", 0)),
                                "close":  float(mid.get("c", 0)),
                                "volume": int(c.get("volume", 0)),
                            })
                    time.sleep(0.2)
                archivo.write_text(json.dumps(velas))
                log(f"{par} {tf}: {len(velas):,} velas descargadas")
            except Exception as e:
                log(f"{par} {tf}: ERROR — {e}", ok=False)

    return True


# ── PASO 2: BACKTESTING ───────────────────────────────────────────────────────

def atr(velas, n=14):
    if len(velas) < n+1: return 0.0
    trs = [max(v["high"]-v["low"],
               abs(v["high"]-velas[i-1]["close"]),
               abs(v["low"]-velas[i-1]["close"]))
           for i, v in enumerate(velas) if i > 0]
    return sum(trs[-n:]) / n

def rsi(cierres, n=14):
    if len(cierres) < n+1: return 50.0
    g = [max(cierres[i]-cierres[i-1], 0) for i in range(1, len(cierres))]
    p = [abs(min(cierres[i]-cierres[i-1], 0)) for i in range(1, len(cierres))]
    ag = sum(g[-n:])/n; ap = sum(p[-n:])/n
    return 100 if ap==0 else 100-(100/(1+ag/ap))

def ema(cierres, n):
    if len(cierres) < n: return cierres[-1] if cierres else 0.0
    k = 2/(n+1); e = sum(cierres[:n])/n
    for p in cierres[n:]: e = p*k + e*(1-k)
    return e

def sesion(ts):
    try:
        h = int(ts[11:13])
        if 7<=h<13: return "london"
        if 13<=h<17: return "overlap"
        if 17<=h<22: return "new_york"
        return "asia"
    except: return "unknown"

def backtest_estrategia(nombre, velas, par, params):
    if len(velas) < 120: return None
    pip = 0.01 if "JPY" in par else 0.0001
    rr  = params["rr_ratio"]; sl_m = params["sl_atr_mult"]
    min_sl = params["min_sl_pips"] * pip
    capital = 1000.0; peak = capital; max_dd = 0.0
    trades = []; cooldown = {}; cierres = [v["close"] for v in velas]

    for i in range(100, len(velas)-60):
        ts = velas[i]["timestamp"]; ses = sesion(ts)
        if ses == "asia" and par in ["EUR_USD","GBP_USD"]: continue
        if cooldown.get(ses, 0) > i-15: continue

        atr_v = atr(velas[max(0,i-50):i+1])
        rsi_v = rsi(cierres[max(0,i-20):i+1])
        e20   = ema(cierres[max(0,i-20):i+1], 20)
        e50   = ema(cierres[max(0,i-50):i+1], 50)
        p     = velas[i]["close"]

        if atr_v == 0: continue

        dir_, conf = None, 0
        o_,h_,l_,c_ = velas[i]["open"],velas[i]["high"],velas[i]["low"],velas[i]["close"]
        cuerpo = abs(c_-o_); rango = h_-l_

        if nombre == "Hammer":
            si = min(o_,c_)-l_; ss = h_-max(o_,c_)
            if cuerpo>0 and si>=2*cuerpo and ss<=cuerpo*0.5 and e20<e50*0.9998:
                dir_="long"; conf=min(si/(cuerpo+1e-9)/5,1.0)
        elif nombre == "Doji":
            if rango>0 and rango>atr_v*0.4 and cuerpo/rango<0.10:
                dir_="long" if c_>e20 else "short"; conf=0.5
        elif nombre == "Engulfing":
            if i>0:
                po,pca=velas[i-1]["open"],velas[i-1]["close"]
                if c_>o_ and c_>po and o_<pca and abs(c_-o_)>abs(pca-po)*1.05:
                    dir_="long"; conf=0.65
                elif c_<o_ and c_<po and o_>pca and abs(c_-o_)>abs(pca-po)*1.05:
                    dir_="short"; conf=0.65
        elif nombre == "RSI_Divergence":
            if i>=10:
                ra=rsi(cierres[max(0,i-21):i],14)
                if cierres[i]<cierres[i-5] and rsi_v>ra+2 and rsi_v<48:
                    dir_="long"; conf=(48-rsi_v)/48
                elif cierres[i]>cierres[i-5] and rsi_v<ra-2 and rsi_v>52:
                    dir_="short"; conf=(rsi_v-52)/48
        elif nombre == "RSI_Bollinger":
            if i>=20:
                desv=statistics.stdev(cierres[max(0,i-20):i+1]) if len(cierres[max(0,i-20):i+1])>1 else 0
                bi=e20-2*desv; bs=e20+2*desv
                if rsi_v<30 and p<bi*1.003: dir_="long"; conf=(30-rsi_v)/30
                elif rsi_v>70 and p>bs*0.997: dir_="short"; conf=(rsi_v-70)/30
        elif nombre == "EMA_Crossover":
            if i>=3:
                e9=ema(cierres[max(0,i-10):i+1],9); e9a=ema(cierres[max(0,i-11):i],9)
                e20a=ema(cierres[max(0,i-21):i],20)
                if e9a<=e20a and e9>e20: dir_="long"; conf=abs(e9-e20)/(p+1e-9)
                elif e9a>=e20a and e9<e20: dir_="short"; conf=abs(e9-e20)/(p+1e-9)

        if not dir_ or conf < 0.15: continue

        sl_d = max(atr_v*sl_m, min_sl)
        sl   = p-sl_d if dir_=="long" else p+sl_d
        tp   = p+sl_d*rr if dir_=="long" else p-sl_d*rr
        rk   = capital * 0.005; gan = False; pnl = 0

        for j in range(i+1, min(i+81, len(velas))):
            hf=velas[j]["high"]; lf=velas[j]["low"]
            if dir_=="long":
                if lf<=sl: pnl=-rk*1.001; break
                elif hf>=tp: pnl=rk*rr*0.999; gan=True; break
            else:
                if hf>=sl: pnl=-rk*1.001; break
                elif lf<=tp: pnl=rk*rr*0.999; gan=True; break
        else: pnl=-rk*0.4

        capital+=pnl; peak=max(peak,capital)
        dd=(peak-capital)/peak*100; max_dd=max(max_dd,dd)
        cooldown[ses]=i
        trades.append({"par":par,"ses":ses,"gan":gan,"pnl":round(pnl,4)})

    if len(trades)<20: return None
    wins=[t for t in trades if t["gan"]]; losses=[t for t in trades if not t["gan"]]
    wr=len(wins)/len(trades)
    gw=sum(t["pnl"] for t in wins); gl=abs(sum(t["pnl"] for t in losses))
    pf=gw/gl if gl>0 else 9.99
    return {"estrategia":nombre,"par":par,"trades":len(trades),
            "wr":round(wr,3),"pf":round(min(pf,9.99),3),
            "pnl":round(sum(t["pnl"] for t in trades),4),"max_dd":round(max_dd,2)}

def correr_backtesting():
    print("\n\033[1m── PASO 2: Backtesting de estrategias\033[0m")
    estrategias = ["Hammer","Doji","Engulfing","RSI_Divergence","RSI_Bollinger","EMA_Crossover"]
    params = {"rr_ratio":1.8,"sl_atr_mult":1.5,"min_sl_pips":10}
    resultados = []

    for par in PARES:
        archivo = HIST_DIR / f"{par}_M15.json"
        if not archivo.exists(): continue
        velas = json.loads(archivo.read_text())
        if len(velas) < 500: continue
        print(f"\n  {par} ({len(velas):,} velas M15):")
        for nombre in estrategias:
            r = backtest_estrategia(nombre, velas, par, params)
            if r and r["trades"] >= 20:
                resultados.append(r)
                c = "\033[92m" if r["wr"]>=0.55 else "\033[93m" if r["wr"]>=0.45 else "\033[91m"
                print(f"    {nombre:<18} WR={c}{r['wr']:.0%}\033[0m PF={r['pf']:.2f} DD={r['max_dd']:.1f}%")

    (BT_DIR/"resultados.json").write_text(json.dumps(resultados, indent=2))
    log(f"Backtesting completado: {len(resultados)} combinaciones")
    return resultados


# ── PASO 3: CALIBRACIÓN ───────────────────────────────────────────────────────

def calibrar(resultados):
    print("\n\033[1m── PASO 3: Calibración con DeepSeek V4-Pro\033[0m")

    # Resumen por estrategia
    por_strat = {}
    for r in resultados:
        s = r["estrategia"]
        if s not in por_strat:
            por_strat[s] = {"wins":0,"ops":0,"pfs":[]}
        por_strat[s]["ops"] += r["trades"]
        por_strat[s]["wins"] += int(r["trades"]*r["wr"])
        por_strat[s]["pfs"].append(r["pf"])

    if DEEPSEEK_KEY:
        try:
            from openai import OpenAI
            import re
            client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE_URL)

            strat_sum = {s: {"wr":round(d["wins"]/d["ops"],3) if d["ops"]>0 else 0,
                             "pf":round(sum(d["pfs"])/len(d["pfs"]),3)}
                         for s,d in por_strat.items()}

            prompt = f"""
Analiza backtesting Forex 5 años y propón parámetros óptimos.
Estrategias (WR, PF): {json.dumps(strat_sum)}
Capital: $200, riesgo: 0.5%/op, OANDA, 6 pares.
Criterio activo: WR>=0.50 Y PF>=1.30

JSON sin markdown:
{{"estrategias_activas":["lista"],"estrategias_pausadas":["lista"],
"sl_atr_mult":num,"min_sl_pips":int,"rr_ratio":num,
"min_win_rate":num,"min_confidence":num,
"sesiones_activas":["london","overlap","new_york"],
"cooldown_minutes":int,"max_posiciones":int,"riesgo_pct":0.005,
"notas_analista":"observaciones"}}
"""
            resp = client.chat.completions.create(
                model=MODEL_DEEP, messages=[{"role":"user","content":prompt}],
                response_format={"type":"json_object"}, max_tokens=500,
            )
            texto  = re.sub(r"```json\s*","",resp.choices[0].message.content)
            texto  = re.sub(r"```\s*","",texto).strip()
            params = json.loads(texto)
            log("Calibración completada por DeepSeek V4-Pro")
        except Exception as e:
            log(f"DeepSeek error: {e} — calibración local", ok=False)
            params = _calibrar_local(por_strat)
    else:
        params = _calibrar_local(por_strat)
        log("Calibración local (sin DeepSeek API)")

    params["calibrado_en"]      = datetime.now(timezone.utc).isoformat()
    params["trades_analizados"] = len(resultados)
    params["version"]           = "v11.0.0"

    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    (CALIB_DIR/"strategy_params.json").write_text(
        json.dumps(params, indent=2, ensure_ascii=False)
    )

    print(f"\n  Parámetros calibrados:")
    print(f"    Estrategias activas: {params.get('estrategias_activas')}")
    print(f"    SL: {params.get('sl_atr_mult')}× ATR | RR: {params.get('rr_ratio')}")
    print(f"    Notas: {params.get('notas_analista','')}")
    return params

def _calibrar_local(por_strat):
    activas  = [s for s,d in por_strat.items()
                if d["wins"]/d["ops"]>=0.50 and sum(d["pfs"])/len(d["pfs"])>=1.30
                if d["ops"]>0]
    pausadas = [s for s in por_strat if s not in activas]
    if not activas: activas = ["RSI_Divergence","Doji","RSI_Bollinger"]
    return {**DEFAULTS, "estrategias_activas":activas,
            "estrategias_pausadas":pausadas,
            "notas_analista":"Calibración local basada en estadísticas"}


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time as _time
    inicio = _time.time()
    print("\n\033[1m" + "="*60 + "\033[0m")
    print("\033[1m  SETUP DATOS — Trading Bot v11\033[0m")
    print("\033[1m" + "="*60 + "\033[0m")

    ok = descargar_historico()
    if ok:
        resultados = correr_backtesting()
        if resultados:
            calibrar(resultados)

    mins = (_time.time()-inicio)/60
    print(f"\n\033[92m{'='*60}\033[0m")
    print(f"\033[92m  ✓ SETUP COMPLETADO en {mins:.0f} minutos\033[0m")
    print(f"\033[92m{'='*60}\033[0m")
    print(f"\n  SIGUIENTE: python main.py\n")
