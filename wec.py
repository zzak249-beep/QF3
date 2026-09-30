"""
C · WEC v2 (fade de extremos con mecha de rechazo) — la MISMA lógica que wec_v2.pine.

Día extremo: la moneda lleva ≥ 9% desde la apertura UTC y ≥ 3.5 puntos más que BTC (o al revés).
Señal: nuevo máximo de 25 velas con mecha superior ≥ 55% del rango (techo) o nuevo mínimo con
mecha inferior (suelo), volumen ≥ 2× la media 20 y eficiencia de 180 velas ≤ 0.35.
Entrada al cierre de la vela. Stop más allá de la mecha (+0.2 ATR). Objetivo 2R (o VWAP).
Salida por tiempo a las 36 velas. Una operación por día y lado. Coste ≤ 0.20R.

CONTROL: exactamente lo mismo pero SIN exigir la mecha. Si WEC no le gana, la mecha no aporta.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

COST_RT = 0.0016


@dataclass
class W:
    min_day: float = 0.09
    rs_min: float = 0.035
    hi_look: int = 25
    wick_min: float = 0.55
    vol_mult: float = 2.0
    er_len: int = 180
    max_er: float = 0.35
    stop_buf: float = 0.2
    tp: str = "R"            # R | VWAP
    rr: float = 2.0
    max_bars: int = 36
    max_cost_r: float = 0.20


VARIANTS = [
    ("BASE (Pine por defecto)", {}),
    ("Sin filtro de eficiencia", dict(max_er=0.0)),
    ("Sin filtro de volumen", dict(vol_mult=0.0)),
    ("Mecha ≥ 70%", dict(wick_min=0.70)),
    ("Objetivo VWAP del día", dict(tp="VWAP")),
    ("Objetivo 1R", dict(rr=1.0)),
]


def features(df: pd.DataFrame, btc: pd.DataFrame | None) -> pd.DataFrame:
    d = df.copy()
    day = d.index.floor("D")
    d["dopen"] = d.groupby(day)["open"].transform("first")
    d["pct"] = d["close"] / d["dopen"] - 1
    if btc is not None:
        b = btc.reindex(d.index)
        bday = b.index.floor("D")
        bo = b.groupby(bday)["open"].transform("first")
        d["bpct"] = (b["close"] / bo - 1).fillna(0.0)
    else:
        d["bpct"] = 0.0
    d["rs"] = d["pct"] - d["bpct"]
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["volavg"] = d["volume"].rolling(20).mean()
    tp = (d["high"] + d["low"] + d["close"]) / 3
    pv = (tp * d["volume"]).groupby(day).cumsum()
    vv = d["volume"].groupby(day).cumsum()
    d["vwap"] = np.where(vv > 0, pv / vv.replace(0, np.nan), d["close"])
    rng = (d["high"] - d["low"]).replace(0, np.nan)
    d["up"] = ((d["high"] - d[["open", "close"]].max(axis=1)) / rng).fillna(0)
    d["lo"] = ((d[["open", "close"]].min(axis=1) - d["low"]) / rng).fillna(0)
    return d


def run_symbol(d: pd.DataFrame, p: W, sym: str) -> tuple[list, list]:
    """Devuelve (operaciones WEC, operaciones control) con la R neta de cada una."""
    hh = d["high"].rolling(p.hi_look).max().to_numpy()
    ll = d["low"].rolling(p.hi_look).min().to_numpy()
    path = d["close"].diff().abs().rolling(p.er_len).sum()
    er = ((d["close"] - d["close"].shift(p.er_len)).abs() / path).fillna(1.0).to_numpy()
    o, h, l, c = (d[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr, vol, va, vw = d["atr"].to_numpy(), d["volume"].to_numpy(), d["volavg"].to_numpy(), d["vwap"].to_numpy()
    pct, rs, up, lo = d["pct"].to_numpy(), d["rs"].to_numpy(), d["up"].to_numpy(), d["lo"].to_numpy()
    days = d.index.floor("D").to_numpy()
    idx = d.index
    n = len(d)
    vol_ok = (p.vol_mult <= 0) | ((va > 0) & (vol >= p.vol_mult * np.nan_to_num(va)))
    er_ok = (p.max_er <= 0) | (er <= p.max_er)
    top = (pct >= p.min_day) & (rs >= p.rs_min) & (h >= hh) & vol_ok & er_ok
    bot = (pct <= -p.min_day) & (rs <= -p.rs_min) & (l <= ll) & vol_ok & er_ok
    out = {}
    for kind, need_wick in (("wec", True), ("ctrl", False)):
        cand_t = top & ((up >= p.wick_min) if need_wick else True)
        cand_b = bot & ((lo >= p.wick_min) if need_wick else True)
        trades, free, done = [], 0, set()
        for t in np.where(cand_t | cand_b)[0]:
            if t < free or t >= n - 2 or np.isnan(atr[t]):
                continue
            short = bool(cand_t[t])
            key = (days[t], short)
            if key in done:
                continue
            ent = c[t]
            stp = h[t] + p.stop_buf * atr[t] if short else l[t] - p.stop_buf * atr[t]
            risk = (stp - ent) if short else (ent - stp)
            if risk <= 0:
                continue
            cr = COST_RT * ent / risk
            if cr > p.max_cost_r:
                continue
            tgt = ent - p.rr * risk if short else ent + p.rr * risk
            if p.tp == "VWAP":
                if short and vw[t] < ent - 0.5 * risk:
                    tgt = vw[t]
                elif not short and vw[t] > ent + 0.5 * risk:
                    tgt = vw[t]
            r, k = None, t
            for k in range(t + 1, min(n, t + p.max_bars + 1)):
                hit_s = h[k] >= stp if short else l[k] <= stp
                hit_t = l[k] <= tgt if short else h[k] >= tgt
                if hit_s:
                    r = -1.0 - cr
                    break
                if hit_t:
                    r = abs(tgt - ent) / risk - cr
                    break
            if r is None:
                r = ((ent - c[k]) if short else (c[k] - ent)) / risk - cr
            trades.append(dict(sym=sym, t_entry=idx[t], day=days[t], dir=-1 if short else 1, net=r,
                               day_pct=pct[t], wick=up[t] if short else lo[t], er=er[t]))
            done.add(key)
            free = k + 1
        out[kind] = trades
    return out["wec"], out["ctrl"]


def _welch(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 5 or len(b) < 5:
        return np.nan
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return (a.mean() - b.mean()) / se if se > 0 else np.nan


def _t_by_day(df):
    if len(df) < 5:
        return np.nan
    g = df.groupby("day")["net"].mean()
    return g.mean() / (g.std(ddof=1) / math.sqrt(len(g))) if len(g) > 4 and g.std() > 0 else np.nan


def study_c(frames5: dict, btc: pd.DataFrame | None):
    feats = {s: features(df, btc if s.upper() not in ("BTC", "BTCUSDT") else None) for s, df in frames5.items()}
    rows, table, all_tr = [], [], []
    for name, kw in VARIANTS:
        p = replace(W(), **kw)
        wt, ct = [], []
        for s, d in feats.items():
            a_, b_ = run_symbol(d, p, s)
            wt += a_
            ct += b_
        wd, cd = pd.DataFrame(wt), pd.DataFrame(ct)
        if len(wd):
            wd = wd.sort_values("t_entry")
            k = int(len(wd) * 0.7)
            # "resto": nuevos extremos del control que NO tenían mecha (lo que la mecha descarta)
            keys = set(zip(wd["sym"], wd["t_entry"]))
            rest = cd[[(a, b) not in keys for a, b in zip(cd["sym"], cd["t_entry"])]] if len(cd) else cd
            s = dict(name=name, n=len(wd), days=wd["day"].nunique(), win=(wd["net"] > 0).mean() * 100,
                     mean=wd["net"].mean(), t=_t_by_day(wd), tr=wd["net"].iloc[:k].mean(), te=wd["net"].iloc[k:].mean(),
                     ctrl=rest["net"].mean() if len(rest) else np.nan, nctrl=len(rest),
                     tdiff=_welch(wd["net"], rest["net"]) if len(rest) >= 10 else np.nan)
            all_tr.append(wd.assign(variant=name))
        else:
            s = dict(name=name, n=0, days=0, win=np.nan, mean=np.nan, t=np.nan, tr=np.nan, te=np.nan,
                     ctrl=cd["net"].mean() if len(cd) else np.nan, nctrl=len(cd), tdiff=np.nan)
        s["delta"] = s["mean"] - s["ctrl"] if s["n"] and not np.isnan(s["ctrl"]) else np.nan
        rows.append(s)
        f = lambda x, fm="{:+.2f}": "—" if x is None or (isinstance(x, float) and np.isnan(x)) else fm.format(x)
        table.append([name, s["n"], s["days"], f(s["win"], "{:.0f}%"), f(s["mean"]) + "R", f(s["t"]),
                      f(s["tr"]) + "R", f(s["te"]) + "R", f(s["ctrl"]) + f"R ({s['nctrl']})",
                      f(s["delta"]) + "R · t " + f(s["tdiff"])])
    ok = [r for r in rows if r["n"] >= 30 and not np.isnan(r["t"])]
    if not ok:
        verd = "❌ sin muestra suficiente (< 30 operaciones en todas las variantes)"
    else:
        best = max(ok, key=lambda r: r["t"])
        if best["t"] >= 3 and best["te"] > 0 and best["mean"] > 0:
            verd = f"✅ APORTA ({best['name']}): t {best['t']:+.2f}, positiva en prueba y neta de costes"
        elif best["t"] >= 2 and best["mean"] > 0:
            verd = f"🟡 INDICIO ({best['name']}): t {best['t']:+.2f}, no concluyente (se pide t ≥ 3)"
        else:
            verd = "❌ NO APORTA: no se distingue del azar después de costes"
    wk = [r for r in rows if r["n"] >= 30 and r["nctrl"] >= 30 and not np.isnan(r["tdiff"])]
    if wk:
        bw = max(wk, key=lambda r: r["tdiff"])
        wick_v = (f"✅ la mecha SÍ aporta ({bw['name']}: {bw['delta']:+.2f}R sobre los extremos sin mecha, t {bw['tdiff']:+.2f})"
                  if bw["tdiff"] >= 3 and bw["delta"] > 0.1 else
                  f"❌ la mecha NO aporta: los extremos sin mecha rinden igual (mejor t de la diferencia {bw['tdiff']:+.2f})")
    else:
        wick_v = "— sin extremos sin mecha suficientes para comparar (los filtros ya los eliminan)"
    verd = verd + " · " + wick_v
    md = "\n".join(["| " + " | ".join(h) + " |" for h in [["Variante", "ops", "días", "acierto", "E neta", "t (por día)",
                                                           "entrenamiento", "prueba", "extremos SIN mecha", "WEC − sin mecha"]]] +
                   ["|" + "---|" * 10] + ["| " + " | ".join(str(c) for c in r) + " |" for r in table])
    txt = ("## C · WEC v2 (fade de extremos con mecha)\n\n**" + verd + "**\n\n" + md +
           "\n\n*E neta en R después de 0.16% de costes. t agrupada por día (varias monedas el mismo día no son "
           "independientes). WEC − sin mecha = cuánto mejor rinde WEC que hacer fade de los nuevos "
           "extremos que la mecha descarta, con los mismos filtros; t ≥ 3 para decir que la mecha aporta.*")
    return txt, verd, (pd.concat(all_tr, ignore_index=True) if all_tr else pd.DataFrame())
