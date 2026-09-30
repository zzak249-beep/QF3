#!/usr/bin/env python3
"""
FED — Failed Extension Fade · escáner + backtester multi-símbolo para BingX USDT-M perpetuos.

Comandos
  download   descarga/actualiza velas + funding del universo a ./data (incremental)
  backtest   backtest sobre la caché con los parámetros dados
  sweep      rejilla de parámetros con split IS/OOS  (--grid "vol_mode=none,climax;min_move=7,9,12")
  scan       escáner en vivo del último cierre (--loop para correr continuo, Telegram opcional)

Lógica = FED v2 (Pine):
  contexto   máximo/mínimo del día UTC ≥ min_move % vs open diario, RS vs BTC ≥ rs_min,
             |BTC día| ≤ btc_max, toque de banda VWAP diaria ± band_mult σ
  extensión  rompe el máx/mín de ext_look velas estando en contexto (se re-arma con nuevos extremos)
  fallo      en ≤ fail_bars velas: cierre de vuelta dentro del nivel roto + retroceso ≥ min_retr % del
             movimiento del día + vela de rechazo + filtro de volumen (vol_mode)
  salida     SL estructural sobre el extremo + buffer ATR · TP1 parcial y BE · chandelier · TP2 · time stop

Sin lookahead: universo diario por volumen medio de 7 días desplazado 1 día, entradas al cierre de la vela
de señal, SL tocado antes que TP dentro de la misma vela, gaps rellenados al open.
Sesgo que NO se elimina: supervivencia (solo símbolos listados hoy).
"""
import argparse
import dataclasses
import math
import os
import re
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

CODE_VERSION = "fed-scan 1.1.0"
BASE = "https://open-api.bingx.com"
BTC = "BTC-USDT"
# no-crypto de BingX: acciones/forex/commodities/índices sintéticos (NCSK, NCFX, NCCO, NCSI...), oro y stables
EXCLUDE_RE = re.compile(r"^(NC[A-Z]{2}|XAUT-|PAXG-|USDC-|FDUSD-|USDE-|TUSD-|DAI-)")


def is_crypto(sym):
    return not EXCLUDE_RE.match(sym)


IV_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


def env(name, default=None):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().strip('"').strip("'")


# ═══════════════════════════════════════════════════════════════
# PARÁMETROS
# ═══════════════════════════════════════════════════════════════
@dataclass
class P:
    interval: str = "5m"
    # contexto
    min_move: float = 9.0        # % mínimo del extremo del día vs open UTC
    rs_min: float = 3.5          # pp sobre BTC
    btc_max: float = 4.0         # |% BTC día| máximo (0 = off)
    band: bool = True            # exigir toque de banda VWAP
    band_mult: float = 2.0
    # extensión / fallo
    ext_look: int = 20
    fail_bars: int = 3
    min_retr: float = 20.0       # % del movimiento del día
    reject: bool = True
    wick: float = 0.40
    vol_mode: str = "none"       # none | climax | low | exhaust
    vol_mult: float = 2.0        # climax: vol extensión ≥ x media(20)
    low_mult: float = 1.5        # low: vol vela de ruptura < x media(20)
    shorts: bool = True
    longs: bool = True
    # riesgo
    atr_len: int = 14
    sl_buf: float = 0.25
    min_risk_atr: float = 0.5
    max_risk_atr: float = 3.0
    cooldown: int = 6
    one_per_day: bool = True
    # salidas
    tp1_r: float = 1.0
    tp1_pct: float = 50.0
    tp2_mode: str = "retr"       # retr | vwap | r
    tp2_retr: float = 50.0
    tp2_r: float = 3.0
    min_tp2_r: float = 1.5
    trail_atr: float = 1.5
    max_bars: int = 48
    # costes
    fee_taker: float = 0.05      # % por lado (entrada, SL, time stop)
    fee_maker: float = 0.02      # % en TP límite
    slip_bps: float = 2.0        # bps adversos en órdenes a mercado
    # universo (sin lookahead: media 7d desplazada 1 día)
    excl_top: int = 10           # excluye los N de más volumen (majors → momentum)
    min_qv: float = 5e6          # USDT/día mínimos
    max_qv: float = 0.0          # 0 = sin tope
    # filtro de funding (0 = off). Short exige funding ≥ f_min, long exige funding ≤ -f_min (en %)
    f_min: float = 0.0
    # sizing para la curva
    risk_pct: float = 1.0


def str2bool(s):
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on", "si", "sí")


def add_param_args(ap):
    for f in fields(P):
        t = str2bool if f.type in (bool, "bool") else ({"int": int, "float": float, "str": str}.get(f.type, f.type) if isinstance(f.type, str) else f.type)
        ap.add_argument("--" + f.name.replace("_", "-"), dest=f.name, type=t, default=None)


def params_from(ns):
    p = P()
    for f in fields(P):
        v = getattr(ns, f.name, None)
        if v is not None:
            setattr(p, f.name, v)
    return p


# ═══════════════════════════════════════════════════════════════
# API PÚBLICA BINGX
# ═══════════════════════════════════════════════════════════════
S = requests.Session()
S.headers["User-Agent"] = CODE_VERSION
PAUSE = 0.12


class ApiError(RuntimeError):
    pass


def api(path, params=None, tries=5):
    last = None
    for k in range(tries):
        try:
            r = S.get(BASE + path, params=params or {}, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** k)
                continue
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1 + k)
            continue
        if j.get("code", 0) != 0:
            if j.get("code") in (100410, 109400) or "frequency" in str(j.get("msg", "")).lower():
                last = j
                time.sleep(2 ** k)
                continue
            raise ApiError(f"{path} {params} → {j.get('code')} {j.get('msg')}")
        return j.get("data")
    raise ApiError(f"{path} {params} → {last}")


def list_contracts():
    out = []
    for c in api("/openApi/swap/v2/quote/contracts") or []:
        s = c.get("symbol", "")
        if not s.endswith("-USDT"):
            continue
        if str(c.get("status", 1)) != "1":
            continue
        if str(c.get("apiStateOpen", "true")).lower() == "false":
            continue
        if not is_crypto(s):
            continue
        out.append(s)
    return out


def tickers():
    d = api("/openApi/swap/v2/quote/ticker") or []
    out = {}
    for t in d:
        try:
            out[t["symbol"]] = {
                "qv": float(t.get("quoteVolume") or 0),
                "high": float(t.get("highPrice") or 0),
                "low": float(t.get("lowPrice") or 0),
                "last": float(t.get("lastPrice") or 0),
            }
        except (KeyError, ValueError, TypeError):
            pass
    return out


def fetch_klines(sym, interval, start_ms, end_ms):
    step = IV_MS[interval]
    rows, cur = [], start_ms
    while cur < end_ms:
        stop = min(end_ms, cur + step * 1440)
        d = api("/openApi/swap/v3/quote/klines",
                {"symbol": sym, "interval": interval, "startTime": cur, "endTime": stop, "limit": 1440})
        time.sleep(PAUSE)
        if d:
            rows.extend(d)
        cur = stop + 1
    return klines_df(rows, step)


def fetch_last_klines(sym, interval, limit=1440):
    d = api("/openApi/swap/v3/quote/klines", {"symbol": sym, "interval": interval, "limit": limit})
    time.sleep(PAUSE)
    return klines_df(d or [], IV_MS[interval])


def klines_df(rows, step):
    if not rows:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])
    df = pd.DataFrame(rows).rename(columns={"time": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"})
    df = df[["t", "o", "h", "l", "c", "v"]].apply(pd.to_numeric, errors="coerce").dropna()
    df["t"] = df["t"].astype("int64")
    df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    now = int(time.time() * 1000)
    return df[df["t"] + step <= now].reset_index(drop=True)   # solo velas cerradas


def fetch_funding(sym, start_ms, end_ms):
    rows, cur = [], start_ms
    for _ in range(200):
        d = api("/openApi/swap/v2/quote/fundingRate",
                {"symbol": sym, "startTime": cur, "endTime": end_ms, "limit": 1000})
        time.sleep(PAUSE)
        if not d:
            break
        rows.extend(d)
        mx = max(int(x["fundingTime"]) for x in d)
        if len(d) < 1000 or mx <= cur:
            break
        cur = mx + 1
    if not rows:
        return pd.DataFrame(columns=["t", "f"])
    df = pd.DataFrame(rows)
    df = pd.DataFrame({"t": pd.to_numeric(df["fundingTime"]).astype("int64"),
                       "f": pd.to_numeric(df["fundingRate"]) * 100})
    return df.drop_duplicates("t").sort_values("t").reset_index(drop=True)


def live_funding_oi(sym):
    f = oi = None
    try:
        d = api("/openApi/swap/v2/quote/premiumIndex", {"symbol": sym})
        d = d[0] if isinstance(d, list) else d
        f = float(d.get("lastFundingRate")) * 100
    except Exception:
        pass
    try:
        d = api("/openApi/swap/v2/quote/openInterest", {"symbol": sym})
        d = d[0] if isinstance(d, list) else d
        oi = float(d.get("openInterest"))
    except Exception:
        pass
    return f, oi


# ═══════════════════════════════════════════════════════════════
# CACHÉ
# ═══════════════════════════════════════════════════════════════
def kpath(data_dir, interval, sym):
    return os.path.join(data_dir, interval, f"{sym}.csv.gz")


def fpath(data_dir, sym):
    return os.path.join(data_dir, "funding", f"{sym}.csv.gz")


def read_csv(path):
    return pd.read_csv(path) if os.path.exists(path) else None


def update_cache(sym, interval, days, data_dir, funding=True):
    step = IV_MS[interval]
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    path = kpath(data_dir, interval, sym)
    old = read_csv(path)
    if old is not None and len(old) and old["t"].min() <= start + step:
        new = fetch_klines(sym, interval, int(old["t"].max()) + step, now)
        df = pd.concat([old, new])
    else:
        df = fetch_klines(sym, interval, start, now)
    df = df.drop_duplicates("t").sort_values("t")
    df = df[df["t"] >= start]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")
    nf = 0
    if funding:
        fp = fpath(data_dir, sym)
        fo = read_csv(fp)
        fs = int(fo["t"].max()) + 1 if fo is not None and len(fo) and fo["t"].min() <= start + 8 * 3_600_000 else start
        fn = fetch_funding(sym, fs, now)
        fd = pd.concat([fo, fn]) if fo is not None and fs != start else fn
        fd = fd.drop_duplicates("t").sort_values("t")
        fd = fd[fd["t"] >= start - 86_400_000]
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        fd.to_csv(fp, index=False, compression="gzip")
        nf = len(fd)
    return len(df), nf


def cached_symbols(data_dir, interval):
    d = os.path.join(data_dir, interval)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-7] for f in os.listdir(d) if f.endswith(".csv.gz"))


# ═══════════════════════════════════════════════════════════════
# INDICADORES
# ═══════════════════════════════════════════════════════════════
def prep_btc(btc):
    b = btc.copy()
    b["day"] = (b["t"] // 86_400_000).astype("int64")
    b["dopen"] = b.groupby("day")["o"].transform("first")
    b["btc_pct"] = (b["c"] - b["dopen"]) / b["dopen"] * 100
    return b[["t", "btc_pct"]]


def prep(df, btcp, p):
    d = df.copy().reset_index(drop=True)
    d["day"] = (d["t"] // 86_400_000).astype("int64")
    g = d.groupby("day", sort=False)
    d["dopen"] = g["o"].transform("first")
    d["newday"] = d["day"] != d["day"].shift(1)
    first_day = d["day"].iloc[0]
    # el primer día puede estar incompleto → open diario falso
    d["valid"] = d["day"] != first_day
    pc = d["c"].shift(1)
    tr = pd.concat([d["h"] - d["l"], (d["h"] - pc).abs(), (d["l"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / p.atr_len, adjust=False).mean()
    hlc3 = (d["h"] + d["l"] + d["c"]) / 3
    cv = d.groupby("day")["v"].cumsum().replace(0, np.nan)
    cpv = (hlc3 * d["v"]).groupby(d["day"]).cumsum()
    cpv2 = (hlc3 * hlc3 * d["v"]).groupby(d["day"]).cumsum()
    vw = cpv / cv
    sd = np.sqrt((cpv2 / cv - vw * vw).clip(lower=0))
    d["vw"] = vw.fillna(hlc3)
    d["vwup"] = (vw + p.band_mult * sd).fillna(hlc3)
    d["vwlo"] = (vw - p.band_mult * sd).fillna(hlc3)
    d["hh"] = d["h"].rolling(p.ext_look).max().shift(1)
    d["ll"] = d["l"].rolling(p.ext_look).min().shift(1)
    d["vr"] = d["v"] / d["v"].rolling(20).mean().shift(1)
    rng = (d["h"] - d["l"]).replace(0, np.nan)
    d["upw"] = ((d["h"] - d[["o", "c"]].max(axis=1)) / rng).fillna(0)
    d["dnw"] = ((d[["o", "c"]].min(axis=1) - d["l"]) / rng).fillna(0)
    d = d.merge(btcp, on="t", how="left")
    d["btc_pct"] = d["btc_pct"].ffill().fillna(0)
    d["day_pct"] = (d["c"] - d["dopen"]) / d["dopen"] * 100
    d["hi_pct"] = (d["h"] - d["dopen"]) / d["dopen"] * 100
    d["lo_pct"] = (d["l"] - d["dopen"]) / d["dopen"] * 100
    d["qv"] = d["v"] * d["c"]
    return d


def vol_ok(mode, ext_vr, brk_vr, cur_vr, p):
    if mode == "none":
        return True
    if mode == "climax":
        return ext_vr >= p.vol_mult
    if mode == "low":
        return brk_vr < p.low_mult
    if mode == "exhaust":
        return ext_vr >= p.vol_mult and cur_vr < ext_vr
    raise ValueError(f"vol_mode desconocido: {mode}")


def signals(d, p):
    """Máquina de estados extensión → fallo. Devuelve dict de arrays con señales y metadatos."""
    n = len(d)
    L = {k: d[k].tolist() for k in ("o", "h", "l", "c", "dopen", "hi_pct", "lo_pct", "btc_pct", "vwup", "vwlo",
                                    "hh", "ll", "vr", "upw", "dnw", "newday")}
    o, h, l, c = L["o"], L["h"], L["l"], L["c"]
    cand = ((d["hi_pct"] >= p.min_move) | (d["lo_pct"] <= -p.min_move)).tolist()
    out_side = np.zeros(n, np.int8)
    out_ext = np.full(n, np.nan)
    out_lvl = np.full(n, np.nan)
    out_evr = np.full(n, np.nan)
    out_bvr = np.full(n, np.nan)
    tArm = bArm = False
    tExt = tLvl = bExt = bLvl = 0.0
    tBar = bBar = 0
    tVr = tBvr = bVr = bBvr = 0.0
    btc_lim = p.btc_max if p.btc_max > 0 else 1e9

    for i in range(n):
        if L["newday"][i]:
            tArm = bArm = False
        if not (tArm or bArm or cand[i]):
            continue
        vr = L["vr"][i]
        vr = 0.0 if vr != vr else vr
        bp = L["btc_pct"][i]
        btc_ok = abs(bp) <= btc_lim
        hh, ll = L["hh"][i], L["ll"][i]
        dop = L["dopen"][i]

        # ── techo
        top_ctx = (L["hi_pct"][i] >= p.min_move and L["hi_pct"][i] - bp >= p.rs_min and btc_ok
                   and (not p.band or h[i] >= L["vwup"][i]))
        if hh == hh and h[i] > hh and top_ctx and (not tArm or h[i] > tExt):
            if tArm:
                tVr = max(tVr, vr)
            else:
                tVr = tBvr = vr
            tArm, tExt, tLvl, tBar = True, h[i], hh, i
        elif tArm and h[i] > tExt:
            tExt, tBar, tVr = h[i], i, max(tVr, vr)
        if tArm and i - tBar > p.fail_bars:
            tArm = False

        # ── suelo
        bot_ctx = (L["lo_pct"][i] <= -p.min_move and L["lo_pct"][i] - bp <= -p.rs_min and btc_ok
                   and (not p.band or l[i] <= L["vwlo"][i]))
        if ll == ll and l[i] < ll and bot_ctx and (not bArm or l[i] < bExt):
            if bArm:
                bVr = max(bVr, vr)
            else:
                bVr = bBvr = vr
            bArm, bExt, bLvl, bBar = True, l[i], ll, i
        elif bArm and l[i] < bExt:
            bExt, bBar, bVr = l[i], i, max(bVr, vr)
        if bArm and i - bBar > p.fail_bars:
            bArm = False

        # ── fallo
        if p.shorts and tArm and tExt > dop:
            retr = (tExt - c[i]) / (tExt - dop) * 100
            rej = c[i] < o[i] or L["upw"][i] >= p.wick
            if c[i] < tLvl and retr >= p.min_retr and (not p.reject or rej) and vol_ok(p.vol_mode, tVr, tBvr, vr, p):
                out_side[i], out_ext[i], out_lvl[i], out_evr[i], out_bvr[i] = -1, tExt, tLvl, tVr, tBvr
                tArm = False
                continue
        if p.longs and bArm and bExt < dop:
            retr = (c[i] - bExt) / (dop - bExt) * 100
            rej = c[i] > o[i] or L["dnw"][i] >= p.wick
            if c[i] > bLvl and retr >= p.min_retr and (not p.reject or rej) and vol_ok(p.vol_mode, bVr, bBvr, vr, p):
                out_side[i], out_ext[i], out_lvl[i], out_evr[i], out_bvr[i] = 1, bExt, bLvl, bVr, bBvr
                bArm = False
    return {"side": out_side, "ext": out_ext, "lvl": out_lvl, "evr": out_evr, "bvr": out_bvr}


def plan(d, i, side, ext, p):
    """Entrada/SL/TPs para una señal en la vela i. None si el riesgo excede max_risk_atr."""
    c, atr, dop, vw = d["c"].iat[i], d["atr"].iat[i], d["dopen"].iat[i], d["vw"].iat[i]
    if side == -1:
        sl = max(ext + p.sl_buf * atr, c + p.min_risk_atr * atr)
        risk = sl - c
        tgt = {"retr": ext - (ext - dop) * p.tp2_retr / 100, "vwap": vw}.get(p.tp2_mode, c - p.tp2_r * risk)
        tp1 = c - p.tp1_r * risk
        tp2 = min(tgt, c - max(p.min_tp2_r, p.tp1_r + 0.25) * risk)
    else:
        sl = min(ext - p.sl_buf * atr, c - p.min_risk_atr * atr)
        risk = c - sl
        tgt = {"retr": ext + (dop - ext) * p.tp2_retr / 100, "vwap": vw}.get(p.tp2_mode, c + p.tp2_r * risk)
        tp1 = c + p.tp1_r * risk
        tp2 = max(tgt, c + max(p.min_tp2_r, p.tp1_r + 0.25) * risk)
    if risk <= 0 or risk > p.max_risk_atr * atr:
        return None
    return {"entry": c, "sl": sl, "tp1": tp1, "tp2": tp2, "risk": risk, "atr": atr}


# ═══════════════════════════════════════════════════════════════
# SIMULACIÓN
# ═══════════════════════════════════════════════════════════════
def run_trade(A, i, s, pl, p):
    """s = +1 long / -1 short. Conservador: dentro de una vela se evalúa el stop antes que los TP."""
    n = len(A["c"])
    slip = p.slip_bps / 1e4
    ent = pl["entry"] * (1 + s * slip)
    stop, tp1, tp2 = pl["sl"], pl["tp1"], pl["tp2"]
    frac1 = p.tp1_pct / 100
    rem, be, best = 1.0, False, pl["entry"]
    ex = []  # (frac, px, reason, maker)
    j = i + 1
    while j < n:
        o, h, l, c = A["o"][j], A["h"][j], A["l"][j], A["c"][j]
        hit_stop = (l <= stop) if s == 1 else (h >= stop)
        if hit_stop:
            px = o if ((s == 1 and o < stop) or (s == -1 and o > stop)) else stop
            ex.append((rem, px * (1 - s * slip), "trail" if be else "sl", False))
            rem = 0
            break
        if not be and ((s == 1 and h >= tp1) or (s == -1 and l <= tp1)):
            if frac1 > 0:
                ex.append((rem * frac1, tp1, "tp1", True))
                rem -= rem * frac1
            be = True
            stop = max(stop, ent) if s == 1 else min(stop, ent)
        if (s == 1 and h >= tp2) or (s == -1 and l <= tp2):
            ex.append((rem, tp2, "tp2", True))
            rem = 0
            break
        best = max(best, h) if s == 1 else min(best, l)
        if be:
            a = A["atr"][j]
            stop = max(stop, best - p.trail_atr * a) if s == 1 else min(stop, best + p.trail_atr * a)
        if not be and j - i >= p.max_bars:
            ex.append((rem, c * (1 - s * slip), "time", False))
            rem = 0
            break
        j += 1
    if rem > 0:
        j = n - 1
        ex.append((rem, A["c"][j] * (1 - s * slip), "eod", False))
    gross = sum(f * (px - ent) * s for f, px, _, _ in ex)
    fees = ent * p.fee_taker / 100 + sum(f * px * (p.fee_maker if mk else p.fee_taker) / 100 for f, px, _, mk in ex)
    risk = pl["risk"]
    reasons = "+".join(r for _, _, r, _ in ex)
    return {"exit_idx": j, "R": (gross - fees) / risk, "R_gross": gross / risk, "fee_R": fees / risk,
            "exit": reasons, "exit_px": sum(f * px for f, px, _, _ in ex), "entry_fill": ent}


def eligibility(frames, p):
    """Universo por día sin lookahead: media 7d del volumen USDT, desplazada 1 día, ranking transversal."""
    daily = {}
    for sym, d in frames.items():
        daily[sym] = d.groupby("day")["qv"].sum()
    q = pd.DataFrame(daily).sort_index()
    q7 = q.rolling(7, min_periods=3).mean().shift(1)
    rank = q7.rank(axis=1, ascending=False, method="first")
    ok = (rank > p.excl_top) & (q7 >= p.min_qv)
    if p.max_qv > 0:
        ok &= q7 <= p.max_qv
    return ok.fillna(False), rank, q7


def backtest(frames, fundings, p, quiet=False):
    ok, rank, q7 = eligibility(frames, p)
    trades = []
    for sym, d in frames.items():
        if sym == BTC:
            continue
        sig = signals(d, p)
        idx = np.nonzero(sig["side"])[0]
        if not len(idx):
            continue
        A = {k: d[k].tolist() for k in ("o", "h", "l", "c", "atr")}
        days = d["day"].to_numpy()
        valid = d["valid"].to_numpy()
        ok_s = ok[sym] if sym in ok else None
        fund = fundings.get(sym)
        ft = fund["t"].to_numpy() if fund is not None and len(fund) else None
        pos_end, last_exit = -1, -10 ** 9
        done = {}
        for i in idx:
            if i <= pos_end or i - last_exit < p.cooldown or not valid[i]:
                continue
            day = days[i]
            if ok_s is None or not bool(ok_s.get(day, False)):
                continue
            s = int(sig["side"][i])
            if p.one_per_day and done.get((day, s)):
                continue
            f = np.nan
            if ft is not None:
                k = np.searchsorted(ft, d["t"].iat[i], side="right") - 1
                if k >= 0:
                    f = fund["f"].iat[k]
            if p.f_min > 0:
                if f != f or (s == -1 and f < p.f_min) or (s == 1 and f > -p.f_min):
                    continue
            pl = plan(d, i, s, sig["ext"][i], p)
            if pl is None:
                continue
            r = run_trade(A, i, s, pl, p)
            done[(day, s)] = True
            pos_end = last_exit = r["exit_idx"]
            trades.append({
                "symbol": sym, "side": "short" if s == -1 else "long",
                "entry_time": pd.to_datetime(d["t"].iat[i], unit="ms", utc=True),
                "exit_time": pd.to_datetime(d["t"].iat[r["exit_idx"]], unit="ms", utc=True),
                "bars": r["exit_idx"] - i, "entry": pl["entry"], "sl": pl["sl"], "tp1": pl["tp1"], "tp2": pl["tp2"],
                "R": r["R"], "R_gross": r["R_gross"], "fee_R": r["fee_R"], "exit": r["exit"],
                "risk_pct": pl["risk"] / pl["entry"] * 100, "risk_atr": pl["risk"] / pl["atr"],
                "day_pct": d["day_pct"].iat[i], "ext_pct": (sig["ext"][i] - d["dopen"].iat[i]) / d["dopen"].iat[i] * 100,
                "btc_pct": d["btc_pct"].iat[i], "ext_vr": sig["evr"][i], "brk_vr": sig["bvr"][i],
                "sig_vr": d["vr"].iat[i], "funding": f,
                "qv_rank": rank.at[day, sym] if day in rank.index else np.nan,
                "qv7": q7.at[day, sym] if day in q7.index else np.nan,
            })
    tr = pd.DataFrame(trades)
    if len(tr):
        tr = tr.sort_values("exit_time").reset_index(drop=True)
    return tr


# ═══════════════════════════════════════════════════════════════
# ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════
def stats(R):
    R = np.asarray(R, float)
    n = len(R)
    if n == 0:
        return {"n": 0}
    pos, neg = R[R > 0].sum(), -R[R < 0].sum()
    cum = np.cumsum(R)
    dd = (np.maximum.accumulate(np.maximum(cum, 0)) - cum).max()
    sd = R.std(ddof=1) if n > 1 else np.nan
    return {"n": n, "wr": (R > 0).mean() * 100, "avgR": R.mean(), "medR": np.median(R),
            "PF": pos / neg if neg > 0 else np.inf, "sumR": R.sum(), "t": R.mean() / sd * math.sqrt(n) if sd and sd > 0 else np.nan,
            "maxDD_R": dd}


def fmt(st):
    if not st.get("n"):
        return "n=0"
    return (f"n={st['n']:<5} WR={st['wr']:5.1f}%  avgR={st['avgR']:+.3f}  medR={st['medR']:+.2f}  "
            f"PF={st['PF']:.2f}  ΣR={st['sumR']:+.1f}  t={st['t']:+.2f}  DD={st['maxDD_R']:.1f}R")


def monthly(tr):
    m = tr.groupby(tr["exit_time"].dt.strftime("%Y-%m"))["R"].agg(["count", "sum", "mean"])
    s = m["sum"]
    t = s.mean() / s.std(ddof=1) * math.sqrt(len(s)) if len(s) > 1 and s.std(ddof=1) > 0 else np.nan
    return m, t


def bucket_report(tr, col, q=3):
    x = tr[col]
    if x.notna().sum() < q * 5:
        return None
    try:
        b = pd.qcut(x, q, duplicates="drop")
    except ValueError:
        return None
    rows = []
    for k, g in tr.groupby(b, observed=True):
        rows.append(f"    {str(k):<28} {fmt(stats(g['R']))}")
    return "\n".join(rows)


def report(tr, p, label=""):
    L = [f"═══ FED backtest {label} · {CODE_VERSION}",
         f"params: {dataclasses.asdict(p)}"]
    if not len(tr):
        L.append("SIN TRADES")
        return "\n".join(L)
    L.append(f"periodo: {tr['entry_time'].min():%Y-%m-%d} → {tr['exit_time'].max():%Y-%m-%d} · símbolos con trades: {tr['symbol'].nunique()}")
    L.append("")
    L.append(f"TOTAL (neto)   {fmt(stats(tr['R']))}")
    L.append(f"TOTAL (bruto)  {fmt(stats(tr['R_gross']))}   coste medio {tr['fee_R'].mean():.3f}R/trade")
    for s in ("short", "long"):
        g = tr[tr.side == s]
        if len(g):
            L.append(f"  {s:<6}       {fmt(stats(g['R']))}")
    half = tr["entry_time"].min() + (tr["exit_time"].max() - tr["entry_time"].min()) / 2
    L.append("")
    L.append(f"IS  (< {half:%Y-%m-%d})  {fmt(stats(tr[tr.entry_time < half]['R']))}")
    L.append(f"OOS (≥ {half:%Y-%m-%d})  {fmt(stats(tr[tr.entry_time >= half]['R']))}")
    m, tm = monthly(tr)
    L.append("")
    L.append(f"MENSUAL (ΣR)   t-stat mensual = {tm:+.2f}   ({len(m)} meses)")
    for k, r in m.iterrows():
        L.append(f"    {k}  n={int(r['count']):<4} ΣR={r['sum']:+7.2f}  avgR={r['mean']:+.3f}")
    L.append("")
    L.append("SALIDAS")
    for k, g in tr.groupby("exit"):
        L.append(f"    {k:<14} n={len(g):<5} avgR={g['R'].mean():+.3f}")
    L.append("")
    L.append("CONCENTRACIÓN")
    top = tr.groupby("symbol")["R"].sum().sort_values()
    L.append(f"    peores: {', '.join(f'{k} {v:+.1f}' for k, v in top.head(5).items())}")
    L.append(f"    mejores: {', '.join(f'{k} {v:+.1f}' for k, v in top.tail(5)[::-1].items())}")
    L.append(f"    ΣR sin los 5 mejores símbolos: {top.iloc[:-5].sum() if len(top) > 5 else float('nan'):+.1f}")
    L.append(f"    peor trade {tr['R'].min():+.2f}R · mejor {tr['R'].max():+.2f}R")
    L.append("")
    L.append("DESGLOSES (terciles — hipótesis, NO optimizar sobre esto)")
    for col, name in (("funding", "funding % al entrar"), ("ext_vr", "vol extensión / media"),
                      ("brk_vr", "vol vela ruptura / media"), ("ext_pct", "extremo del día %"),
                      ("qv_rank", "rank volumen"), ("risk_atr", "riesgo en ATR"), ("btc_pct", "BTC día %")):
        b = bucket_report(tr, col)
        if b:
            L.append(f"  {name}")
            L.append(b)
    hrs = tr.groupby(tr["entry_time"].dt.hour // 4 * 4)["R"].agg(["count", "mean"])
    L.append("  hora UTC (bloques 4h)")
    for k, r in hrs.iterrows():
        L.append(f"    {k:02d}-{k + 3:02d}h  n={int(r['count']):<4} avgR={r['mean']:+.3f}")
    # curva de equity (riesgo fijo, sin compuesto)
    eq = (tr["R"] * p.risk_pct).cumsum()
    L.append("")
    L.append(f"EQUITY ({p.risk_pct}% riesgo/trade, sin compuesto): {eq.iloc[-1]:+.1f}%  · DD máx {(eq.cummax().clip(lower=0) - eq).max():.1f}%")
    ev = pd.concat([pd.Series(1, tr["entry_time"]), pd.Series(-1, tr["exit_time"])]).sort_index().cumsum()
    L.append(f"posiciones simultáneas máx: {int(ev.max())}")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════
# CARGA
# ═══════════════════════════════════════════════════════════════
def load_frames(data_dir, p, symbols=None, quiet=False):
    syms = [s for s in (symbols or cached_symbols(data_dir, p.interval)) if is_crypto(s)]
    if BTC not in syms:
        raise SystemExit(f"Falta {BTC} en la caché ({data_dir}/{p.interval}). Ejecuta 'download' primero.")
    raw = {s: read_csv(kpath(data_dir, p.interval, s)) for s in syms}
    btcp = prep_btc(raw[BTC])
    frames, fundings = {}, {}
    for s, df in raw.items():
        if df is None or len(df) < 500:
            continue
        frames[s] = prep(df, btcp, p)
        fd = read_csv(fpath(data_dir, s))
        if fd is not None and len(fd):
            fundings[s] = fd
    if not quiet:
        print(f"cargados {len(frames)} símbolos · funding para {len(fundings)}", file=sys.stderr)
    return frames, fundings, raw, btcp


# ═══════════════════════════════════════════════════════════════
# COMANDOS
# ═══════════════════════════════════════════════════════════════
def cmd_download(a):
    p = params_from(a)
    tk = tickers()
    contracts = set(list_contracts())
    ranked = sorted((s for s in tk if s in contracts), key=lambda s: -tk[s]["qv"])
    syms = [s for s in ranked if tk[s]["qv"] >= a.min_24h_qv][: a.max_symbols]
    if BTC not in syms:
        syms.insert(0, BTC)
    print(f"{CODE_VERSION} · descargando {len(syms)} símbolos · {p.interval} · {a.days} días → {a.data}", file=sys.stderr)
    for k, s in enumerate(syms, 1):
        try:
            nk, nf = update_cache(s, p.interval, a.days, a.data, funding=not a.no_funding)
            print(f"[{k}/{len(syms)}] {s:<16} velas={nk:<7} funding={nf}", file=sys.stderr)
        except ApiError as e:
            print(f"[{k}/{len(syms)}] {s:<16} ERROR {e}", file=sys.stderr)


def cmd_backtest(a):
    p = params_from(a)
    frames, fundings, _, _ = load_frames(a.data, p)
    t0 = time.time()
    tr = backtest(frames, fundings, p)
    rep = report(tr, p)
    print(rep)
    os.makedirs(a.out, exist_ok=True)
    tr.to_csv(os.path.join(a.out, "trades.csv"), index=False)
    with open(os.path.join(a.out, "report.txt"), "w") as f:
        f.write(rep + "\n")
    if len(tr):
        eq = pd.DataFrame({"time": tr["exit_time"], "R": tr["R"], "cumR": tr["R"].cumsum()})
        eq.to_csv(os.path.join(a.out, "equity.csv"), index=False)
    print(f"\n→ {a.out}/trades.csv, report.txt, equity.csv  ({time.time() - t0:.1f}s)", file=sys.stderr)


def parse_grid(s):
    grid = {}
    types = {f.name: f.type for f in fields(P)}
    for part in s.split(";"):
        if not part.strip():
            continue
        k, v = part.split("=", 1)
        k = k.strip().replace("-", "_")
        if k not in types:
            raise SystemExit(f"parámetro desconocido en grid: {k}")
        t = types[k]
        t = {"int": int, "float": float, "str": str, "bool": str2bool}.get(t, t) if isinstance(t, str) else t
        t = str2bool if t is bool else t
        grid[k] = [t(x.strip()) for x in v.split(",")]
    return grid


def cmd_sweep(a):
    import itertools
    base = params_from(a)
    grid = parse_grid(a.grid)
    keys = list(grid)
    combos = list(itertools.product(*grid.values()))
    prep_keys = {"atr_len", "ext_look", "band_mult", "interval"}
    cache = {}
    rows = []
    print(f"{len(combos)} combinaciones", file=sys.stderr)
    for vals in combos:
        p = dataclasses.replace(base, **dict(zip(keys, vals)))
        pk = tuple(getattr(p, k) for k in sorted(prep_keys))
        if pk not in cache:
            cache.clear()
            cache[pk] = load_frames(a.data, p, quiet=True)
        frames, fundings, _, _ = cache[pk]
        tr = backtest(frames, fundings, p)
        row = dict(zip(keys, vals))
        if len(tr):
            half = tr["entry_time"].min() + (tr["exit_time"].max() - tr["entry_time"].min()) / 2
            a_, i_, o_ = stats(tr["R"]), stats(tr[tr.entry_time < half]["R"]), stats(tr[tr.entry_time >= half]["R"])
            _, tm = monthly(tr)
            row.update(n=a_["n"], wr=round(a_["wr"], 1), avgR=round(a_["avgR"], 3), PF=round(a_["PF"], 2),
                       sumR=round(a_["sumR"], 1), t=round(a_["t"], 2), t_month=round(tm, 2), dd=round(a_["maxDD_R"], 1),
                       IS_avgR=round(i_.get("avgR", np.nan), 3), OOS_avgR=round(o_.get("avgR", np.nan), 3),
                       OOS_n=o_.get("n", 0))
        else:
            row.update(n=0)
        rows.append(row)
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()), file=sys.stderr)
    df = pd.DataFrame(rows)
    if "avgR" in df:
        df = df.sort_values("avgR", ascending=False)
    os.makedirs(a.out, exist_ok=True)
    df.to_csv(os.path.join(a.out, "sweep.csv"), index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 50, "display.max_rows", 500):
        print(df.to_string(index=False))
    print(f"\n{len(combos)} combinaciones probadas → ajusta tus expectativas: el mejor t-stat de una rejilla está inflado.",
          file=sys.stderr)
    print(f"→ {a.out}/sweep.csv", file=sys.stderr)


def tg_send(text):
    tok = env("TELEGRAM_TOKEN") or env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=10)
    except requests.RequestException as e:
        print(f"telegram error: {e}", file=sys.stderr)


def scan_once(p, a, seen):
    tk = tickers()
    contracts = set(list_contracts())
    ranked = sorted((s for s in tk if s in contracts), key=lambda s: -tk[s]["qv"])
    universe = [s for s in ranked[p.excl_top:] if tk[s]["qv"] >= p.min_qv and (p.max_qv <= 0 or tk[s]["qv"] <= p.max_qv)]
    # prefiltro barato: el rango 24h tiene que poder contener un extremo diario ≥ min_move
    cands = [s for s in universe if tk[s]["low"] > 0 and (tk[s]["high"] / tk[s]["low"] - 1) * 100 >= p.min_move]
    btc = fetch_last_klines(BTC, p.interval, 1440)
    btcp = prep_btc(btc)
    now = datetime.now(timezone.utc)
    hits, armed = [], []
    for s in cands:
        try:
            df = fetch_last_klines(s, p.interval, 1440)
        except ApiError as e:
            print(f"{s}: {e}", file=sys.stderr)
            continue
        if len(df) < 300:
            continue
        d = prep(df, btcp, p)
        sig = signals(d, p)
        i = len(d) - 1
        last = d.iloc[i]
        today = d[d["day"] == last["day"]]
        dhi = (today["h"].max() - last["dopen"]) / last["dopen"] * 100
        dlo = (today["l"].min() - last["dopen"]) / last["dopen"] * 100
        if (dhi >= p.min_move and dhi - last["btc_pct"] >= p.rs_min) or (dlo <= -p.min_move and dlo - last["btc_pct"] <= -p.rs_min):
            armed.append(f"{s} día {last['day_pct']:+.1f}% (máx {dhi:+.1f} / mín {dlo:+.1f})")
        side = int(sig["side"][i])
        if not side:
            continue
        key = (s, int(d["t"].iat[i]))
        if key in seen:
            continue
        seen.add(key)
        pl = plan(d, i, side, sig["ext"][i], p)
        if pl is None:
            continue
        f, oi = live_funding_oi(s)
        if p.f_min > 0 and f is not None and ((side == -1 and f < p.f_min) or (side == 1 and f > -p.f_min)):
            continue
        dec = max(0, -int(math.floor(math.log10(abs(pl["entry"]))) ) + 4) if pl["entry"] > 0 else 6
        msg = (f"FED {'↓ SHORT' if side == -1 else '↑ LONG'} {s}\n"
               f"entry {pl['entry']:.{dec}f} · SL {pl['sl']:.{dec}f} · TP1 {pl['tp1']:.{dec}f} · TP2 {pl['tp2']:.{dec}f}\n"
               f"riesgo {pl['risk'] / pl['entry'] * 100:.2f}% ({pl['risk'] / pl['atr']:.2f} ATR) · día {last['day_pct']:+.1f}% · "
               f"BTC {last['btc_pct']:+.1f}% · volExt {sig['evr'][i]:.1f}x\n"
               f"funding {f if f is None else round(f, 4)}% · OI {oi}\n"
               f"vela {pd.to_datetime(d['t'].iat[i], unit='ms', utc=True):%Y-%m-%d %H:%M} UTC")
        hits.append(msg)
    print(f"[{now:%Y-%m-%d %H:%M:%S}Z] universo {len(universe)} · candidatos {len(cands)} · en contexto {len(armed)} · señales {len(hits)}")
    for x in armed:
        print("   · " + x)
    for m in hits:
        print("\n" + m)
        tg_send(m)


def cmd_scan(a):
    p = params_from(a)
    print(f"{CODE_VERSION} · scan {p.interval} · modo SEÑAL (no opera)", file=sys.stderr)
    seen = set()
    step = IV_MS[p.interval] / 1000
    while True:
        try:
            scan_once(p, a, seen)
        except Exception as e:  # el loop no debe morir por un fallo puntual de red
            print(f"scan error: {e!r}", file=sys.stderr)
        if not a.loop:
            break
        if len(seen) > 5000:
            seen.clear()
        wait = step - (time.time() % step) + 8
        time.sleep(wait)


def main():
    ap = argparse.ArgumentParser(description=CODE_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("download", "backtest", "sweep", "scan"):
        sp = sub.add_parser(name)
        sp.add_argument("--data", default=env("FED_DATA", "data"))
        sp.add_argument("--out", default=env("FED_OUT", "out"))
        add_param_args(sp)
        if name == "download":
            sp.add_argument("--days", type=int, default=120)
            sp.add_argument("--max-symbols", type=int, default=200)
            sp.add_argument("--min-24h-qv", type=float, default=2e6)
            sp.add_argument("--no-funding", action="store_true")
        if name == "sweep":
            sp.add_argument("--grid", required=True)
        if name == "scan":
            sp.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    {"download": cmd_download, "backtest": cmd_backtest, "sweep": cmd_sweep, "scan": cmd_scan}[a.cmd](a)


if __name__ == "__main__":
    main()
