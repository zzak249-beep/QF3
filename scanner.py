"""
B · Escáner en vivo de la prima BingX / Binance — SOLO SEÑALES Y REGISTRO (no opera).

Misma regla que el laboratorio:
  prima = precio medio BingX / precio medio Binance − 1
  desviación = prima − mediana de las últimas 24 h (muestras cada 5 min)
  |desviación| ≥ 0.3%  →  señal en BingX en contra de la desviación
  (BingX caro → corto · BingX barato → largo)

Lo que el laboratorio no podía saber y esto mide:
  · entrada y salida al precio EJECUTABLE del libro de BingX (bid/ask real, con el spread)
  · dos salidas por señal: "converge" (|desv| ≤ la mitad del umbral, máx 30 min) y "15 min fijos"
  · placebo en vivo: entradas al azar en momentos tranquilos, para comparar
Cada día manda a Telegram el acumulado con su t y el CSV.
"""
from __future__ import annotations

import csv
import math
import os
import random
import statistics
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone

import requests


def env(k, d):
    v = os.getenv(k)
    return v.strip().strip('"').strip("'") if v and v.strip() else d


DEFAULT_SYMBOLS = ("SOL,XRP,DOGE,ADA,AVAX,LINK,SUI,LTC,AAVE,NEAR,ENA,WIF,ARB,OP,TIA,INJ,SEI,APT,FIL,"
                   "TAO,ZEC,LDO,CRV,ORDI,WLD,ONDO,JUP,STX,DYDX,GALA,SAND,AXS,IMX,FET,KAS,HBAR,ICP,ETC,UNI")
SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
POLL = float(env("POLL_SEC", "5"))
THR = float(env("THRESHOLD_PCT", "0.3")) / 100
MAX_MIN = float(env("MAX_MIN", "30"))
FIX_MIN = 15.0
FEE_RT = float(env("FEE_RT_PCT", "0.10")) / 100          # comisiones taker ida y vuelta (el spread ya va en los precios)
COOLDOWN_MIN = float(env("COOLDOWN_MIN", "10"))
PLACEBO_MIN = float(env("PLACEBO_MIN", "20"))
REPORT_HOUR_UTC = int(env("REPORT_HOUR_UTC", "19"))
NOTIFY_EACH = env("NOTIFY_EACH", "1") == "1"
STATE_DIR = env("STATE_DIR", "data")
BINANCE = env("BINANCE_URL", "https://fapi.binance.com")
BINGX = "https://open-api.bingx.com"
BASE_LEN, BASE_MIN = 288, 60
SIG_FILE, PLA_FILE = "senales.csv", "placebo.csv"

SIG_COLS = ["sym", "t_open", "dir", "dev_pb", "spread_pb", "entry",
            "exit_conv", "min_conv", "net_conv_pct", "mid_conv_pct", "bx_part_pb", "bn_part_pb",
            "exit_15", "net_15_pct"]
PLA_COLS = ["sym", "t_open", "dir", "entry", "exit_15", "net_15_pct"]


class RegionBlocked(Exception):
    pass


def bx_sym(s):
    return f"{s}-USDT"


def bn_sym(s):
    return f"{s}USDT"


def _f(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


# ───────────────────────── exchanges (sustituibles en el test) ─────────────────────────
class Feeds:
    def __init__(self):
        self.s = requests.Session()

    def _get(self, url, params=None):
        r = self.s.get(url, params=params, timeout=10)
        if r.status_code in (451, 403) and "binance" in url:
            raise RegionBlocked(f"Binance responde {r.status_code}: la región del servidor está bloqueada")
        r.raise_for_status()
        return r.json()

    def binance_book(self):
        return {d["symbol"]: (float(d["bidPrice"]), float(d["askPrice"]))
                for d in self._get(BINANCE + "/fapi/v1/ticker/bookTicker")}

    def bingx_book(self):
        js = self._get(BINGX + "/openApi/swap/v2/quote/ticker")
        out = {}
        for d in js.get("data") or []:
            bid, ask = _f(d, "bidPrice", "bid_price"), _f(d, "askPrice", "ask_price")
            last = _f(d, "lastPrice", "last_price")
            if bid <= 0 or ask <= 0 or ask < bid:
                bid = ask = last
            if bid > 0:
                out[d.get("symbol", "")] = (bid, ask)
        return out

    def bingx_exact(self, sym):
        """bid/ask exactos del libro de BingX para un símbolo (entradas y salidas)."""
        js = self._get(BINGX + "/openApi/swap/v2/quote/bookTicker", {"symbol": sym})
        d = js.get("data") or {}
        d = d.get("book_ticker", d) if isinstance(d, dict) else (d[0] if d else {})
        bid, ask = _f(d, "bid_price", "bidPrice"), _f(d, "ask_price", "askPrice")
        if bid <= 0 or ask <= 0:
            raise ValueError(f"libro vacío {sym}")
        return bid, ask

    def binance_closes(self, sym):
        js = self._get(BINANCE + "/fapi/v1/klines", dict(symbol=sym, interval="5m", limit=300))
        return {int(k[0]): float(k[4]) for k in js[:-1]}

    def bingx_closes(self, sym):
        js = self._get(BINGX + "/openApi/swap/v3/quote/klines", dict(symbol=sym, interval="5m", limit=300))
        rows = js.get("data") or []
        out = {int(d["time"]): float(d["close"]) for d in rows}
        if out:
            out.pop(max(out))   # la vela en curso no está cerrada
        return out


# ───────────────────────── Telegram ─────────────────────────
def telegram(text, file=None):
    tok = env("TELEGRAM_BOT_TOKEN", "") or env("TELEGRAM_TOKEN", "")
    chat = env("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data=dict(chat_id=chat, text=text[:4000]), timeout=15)
        if file and os.path.exists(file):
            with open(file, "rb") as fh:
                requests.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                              data=dict(chat_id=chat), files=dict(document=fh), timeout=60)
    except Exception as e:
        print("telegram:", e, flush=True)


# ───────────────────────── estadística ─────────────────────────
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def t_clustered(rows, key):
    """t de la media agrupando por bloque de 5 min: señales simultáneas no son independientes."""
    g = {}
    for r in rows:
        try:
            v = float(r[key])
        except (TypeError, ValueError, KeyError):
            continue
        b = int(float(r["t_open"]) // 300)
        g.setdefault(b, []).append(v)
    m = [sum(v) / len(v) for v in g.values()]
    if len(m) < 5:
        return float("nan")
    sd = statistics.stdev(m)
    return statistics.mean(m) / (sd / math.sqrt(len(m))) if sd > 0 else float("nan")


def stats(rows, key):
    vals = []
    for r in rows:
        try:
            vals.append(float(r[key]))
        except (TypeError, ValueError, KeyError):
            pass
    if not vals:
        return dict(n=0, mean=float("nan"), win=float("nan"), t=float("nan"))
    return dict(n=len(vals), mean=statistics.mean(vals), win=100 * sum(v > 0 for v in vals) / len(vals),
                t=t_clustered(rows, key))


def report(state_dir, days_running):
    sig = read_csv(os.path.join(state_dir, SIG_FILE))
    pla = read_csv(os.path.join(state_dir, PLA_FILE))
    c, f, p = stats(sig, "net_conv_pct"), stats(sig, "net_15_pct"), stats(pla, "net_15_pct")
    best = max((x for x in (c, f) if x["n"]), key=lambda x: (x["t"] if not math.isnan(x["t"]) else -9), default=None)
    if not best or best["n"] < 30:
        verd = f"⏳ ACUMULANDO: {best['n'] if best else 0} señales (hacen falta ≥ 30)"
    elif best["t"] >= 3 and best["mean"] > 0 and (p["n"] == 0 or best["mean"] > p["mean"]):
        verd = "✅ CONFIRMADA en vivo con precios ejecutables (t ≥ 3)"
    elif best["t"] >= 2 and best["mean"] > 0:
        verd = "🟡 INDICIO: positiva pero t < 3, seguir acumulando"
    else:
        verd = "❌ NO SE SOSTIENE con precios reales: descartar"
    fm = lambda x: "—" if math.isnan(x) else f"{x:+.3f}%"
    ft = lambda x: "—" if math.isnan(x) else f"{x:+.2f}"
    parts = [x for x in (r.get("bx_part_pb") for r in sig) if x not in (None, "")]
    bxp = statistics.mean(float(x) for x in parts) if parts else float("nan")
    bnp = statistics.mean(float(r["bn_part_pb"]) for r in sig if r.get("bn_part_pb")) if parts else float("nan")
    return (f"📡 ESCÁNER B · día {days_running}\n{verd}\n\n"
            f"Converge: {c['n']} · media neta {fm(c['mean'])} · acierto {c['win']:.0f}% · t {ft(c['t'])}\n"
            f"15 min:   {f['n']} · media neta {fm(f['mean'])} · acierto {f['win']:.0f}% · t {ft(f['t'])}\n"
            f"Placebo:  {p['n']} · media neta {fm(p['mean'])}\n"
            f"Vuelta de BingX / movimiento de Binance: {bxp:+.1f} / {bnp:+.1f} pb\n"
            f"(neto = precio ejecutable bid/ask con spread − {FEE_RT*100:.2f}% de comisiones)").replace("nan%", "—")


# ───────────────────────── escáner ─────────────────────────
class Scanner:
    def __init__(self, feeds, state_dir=STATE_DIR, rnd=None):
        self.f, self.dir = feeds, state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.rnd = rnd or random.Random()
        self.hist = {}          # sym → deque de primas (una por vela de 5m)
        self.bucket = None
        self.open = {}          # sym → señal abierta
        self.pla = {}           # sym → placebo abierto
        self.cool = {}
        self.next_placebo = 0.0
        self.syms = []
        self.last_dev = {}

    # arranque: 24 h de historia desde las velas de 5m de los dos exchanges
    def seed(self):
        bn, bx = self.f.binance_book(), self.f.bingx_book()
        for s in SYMBOLS:
            if bn_sym(s) not in bn or bx_sym(s) not in bx:
                print(f"  {s}: no cotiza en los dos exchanges, se omite", flush=True)
                continue
            dq = deque(maxlen=BASE_LEN)
            try:
                a, b = self.f.bingx_closes(bx_sym(s)), self.f.binance_closes(bn_sym(s))
                for t in sorted(set(a) & set(b)):
                    dq.append(a[t] / b[t] - 1)
            except RegionBlocked:
                raise
            except Exception as e:
                print(f"  {s}: sin historia ({str(e)[:60]}), arranca vacío", flush=True)
            self.hist[s] = dq
            self.syms.append(s)
            time.sleep(0.1)
        print(f"Vigilando {len(self.syms)} monedas · umbral {THR*100:.2f}% · sondeo {POLL:.0f}s", flush=True)

    def _exec(self, s, short, closing):
        """precio ejecutable en BingX: abrir corto/cerrar largo al bid; abrir largo/cerrar corto al ask."""
        bid, ask = self.f.bingx_exact(bx_sym(s))
        sell = short != closing
        return (bid if sell else ask), (ask - bid) / ((ask + bid) / 2)

    def _append(self, name, cols, row):
        path = os.path.join(self.dir, name)
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in cols})

    def step(self, now):
        bn, bx = self.f.binance_book(), self.f.bingx_book()
        bucket = int(now // 300)
        new_bar = bucket != self.bucket
        self.bucket = bucket
        calm = []
        for s in self.syms:
            if bn_sym(s) not in bn or bx_sym(s) not in bx:
                continue
            bnm = sum(bn[bn_sym(s)]) / 2
            bxm = sum(bx[bx_sym(s)]) / 2
            if bnm <= 0 or bxm <= 0:
                continue
            prem = bxm / bnm - 1
            h = self.hist[s]
            base = statistics.median(h) if len(h) >= BASE_MIN else None
            if new_bar:
                h.append(prem)
            if base is None:
                continue
            dev = prem - base
            self.last_dev[s] = dev
            try:
                if s in self.open:
                    self._manage(s, self.open[s], dev, bxm, bnm, now)
                elif abs(dev) >= THR and now >= self.cool.get(s, 0):
                    self._open(s, dev, bxm, bnm, now)
                elif abs(dev) < THR / 3 and s not in self.pla:
                    calm.append(s)
                if s in self.pla and now - self.pla[s]["t_open"] >= FIX_MIN * 60:
                    p = self.pla.pop(s)
                    px, _ = self._exec(s, p["short"], True)
                    d = -1 if p["short"] else 1
                    p.update(exit_15=px, net_15_pct=100 * (d * (px / p["entry"] - 1) - FEE_RT))
                    self._append(PLA_FILE, PLA_COLS, p)
            except RegionBlocked:
                raise
            except Exception as e:
                print(f"  {s}: {str(e)[:80]}", flush=True)
        if calm and now >= self.next_placebo:
            s = self.rnd.choice(calm)
            short = self.rnd.random() < 0.5
            try:
                px, _ = self._exec(s, short, False)
                self.pla[s] = dict(sym=s, t_open=now, dir="corto" if short else "largo", short=short, entry=px)
            except Exception as e:
                print(f"  placebo {s}: {str(e)[:60]}", flush=True)
            self.next_placebo = now + PLACEBO_MIN * 60

    def _open(self, s, dev, bxm, bnm, now):
        short = dev > 0
        px, spread = self._exec(s, short, False)
        sig = dict(sym=s, t_open=now, dir="corto" if short else "largo", short=short, dev_pb=dev * 1e4,
                   spread_pb=spread * 1e4, entry=px, bx0=bxm, bn0=bnm)
        self.open[s] = sig
        msg = (f"📡 B {s}: BingX {'CARO' if short else 'BARATO'} {dev*100:+.2f}% vs Binance → "
               f"{'CORTO' if short else 'LARGO'} (papel) a {px:g} · spread {spread*1e4:.1f} pb")
        print(msg, flush=True)
        if NOTIFY_EACH:
            telegram(msg)

    def _manage(self, s, g, dev, bxm, bnm, now):
        d = -1 if g["short"] else 1
        el = (now - g["t_open"]) / 60
        if "exit_15" not in g and el >= FIX_MIN:
            px, _ = self._exec(s, g["short"], True)
            g.update(exit_15=px, net_15_pct=100 * (d * (px / g["entry"] - 1) - FEE_RT))
        if "exit_conv" not in g and (abs(dev) <= THR / 2 or el >= MAX_MIN):
            px, _ = self._exec(s, g["short"], True)
            g.update(exit_conv=px, min_conv=round(el, 1),
                     net_conv_pct=100 * (d * (px / g["entry"] - 1) - FEE_RT),
                     mid_conv_pct=100 * (d * (bxm / g["bx0"] - 1) - FEE_RT),
                     bx_part_pb=1e4 * d * (bxm / g["bx0"] - 1),
                     bn_part_pb=-1e4 * d * (bnm / g["bn0"] - 1))
            msg = (f"{'✅' if g['net_conv_pct'] > 0 else '❌'} B {s} cerrada en {el:.0f} min: "
                   f"{g['net_conv_pct']:+.2f}% neto (ejecutable)")
            print(msg, flush=True)
            if NOTIFY_EACH:
                telegram(msg)
        if "exit_15" in g and "exit_conv" in g:
            self._append(SIG_FILE, SIG_COLS, g)
            del self.open[s]
            self.cool[s] = now + COOLDOWN_MIN * 60


def main():
    if "--report" in sys.argv:
        print(report(STATE_DIR, "?"))
        return
    feeds = Feeds()
    sc = Scanner(feeds)
    start_file = os.path.join(STATE_DIR, "inicio.txt")
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(start_file):
        with open(start_file, "w") as fh:
            fh.write(str(time.time()))
    t_start = float(open(start_file).read().strip() or time.time())
    while True:
        try:
            sc.seed()
            break
        except RegionBlocked as e:
            msg = (f"⛔ ESCÁNER B: {e}. En Railway: Settings → Region → Europe West (Amsterdam) y redeploy.")
            print(msg, flush=True)
            telegram(msg)
            time.sleep(1800)
        except Exception as e:
            print("arranque:", e, flush=True)
            time.sleep(30)
    telegram(f"📡 ESCÁNER B en marcha · {len(sc.syms)} monedas · umbral {THR*100:.2f}% · solo papel")
    last_report_day, last_beat = None, 0
    while True:
        now = time.time()
        try:
            sc.step(now)
        except RegionBlocked as e:
            telegram(f"⛔ ESCÁNER B: {e}")
            time.sleep(600)
            continue
        except Exception:
            traceback.print_exc()
            time.sleep(10)
        if now - last_beat >= 300:
            top = sorted(sc.last_dev.items(), key=lambda kv: -abs(kv[1]))[:3]
            print(f"{datetime.now(timezone.utc):%H:%M} · abiertas {len(sc.open)} · placebo {len(sc.pla)} · "
                  f"mayores desviaciones: " + ", ".join(f"{k} {v*1e4:+.0f}pb" for k, v in top), flush=True)
            last_beat = now
        utc = datetime.now(timezone.utc)
        if utc.hour == REPORT_HOUR_UTC and last_report_day != utc.date():
            last_report_day = utc.date()
            days = int((now - t_start) // 86400) + 1
            telegram(report(STATE_DIR, days), os.path.join(STATE_DIR, SIG_FILE))
        time.sleep(max(0.5, POLL - (time.time() - now)))


if __name__ == "__main__":
    main()
