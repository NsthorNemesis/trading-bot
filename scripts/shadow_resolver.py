#!/usr/bin/env python3
"""
Fase2e — Resuelve el resultado de las señales shadow (estrategias pausadas).

Lee logs/shadow_signals.jsonl (resultado=None) y determina win/loss/timeout
recorriendo las velas M15 posteriores a cada señal:
  long:  low <= SL → loss (se chequea primero: conservador); high >= TP → win
  short: high >= SL → loss; low <= TP → win
Timeout: 72h sin tocar SL ni TP → "timeout" (R=0).

Escribe logs/shadow_resultados.jsonl (idempotente: no re-resuelve).
Uso: python3 scripts/shadow_resolver.py   (también vía systemd timer cada hora)

Fuente de velas: data/historical/{PAR}_M15.json (caché viva del MarketAgent);
si la señal es anterior al caché, pide a OANDA REST (lee .env del proyecto).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADOW_LOG = ROOT / "logs" / "shadow_signals.jsonl"
RESULT_LOG = ROOT / "logs" / "shadow_resultados.jsonl"
TIMEOUT_H = 72


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _oanda_token() -> str | None:
    try:
        env = (ROOT / ".env").read_text()
        m = re.search(r"OANDA_ACCESS_TOKEN=(.*)", env)
        return m.group(1).strip().strip('"') if m else None
    except Exception:
        return None


def _velas_oanda(par: str, desde_iso: str) -> list:
    """Pide velas M15 a OANDA desde `desde_iso` (fallback si el caché no cubre)."""
    token = _oanda_token()
    if not token:
        return []
    url = (f"https://api-fxpractice.oanda.com/v3/instruments/{par}/candles"
           f"?granularity=M15&price=M&from={desde_iso}&count=5000&includeFirst=false")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        print(f"[resolver] OANDA fallback falló para {par}: {e}", file=sys.stderr)
        return []
    out = []
    for c in d.get("candles", []):
        if not c.get("complete", True):
            continue
        out.append({"timestamp": c["time"], "open": float(c["mid"]["o"]),
                    "high": float(c["mid"]["h"]), "low": float(c["mid"]["l"]),
                    "close": float(c["mid"]["c"])})
    return out


def velas_desde(par: str, desde: datetime) -> list:
    """Velas M15 de `par` posteriores a `desde` (caché local + fallback OANDA)."""
    velas: list = []
    p = ROOT / "data" / "historical" / f"{par}_M15.json"
    if p.exists():
        try:
            for v in json.loads(p.read_text()):
                if parse_ts(v["timestamp"]) > desde:
                    velas.append(v)
        except Exception as e:
            print(f"[resolver] caché {p.name}: {e}", file=sys.stderr)
    if not velas:
        # El caché no cubre: pedir a OANDA desde la señal
        velas = [v for v in _velas_oanda(par, desde.isoformat())
                 if parse_ts(v["timestamp"]) > desde]
    return sorted(velas, key=lambda v: v["timestamp"])


def resolver(rec: dict) -> dict | None:
    """Devuelve {resultado, R, ts_fin} o None si aún no se puede resolver."""
    if rec.get("sl") is None or rec.get("tp") is None:
        return {"resultado": "sin_datos", "R": 0.0, "ts_fin": None}
    entry, sl, tp = float(rec["entry"]), float(rec["sl"]), float(rec["tp"])
    dire = rec.get("dir_hint")
    t0 = parse_ts(rec["ts"])
    vs = velas_desde(rec["par"], t0)
    if len(vs) < 2:
        return None  # señal muy reciente, aún sin velas posteriores
    denom = abs(entry - sl)
    rr = abs(tp - entry) / denom if denom > 0 else 2.0
    for v in vs:
        ts = parse_ts(v["timestamp"])
        if (ts - t0).total_seconds() > TIMEOUT_H * 3600:
            return {"resultado": "timeout", "R": 0.0, "ts_fin": v["timestamp"]}
        hi, lo = float(v["high"]), float(v["low"])
        if dire == "long":
            if lo <= sl:
                return {"resultado": "loss", "R": -1.0, "ts_fin": v["timestamp"]}
            if hi >= tp:
                return {"resultado": "win", "R": round(rr, 2), "ts_fin": v["timestamp"]}
        elif dire == "short":
            if hi >= sl:
                return {"resultado": "loss", "R": -1.0, "ts_fin": v["timestamp"]}
            if lo <= tp:
                return {"resultado": "win", "R": round(rr, 2), "ts_fin": v["timestamp"]}
    # Se acabaron las velas sin resolución: timeout solo si ya pasó el plazo en
    # tiempo real (si no, esperar a la próxima corrida del resolver)
    if (datetime.now(timezone.utc) - t0).total_seconds() > TIMEOUT_H * 3600:
        return {"resultado": "timeout", "R": 0.0, "ts_fin": vs[-1]["timestamp"]}
    return None


def main() -> None:
    if not SHADOW_LOG.exists():
        print("[resolver] sin shadow log todavía")
        return
    resueltas = set()
    if RESULT_LOG.exists():
        for l in RESULT_LOG.read_text(encoding="utf-8").splitlines():
            if l.strip():
                r = json.loads(l)
                resueltas.add((r["ts"], r["par"], r["estrategia"], r["dir_hint"]))
    nuevos = 0
    with RESULT_LOG.open("a", encoding="utf-8") as out:
        for l in SHADOW_LOG.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            rec = json.loads(l)
            key = (rec["ts"], rec["par"], rec["estrategia"], rec["dir_hint"])
            if key in resueltas:
                continue
            r = resolver(rec)
            if r:
                rec.update(r)
                rec["resolved_ts"] = datetime.now(timezone.utc).isoformat()
                out.write(json.dumps(rec) + "\n")
                nuevos += 1
    print(f"[resolver] señales nuevas resueltas: {nuevos}")


if __name__ == "__main__":
    main()
