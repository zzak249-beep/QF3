"""
Motor Z-LAB v2 (5m) — calculo de indicadores y senales identico al script de TradingView
"KIBITO VWAP Z-LAB v2 5m" con valores por defecto. Lo usan el bot en vivo y el backtest.
"""
import math
from dataclasses import dataclass, field

P = dict(
    fadeZ=2.0, extZ=3.0, horizon=24, regLen=48, minSample=10,
    minProb=55.0, minTgtPct=0.30,
    riskPct=0.5, maxLev=2.0, maxStopPct=5.0, minRR=1.0, cooldown=6,
    fadeMinZ=2.5, htfLen=50, dayMovePct=8.0, maxDay=3, lossStreak=3, pauseBars=144,
    maxDDDay=2.5, fee=0.0005, sigGap=6,
)


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float
    tb: float | None = None      # volumen comprador agresivo (taker buy base)


def parse_kline(k):
    if isinstance(k, dict):
        tb = k.get("takerBuyBaseVolume")
        return Bar(int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]),
                   float(k["close"]), float(k.get("volume", 0)), float(tb) if tb not in (None, "") else None)
    tb = float(k[9]) if len(k) > 9 and k[9] not in (None, "") else None
    return Bar(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), tb)


def _ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for x in vals:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


class Features:
    """Indicadores por vela, todos causales (solo usan datos hasta esa vela)."""

    def __init__(self, bars, p=P):
        self.bars, self.p = bars, p
        n = self.n = len(bars)
        # ATR (RMA 14)
        self.atr, a = [], None
        for i, b in enumerate(bars):
            tr = b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c))
            a = tr if a is None else (a * 13 + tr) / 14
            self.atr.append(a)
        # VWAP diario UTC + desviacion ponderada por volumen
        self.vw, self.sd, self.day_open = [0.0] * n, [0.0] * n, [0.0] * n
        cur, sv, svp, svp2, dopen = None, 0.0, 0.0, 0.0, 0.0
        for i, b in enumerate(bars):
            d = b.t // 86_400_000
            if d != cur:
                cur, sv, svp, svp2, dopen = d, 0.0, 0.0, 0.0, b.o
            src = (b.h + b.l + b.c) / 3
            v = max(b.v, 1e-12)
            sv += v
            svp += v * src
            svp2 += v * src * src
            m = svp / sv
            self.vw[i] = m
            self.sd[i] = math.sqrt(max(svp2 / sv - m * m, 0.0))
            self.day_open[i] = dopen
        # tendencia 1h sin repintar (ultima hora cerrada)
        hc = {}
        for b in bars:
            hc[b.t // 3_600_000] = b.c
        hours = sorted(hc)
        he = dict(zip(hours, _ema([hc[h] for h in hours], p["htfLen"])))
        self.htf_up, self.htf_dn = [False] * n, [False] * n
        for i, b in enumerate(bars):
            ph = b.t // 3_600_000 - 1
            if ph in he:
                self.htf_up[i] = hc[ph] > he[ph]
                self.htf_dn[i] = hc[ph] < he[ph]
        self.hour_close = [hc[h] for h in hours]
        # regimen
        self.above = [1 if bars[i].c > self.vw[i] else 0 for i in range(n)]
        self.cross = [0] + [1 if (bars[i].c - self.vw[i]) * (bars[i - 1].c - self.vw[i - 1]) < 0 else 0
                            for i in range(1, n)]
        self.tr_up, self.tr_dn, self.is_rot = [False] * n, [False] * n, [True] * n
        L = p["regLen"]
        sa = sc = 0
        for i in range(n):
            sa += self.above[i]
            sc += self.cross[i]
            if i >= L:
                sa -= self.above[i - L]
                sc -= self.cross[i - L]
            if i >= L - 1:
                side_r = sa / L
                self.tr_up[i] = side_r >= 0.8 and sc <= 2
                self.tr_dn[i] = side_r <= 0.2 and sc <= 2
                self.is_rot[i] = not self.tr_up[i] and not self.tr_dn[i]
        # estadistica de toques +-fadeZ en rango: n_touch/n_rev acumulados hasta i
        self.rot_touch, self.rot_rev = [0] * n, [0] * n
        ev, nt, nr = [], [0, 0], [0, 0]
        for i in range(1, n):
            b = bars[i]
            keep = []
            for (eb, ed, er) in ev:
                ext = b.h >= self.vw[i] + p["extZ"] * self.sd[i] if ed == 1 else b.l <= self.vw[i] - p["extZ"] * self.sd[i]
                rev = b.l <= self.vw[i] if ed == 1 else b.h >= self.vw[i]
                if ext or rev or i - eb >= p["horizon"]:
                    nt[er] += 1
                    if rev and not ext:
                        nr[er] += 1
                else:
                    keep.append((eb, ed, er))
            ev = keep
            if self.sd[i] > 0:
                reg = 0 if self.is_rot[i - 1] else 1
                if self.zv(i, b.h) >= p["fadeZ"] and self.zv(i - 1, bars[i - 1].h) < p["fadeZ"]:
                    ev.append((i, 1, reg))
                if self.zv(i, b.l) <= -p["fadeZ"] and self.zv(i - 1, bars[i - 1].l) > -p["fadeZ"]:
                    ev.append((i, -1, reg))
            self.rot_touch[i], self.rot_rev[i] = nt[0], nr[0]

    def zv(self, i, price):
        return (price - self.vw[i]) / self.sd[i] if self.sd[i] > 0 else 0.0

    def delta(self, i):
        b = self.bars[i]
        if b.tb is not None:
            return 2 * b.tb - b.v
        return b.v if b.c > b.o else (-b.v if b.c < b.o else 0.0)

    def rev_prob(self, i):
        return self.rot_rev[i] / self.rot_touch[i] * 100 if self.rot_touch[i] else 0.0

    def regime(self, i):
        return "TEND↑" if self.tr_up[i] else "TEND↓" if self.tr_dn[i] else "RANGO"


def raw_signal(F: Features, i, fail_l=0, fail_s=0, p=P):
    """Senal Z-LAB v2 en la vela cerrada i (sin gating de posicion/pausa/limites).
    Devuelve dict(side, type, stop, tgt, dist, rr) o None."""
    bars = F.bars
    if i < 3 or F.sd[i] <= 0:
        return None
    b, b1 = bars[i], bars[i - 1]
    vw, sd = F.vw[i], F.sd[i]
    z, zHi, zLo = F.zv(i, b.c), F.zv(i, b.h), F.zv(i, b.l)
    zHi1, zLo1 = F.zv(i - 1, b1.h), F.zv(i - 1, b1.l)
    day_move = (b.c - F.day_open[i]) / F.day_open[i] * 100
    pump = abs(day_move) >= p["dayMovePct"]
    zhi3 = max(F.zv(k, bars[k].h) for k in range(i - 2, i + 1))
    zlo3 = min(F.zv(k, bars[k].l) for k in range(i - 2, i + 1))
    dlt = F.delta(i)
    u2, l2 = vw + p["fadeZ"] * sd, vw - p["fadeZ"] * sd
    calib = F.rot_touch[i] >= p["minSample"] and F.rev_prob(i) >= p["minProb"]
    ok_fs = fail_s < 2 and not pump and not F.htf_up[i] and zhi3 >= p["fadeMinZ"]
    ok_fl = fail_l < 2 and not pump and not F.htf_dn[i] and zlo3 <= -p["fadeMinZ"]
    rot, tu, td = F.is_rot[i], F.tr_up[i], F.tr_dn[i]

    fade_s = rot and ok_fs and zHi1 >= p["fadeZ"] and b.c < u2 and b.c < b.o and dlt < 0 and calib \
        and (b.c - vw) / b.c * 100 >= p["minTgtPct"]
    fade_l = rot and ok_fl and zLo1 <= -p["fadeZ"] and b.c > l2 and b.c > b.o and dlt > 0 and calib \
        and (vw - b.c) / b.c * 100 >= p["minTgtPct"]
    trend_l = tu and F.htf_up[i] and zLo <= 0.5 and z > 0.5 and b.c > b.o and dlt > 0 \
        and ((vw + 2 * sd) - b.c) / b.c * 100 >= p["minTgtPct"]
    trend_s = td and F.htf_dn[i] and zHi >= -0.5 and z < -0.5 and b.c < b.o and dlt < 0 \
        and (b.c - (vw - 2 * sd)) / b.c * 100 >= p["minTgtPct"]

    if fade_l:
        side, typ = 1, "fade"
    elif fade_s:
        side, typ = -1, "fade"
    elif trend_l:
        side, typ = 1, "trend"
    elif trend_s:
        side, typ = -1, "trend"
    else:
        return None
    a = F.atr[i]
    stop = (min(b.l, b1.l) - a * 0.3) if side == 1 else (max(b.h, b1.h) + a * 0.3)
    dist = abs(b.c - stop)
    tgt = vw if typ == "fade" else (vw + 2 * sd if side == 1 else vw - 2 * sd)
    rr = abs(tgt - b.c) / dist if dist > 0 else 0.0
    valid = dist > 0 and dist / b.c * 100 <= p["maxStopPct"] and rr >= p["minRR"]
    return dict(side=side, type=typ, stop=stop, tgt=tgt, dist=dist, rr=rr, valid=valid,
                z=z, regime=F.regime(i), rev_prob=F.rev_prob(i))


# ───────────────────────── backtest (misma logica) ─────────────────────────
@dataclass
class Result:
    symbol: str
    trades: int = 0
    wins: int = 0
    gp: float = 0.0
    gl: float = 0.0
    pnl_pct: float = 0.0
    max_dd_pct: float = 0.0
    atr_pct: float = 0.0
    eff_ratio: float = 0.0
    rev_prob: float | None = None
    rev_sample: int = 0
    last_regime: str = ""
    days: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def pf(self):
        return self.gp / self.gl if self.gl > 0 else (float("inf") if self.gp > 0 else 0.0)

    @property
    def winrate(self):
        return self.wins / self.trades * 100 if self.trades else 0.0


def backtest(symbol, bars, p=P):
    res = Result(symbol)
    n = len(bars)
    if n < 600:
        res.notes.append("pocos datos")
        return res
    res.days = n * 5 / 1440
    F = Features(bars, p)
    equity = peak = 10_000.0
    max_dd = 0.0
    pos = pending = None
    last_sig, last_exit = -100, -1000
    trades_day, day_id = 0, None
    fail_l = fail_s = 0
    streak, pause_until = 0, -1
    day_start_eq, day_block = equity, False

    def close_trade(price, i):
        nonlocal equity, peak, max_dd, pos, last_exit, streak, pause_until, fail_l, fail_s
        side = pos["side"]
        pnl = (price - pos["entry"]) * pos["qty"] * side - (pos["entry"] + price) * pos["qty"] * p["fee"]
        equity += pnl
        res.trades += 1
        if pnl > 0:
            res.wins += 1
            res.gp += pnl
            streak = 0
        else:
            res.gl -= pnl
            streak += 1
            if streak >= p["lossStreak"]:
                pause_until, streak = i + p["pauseBars"], 0
        if pos["type"] == "fade":
            if side == 1:
                fail_l = fail_l + 1 if pnl <= 0 else 0
            else:
                fail_s = fail_s + 1 if pnl <= 0 else 0
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        last_exit, pos = i, None

    for i in range(1, n):
        b = bars[i]
        d = b.t // 86_400_000
        if d != day_id:
            day_id, trades_day, fail_l, fail_s = d, 0, 0, 0
            day_start_eq, day_block = equity, False
        if pending and pos is None:
            entry = b.o
            dist = abs(entry - pending["stop"])
            if dist > 0 and ((pending["side"] == 1 and pending["stop"] < entry) or (pending["side"] == -1 and pending["stop"] > entry)):
                qty = min(equity * p["riskPct"] / 100 / dist, equity * p["maxLev"] / entry)
                pos = dict(side=pending["side"], qty=qty, entry=entry, stop=pending["stop"],
                           tgt=pending["tgt"], type=pending["type"], bar=i)
                trades_day += 1
            pending = None
        if pos is not None:
            tgt = F.vw[i] if pos["type"] == "fade" else pos["tgt"]
            if pos["side"] == 1:
                if b.l <= pos["stop"]:
                    close_trade(min(pos["stop"], b.o), i)
                elif b.h >= tgt:
                    close_trade(max(tgt, b.o), i)
            else:
                if b.h >= pos["stop"]:
                    close_trade(max(pos["stop"], b.o), i)
                elif b.l <= tgt:
                    close_trade(min(tgt, b.o), i)
            if pos is not None and i - pos["bar"] >= p["horizon"] * 2:
                close_trade(b.c, i)
        if (day_start_eq - equity) / day_start_eq * 100 >= p["maxDDDay"]:
            day_block = True
        if pos is not None or pending is not None:
            continue
        s = raw_signal(F, i, fail_l, fail_s, p)
        if s is None or not (i - last_sig > p["sigGap"]):
            continue
        last_sig = i
        can = (i - last_exit > p["cooldown"]) and i > pause_until and trades_day < p["maxDay"] and not day_block
        if can and s["valid"]:
            pending = dict(side=s["side"], stop=s["stop"], tgt=s["tgt"], type=s["type"])
    if pos is not None:
        close_trade(bars[-1].c, n - 1)

    res.pnl_pct = (equity - 10_000) / 100
    res.max_dd_pct = max_dd
    res.rev_prob = F.rev_prob(n - 1) if F.rot_touch[n - 1] else None
    res.rev_sample = F.rot_touch[n - 1]
    last = bars[-288:]
    res.atr_pct = sum(F.atr[-288:]) / len(last) / (sum(x.c for x in last) / len(last)) * 100
    hc = F.hour_close[-48:]
    if len(hc) > 2:
        path = sum(abs(hc[k] - hc[k - 1]) for k in range(1, len(hc)))
        res.eff_ratio = abs(hc[-1] - hc[0]) / path if path > 0 else 0.0
    res.last_regime = F.regime(n - 1)
    return res
