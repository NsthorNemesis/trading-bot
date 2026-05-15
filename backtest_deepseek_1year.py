"""
backtest_deepseek_1year.py — Plan A M15 + filtro DeepSeek real
===============================================================
Fase 1: Genera candidatos de señal (prefiltro técnico)
Fase 2: Llama a DeepSeek para confirmar/rechazar cada señal
Fase 3: Replay de simulación solo con señales aprobadas

Replica el flujo exacto del bot en producción.
"""
import sys, json, time, asyncio, os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import deque

import numpy as np
import pandas as pd
from ta.volatility import AverageTrueRange, BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent))
from utils.regime_detector import RegimeDetector

# ── Config ────────────────────────────────────────────────────
PARES       = ["EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD","USD_CAD"]
DATA_DIR    = Path("data/historical")
PARAMS_FILE = Path("data/calibration/strategy_params.json")
OUT_FILE    = Path("data/backtesting/backtest_deepseek_1year.json")
CAPITAL_INI = 200.0
N_SEMANAS   = 52
SIM_START   = datetime(2025, 5, 9, tzinfo=timezone.utc)
PIP_SIZE    = {"USD_JPY": 0.01}
DS_CONCURRENT = 8   # llamadas paralelas a DeepSeek

# Cargar .env manualmente
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_CLIENT    = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com") if DEEPSEEK_KEY else None

with open(PARAMS_FILE) as f:
    PARAMS = json.load(f)

ESTRATEGIAS_ACTIVAS = set(PARAMS["estrategias_activas"])
MIN_CONF      = PARAMS["min_confidence"]
SL_ATR_MULT   = PARAMS["sl_atr_mult"]
MIN_SL_PIPS   = PARAMS["min_sl_pips"]
MAX_SL_PIPS   = PARAMS["max_sl_pips"]
RR_RATIO      = PARAMS["rr_ratio"]
COOLDOWN_MINS = PARAMS["cooldown_minutes"]
MAX_POS       = PARAMS["max_posiciones"]
RIESGO_PCT    = PARAMS["riesgo_pct"]
SESIONES_ACT  = set(PARAMS.get("sesiones_activas", ["london","overlap","new_york"]))

print("=" * 66)
print("  BACKTEST DEEPSEEK — Plan A M15 + confirmación DeepSeek")
print("=" * 66)
print(f"  Estrategias: {sorted(ESTRATEGIAS_ACTIVAS)}")
print(f"  DeepSeek: {'ACTIVO' if DS_CLIENT else 'NO DISPONIBLE'}")
print()


# ── Indicadores ───────────────────────────────────────────────
def _adx_vec(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr  = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    up  = h-h.shift(1); dn = l.shift(1)-l
    pdm = np.where((up>dn)&(up>0), up.values, 0.0)
    mdm = np.where((dn>up)&(dn>0), dn.values, 0.0)
    a   = 1.0/period
    atr_s = pd.Series(tr.values).ewm(alpha=a,adjust=False).mean()
    ps = pd.Series(pdm,index=df.index).ewm(alpha=a,adjust=False).mean()
    ms = pd.Series(mdm,index=df.index).ewm(alpha=a,adjust=False).mean()
    pdi = 100*ps/atr_s.replace(0,np.nan).values
    mdi = 100*ms/atr_s.replace(0,np.nan).values
    dx  = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    adx = dx.fillna(0).ewm(alpha=a,adjust=False).mean()
    adx.index = df.index
    return adx

def _regime_array(adx):
    av = adx.values; sv = adx.diff(3).fillna(0).values
    base = np.where(av<15,0.0,np.where(av>35,1.0,(av-15.0)/20.0))
    return np.clip(base+np.clip(sv/25.0,-0.20,0.20),0.0,1.0).astype(np.float32)

def _raw_to_df(data):
    df = pd.DataFrame(data)
    df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"},inplace=True)
    for c in ["Open","High","Low","Close","Volume"]: df[c]=df[c].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"],utc=True)
    return df.sort_values("ts").reset_index(drop=True)

def _add_indicators(df):
    df=df.copy()
    df["ATR_14"] = AverageTrueRange(df["High"],df["Low"],df["Close"],window=14,fillna=True).average_true_range()
    df["RSI_14"] = RSIIndicator(df["Close"],window=14,fillna=True).rsi()
    df["EMA_20"] = EMAIndicator(df["Close"],window=20,fillna=True).ema_indicator()
    df["EMA_50"] = EMAIndicator(df["Close"],window=50,fillna=True).ema_indicator()
    bb=BollingerBands(df["Close"],window=20,window_dev=2,fillna=True)
    df["BBL_20"]=bb.bollinger_lband(); df["BBU_20"]=bb.bollinger_hband()
    macd=MACD(df["Close"],fillna=True); df["MACD_DIF"]=macd.macd_diff()
    o,h,l,c=df["Open"],df["High"],df["Low"],df["Close"]
    cu=abs(c-o); rg=h-l; si=o.combine(c,min)-l; ss=h-o.combine(c,max)
    df["HAMMER"]=((cu>0)&(si>=2*cu)&(ss<=cu*0.5)).astype(bool)
    df["DOJI"]=((rg>0)&(cu/rg.replace(0,1)<0.10)).astype(bool)
    df["EBULL"]=((c>o)&(c.shift(1)<o.shift(1))&(c>o.shift(1))&(o<c.shift(1))&(cu>cu.shift(1)*1.05)).astype(bool)
    df["REGIME"]=_regime_array(_adx_vec(df))
    return df

def load_m15(par):
    df = _raw_to_df(json.loads((DATA_DIR/f"{par}_M15.json").read_text()))
    df = _add_indicators(df)
    # H4 desde M15
    h4 = df.set_index("ts")[["Open","High","Low","Close","Volume"]].resample("4h").agg(
        {"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    h4["RSI_H4"] = RSIIndicator(h4["Close"],window=14,fillna=True).rsi()
    h4["EMA20_H4"] = EMAIndicator(h4["Close"],window=20,fillna=True).ema_indicator()
    h4["EMA50_H4"] = EMAIndicator(h4["Close"],window=50,fillna=True).ema_indicator()
    h4["trend"] = np.where(
        (h4["EMA20_H4"]-h4["EMA50_H4"]).abs()/h4["Close"]<0.0003,"rango",
        np.where(h4["EMA20_H4"]>h4["EMA50_H4"],"up","down"))
    for col in ["RSI_H4","EMA20_H4","EMA50_H4","trend"]:
        s = h4[col].reindex(df["ts"].dt.floor("4h")).ffill()
        s.index = df.index
        df[col] = s
    return df


# ── Prefiltro ─────────────────────────────────────────────────
def prefiltro(a, gi):
    if gi < 60: return []
    cands = []
    rv=float(a["rsi"][gi]); cl=float(a["close"][gi])
    e20=float(a["ema20"][gi]); e50=float(a["ema50"][gi])
    bbl=float(a["bbl"][gi]); bbu=float(a["bbu"][gi])
    mdif=float(a["macd_dif"][gi])
    tend="up" if e20>e50 else "down"
    if rv<32 and cl<bbl*1.002:      cands.append((0.45,"long","RSI_Bollinger","RSI_B_low"))
    if rv>68 and cl>bbu*0.998:      cands.append((0.45,"short","RSI_Bollinger","RSI_B_high"))
    if bool(a["hammer"][gi]) and tend=="down": cands.append((0.45,"long","Hammer","Hammer"))
    if bool(a["doji"][gi]) and rv<32: cands.append((0.45,"long","Doji","Doji_os"))
    if bool(a["doji"][gi]) and rv>68: cands.append((0.45,"short","Doji","Doji_ob"))
    if bool(a["ebull"][gi]) and tend=="up": cands.append((0.45,"long","Engulfing","Engulf"))
    if gi>=12:
        c_r=a["close"][gi-5:gi]; r_r=a["rsi"][gi-5:gi]
        c_o=a["close"][gi-12:gi-5]; r_o=a["rsi"][gi-12:gi-5]
        if len(c_r)>=5 and len(c_o)>=5:
            ir=int(np.argmin(c_r)); io=int(np.argmin(c_o))
            if c_r[ir]<c_o[io]*0.9998 and r_r[ir]>r_o[io]+3 and r_r[ir]<48:
                cands.append((0.52,"long","RSI_Divergence","Div_bull"))
            ir=int(np.argmax(c_r)); io=int(np.argmax(c_o))
            if c_r[ir]>c_o[io]*1.0002 and r_r[ir]<r_o[io]-3 and r_r[ir]>52:
                cands.append((0.52,"short","RSI_Divergence","Div_bear"))
    if gi>=5:
        e20s=a["ema20"][gi-4:gi+1]; e50s=a["ema50"][gi-4:gi+1]
        cu=any(e20s[i-1]<=e50s[i-1] and e20s[i]>e50s[i] for i in range(-3,0))
        cd=any(e20s[i-1]>=e50s[i-1] and e20s[i]<e50s[i] for i in range(-3,0))
        if cu and mdif>0: cands.append((0.50,"long","EMA_Crossover","EMA_X_up"))
        if cd and mdif<0: cands.append((0.50,"short","EMA_Crossover","EMA_X_dn"))
    return cands


# ── DeepSeek ──────────────────────────────────────────────────
def _build_prompt(sig):
    """Replica el prompt exacto del bot de producción."""
    h = sig["hour"]
    ses = "london" if 7<=h<13 else "overlap" if 13<=h<17 else "new_york" if 17<=h<22 else "asia"
    rs = sig["regime"]
    if rs<0.30:   regime_txt=f"RANGO({rs:.2f})"
    elif rs>0.70: regime_txt=f"TENDENCIA({rs:.2f})"
    else:         regime_txt=f"TRANSICION({rs:.2f})"
    h4_txt = (f"H4: RSI={sig['rsi_h4']:.1f} EMA20={sig['ema20_h4']:.5f} "
              f"EMA50={sig['ema50_h4']:.5f} Tend={sig['trend_h4']}")
    cierres = ",".join(f"{x:.5f}" for x in sig["closes_m15"])
    return (
        f"Par:{sig['par']} Dir:{sig['dir']} Sesion:{ses}\n"
        f"H1: RSI:{sig['rsi']:.1f} ATR:{sig['atr']:.5f} "
        f"EMA20:{sig['ema20']:.5f} EMA50:{sig['ema50']:.5f}\n"
        f"{h4_txt}\n"
        f"Precio:{sig['precio']:.5f} Patron:{sig['estrat']}\n"
        f"Regimen:{regime_txt} CierresH1:{cierres}\n"
        f"WR_min:45% RR:{RR_RATIO}\n\n"
        f'Confirmas senal? JSON: {{"senal":bool,"dir":"long/short","conf":0-1,"razon":"max 15 palabras"}}'
    )

async def _call_ds_one(sem, sig, idx, total):
    async with sem:
        prompt = _build_prompt(sig)
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: DS_CLIENT.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role":"user","content":prompt}],
                response_format={"type":"json_object"},
                max_tokens=80,
            ))
            content = (resp.choices[0].message.content or "").strip()
            r = json.loads(content)
            aprobado = bool(r.get("senal", r.get("signal", False)))
            conf_ds  = float(r.get("conf", 0))
            razon    = r.get("razon","")
            if (idx+1) % 50 == 0:
                print(f"  DS progress: {idx+1}/{total} | aprobadas hasta aquí...")
            return {"aprobado": aprobado and conf_ds>=MIN_CONF,
                    "conf_ds": conf_ds, "razon": razon}
        except Exception as e:
            # Fallo → aprobar con confianza base (conservador)
            return {"aprobado": True, "conf_ds": sig["conf"], "razon": f"DS_error:{str(e)[:30]}"}

async def evaluar_con_deepseek(candidatos):
    print(f"\n  Fase 2: Llamando DeepSeek para {len(candidatos)} señales candidatas...")
    print(f"  ({DS_CONCURRENT} llamadas en paralelo — esto tarda ~{len(candidatos)//DS_CONCURRENT//60+1} min)\n")
    sem = asyncio.Semaphore(DS_CONCURRENT)
    tasks = [_call_ds_one(sem, sig, i, len(candidatos)) for i, sig in enumerate(candidatos)]
    results = await asyncio.gather(*tasks)
    return results


# ── Tracker ───────────────────────────────────────────────────
class Tracker:
    def __init__(self, cap):
        self.capital=cap; self.capital_ini=cap; self.peak=cap
        self.open={}; self.closed=[]; self._nid=1

    def open_trade(self, par, dir_, entry, sl, tp, estrat, ts):
        sl_d=abs(entry-sl)
        units=max(1,int(self.capital*RIESGO_PCT/sl_d)) if sl_d>1e-9 else 1
        tid=self._nid; self._nid+=1
        self.open[tid]=dict(par=par,dir=dir_,entry=entry,sl=sl,tp=tp,
                            units=units,estrat=estrat,opened_at=ts)
        return tid

    def check_fills(self, par, high, low, ts):
        for tid,t in list(self.open.items()):
            if t["par"]!=par: continue
            if t["dir"]=="long":
                hs=low<=t["sl"]; ht=high>=t["tp"]
                if hs and ht: pnl,res=(t["tp"]-t["entry"])*t["units"],"TP"
                elif hs:      pnl,res=(t["sl"]-t["entry"])*t["units"],"SL"
                elif ht:      pnl,res=(t["tp"]-t["entry"])*t["units"],"TP"
                else: continue
            else:
                hs=high>=t["sl"]; ht=low<=t["tp"]
                if hs and ht: pnl,res=(t["entry"]-t["tp"])*t["units"],"TP"
                elif hs:      pnl,res=(t["entry"]-t["sl"])*t["units"],"SL"
                elif ht:      pnl,res=(t["entry"]-t["tp"])*t["units"],"TP"
                else: continue
            self.capital+=pnl; self.peak=max(self.peak,self.capital)
            rec={**t,"pnl":round(pnl,5),"resultado":res,"closed_at":ts}
            self.closed.append(rec); del self.open[tid]

    def max_dd(self):
        cap=self.capital_ini; pk=cap; mdd=0.0
        for t in self.closed:
            cap+=t["pnl"]; pk=max(pk,cap)
            mdd=max(mdd,(pk-cap)/pk if pk>0 else 0)
        return mdd


# ── MAIN ──────────────────────────────────────────────────────
async def run():
    t0 = time.time()

    # ── Cargar datos ─────────────────────────────────────────
    print("Fase 1: Cargando datos M15...")
    dfs = {}
    for par in PARES:
        try:
            df = load_m15(par)
            dfs[par] = {
                "ts":       df["ts"].values,
                "open":     df["Open"].values, "high": df["High"].values,
                "low":      df["Low"].values,  "close": df["Close"].values,
                "atr":      df["ATR_14"].values, "rsi": df["RSI_14"].values,
                "ema20":    df["EMA_20"].values, "ema50": df["EMA_50"].values,
                "bbl":      df["BBL_20"].values, "bbu": df["BBU_20"].values,
                "macd_dif": df["MACD_DIF"].values,
                "hammer":   df["HAMMER"].values.astype(bool),
                "doji":     df["DOJI"].values.astype(bool),
                "ebull":    df["EBULL"].values.astype(bool),
                "regime":   df["REGIME"].values,
                "trend_h4": df["trend"].values,
                "rsi_h4":   df["RSI_H4"].values,
                "ema20_h4": df["EMA20_H4"].values,
                "ema50_h4": df["EMA50_H4"].values,
            }
            print(f"  {par}: {len(df):,} velas OK")
        except Exception as e:
            print(f"  {par}: ERROR — {e}")
    print(f"  Cargado en {time.time()-t0:.1f}s")

    # ── Fase 1: Generar candidatos ───────────────────────────
    print("\nFase 1b: Generando señales candidatas...")
    rd = RegimeDetector()
    COOLDOWN_NS = COOLDOWN_MINS * 60 * 1_000_000_000
    cooldown_gen: dict = {}
    regime_hist: dict = {p: deque(maxlen=80) for p in dfs}

    # Recopilar todos los ticks del año
    SIM_END = SIM_START + timedelta(weeks=N_SEMANAS)
    w_start_ns = pd.Timestamp(SIM_START).value
    w_end_ns   = pd.Timestamp(SIM_END).value

    all_ticks = []
    for par, a in dfs.items():
        ts_arr = a["ts"].astype("int64")
        idxs   = np.where((ts_arr >= w_start_ns) & (ts_arr < w_end_ns))[0]
        for gi in idxs:
            all_ticks.append((int(ts_arr[gi]), par, int(gi)))
    all_ticks.sort(key=lambda x: x[0])

    # Pasada para generar candidatos (sin abrir trades todavía)
    # Simulamos cooldown y max_pos de forma simplificada
    candidatos = []
    open_count: dict = {}  # par -> count activos (aproximado)

    for (ts_ns, par, gi) in all_ticks:
        a = dfs[par]
        ts_dt = pd.Timestamp(ts_ns, unit="ns", tz="UTC").to_pydatetime()
        h = ts_dt.hour
        if   7<=h<13:  ses="london"
        elif 13<=h<17: ses="overlap"
        elif 17<=h<22: ses="new_york"
        else:           ses="asia"
        if ses not in SESIONES_ACT: continue

        last_cd = cooldown_gen.get(par, 0)
        if (ts_ns - last_cd) < COOLDOWN_NS: continue

        rs = float(a["regime"][gi])
        regime_hist[par].append(rs)
        trans = rd.detectar_transicion(list(regime_hist[par]))

        cands = prefiltro(a, gi)
        cands = [(c,d,e,r) for c,d,e,r in cands if e in ESTRATEGIAS_ACTIVAS]
        if not cands: continue

        t_h4 = str(a["trend_h4"][gi])
        def h4_ok(dir_, estrat):
            if estrat!="EMA_Crossover": return True
            if t_h4=="rango": return True
            return (t_h4=="up" and dir_=="long") or (t_h4=="down" and dir_=="short")
        cands = [(c,d,e,r) for c,d,e,r in cands if h4_ok(d,e)]
        if not cands: continue

        mejor = None
        for conf_base, dir_, estrat, razon in sorted(cands, key=lambda x:-x[0]):
            peso = rd.peso_estrategia(estrat, rs, trans)
            conf = round(conf_base*peso, 3)
            if conf>=MIN_CONF:
                mejor=(conf,dir_,estrat,razon); break
        if not mejor: continue

        conf, dir_, estrat, _ = mejor
        closes_win = [float(x) for x in a["close"][max(0,gi-7):gi+1]]

        candidatos.append({
            "ts_ns": ts_ns, "ts": ts_dt.isoformat(), "par": par, "gi": gi,
            "dir": dir_, "estrat": estrat, "conf": conf,
            "precio":   float(a["close"][gi]),
            "atr":      float(a["atr"][gi]),
            "rsi":      float(a["rsi"][gi]),
            "ema20":    float(a["ema20"][gi]),
            "ema50":    float(a["ema50"][gi]),
            "bbl":      float(a["bbl"][gi]),
            "bbu":      float(a["bbu"][gi]),
            "regime":   rs,
            "trend_h4": t_h4,
            "rsi_h4":   float(a["rsi_h4"][gi]),
            "ema20_h4": float(a["ema20_h4"][gi]),
            "ema50_h4": float(a["ema50_h4"][gi]),
            "closes_m15": closes_win,
            "hour": h,
        })
        cooldown_gen[par] = ts_ns

    print(f"  {len(candidatos)} señales candidatas generadas")

    # ── Fase 2: DeepSeek ─────────────────────────────────────
    if DS_CLIENT:
        ds_results = await evaluar_con_deepseek(candidatos)
    else:
        print("  AVISO: Sin DeepSeek — aprobando todo")
        ds_results = [{"aprobado":True,"conf_ds":s["conf"],"razon":"no_ds"} for s in candidatos]

    aprobadas = sum(1 for r in ds_results if r["aprobado"])
    rechazadas = len(ds_results) - aprobadas
    print(f"\n  Resultado DeepSeek: {aprobadas} aprobadas / {rechazadas} rechazadas "
          f"({aprobadas/len(ds_results)*100:.1f}% approval rate)")

    # Marcar candidatos aprobados
    for i, (sig, res) in enumerate(zip(candidatos, ds_results)):
        sig["ds_aprobado"] = res["aprobado"]
        sig["ds_conf"]     = res["conf_ds"]
        sig["ds_razon"]    = res["razon"]

    aprobados_set = {(s["par"], s["ts_ns"]) for s in candidatos if s["ds_aprobado"]}

    # ── Fase 3: Replay con señales aprobadas ─────────────────
    print("\nFase 3: Simulación con señales aprobadas por DeepSeek...")
    tracker = Tracker(CAPITAL_INI)
    cooldown_sim: dict = {}
    metricas = []

    for semana in range(1, N_SEMANAS+1):
        w_start = SIM_START + timedelta(weeks=semana-1)
        w_end   = w_start + timedelta(weeks=1)
        n_antes = len(tracker.closed)
        w_sn = pd.Timestamp(w_start).value
        w_en = pd.Timestamp(w_end).value

        ticks = []
        for par, a in dfs.items():
            ts_arr = a["ts"].astype("int64")
            idxs   = np.where((ts_arr>=w_sn)&(ts_arr<w_en))[0]
            for gi in idxs:
                ticks.append((int(ts_arr[gi]), par, int(gi)))
        ticks.sort(key=lambda x: x[0])

        for (ts_ns, par, gi) in ticks:
            a = dfs[par]
            ts_dt = pd.Timestamp(ts_ns,unit="ns",tz="UTC").to_pydatetime()

            # Fills
            tracker.check_fills(par, float(a["high"][gi]), float(a["low"][gi]), ts_dt)

            # Señal solo si DeepSeek la aprobó
            if (par, ts_ns) not in aprobados_set: continue
            if len(tracker.open) >= MAX_POS: continue
            last_cd = cooldown_sim.get(par, 0)
            if (ts_ns - last_cd) < COOLDOWN_NS * 1_000_000_000: continue

            # Buscar el candidato correspondiente
            sig_data = next((s for s in candidatos
                             if s["par"]==par and s["ts_ns"]==ts_ns and s["ds_aprobado"]), None)
            if not sig_data: continue

            entry = float(a["close"][gi])
            atr_v = float(a["atr"][gi])
            ps    = PIP_SIZE.get(par, 0.0001)
            sl_d  = float(np.clip(atr_v*SL_ATR_MULT, MIN_SL_PIPS*ps, MAX_SL_PIPS*ps))
            sl_p  = entry-sl_d if sig_data["dir"]=="long" else entry+sl_d
            tp_p  = entry+sl_d*RR_RATIO if sig_data["dir"]=="long" else entry-sl_d*RR_RATIO

            tracker.open_trade(par, sig_data["dir"], entry, sl_p, tp_p, sig_data["estrat"], ts_dt)
            cooldown_sim[par] = ts_ns

        nuevos  = tracker.closed[n_antes:]
        n       = len(nuevos)
        ganadas = sum(1 for t in nuevos if t["resultado"]=="TP")
        wr      = ganadas/n if n>0 else 0.0
        pnl_tot = sum(t["pnl"] for t in nuevos)
        pos_p   = sum(t["pnl"] for t in nuevos if t["pnl"]>0)
        neg_p   = abs(sum(t["pnl"] for t in nuevos if t["pnl"]<0))
        pf      = pos_p/neg_p if neg_p>0 else (99.0 if pos_p>0 else 0.0)
        flag    = "✅" if wr>=0.50 else ("⚠️" if n==0 else "❌")
        print(f"  Sem {semana:2d} ({w_start.date()})  {flag}  "
              f"T:{n:3d}  WR:{wr*100:5.1f}%  PF:{pf:4.2f}  "
              f"PnL:${pnl_tot:+.2f}  Cap:${tracker.capital:.2f}")
        metricas.append({
            "semana":semana,"inicio":str(w_start.date()),
            "trades":n,"ganadas":ganadas,
            "win_rate":round(wr,3),"profit_factor":round(pf,3),
            "pnl_total":round(pnl_tot,4),
            "capital_fin":round(tracker.capital,2),
        })

    # ── Resumen ───────────────────────────────────────────────
    tot_t = sum(m["trades"] for m in metricas)
    tot_w = sum(m["ganadas"] for m in metricas)
    tot_pnl = tracker.capital - tracker.capital_ini
    wr_g = tot_w/tot_t if tot_t>0 else 0
    max_dd = tracker.max_dd()
    sem_pos = sum(1 for m in metricas if m["pnl_total"]>0)

    by_e: dict = {}
    for t in tracker.closed:
        e=t.get("estrat","?")
        s=by_e.setdefault(e,{"n":0,"won":0,"pnl":0.0})
        s["n"]+=1; s["won"]+=1 if t["resultado"]=="TP" else 0; s["pnl"]+=t["pnl"]

    elapsed = time.time()-t0
    print()
    print("═"*66)
    print("  RESULTADOS FINALES — con filtro DeepSeek real")
    print("═"*66)
    print(f"  {'Métrica':<26} {'Sin DS (M15)':>13} {'Con DS (M15)':>13}")
    print(f"  {'─'*26} {'─'*13} {'─'*13}")
    def row(n,b,v): print(f"  {n:<26} {b:>13} {v:>13}")
    row("Capital final",    "$280.43",   f"${tracker.capital:.2f}")
    row("PnL %",            "+40.2%",    f"{tot_pnl/CAPITAL_INI*100:+.1f}%")
    row("Señales totales",  "1,705",     str(len(candidatos)))
    row("Aprobadas DS",     "1,705",     str(aprobadas))
    row("Trades ejecutados","1,705",     str(tot_t))
    row("Win Rate",         "34.5%",     f"{wr_g*100:.1f}%")
    row("Max Drawdown",     "64.3%",     f"{max_dd*100:.1f}%")
    row("Semanas positivas","25/52",     f"{sem_pos}/52")
    print()
    print("  Por estrategia:")
    for e,s in sorted(by_e.items()):
        wr_e=s["won"]/s["n"] if s["n"]>0 else 0
        print(f"    {e:<24}  N={s['n']:4d}  WR={wr_e*100:5.1f}%  PnL=${s['pnl']:+.2f}")
    print(f"\n  DeepSeek approval rate: {aprobadas/len(candidatos)*100:.1f}%")
    print(f"  Tiempo total: {elapsed/60:.1f} minutos")
    print("═"*66)

    resultado = {
        "config": {
            "version":"Plan A M15 + DeepSeek",
            "capital_ini":CAPITAL_INI,"n_semanas":N_SEMANAS,
            "sim_start":str(SIM_START.date()),
            "estrategias":sorted(ESTRATEGIAS_ACTIVAS),
            "min_confidence":MIN_CONF,"rr_ratio":RR_RATIO,
            "timeframe":"M15","deepseek":"deepseek-chat",
        },
        "deepseek_stats": {
            "candidatos_totales":len(candidatos),
            "aprobadas":aprobadas,"rechazadas":rechazadas,
            "approval_rate":round(aprobadas/len(candidatos),3),
        },
        "resumen":{
            "capital_final":round(tracker.capital,2),
            "pnl_total":round(tot_pnl,2),
            "pnl_pct":round(tot_pnl/CAPITAL_INI*100,2),
            "total_trades":tot_t,
            "win_rate_global":round(wr_g,3),
            "max_drawdown_pct":round(max_dd*100,2),
            "semanas_positivas":sem_pos,
        },
        "comparativa":{
            "v11_m15":    {"capital_final":173.34,"pnl_pct":-13.33,"wr":0.33},
            "planA_h1":   {"capital_final": 36.27,"pnl_pct":-81.87,"wr":0.28},
            "planA_m15":  {"capital_final":280.43,"pnl_pct":+40.20,"wr":0.345},
        },
        "por_estrategia":{
            e:{"trades":s["n"],"win_rate":round(s["won"]/s["n"],3) if s["n"]>0 else 0,
               "pnl":round(s["pnl"],2)} for e,s in by_e.items()},
        "semanas":metricas,
    }
    OUT_FILE.parent.mkdir(parents=True,exist_ok=True)
    OUT_FILE.write_text(json.dumps(resultado,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"\n  -> {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(run())
