"""
Descarga de velas 5m de fuentes PÚBLICAS (sin API key) con caché en disco.

  auto    → (por defecto) API de Binance y, si bloquea la región, el archivo público
            data.binance.vision. Historial completo en ambos casos.
  binance → fapi.binance.com. Bloquea IPs de EE. UU. (región por defecto de Railway).
  vision  → data.binance.vision: ficheros ZIP mensuales/diarios de Binance Futures.
            No es la API, así que no aplica el bloqueo por región. El mes en curso
            llega con 1 día de retraso.
  bingx   → open-api.bingx.com. OJO: solo da ~45 días de 5m. Úsalo solo para monedas
            que no estén en Binance, sabiendo que la muestra será corta.

La caché (data/<fuente>_<SIMBOLO>_5m.csv) se amplía de forma incremental.
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

TF_MS = 5 * 60_000
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "p12-study/1.1"


class RegionBlocked(RuntimeError):
    pass


def norm_symbol(sym: str, source: str = "binance") -> str:
    s = sym.upper().replace("-", "").replace(".P", "").replace("_", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s[:-4] + "-USDT" if source == "bingx" else s


def _get(url, params=None, tries=5, ok404=False):
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code in (451, 403) and "fapi.binance.com" in url:
                raise RegionBlocked("Binance bloquea esta IP por ubicación (EE. UU.)")
            if r.status_code == 404 and ok404:
                return r
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** i)
                continue
            return r
        except requests.RequestException:
            time.sleep(2 ** i)
    raise RuntimeError(f"sin respuesta de {url}")


# ───────────────────────── Binance API ─────────────────────────
def _binance(sym, start_ms, end_ms):
    rows, cur = [], start_ms
    while cur < end_ms:
        r = _get("https://fapi.binance.com/fapi/v1/klines",
                 dict(symbol=sym, interval="5m", startTime=cur, endTime=end_ms, limit=1500))
        if r.status_code == 400:
            raise ValueError(f"{sym} no existe en Binance Futures ({r.text[:120]})")
        data = r.json()
        if not data:
            break
        rows += [[d[0], float(d[1]), float(d[2]), float(d[3]), float(d[4]), float(d[5])] for d in data]
        nxt = data[-1][0] + TF_MS
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.12)
    return rows


# ───────────────────────── data.binance.vision ─────────────────────────
VISION = "https://data.binance.vision/data/futures/um"


def _parse_zip(content: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = z.namelist()[0]
        raw = pd.read_csv(z.open(name), header=None, usecols=[0, 1, 2, 3, 4, 5], dtype=str)
    raw = raw[pd.to_numeric(raw[0], errors="coerce").notna()]  # quita la cabecera si la hay
    raw = raw.astype(float)
    ts = raw[0].astype("int64")
    ts = ts.where(ts < 10**14, ts // 1000)  # algunos ficheros recientes vienen en microsegundos
    return [[int(t), o, h, l, c, v] for t, o, h, l, c, v in zip(ts, raw[1], raw[2], raw[3], raw[4], raw[5])]


def _vision(sym, start_ms, end_ms):
    rows = []
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    m = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    found_any = False
    while m <= end:
        ym = f"{m.year}-{m.month:02d}"
        r = _get(f"{VISION}/monthly/klines/{sym}/5m/{sym}-5m-{ym}.zip", ok404=True)
        if r.status_code == 200:
            rows += _parse_zip(r.content)
            found_any = True
        else:
            # mes en curso (aún sin fichero mensual): ficheros diarios
            d = max(m, datetime(start.year, start.month, start.day, tzinfo=timezone.utc))
            nm = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=timezone.utc)
            while d < nm and d <= end:
                rd = _get(f"{VISION}/daily/klines/{sym}/5m/{sym}-5m-{d:%Y-%m-%d}.zip", ok404=True)
                if rd.status_code == 200:
                    rows += _parse_zip(rd.content)
                    found_any = True
                d += timedelta(days=1)
                time.sleep(0.05)
        m = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=timezone.utc)
        time.sleep(0.05)
    if not found_any:
        raise ValueError(f"{sym} no está en data.binance.vision")
    return [r_ for r_ in rows if start_ms <= r_[0] < end_ms]


# ───────────────────────── BingX ─────────────────────────
def _bingx(sym, start_ms, end_ms):
    rows, cur, step = [], start_ms, 1000 * TF_MS
    while cur < end_ms:
        r = _get("https://open-api.bingx.com/openApi/swap/v3/quote/klines",
                 dict(symbol=sym, interval="5m", startTime=cur, endTime=min(cur + step, end_ms), limit=1000))
        js = r.json()
        if js.get("code", 0) != 0:
            raise ValueError(f"{sym} error BingX: {js.get('msg')}")
        for d in js.get("data") or []:
            rows.append([int(d["time"]), float(d["open"]), float(d["high"]), float(d["low"]),
                         float(d["close"]), float(d["volume"])])
        cur += step
        time.sleep(0.15)
    return rows


FETCH = {"binance": _binance, "vision": _vision, "bingx": _bingx}
_blocked = False  # si la API de Binance bloquea una vez, auto pasa a vision para el resto


def load(symbol: str, days: int, source: str = "auto", cache_dir: str = "data") -> tuple[pd.DataFrame, str]:
    """Devuelve (velas, fuente usada)."""
    global _blocked
    os.makedirs(cache_dir, exist_ok=True)
    end_ms = (int(time.time() * 1000) // TF_MS) * TF_MS
    start_ms = end_ms - days * 86_400_000

    order = {"auto": (["vision"] if _blocked else ["binance", "vision"])}.get(source, [source])
    last_err = None
    for src in order:
        try:
            return _load_src(symbol, src, start_ms, end_ms, cache_dir), src
        except RegionBlocked as e:
            _blocked = True
            last_err = e
            continue
    raise RuntimeError(f"{last_err}. Pon la región de Railway en Europa o usa SOURCE=vision")


def _load_src(symbol, src, start_ms, end_ms, cache_dir):
    sym = norm_symbol(symbol, src)
    path = os.path.join(cache_dir, f"{src}_{sym}_5m.csv")
    cached = None
    fetch = [(start_ms, end_ms)]
    if os.path.exists(path):
        cached = pd.read_csv(path)
        have_from, have_to = int(cached["ts"].min()), int(cached["ts"].max())
        fetch = []
        if start_ms < have_from - TF_MS:
            fetch.append((start_ms, have_from))
        if have_to + TF_MS < end_ms:
            fetch.append((have_to + TF_MS, end_ms))
    new = []
    for a, b in fetch:
        new += FETCH[src](sym, a, b)
    df = pd.DataFrame(new, columns=["ts", "open", "high", "low", "close", "volume"])
    if cached is not None:
        df = pd.concat([cached, df], ignore_index=True)
    if df.empty:
        raise ValueError(f"{sym}: sin datos")
    df = df.drop_duplicates("ts").sort_values("ts")
    df.to_csv(path, index=False)
    df = df[df["ts"] >= start_ms]
    return df.set_index(pd.to_datetime(df["ts"], unit="ms", utc=True))[["open", "high", "low", "close", "volume"]]


# ───────────────────────── eventos de funding (hora exacta + tasa) ─────────────────────────
def load_funding_events(symbol: str, days: int, source: str = "auto") -> pd.DataFrame:
    """DataFrame con columnas time (UTC) y rate, un registro por liquidación de funding.
    Binance API y, si bloquea la región, los ficheros mensuales de data.binance.vision."""
    global _blocked
    sym = norm_symbol(symbol, "binance")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    rows = []
    if source in ("auto", "binance") and not _blocked:
        try:
            cur = start_ms
            while cur < now_ms:
                r = _get("https://fapi.binance.com/fapi/v1/fundingRate",
                         dict(symbol=sym, startTime=cur, endTime=now_ms, limit=1000))
                data = r.json() if r.status_code == 200 else []
                if not data:
                    break
                rows += [(int(d["fundingTime"]), float(d["fundingRate"])) for d in data]
                nxt = int(data[-1]["fundingTime"]) + 1
                if nxt <= cur:
                    break
                cur = nxt
                time.sleep(0.1)
        except RegionBlocked:
            _blocked = True
            rows = []
    if not rows:
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        m = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        while m <= end:
            try:
                r = _get(f"{VISION}/monthly/fundingRate/{sym}/{sym}-fundingRate-{m:%Y-%m}.zip", ok404=True)
                if r.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                        raw = pd.read_csv(z.open(z.namelist()[0]), header=None, dtype=str)
                    raw = raw[pd.to_numeric(raw[0], errors="coerce").notna()]
                    rows += [(int(float(t)), float(v)) for t, v in zip(raw[0], raw[raw.columns[-1]])]
            except RuntimeError:
                pass
            m = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=timezone.utc)
    if not rows:
        raise ValueError(f"{sym}: sin historial de funding")
    df = pd.DataFrame(rows, columns=["t", "rate"]).drop_duplicates("t").sort_values("t")
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.floor("min")
    return df[df["t"] >= start_ms][["time", "rate"]].reset_index(drop=True)
