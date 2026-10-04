"""
Zonas de oferta/demanda (base → impulso) — port de "MTF S/D Zones v3" (Pine v6) para usarlas como
CONTEXTO DE UBICACIÓN de las señales Wyckoff, no como estrategia aparte.

Por cada señal se mide:
  zone_align  "a favor" si hay una zona del lado de la operación (demanda en largos, oferta en cortos)
              que solapa el tramo de riesgo [SL, entrada]: el Spring/Test se apoyó en una zona.
  zone_touch  cuántas veces se había visitado esa zona (el script opera solo la fresca; el estudio
              arXiv 2101.07410 encuentra lo contrario: más rebotes previos → más probable el siguiente)
  obst_r      distancia en R hasta la zona OPUESTA más cercana en la dirección de la operación
              (una oferta fresca a 0.5R de un largo es un muro antes del TP1)

Fidelidad con el Pine:
  · detección en el TF de la zona con impulso en [1], base en [2..], confirmación en [0]
  · disponible desde la primera vela del gráfico que ABRE después del cierre de la vela de confirmación
    (equivale a lookahead_on + [1]: sin repintado)
  · invalidación por cierre, eliminación en el toque nº KILL_TOUCH, máx. MAX_ZONES por TF y lado
  · el "delta" es la APROXIMACIÓN del script (posición del cierre en la vela), no delta real
"""
import math

NaN = float("nan")

# parámetros por defecto del script
ATR_LEN, IMPULSE_MULT, BODY_PCT = 14, 1.5, 0.6
BASE_MULT, BASE_BODY_PCT, MAX_BASE_BARS, MAX_BASE_RANGE, MIN_DEP_RATIO = 0.8, 0.7, 5, 1.5, 2.0
VOL_LEN, MIN_IMP_RVOL, MIN_IMP_BASE, MIN_DELTA = 20, 1.3, 1.5, 15
SWING_LEN = 5
MAX_ZONES, KILL_TOUCH, MIN_SCORE, MAX_AGE = 4, 3, 55, 300


def _na(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


class SDDetector:
    """Detector de zonas en UN timeframe. update() con velas CERRADAS en orden."""

    def __init__(self, tf_ms, tfi=0, tick=1e-8, keep=400):
        self.tf_ms, self.tfi, self.tick, self.keep = tf_ms, tfi, tick, keep
        self.o, self.h, self.l, self.c, self.v, self.t = [], [], [], [], [], []
        self.atr, self.vma, self.lastH, self.lastL = [], [], [], []
        self._seed = []

    def _trim(self):
        if len(self.c) > self.keep + 50:
            cut = len(self.c) - self.keep
            for a in (self.o, self.h, self.l, self.c, self.v, self.t, self.atr, self.vma, self.lastH, self.lastL):
                del a[:cut]

    def update(self, t, o, h, l, c, v):
        """Devuelve una zona {dir, top, bot, score, avail_t, ...} si la vela recién cerrada la confirma."""
        self.t.append(t)
        self.o.append(o)
        self.h.append(h)
        self.l.append(l)
        self.c.append(c)
        self.v.append(max(v or 0.0, 0.0))
        n = len(self.c)
        # ATR (RMA con semilla SMA, como ta.atr)
        tr = h - l if n == 1 else max(h - l, abs(h - self.c[-2]), abs(l - self.c[-2]))
        prev = self.atr[-1] if self.atr else NaN
        if _na(prev):
            self._seed.append(tr)
            a = sum(self._seed[-ATR_LEN:]) / ATR_LEN if len(self._seed) >= ATR_LEN else NaN
        else:
            a = (prev * (ATR_LEN - 1) + tr) / ATR_LEN
        self.atr.append(a)
        self.vma.append(sum(self.v[-VOL_LEN:]) / VOL_LEN if n >= VOL_LEN else NaN)
        # último pivote confirmado (ta.pivothigh/low(5,5); empates como TradingView)
        lh = self.lastH[-1] if self.lastH else NaN
        ll = self.lastL[-1] if self.lastL else NaN
        L = SWING_LEN
        if n >= 2 * L + 1:
            ci = n - 1 - L
            ch, cl = self.h[ci], self.l[ci]
            if all(not (self.h[ci - k] > ch) for k in range(1, L + 1)) and all(self.h[ci + k] < ch for k in range(1, L + 1)):
                lh = ch
            if all(not (self.l[ci - k] < cl) for k in range(1, L + 1)) and all(self.l[ci + k] > cl for k in range(1, L + 1)):
                ll = cl
        self.lastH.append(lh)
        self.lastL.append(ll)
        z = self._detect()
        self._trim()
        return z

    def _detect(self):
        n = len(self.c)
        if n < MAX_BASE_BARS + 3:
            return None
        H, Lo, O, C, V = self.h, self.l, self.o, self.c, self.v
        i1 = n - 2  # impulso
        a1 = self.atr[i1]
        if _na(a1) or a1 <= 0:
            return None
        rng1 = H[i1] - Lo[i1]
        bull = C[i1] > O[i1] and rng1 >= a1 * IMPULSE_MULT and (C[i1] - O[i1]) >= rng1 * BODY_PCT
        bear = C[i1] < O[i1] and rng1 >= a1 * IMPULSE_MULT and (O[i1] - C[i1]) >= rng1 * BODY_PCT
        if not (bull or bear):
            return None
        bh = bl = None
        bvol, nb = 0.0, 0
        for k in range(2, MAX_BASE_BARS + 2):
            j = n - 1 - k
            if j < 0:
                break
            r = H[j] - Lo[j]
            if r > a1 * BASE_MULT or abs(C[j] - O[j]) > r * BASE_BODY_PCT:
                break
            bh = H[j] if bh is None else max(bh, H[j])
            bl = Lo[j] if bl is None else min(bl, Lo[j])
            bvol += V[j]
            nb += 1
        if nb == 0:
            return None
        hgt = max(bh - bl, self.tick)
        if hgt > a1 * MAX_BASE_RANGE:
            return None
        i0 = n - 1
        dep = (max(H[i1], H[i0]) - bh) if bull else (bl - min(Lo[i1], Lo[i0]))
        mid1 = (O[i1] + C[i1]) / 2
        hold = C[i0] > mid1 if bull else C[i0] < mid1
        ratio = dep / hgt
        vma2 = self.vma[n - 3]
        has_vol = V[i1] > 0 and not _na(vma2) and vma2 > 0
        rvol = V[i1] / vma2 if has_vol else NaN
        base_av = bvol / nb
        imp_vs_b = V[i1] / base_av if (has_vol and base_av > 0) else NaN

        def buy(hh, lo_, cc, vv):
            return vv * (cc - lo_) / (hh - lo_) if hh > lo_ else vv * 0.5

        vtot = V[i1] + V[i0]
        bvt = buy(H[i1], Lo[i1], C[i1], V[i1]) + buy(H[i0], Lo[i0], C[i0], V[i0])
        dlt = (2 * bvt - vtot) / vtot * 100 if vtot > 0 else NaN
        ddl = NaN if _na(dlt) else (dlt if bull else -dlt)
        vol_ok = ((_na(rvol) or rvol >= MIN_IMP_RVOL) and (_na(imp_vs_b) or imp_vs_b >= MIN_IMP_BASE)
                  and (_na(ddl) or ddl >= MIN_DELTA))
        if not (hold and ratio >= MIN_DEP_RATIO and vol_ok):
            return None
        i2 = n - 3
        fvg = Lo[i0] > H[i2] if bull else H[i0] < Lo[i2]
        lh2, ll2 = self.lastH[i2], self.lastL[i2]
        bos = (not _na(lh2) and max(H[i1], H[i0]) > lh2) if bull else (not _na(ll2) and min(Lo[i1], Lo[i0]) < ll2)
        dep_s = min(ratio / 5.0, 1.0) * 30.0
        imp_s = min(rng1 / a1 / 3.0, 1.0) * 15.0
        base_s = 10.0 if nb <= 2 else 7.0 if nb <= 4 else 3.0
        vol_s = 12.0 if _na(rvol) else min(rvol / 3.0, 1.0) * 15.0 + max(min((0 if _na(ddl) else ddl) / 60.0, 1.0), 0.0) * 10.0
        sc = dep_s + imp_s + base_s + vol_s + (10.0 if fvg else 0.0) + (10.0 if bos else 0.0)
        return {"dir": 1 if bull else -1, "top": bh, "bot": bl, "origin": self.t[n - 1 - (nb + 1)],
                "score": int(round(min(100.0, sc + self.tfi * 5.0))), "tfi": self.tfi,
                "avail_t": self.t[i0] + self.tf_ms, "fvg": fvg, "bos": bos}


class ZoneTracker:
    """Zonas de un símbolo vistas desde el TF del gráfico (el de las señales Wyckoff), con zonas propias (tfi 0)
    y del TF de contexto (tfi 1). Ciclo: feed_htf() con velas superiores, feed_chart() con cada vela del gráfico."""

    def __init__(self, chart_ms, htf_ms=None, tick=1e-8):
        self.chart = SDDetector(chart_ms, 0, tick)
        self.htf = SDDetector(htf_ms, 1, tick) if htf_ms else None
        self.zones, self.pending, self.bar = [], [], -1
        self.last_origin = {}

    def feed_htf(self, t, o, h, l, c, v):
        if self.htf is None or (v <= 0 and h == l):
            return
        z = self.htf.update(t, o, h, l, c, v)
        if z:
            self.pending.append(z)

    def feed_chart(self, t, o, h, l, c, v):
        """Procesa una vela CERRADA del gráfico: añade zonas disponibles, aplica toques e invalidaciones,
        y detecta zonas propias (que quedan pendientes hasta la vela siguiente, como en el Pine)."""
        if v <= 0 and h == l:
            return
        self.bar += 1
        still = []
        for z in sorted(self.pending, key=lambda x: x["avail_t"]):
            if z["avail_t"] <= t:
                self._add(z, c)
            else:
                still.append(z)
        self.pending = still
        keep = []
        for z in self.zones:
            if self.bar <= z["born"]:
                keep.append(z)
                continue
            broken = c < z["bot"] if z["dir"] == 1 else c > z["top"]
            if broken:
                continue
            inside = l <= z["top"] if z["dir"] == 1 else h >= z["bot"]
            if inside and not z["inside"]:
                z["touches"] += 1
                if KILL_TOUCH > 0 and z["touches"] >= KILL_TOUCH:
                    continue
            z["inside"] = inside
            keep.append(z)
        self.zones = keep
        z = self.chart.update(t, o, h, l, c, v)
        if z:
            self.pending.append(z)

    def _add(self, z, close):
        key = (z["tfi"], z["dir"])
        if self.last_origin.get(key) == z["origin"]:
            return
        self.last_origin[key] = z["origin"]
        if (z["dir"] == 1 and close < z["bot"]) or (z["dir"] == -1 and close > z["top"]):
            return
        same = [x for x in self.zones if x["tfi"] == z["tfi"] and x["dir"] == z["dir"]]
        if len(same) >= MAX_ZONES:
            self.zones.remove(same[0])
        self.zones.append(dict(z, born=self.bar, touches=0, inside=False))

    def features(self, side, entry, sl):
        """Rasgos de ubicación de una señal: zona a favor bajo el riesgo y obstáculo opuesto en R."""
        d = 1 if side == "LONG" else -1
        risk = abs(entry - sl)
        lo, hi = min(entry, sl), max(entry, sl)
        best = None
        for z in self.zones:
            if z["dir"] != d or z["score"] < MIN_SCORE or self.bar - z["born"] > MAX_AGE:
                continue
            if z["bot"] <= hi and z["top"] >= lo:
                if best is None or z["score"] > best["score"]:
                    best = z
        obst = None
        for z in self.zones:
            if z["dir"] != -d:
                continue
            lvl = z["bot"] if d == 1 else z["top"]
            dist = (lvl - entry) * d
            if dist > 0 and (obst is None or dist < obst):
                obst = dist
        return {
            "zone_align": "a favor" if best else "sin zona",
            "zone_touch": best["touches"] if best else None,
            "zone_score": best["score"] if best else None,
            "zone_tf": ("gráfico" if best["tfi"] == 0 else "superior") if best else None,
            "obst_r": round(obst / risk, 2) if (obst is not None and risk > 0) else None,
        }


def obst_label(x):
    if x is None:
        return "ninguna"
    return "<1R" if x < 1 else "1-2R" if x < 2 else "2R+"


def replay(chart_rows, htf_rows, chart_ms, htf_ms, tick):
    """Reconstruye un ZoneTracker con historia en orden cronológico (calentamiento del bot)."""
    zt = ZoneTracker(chart_ms, htf_ms if htf_rows else None, tick)
    k = 0
    for r in chart_rows:
        close_t = r[0] + chart_ms
        while htf_rows and k < len(htf_rows) and htf_rows[k][0] + htf_ms <= close_t:
            zt.feed_htf(*htf_rows[k][:6])
            k += 1
        zt.feed_chart(*r[:6])
    return zt
