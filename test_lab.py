"""El laboratorio debe decir NO APORTA en ruido y APORTA con un efecto real plantado."""
import numpy as np
import pandas as pd

import lab


def five_min(days, seed, vol=0.0015):
    rng = np.random.default_rng(seed)
    n = days * 288
    idx = pd.date_range("2025-10-01", periods=n, freq="5min", tz="UTC")
    c = 100 * np.exp(np.cumsum(rng.standard_normal(n) * vol))
    return idx, c, rng


def frame(idx, c):
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c), low=np.minimum(o, c), close=c, volume=1.0), index=idx)


def a_data(planted, n_sym=12, days=200):
    frames, events = {}, {}
    for s in range(n_sym):
        idx, c, rng = five_min(days, 100 + s)
        times = idx[(idx.hour % 8 == 0) & (idx.minute == 0)][1:]
        rates = rng.choice([0.0001, 0.00005, -0.00003, 0.0008, -0.0008, 0.0015], size=len(times),
                           p=[0.45, 0.2, 0.15, 0.08, 0.07, 0.05])
        if planted:
            lr = np.diff(np.log(c), prepend=np.log(c[0]))
            pos = {t: i for i, t in enumerate(idx)}
            for k in range(1, len(times)):
                rp = rates[k - 1]
                if abs(rp) >= 0.0005 and times[k] in pos:
                    i = pos[times[k]]
                    lr[i - 6:i] += -np.sign(rp) * 0.0012   # −0.72% en contra en los 30 min previos
            c = 100 * np.exp(np.cumsum(lr))
        frames[f"S{s}"] = frame(idx, c)
        events[f"S{s}"] = pd.DataFrame(dict(time=times, rate=rates))
    return frames, events


def b_data(planted, n_sym=15, days=45):
    bx, bn = {}, {}
    for s in range(n_sym):
        idx, c, rng = five_min(days, 300 + s, vol=0.003)
        if planted:   # estirones de BingX que se deshacen solos en minutos
            spike = np.zeros(len(c))
            for i in rng.choice(len(c), size=len(c) // 150, replace=False):
                spike[i] += rng.choice([-1, 1]) * rng.uniform(0.006, 0.015)
            prem = np.zeros(len(c))
            for i in range(1, len(c)):
                prem[i] = 0.45 * prem[i - 1] + spike[i]
        else:         # prima que vaga sin volver (paseo aleatorio): no hay nada que cobrar
            prem = np.cumsum(rng.standard_normal(len(c)) * 0.0012)
        bn[f"S{s}"] = frame(idx, c)
        bx[f"S{s}"] = frame(idx, c * (1 + prem))
    return bx, bn


def c_data(planted, n_sym=10, days=150, seed=900):
    """Días de pump (+~11%/día) con mechas al azar. Plantado: tras un nuevo máximo de 25 velas
    con mecha superior ≥ 55% en día extremo, el precio cae ~1.5% en 30 min (y lo simétrico)."""
    frames = {}
    for s in range(n_sym + 1):
        rng = np.random.default_rng(seed + s)
        n = days * 288
        idx = pd.date_range("2025-06-01", periods=n, freq="5min", tz="UTC")
        o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n); v = np.empty(n)
        px, pending, dayopen, hist_h, hist_l = 50.0, 0.0, 50.0, [], []
        drift = 0.0
        for i in range(n):
            if i % 288 == 0:
                dayopen = px
                drift = rng.choice([0.0, 0.00035, -0.00035], p=[0.6, 0.2, 0.2]) if s else 0.0
            op = px
            r = rng.standard_normal() * 0.003 + drift + pending
            pending *= 0.7
            cl = op * np.exp(r)
            uw, lw = rng.exponential(0.0015) * op, rng.exponential(0.0015) * op
            hi, lo_ = max(op, cl) + uw, min(op, cl) - lw
            o[i], h[i], l[i], c[i] = op, hi, lo_, cl
            rngb = hi - lo_
            upf = (hi - max(op, cl)) / rngb if rngb > 0 else 0
            lof = (min(op, cl) - lo_) / rngb if rngb > 0 else 0
            v[i] = rng.lognormal(0, 0.3) * (3.0 if max(upf, lof) >= 0.55 else 1.0)
            pct = cl / dayopen - 1
            hist_h.append(hi); hist_l.append(lo_)
            if planted and s and i > 30:
                if pct >= 0.09 and hi >= max(hist_h[-25:]) and upf >= 0.55:
                    pending = -0.006
                elif pct <= -0.09 and lo_ <= min(hist_l[-25:]) and lof >= 0.55:
                    pending = 0.006
            px = cl
        name = "BTC" if s == 0 else f"P{s}"
        frames[name] = pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)
    return frames, frames["BTC"]


def run(planted):
    r = lab.main(["--out", f"out_test_{'plantado' if planted else 'ruido'}"],
                 a_override=a_data(planted), b_override=b_data(planted), c_override=c_data(planted))
    return r["verdicts"]


if __name__ == "__main__":
    v0 = run(False)
    print("RUIDO:", v0)
    assert all("APORTA:" not in x or "NO APORTA" in x for x in v0), v0
    v1 = run(True)
    print("PLANTADO:", v1)
    assert all("✅ APORTA" in x for x in v1), v1
    assert "la mecha SÍ aporta" in v1[2], v1[2]
    assert "la mecha SÍ aporta" not in v0[2], v0[2]
    print("LABORATORIO OK")
