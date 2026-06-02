"""
╔══════════════════════════════════════════════════════════════════╗
║         CRYPTO SCANNER v4.2 — MARKETSCANNER CLASS AÑADIDA      ║
║                                                                  ║
║  FIXES v4.1 (heredados):                                         ║
║  ✅ Auto-trade abre en STD                                       ║
║  ✅ redondear_qty usa step_size real                             ║
║  ✅ get_info_instrumento cachea resultados                       ║
║  ✅ LOG detallado de skip                                        ║
║  ✅ STD_AUTOTRADE env var                                        ║
║  ✅ MIN_NOTIONAL check                                           ║
║                                                                  ║
║  NUEVO v4.2:                                                     ║
║  ✅ Clase MarketScanner — adaptador async para main.py v5.6     ║
║     Resuelve: ImportError: cannot import name MarketScanner     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, sys, time, hmac, hashlib, logging, math, threading, urllib.parse, csv, json
import asyncio as _asyncio
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
import requests
import numpy as np

# ══════════════════════════════════════════════════════════════════════
# SERVIDOR DE SALUD
# ══════════════════════════════════════════════════════════════════════
_PUERTO = int(os.environ.get("PORT", "8080"))

_estado = {
    "escaneos": 0, "señales": 0, "trades": 0,
    "wins": 0, "losses": 0, "ultimo": "iniciando",
    "balance": 0.0, "pnl_dia": 0.0, "version": "4.2",
    "circuit_breaker": False, "modo": "iniciando",
    "ultimo_skip": ""
}

class _ServidorSalud(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json":
            body = json.dumps(_estado, ensure_ascii=False).encode()
            ct = "application/json"
        else:
            total = _estado["wins"] + _estado["losses"]
            wr = f"{round(_estado['wins']/total*100)}%" if total > 0 else "-"
            cuerpo = (
                f"OK v{_estado['version']} modo={_estado['modo']} "
                f"escaneos={_estado['escaneos']} señales={_estado['señales']} "
                f"trades={_estado['trades']} W/L={_estado['wins']}/{_estado['losses']} "
                f"WR={wr} balance=${_estado['balance']:.2f} "
                f"pnl_dia=${_estado['pnl_dia']:.2f} "
                f"cb={'SI' if _estado['circuit_breaker'] else 'no'} "
                f"ultimo={_estado['ultimo']} skip={_estado['ultimo_skip']}"
            )
            body = cuerpo.encode()
            ct = "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

_http_listo = threading.Event()

def _iniciar_http():
    try:
        srv = HTTPServer(("0.0.0.0", _PUERTO), _ServidorSalud)
        _http_listo.set()
        srv.serve_forever()
    except Exception as e:
        print(f"[salud] ERROR: {e}", flush=True)
        _http_listo.set()

threading.Thread(target=_iniciar_http, daemon=True, name="http").start()
_http_listo.wait(timeout=5)
print(f"[salud] HTTP listo en 0.0.0.0:{_PUERTO}", flush=True)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════
BINGX_API_KEY    = os.getenv("BINGX_API_KEY", "")
BINGX_API_SECRET = (os.getenv("BINGX_API_SECRET", "") or os.getenv("BINGX_SECRET", ""))
TELEGRAM_TOKEN   = (os.getenv("TELEGRAM_TOKEN", "") or os.getenv("TG_TOKEN", ""))
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TG_CHAT_ID", ""))

TRADE_USDT       = float(os.getenv("TRADE_USDT", "5"))
RIESGO_PCT_BAL   = float(os.getenv("RIESGO_PCT_BAL", "0"))
LEVERAGE         = int(os.getenv("LEVERAGE", "5"))
SL_PCT           = float(os.getenv("SL_PCT", "2.5"))
TP_PCT           = float(os.getenv("TP_PCT", "5.0"))
TRAILING_PCT     = float(os.getenv("TRAILING_PCT", "0"))
MAX_TRADES       = int(os.getenv("MAX_OPEN_TRADES", "3"))
MIN_NOTIONAL     = float(os.getenv("MIN_NOTIONAL", "2.0"))

_auto_env  = os.getenv("AUTO_TRADE", "").lower()
AUTO_TRADE = (_auto_env == "true") or (
    _auto_env == "" and bool(BINGX_API_KEY) and bool(BINGX_API_SECRET))
DRY_RUN    = os.getenv("DRY_RUN", "false").lower() == "true"

STD_AUTOTRADE = os.getenv("STD_AUTOTRADE", "true").lower() == "true"

SC_MIN_STD  = int(os.getenv("SC_MIN_STD",  "50"))
SC_MIN_FUEL = int(os.getenv("SC_MIN_FUEL", "62"))
SC_MIN_SUP  = int(os.getenv("SC_MIN_SUP",  "75"))

CB_MAX_LOSSES   = int(os.getenv("CB_MAX_LOSSES", "3"))
CB_PAUSA_MIN    = int(os.getenv("CB_PAUSE_MIN", "30"))
COOLDOWN_LOSS_M = int(os.getenv("COOLDOWN_LOSS_MIN", "60"))

BLACKLIST_RAW = os.getenv(
    "BLACKLIST",
    "ANIME-USDT,WCT-USDT,TAO-USDT,AAPLX-USDT,NCSKGOOGL2USD-USDT,VINE-USDT"
)
BLACKLIST    = set(s.strip().upper() for s in BLACKLIST_RAW.split(",") if s.strip())
VOL_MIN_USDT = float(os.getenv("MIN_VOLUME_USDT", "5000000"))
TOP_N        = int(os.getenv("TOP_N", "10"))

INT_NORMAL      = int(os.getenv("INTERVAL_NORMAL",  "900"))
INT_ACTIVO      = int(os.getenv("INTERVAL_ACTIVO",  "300"))
INT_ALERTA      = int(os.getenv("INTERVAL_ALERTA",  "60"))
ALERTA_COOLDOWN = int(os.getenv("ALERTA_COOLDOWN_SEG", "1800"))

URL_BASE = "https://open-api.bingx.com"

# ══════════════════════════════════════════════════════════════════════
# PARÁMETROS MOTOR QF×JP
# ══════════════════════════════════════════════════════════════════════
I_MOM=20; I_REV=8; I_VOL_L=14; I_ATR_L=10; I_SMO=3
I_W1=0.40; I_W2=0.30; I_W3=0.30
I_ADX_LEN=14; I_ADX_TH=25
I_DLEN=40; I_DTHR=0.35; I_DECAY_PCT=30
I_DPM=2.5; I_DPB=20; I_BPT=0.18; I_ASL=10; I_ARR=1.20; I_ABR=1.20
I_TLB=30; I_TLL=5; I_TLR=3; I_TLM=0.15
I_PLL=5; I_PLR=3; I_PHL=5; I_PHR=3; I_HLC=2; I_HHC=2; I_HLW=40
I_FVG_MIN=0.3; I_FVG_BARS=40; I_OB_IMP=1.5; I_OB_BARS=50
I_CVD_LEN=20; I_CVD_DIV=5; I_CVD_ROLL=100
I_SQ_LEN=20; I_SQ_BBM=2.0; I_SQ_KCM=1.5
SC_W_SCORE=0.30; SC_W_CVD=0.25; SC_W_MOM=0.20; SC_W_DECAY=0.15; SC_W_HTF=0.10
VOL_ATR_THR=0.60

# ══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════
trades_abiertos: dict  = {}
alertas_enviadas: dict = {}
cooldown_sym: dict     = {}
racha_perdidas: int    = 0
circuit_breaker_hasta: float = 0.0
pnl_acumulado_dia: float = 0.0
trades_historico: list = []
archivo_csv = "trades_log.csv"
_info_cache: dict = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ScannerV42")

# ══════════════════════════════════════════════════════════════════════
# FIRMA HMAC
# ══════════════════════════════════════════════════════════════════════
def _firmar(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    return hmac.new(
        BINGX_API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

# ══════════════════════════════════════════════════════════════════════
# LLAMADAS API
# ══════════════════════════════════════════════════════════════════════
def _get(ruta: str, params: dict = None, auth: bool = False) -> Optional[dict]:
    p = dict(params or {})
    headers = {}
    if auth:
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"]  = _firmar(p)
        headers["X-BX-APIKEY"] = BINGX_API_KEY
    try:
        r = requests.get(URL_BASE + ruta, params=p, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.warning(f"GET {ruta}: timeout")
    except requests.exceptions.HTTPError as e:
        log.warning(f"GET {ruta}: HTTP {e.response.status_code}")
    except Exception as e:
        log.warning(f"GET {ruta}: {e}")
    return None

def _post(ruta: str, params: dict, reintentos: int = 3) -> Optional[dict]:
    for intento in range(reintentos):
        p = dict(params)
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"]  = _firmar(p)
        url = URL_BASE + ruta + "?" + urllib.parse.urlencode(p)
        headers = {"X-BX-APIKEY": BINGX_API_KEY}
        try:
            r = requests.post(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0:
                return data
            codigo = data.get("code")
            msg    = data.get("msg", "?")
            log.error(f"POST {ruta} ({intento+1}/{reintentos}) code={codigo} msg={msg}")
            if codigo in (100001, 100004, 100419):
                log.error("Error de autenticacion")
                return None
        except requests.exceptions.Timeout:
            log.error(f"POST {ruta} ({intento+1}/{reintentos}): timeout")
        except Exception as e:
            log.error(f"POST {ruta} ({intento+1}/{reintentos}): {e}")
        if intento < reintentos - 1:
            time.sleep(0.5 * (2 ** intento))
    return None

# ══════════════════════════════════════════════════════════════════════
# FUNCIONES DE MERCADO
# ══════════════════════════════════════════════════════════════════════
def get_tickers() -> list:
    d = _get("/openApi/swap/v2/quote/ticker")
    return d.get("data", []) if d else []

def get_klines(simbolo: str, intervalo: str = "3m", limite: int = 80) -> list:
    d = _get("/openApi/swap/v3/quote/klines",
             {"symbol": simbolo, "interval": intervalo, "limit": limite})
    raw = d.get("data", []) if d else []
    normalizado = []
    for k in raw:
        try:
            if isinstance(k, dict):
                normalizado.append([
                    k.get("time", 0),
                    float(k.get("open",   k.get("o", 0))),
                    float(k.get("high",   k.get("h", 0))),
                    float(k.get("low",    k.get("l", 0))),
                    float(k.get("close",  k.get("c", 0))),
                    float(k.get("volume", k.get("v", 0))),
                ])
            elif isinstance(k, (list, tuple)) and len(k) >= 6:
                normalizado.append([float(x) for x in k[:6]])
        except Exception:
            continue
    return normalizado

def get_posiciones_abiertas() -> list:
    d = _get("/openApi/swap/v2/trade/openPositions", auth=True)
    if not d:
        return []
    data = d.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return [p for p in data if abs(float(p.get("positionAmt", 0))) > 0]
    if isinstance(data, dict):
        return [p for p in data.get("positions", [])
                if abs(float(p.get("positionAmt", 0))) > 0]
    return []

def get_balance() -> float:
    d = _get("/openApi/swap/v2/user/balance", auth=True)
    if not d:
        return 0.0
    try:
        data = d.get("data", {})
        if isinstance(data, dict):
            bal = data.get("balance", {})
            if isinstance(bal, dict):
                for k in ("availableMargin", "available", "equity", "crossUnPnl"):
                    v = bal.get(k)
                    if v is not None:
                        return float(v)
            for k in ("availableMargin", "available", "equity"):
                v = data.get(k)
                if v is not None:
                    return float(v)
        if isinstance(data, list):
            for activo in data:
                if activo.get("asset", "").upper() in ("USDT", ""):
                    for k in ("availableMargin", "available", "equity"):
                        v = activo.get(k)
                        if v is not None:
                            return float(v)
        log.warning(f"Balance: estructura no reconocida")
    except Exception as e:
        log.error(f"get_balance() error: {e}")
    return 0.0

def get_info_instrumento(simbolo: str) -> dict:
    if simbolo in _info_cache:
        return _info_cache[simbolo]
    default = {"step_size": 0.001, "min_qty": 0.001, "price_precision": 6, "min_notional": 5.0}
    try:
        d = _get("/openApi/swap/v2/quote/contracts")
        if d and d.get("data"):
            for c in d["data"]:
                if c.get("symbol") == simbolo:
                    info = {
                        "step_size":       float(c.get("tradeMinQuantity",   0.001)),
                        "min_qty":         float(c.get("tradeMinQuantity",   0.001)),
                        "price_precision": int(c.get("pricePrecision",       6)),
                        "min_notional":    float(c.get("minOrderValue",      5.0)),
                    }
                    _info_cache[simbolo] = info
                    return info
    except Exception:
        pass
    _info_cache[simbolo] = default
    return default

def redondear_qty(qty: float, step: float) -> float:
    if step <= 0:
        return round(qty, 4)
    decimales = max(0, -int(math.floor(math.log10(step + 1e-12))))
    return round(math.floor(qty / step) * step, decimales)

def calcular_usdt_trade(balance: float) -> float:
    if RIESGO_PCT_BAL > 0 and balance > 0:
        usdt = balance * RIESGO_PCT_BAL / 100.0
        return max(1.0, round(usdt, 2))
    return TRADE_USDT

# ══════════════════════════════════════════════════════════════════════
# INDICADORES TÉCNICOS
# ══════════════════════════════════════════════════════════════════════
def f_tanh(x):
    x2 = max(min(2.0 * x, 20.0), -20.0)
    e  = math.exp(x2)
    return (e - 1.0) / (e + 1.0)

def ema(arr, p):
    k = 2.0 / (p + 1)
    r = np.empty(len(arr))
    r[0] = arr[0]
    for i in range(1, len(arr)):
        r[i] = arr[i] * k + r[i - 1] * (1 - k)
    return r

def sma(arr, p):
    out = np.full(len(arr), np.nan)
    for i in range(p - 1, len(arr)):
        out[i] = arr[i - p + 1:i + 1].mean()
    return out

def stdev(arr, p):
    out = np.full(len(arr), np.nan)
    for i in range(p - 1, len(arr)):
        out[i] = arr[i - p + 1:i + 1].std(ddof=0)
    return out

def atr_series(h, l, c, p):
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    return ema(tr, p)

def adx_series(h, l, c, p):
    n = len(c)
    pdm = np.zeros(n); mdm = np.zeros(n); tr = np.zeros(n)
    for i in range(1, n):
        hd = h[i] - h[i-1]; ld = l[i-1] - l[i]
        pdm[i] = hd if hd > ld and hd > 0 else 0
        mdm[i] = ld if ld > hd and ld > 0 else 0
        tr[i]  = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    ae  = ema(tr, p)
    pdi = 100 * ema(pdm, p) / np.maximum(ae, 1e-10)
    mdi = 100 * ema(mdm, p) / np.maximum(ae, 1e-10)
    dx  = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-10)
    return pdi, mdi, ema(dx, p)

def obv_series(c, v):
    obv = np.zeros(len(c))
    for i in range(1, len(c)):
        if   c[i] > c[i-1]: obv[i] = obv[i-1] + v[i]
        elif c[i] < c[i-1]: obv[i] = obv[i-1] - v[i]
        else:                obv[i] = obv[i-1]
    return obv

def pivot_high(h, left, right):
    n = len(h); ph = np.full(n, np.nan)
    for i in range(left, n - right):
        w = h[i - left:i + right + 1]
        if h[i] == w.max() and (w < h[i]).any():
            ph[i] = h[i]
    return ph

def pivot_low(l, left, right):
    n = len(l); pl = np.full(n, np.nan)
    for i in range(left, n - right):
        w = l[i - left:i + right + 1]
        if l[i] == w.min() and (w > l[i]).any():
            pl[i] = l[i]
    return pl

def linreg(arr, longitud):
    if len(arr) < longitud:
        return float(arr[-1])
    y = arr[-longitud:]; x = np.arange(longitud)
    m, b = np.polyfit(x, y, 1)
    return m * (longitud - 1) + b

# ══════════════════════════════════════════════════════════════════════
# MOTOR QF×JP — ANÁLISIS
# ══════════════════════════════════════════════════════════════════════
def analizar_par(klines_3m: list, klines_15m: list) -> Optional[dict]:
    if len(klines_3m) < 50:
        return None

    def _col(kl, idx):
        out = []
        for k in kl:
            try:    out.append(float(k[idx]))
            except: out.append(out[-1] if out else 0.0)
        return np.array(out)

    o = _col(klines_3m, 1); h = _col(klines_3m, 2)
    l = _col(klines_3m, 3); c = _col(klines_3m, 4); v = _col(klines_3m, 5)
    n = len(c)

    atr       = atr_series(h, l, c, I_ATR_L)
    atr_ahora = float(atr[-1])
    atr_avg20 = float(sma(atr, 20)[-1] or atr_ahora)
    vol_ok    = atr_ahora > atr_avg20 * VOL_ATR_THR
    vol_pct   = round(atr_ahora / atr_avg20 * 100) if atr_avg20 > 0 else 100

    hi_lo      = np.log(np.maximum(h / l, 1e-10))
    spread_est = sma(hi_lo, 5) * c
    bp_drain   = (spread_est / np.maximum(c, 1e-10)) * 100
    exec_ok    = bool(bp_drain[-1] < I_BPT)

    pdi, mdi, adx_v = adx_series(h, l, c, I_ADX_LEN)
    adx_ahora    = float(adx_v[-1])
    trend_fuerte = adx_ahora >= I_ADX_TH
    trend_up     = bool(pdi[-1] > mdi[-1] and trend_fuerte)
    trend_dn     = bool(mdi[-1] > pdi[-1] and trend_fuerte)

    sma_mom = float(sma(c, I_MOM)[-1]); std_mom = float(stdev(c, I_MOM)[-1])
    voln    = std_mom / sma_mom if sma_mom else 1e-10
    f_mom_v = ((c[-1] - c[-I_MOM]) / c[-I_MOM]) / voln if (voln and c[-I_MOM]) else 0.0

    bsma    = sma(c, I_REV); bstd = stdev(c, I_REV)
    f_rev_v = -(c[-1] - bsma[-1]) / bstd[-1] if bstd[-1] else 0.0

    obv_a   = obv_series(c, v); oe = ema(obv_a, I_VOL_L); os_ = stdev(obv_a, I_VOL_L)
    f_vol_v = (obv_a[-1] - oe[-1]) / os_[-1] if os_[-1] else 0.0

    adx_f  = min(1.0, adx_ahora / (I_ADX_TH * 2.0))
    w_mom  = I_W1 + adx_f * I_W1 * 0.40
    w_rev  = max(I_W2 * 0.30, I_W2 - adx_f * I_W2 * 0.50)
    w_tot  = w_mom + w_rev + I_W3
    raw_v  = (w_mom * f_mom_v + w_rev * f_rev_v + I_W3 * f_vol_v) / max(w_tot, 1e-10)

    sc_std     = float(stdev(np.array([raw_v] * n), I_DLEN)[-1]) or 1e-10
    norm_score = f_tanh(raw_v / sc_std)

    ic_num = 0.3
    window = min(I_DLEN, n - 5)
    if window >= 8:
        try:
            roc_s = np.array([(c[i] - c[max(0, i - I_MOM)]) / max(c[max(0, i - I_MOM)], 1e-10)
                               for i in range(n)])
            fwd   = np.diff(c) / np.maximum(c[:-1], 1e-10)
            seg_s = roc_s[max(0, n - window - 1):n - 1]
            seg_f = fwd[max(0, n - window - 1):n - 1]
            if len(seg_s) > 4 and seg_s.std() > 1e-10 and seg_f.std() > 1e-10:
                ic_raw = float(np.corrcoef(seg_s, seg_f)[0, 1])
                ic_num = 0.0 if np.isnan(ic_raw) else abs(ic_raw)
        except Exception:
            ic_num = 0.3

    decay_r   = min(1.0, ic_num / max(ic_num, 0.01))
    sig_alive = decay_r >= I_DTHR or ic_num >= 0.15

    vb      = float(sma(v, I_DPB)[-1])
    vs      = bool(v[-1] > vb * I_DPM)
    rn      = bool((h[-1] - l[-1]) < atr_ahora * 0.6)
    dp_buy  = bool(vs and rn and c[-1] > o[-1])
    dp_sell = bool(vs and rn and c[-1] < o[-1])

    if klines_15m and len(klines_15m) >= 22:
        c15      = _col(klines_15m, 4)
        htf_bull = float(ema(c15, 9)[-1]) > float(ema(c15, 21)[-1])
        htf_bear = float(ema(c15, 9)[-1]) < float(ema(c15, 21)[-1])
    else:
        htf_bull = norm_score > 0
        htf_bear = norm_score < 0

    ur  = np.where(c > o, h - l, 0.0); dr = np.where(c < o, h - l, 0.0)
    aur = float(sma(ur, I_ASL)[-1]);   adr = float(sma(dr, I_ASL)[-1])
    asim_bull = (aur / adr if adr > 0 else 1.0) >= I_ARR
    asim_bear = (adr / aur if aur > 0 else 1.0) >= I_ABR

    ph_arr = pivot_high(h, I_TLL, I_TLR)
    pl_arr = pivot_low(l, I_PLL, I_PLR)
    phv = [(i, v2) for i, v2 in enumerate(ph_arr) if not np.isnan(v2)]
    plv = [(i, v2) for i, v2 in enumerate(pl_arr) if not np.isnan(v2)]

    tl_break_long = tl_break_short = False
    if len(phv) >= 2:
        (pb2, ph2), (pb1, ph1) = phv[-2], phv[-1]
        if ph2 > ph1 and (n - 1 - pb2) <= I_TLB:
            sl2 = (ph1 - ph2) / max(pb1 - pb2, 1)
            if c[-1] > ph1 + sl2 * (n - 1 - pb1) + atr_ahora * I_TLM:
                tl_break_long = True
    if len(plv) >= 2:
        (lb2, pl2), (lb1, pl1) = plv[-2], plv[-1]
        if pl2 < pl1 and (n - 1 - lb2) <= I_TLB:
            sl2 = (pl1 - pl2) / max(lb1 - lb2, 1)
            if c[-1] < pl1 + sl2 * (n - 1 - lb1) - atr_ahora * I_TLM:
                tl_break_short = True

    win  = min(I_HLW, n)
    plr  = [(i, v2) for i, v2 in enumerate(pl_arr[-win:]) if not np.isnan(v2)]
    phr  = [(i, v2) for i, v2 in enumerate(ph_arr[-win:]) if not np.isnan(v2)]
    hl_c = sum(1 for j in range(1, len(plr)) if plr[j][1] > plr[j-1][1])
    lh_c = sum(1 for j in range(1, len(phr)) if phr[j][1] < phr[j-1][1])
    venta_agotada  = hl_c >= I_HLC
    compra_agotada = lh_c >= I_HHC

    last_sl = float(plr[-1][1]) if plr else float(l[-10:].min())
    last_sh = float(phr[-1][1]) if phr else float(h[-10:].max())

    en_bull_fvg = en_bear_fvg = False
    for i in range(max(0, n - I_FVG_BARS), n - 2):
        if l[i+2] > h[i] and (l[i+2] - h[i]) > atr_ahora * I_FVG_MIN:
            if h[i] <= c[-1] <= l[i+2]: en_bull_fvg = True
        if h[i+2] < l[i] and (l[i] - h[i+2]) > atr_ahora * I_FVG_MIN:
            if h[i+2] <= c[-1] <= l[i]: en_bear_fvg = True

    en_bull_ob = en_bear_ob = False
    for i in range(max(0, n - I_OB_BARS), n - 1):
        if i >= 1:
            if (c[i]-o[i]) > atr_ahora*I_OB_IMP and c[i] > c[i-1] and c[i-1] < o[i-1]:
                if o[i-1] >= c[-1] >= c[i-1]: en_bull_ob = True
            if (o[i]-c[i]) > atr_ahora*I_OB_IMP and c[i] < c[i-1] and c[i-1] > o[i-1]:
                if c[i-1] >= c[-1] >= o[i-1]: en_bear_ob = True

    hlr  = h - l
    bv   = np.where(hlr > 0, (c - l) / hlr * v, v * 0.5)
    sv   = np.where(hlr > 0, (h - c) / hlr * v, v * 0.5)
    db   = bv - sv
    roll = min(I_CVD_ROLL, n)
    cvd  = float(sma(db, roll)[-1]) * roll
    cvde = float(ema(db, I_CVD_LEN)[-1])
    cvd_rising = cvd > cvde
    cvds       = float(stdev(db, min(I_CVD_LEN * 2, n))[-1])
    cvdz       = (cvd - cvde) / cvds if cvds else 0.0
    cvd_score_v = max(0.0, min(1.0, (f_tanh(cvdz) + 1) / 2))
    dw           = min(I_CVD_DIV, n - 1)
    cvd_prev     = float(sma(db[:-dw], roll)[-1]) * roll if n > dw + roll else cvd
    cvd_bull_div = bool(c[-1] < c[-dw-1] and cvd > cvd_prev)
    cvd_bear_div = bool(c[-1] > c[-dw-1] and cvd < cvd_prev)

    sb = float(sma(c, I_SQ_LEN)[-1]); sd = float(stdev(c, I_SQ_LEN)[-1])
    sk = float(atr_series(h, l, c, I_SQ_LEN)[-1]); se = float(ema(c, I_SQ_LEN)[-1])
    sq_on = (sb + I_SQ_BBM*sd) < (se + I_SQ_KCM*sk) and (sb - I_SQ_BBM*sd) > (se - I_SQ_KCM*sk)
    sq_fire = sq_bull = sq_bear = False
    if n >= I_SQ_LEN + 2:
        sb_p = float(sma(c[:-1], I_SQ_LEN)[-1]); sd_p = float(stdev(c[:-1], I_SQ_LEN)[-1])
        sk_p = float(atr_series(h[:-1], l[:-1], c[:-1], I_SQ_LEN)[-1])
        se_p = float(ema(c[:-1], I_SQ_LEN)[-1])
        sq_on_p = (sb_p + I_SQ_BBM*sd_p) < (se_p + I_SQ_KCM*sk_p) and                   (sb_p - I_SQ_BBM*sd_p) > (se_p - I_SQ_KCM*sk_p)
        sq_fire = not sq_on and sq_on_p
    if sq_fire:
        slr     = linreg(c - (max(h[-I_SQ_LEN:]) + min(l[-I_SQ_LEN:]) +
                              float(sma(c, I_SQ_LEN)[-1])) / 3, I_SQ_LEN)
        sq_bull = slr > 0
        sq_bear = slr < 0

    nsl = (f_tanh(norm_score) + 1) / 2
    mml = (f_tanh(f_mom_v * 2) + 1) / 2
    dn  = min(1.0, decay_r)
    hal = (0.5 if htf_bull else 0.0) + (0.5 if asim_bull else 0.0)
    has = (0.5 if htf_bear else 0.0) + (0.5 if asim_bear else 0.0)

    cl = round(min(100, (SC_W_SCORE*nsl + SC_W_CVD*cvd_score_v +
                         SC_W_MOM*mml + SC_W_DECAY*dn + SC_W_HTF*hal) * 100))
    nss = (f_tanh(-norm_score) + 1) / 2
    mms = (f_tanh(-f_mom_v * 2) + 1) / 2
    cs  = round(min(100, (SC_W_SCORE*nss + SC_W_CVD*(1-cvd_score_v) +
                          SC_W_MOM*mms + SC_W_DECAY*dn + SC_W_HTF*has) * 100))

    lconv = sum([
        norm_score > 0.10, sig_alive, exec_ok, htf_bull, asim_bull,
        venta_agotada, tl_break_long, dp_buy, cvd_rising,
        sq_bull or en_bull_fvg or en_bull_ob
    ])
    sconv = sum([
        norm_score < -0.10, sig_alive, exec_ok, htf_bear, asim_bear,
        compra_agotada, tl_break_short, dp_sell, not cvd_rising,
        sq_bear or en_bear_fvg or en_bear_ob
    ])

    comp_long  = min(100, cl + round(lconv * 0.5))
    comp_short = min(100, cs + round(sconv * 0.5))

    long_base  = comp_long  >= SC_MIN_STD and exec_ok and sig_alive and vol_ok
    short_base = comp_short >= SC_MIN_STD and exec_ok and sig_alive and vol_ok

    long_std  = long_base  and htf_bull
    short_std = short_base and htf_bear

    long_fuel  = (long_std  and comp_long  >= SC_MIN_FUEL and
                  (tl_break_long  or sq_bull or cvd_rising    or en_bull_fvg or en_bull_ob))
    short_fuel = (short_std and comp_short >= SC_MIN_FUEL and
                  (tl_break_short or sq_bear or not cvd_rising or en_bear_fvg or en_bear_ob))

    long_sup  = (long_fuel  and comp_long  >= SC_MIN_SUP and
                 (dp_buy  or cvd_bull_div or venta_agotada))
    short_sup = (short_fuel and comp_short >= SC_MIN_SUP and
                 (dp_sell or cvd_bear_div or compra_agotada))

    if   long_sup:   senal, ss = "LONG SUP",   comp_long
    elif long_fuel:  senal, ss = "LONG FUEL",  comp_long
    elif long_std:   senal, ss = "LONG STD",   comp_long
    elif short_sup:  senal, ss = "SHORT SUP",  comp_short
    elif short_fuel: senal, ss = "SHORT FUEL", comp_short
    elif short_std:  senal, ss = "SHORT STD",  comp_short
    else:            senal, ss = "ESPERAR",    max(comp_long, comp_short)

    return {
        "senal": senal, "score_senal": ss,
        "long_sup": long_sup,   "long_fuel": long_fuel,  "long_std": long_std,
        "short_sup": short_sup, "short_fuel": short_fuel, "short_std": short_std,
        "comp_long": comp_long, "comp_short": comp_short,
        "norm_score": round(norm_score * 100),
        "long_conv": lconv, "short_conv": sconv,
        "sig_alive": sig_alive, "exec_ok": exec_ok, "vol_ok": vol_ok, "vol_pct": vol_pct,
        "htf_bull": htf_bull, "htf_bear": htf_bear,
        "asim_bull": asim_bull, "asim_bear": asim_bear,
        "dp_buy": dp_buy, "dp_sell": dp_sell,
        "tl_break_long": tl_break_long, "tl_break_short": tl_break_short,
        "venta_agotada": venta_agotada, "compra_agotada": compra_agotada,
        "en_bull_fvg": en_bull_fvg, "en_bear_fvg": en_bear_fvg,
        "en_bull_ob": en_bull_ob,   "en_bear_ob":  en_bear_ob,
        "cvd_rising": cvd_rising, "cvd_bull_div": cvd_bull_div, "cvd_bear_div": cvd_bear_div,
        "sq_bull": sq_bull, "sq_bear": sq_bear, "sq_on": sq_on,
        "trend_up": trend_up, "trend_dn": trend_dn, "adx": round(adx_ahora, 1),
        "last_sl": round(last_sl, 6), "last_sh": round(last_sh, 6),
        "decay_r": round(decay_r * 100), "atr": atr_ahora,
    }

# ══════════════════════════════════════════════════════════════════════
# SCANNER
# ══════════════════════════════════════════════════════════════════════
def escanear_mercado():
    log.info("=== Escaneo QF×JP v4.2 ===")
    _estado["escaneos"] += 1

    tickers = get_tickers()
    btc_cambio = btc_precio = 0.0
    for t in tickers:
        if t.get("symbol") == "BTC-USDT":
            try:
                btc_cambio = float(t.get("priceChangePercent", 0))
                btc_precio = float(t.get("lastPrice", 0))
            except Exception:
                pass
            break

    log.info(f"BTC: ${btc_precio:,.0f} ({btc_cambio:+.1f}%) | Pares: {len(tickers)}")

    resultados = []
    for ticker in tickers:
        sym = ticker.get("symbol", "")
        if not sym.endswith("-USDT"):
            continue
        if sym in BLACKLIST:
            continue
        if any(x in sym for x in ("USDC", "BUSD", "TUSD", "DAI", "FDUSD")):
            continue
        if time.time() < cooldown_sym.get(sym, 0):
            continue
        try:
            vol24  = float(ticker.get("quoteVolume", 0))
            precio = float(ticker.get("lastPrice", 0))
            chg24  = float(ticker.get("priceChangePercent", 0))
        except Exception:
            continue
        if vol24 < VOL_MIN_USDT:
            continue

        k3m  = get_klines(sym, "3m",  80)
        k15m = get_klines(sym, "15m", 30)
        if not k3m or len(k3m) < 50:
            time.sleep(0.05)
            continue

        an = analizar_par(k3m, k15m)
        if not an:
            time.sleep(0.05)
            continue

        if an["senal"] == "ESPERAR" and an["comp_long"] < 45 and an["comp_short"] < 45:
            time.sleep(0.05)
            continue

        resultados.append({"simbolo": sym, "precio": precio, "cambio_24h": chg24,
                            "volumen_usdt": vol24, **an})
        time.sleep(0.08)

    orden = {
        "LONG SUP": 0, "SHORT SUP": 1,
        "LONG FUEL": 2, "SHORT FUEL": 3,
        "LONG STD": 4,  "SHORT STD": 5,
        "ESPERAR": 6
    }
    resultados.sort(key=lambda x: (orden.get(x["senal"], 9), -x["score_senal"]))

    senales  = [r for r in resultados if r["senal"] != "ESPERAR"][:TOP_N]
    _estado["senales"] = len(senales)
    _estado["ultimo"]  = datetime.now(timezone.utc).strftime("%H:%M")

    log.info(f"Con senal: {len(senales)} | Total analizados: {len(resultados)}")

    tiene_sup  = any(r["long_sup"]  or r["short_sup"]  for r in senales)
    tiene_fuel = any(r["long_fuel"] or r["short_fuel"] for r in senales)
    intervalo  = (INT_ALERTA if tiene_sup else
                  INT_ACTIVO if (tiene_fuel or senales) else
                  INT_NORMAL)

    return senales, intervalo, btc_cambio

# ══════════════════════════════════════════════════════════════════════
# AUTO-TRADE
# ══════════════════════════════════════════════════════════════════════
def configurar_apalancamiento(simbolo: str):
    r = _post("/openApi/swap/v2/trade/leverage",
              {"symbol": simbolo, "leverage": str(LEVERAGE)})
    if not r:
        _post("/openApi/swap/v2/trade/leverage",
              {"symbol": simbolo, "side": "LONG",  "leverage": str(LEVERAGE)})
        _post("/openApi/swap/v2/trade/leverage",
              {"symbol": simbolo, "side": "SHORT", "leverage": str(LEVERAGE)})
    _post("/openApi/swap/v2/trade/marginType",
          {"symbol": simbolo, "marginType": "ISOLATED"})

def circuit_breaker_activo() -> bool:
    if time.time() < circuit_breaker_hasta:
        restante = int(circuit_breaker_hasta - time.time())
        log.warning(f"Circuit breaker activo — faltan {restante}s")
        return True
    return False

def _skip(razon: str):
    _estado["ultimo_skip"] = razon
    log.warning(f"SKIP trade: {razon}")

def abrir_trade(simbolo: str, precio: float, direccion: str) -> Optional[dict]:
    global racha_perdidas, circuit_breaker_hasta

    if not BINGX_API_KEY:
        _skip("sin API key"); return None
    if not AUTO_TRADE and not DRY_RUN:
        _skip("AUTO_TRADE=false y DRY_RUN=false"); return None
    if simbolo in trades_abiertos:
        _skip(f"{simbolo} ya tiene trade abierto"); return None
    if circuit_breaker_activo():
        _skip("circuit breaker activo"); return None
    if simbolo in BLACKLIST:
        _skip(f"{simbolo} en blacklist"); return None

    posiciones = get_posiciones_abiertas()
    if len(posiciones) >= MAX_TRADES:
        _skip(f"max trades ({MAX_TRADES}) alcanzado — {len(posiciones)} activas")
        return None

    balance = get_balance()
    _estado["balance"] = balance
    usdt_trade = calcular_usdt_trade(balance)
    log.info(f"Balance: ${balance:.2f} | Trade USDT: ${usdt_trade:.2f}")

    if balance < usdt_trade:
        _skip(f"balance ${balance:.2f} < trade ${usdt_trade:.2f}"); return None

    if DRY_RUN:
        log.info(f"[DRY RUN] {direccion} {simbolo} @ {precio}")
        es_long = direccion == "LONG"
        trade = {
            "simbolo": simbolo, "direccion": direccion,
            "entrada": precio, "usdt": usdt_trade,
            "sl": round(precio * (1 - SL_PCT/100 if es_long else 1 + SL_PCT/100), 6),
            "tp": round(precio * (1 + TP_PCT/100 if es_long else 1 - TP_PCT/100), 6),
            "tp_desc": "DRY", "qty": 0, "apalancamiento": LEVERAGE,
            "abierto_en": datetime.now(timezone.utc).isoformat(),
            "dry_run": True,
        }
        trades_abiertos[simbolo] = trade
        _estado["trades"] = len(trades_abiertos)
        return trade

    configurar_apalancamiento(simbolo)
    time.sleep(0.3)

    info     = get_info_instrumento(simbolo)
    qty_raw  = (usdt_trade * LEVERAGE) / precio
    qty      = redondear_qty(qty_raw, info["step_size"])
    notional = qty * precio

    log.info(f"{simbolo} qty_raw={qty_raw:.6f} qty={qty} step={info['step_size']} "
             f"min_qty={info['min_qty']} notional=${notional:.2f}")

    if qty < info["min_qty"]:
        _skip(f"{simbolo} qty {qty} < min_qty {info['min_qty']}"); return None
    if notional < MIN_NOTIONAL:
        _skip(f"{simbolo} notional ${notional:.2f} < MIN_NOTIONAL ${MIN_NOTIONAL}"); return None

    es_long    = (direccion == "LONG")
    lado_abrir = "BUY"  if es_long else "SELL"
    lado_cerrar= "SELL" if es_long else "BUY"
    sl_p = round(precio * (1 - SL_PCT/100 if es_long else 1 + SL_PCT/100), info["price_precision"])
    tp_p = round(precio * (1 + TP_PCT/100 if es_long else 1 - TP_PCT/100), info["price_precision"])

    orden = _post("/openApi/swap/v2/trade/order",
                  {"symbol": simbolo, "side": lado_abrir,
                   "type": "MARKET", "quantity": str(qty)})
    if not orden:
        _skip(f"{simbolo} orden MARKET fallida"); return None

    time.sleep(0.5)

    _post("/openApi/swap/v2/trade/order",
          {"symbol": simbolo, "side": lado_cerrar, "type": "STOP_MARKET",
           "stopPrice": str(sl_p), "closePosition": "true"})

    if TRAILING_PCT > 0:
        _post("/openApi/swap/v2/trade/order",
              {"symbol": simbolo, "side": lado_cerrar,
               "type": "TRAILING_STOP_MARKET",
               "callbackRate": str(round(TRAILING_PCT, 2)),
               "closePosition": "true"})
        tp_desc = f"Trailing {TRAILING_PCT}%"
    else:
        _post("/openApi/swap/v2/trade/order",
              {"symbol": simbolo, "side": lado_cerrar,
               "type": "TAKE_PROFIT_MARKET",
               "stopPrice": str(tp_p), "closePosition": "true"})
        tp_desc = str(tp_p)

    trade = {
        "simbolo": simbolo, "direccion": direccion,
        "entrada": precio, "sl": sl_p, "tp": tp_p,
        "tp_desc": tp_desc, "qty": qty,
        "usdt": usdt_trade, "apalancamiento": LEVERAGE,
        "abierto_en": datetime.now(timezone.utc).isoformat(),
        "dry_run": False,
    }
    trades_abiertos[simbolo] = trade
    _estado["trades"] = len(trades_abiertos)
    _estado["ultimo_skip"] = ""
    log.info(f"TRADE {direccion} {simbolo} @ {precio} | SL={sl_p} TP={tp_desc} Qty={qty}")
    return trade

def actualizar_trades():
    global racha_perdidas, circuit_breaker_hasta, pnl_acumulado_dia

    if not trades_abiertos:
        return
    try:
        posiciones_activas = set()
        if not DRY_RUN:
            posiciones = get_posiciones_abiertas()
            posiciones_activas = {p.get("symbol") for p in posiciones}
        else:
            posiciones_activas = set(trades_abiertos.keys())

        cerrados = [sym for sym in list(trades_abiertos.keys())
                    if sym not in posiciones_activas]

        for sym in cerrados:
            trade = trades_abiertos.pop(sym)
            k     = get_klines(sym, "3m", 3)
            if k:
                pa     = float(k[-1][4])
                en     = trade["entrada"]
                es_long= trade["direccion"] == "LONG"
                pnl    = (pa - en) / en * 100 * (1 if es_long else -1)
                ganado = pnl > 0

                if ganado:
                    _estado["wins"] += 1
                    racha_perdidas   = 0
                    resultado        = f"WIN +{pnl:.2f}%"
                else:
                    _estado["losses"]  += 1
                    racha_perdidas     += 1
                    resultado           = f"LOSS {pnl:.2f}%"
                    cooldown_sym[sym] = time.time() + COOLDOWN_LOSS_M * 60
                    if racha_perdidas >= CB_MAX_LOSSES:
                        circuit_breaker_hasta = time.time() + CB_PAUSA_MIN * 60
                        _estado["circuit_breaker"] = True
                        log.warning(f"Circuit breaker: {CB_MAX_LOSSES} perdidas -> pausa {CB_PAUSA_MIN}min")
                        enviar_telegram(
                            f"*Circuit breaker activado*
"
                            f"{racha_perdidas} perdidas -> pausa {CB_PAUSA_MIN}min")

                pnl_usdt = trade["usdt"] * LEVERAGE * pnl / 100
                pnl_acumulado_dia += pnl_usdt
                _estado["pnl_dia"] = round(pnl_acumulado_dia, 2)

                _guardar_trade_csv(trade, pa, pnl, pnl_usdt, ganado)
                log.info(f"Cerrado: {sym} {trade['direccion']} | {resultado} | ${pnl_usdt:+.2f}")
                enviar_telegram(
                    f"*Trade cerrado*: {sym.replace('-USDT','')}
"
                    f"Dir: {trade['direccion']} | Entrada: `{en}`
"
                    f"Cierre: `{pa:.6f}`
{resultado}
"
                    f"PnL: `${pnl_usdt:+.2f}` | Dia: `${pnl_acumulado_dia:+.2f}`"
                )

        if circuit_breaker_hasta and time.time() >= circuit_breaker_hasta:
            _estado["circuit_breaker"] = False

    except Exception as e:
        log.error(f"actualizar_trades() error: {e}", exc_info=True)

    _estado["trades"] = len(trades_abiertos)

def _guardar_trade_csv(trade, precio_cierre, pnl_pct, pnl_usdt, ganado):
    existe = os.path.exists(archivo_csv)
    try:
        with open(archivo_csv, "a", newline="") as f:
            w = csv.writer(f)
            if not existe:
                w.writerow(["fecha_cierre","simbolo","direccion","entrada","cierre",
                             "sl","tp","qty","usdt","apalancamiento",
                             "pnl_pct","pnl_usdt","resultado","dry_run","abierto_en"])
            w.writerow([
                datetime.now(timezone.utc).isoformat(),
                trade["simbolo"], trade["direccion"], trade["entrada"], precio_cierre,
                trade["sl"], trade["tp"], trade["qty"], trade["usdt"], trade["apalancamiento"],
                round(pnl_pct, 4), round(pnl_usdt, 4),
                "WIN" if ganado else "LOSS", trade.get("dry_run", False), trade["abierto_en"],
            ])
    except Exception as e:
        log.warning(f"CSV log error: {e}")

# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════
def enviar_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def construir_alerta(par: dict) -> str:
    sym  = par["simbolo"].replace("-USDT", "")
    sig  = par["senal"]
    scl  = par["comp_long"]; scs = par["comp_short"]
    p    = par["precio"]
    sl_p = round(p * (1 - SL_PCT / 100), 6)
    tp1  = round(p * (1 + TP_PCT / 100), 6)
    tp2  = round(p * (1 + TP_PCT * 1.5 / 100), 6)

    es_long  = "LONG"  in sig
    es_sup   = "SUP"   in sig
    es_fuel  = "FUEL"  in sig
    emoji    = "BLUE" if es_sup else "YELLOW" if es_fuel else "GREEN"
    dir_e    = "LONG" if es_long else "SHORT"
    modo_tag = " [DRY]" if DRY_RUN else ""

    db    = max(0, min(8, round(par["decay_r"] / 100 * 8)))
    barra = "X" * db + "." * (8 - db)
    trailing = f"  Trailing SL: `{TRAILING_PCT}%`
" if TRAILING_PCT > 0 else ""

    lineas = [
        f"[{emoji}] *{sig}: {sym}*{modo_tag}",
        f"{'─'*30}",
        f"[{dir_e}] SC LONG: `{scl}/100` | SC SHORT: `{scs}/100`",
        f"SCORE: `{par['norm_score']}` | CONV: `{par['long_conv']}L/{par['short_conv']}S`",
        f"{'─'*30}",
        f"Precio: `{p}` | 24h: {par['cambio_24h']:+.1f}% | Vol: ${par['volumen_usdt']/1e6:.1f}M",
        f"SL: `{sl_p}` (-{SL_PCT}%)",
        f"TP1: `{tp1}` (+{TP_PCT}%) | TP2: `{tp2}` (+{TP_PCT*1.5:.1f}%)",
        f"{trailing}{'─'*30}",
        f"*QF×JP v4.2:*",
        f"  DECAY  `{barra} {par['decay_r']}%` {'ok' if par['sig_alive'] else 'x'}",
        f"  HTF    `{'BULL' if par['htf_bull'] else 'BEAR' if par['htf_bear'] else '-'}` | "
        f"ADX `{par['adx']} {'up' if par['trend_up'] else 'dn' if par['trend_dn'] else '~'}`",
        f"  ASIM   `{'up' if par['asim_bull'] else 'dn' if par['asim_bear'] else '-'}` | "
        f"VOL ATR `{par['vol_pct']}%` {'ok' if par['vol_ok'] else 'x'}",
        f"  TL     `{'LONG' if par['tl_break_long'] else 'SHORT' if par['tl_break_short'] else '-'}`",
        f"  SWING  `{'HL up' if par['venta_agotada'] else 'LH dn' if par['compra_agotada'] else '-'}`",
        f"  DP     `{'up' if par['dp_buy'] else 'dn' if par['dp_sell'] else '-'}`",
        f"  FVG    `{'up' if par['en_bull_fvg'] else 'dn' if par['en_bear_fvg'] else '-'}` | "
        f"OB `{'up' if par['en_bull_ob'] else 'dn' if par['en_bear_ob'] else '-'}`",
        f"  CVD    `{'DIV up' if par['cvd_bull_div'] else 'DIV dn' if par['cvd_bear_div'] else 'up' if par['cvd_rising'] else 'dn'}`",
        f"  SQ     `{'fire up' if par['sq_bull'] else 'fire dn' if par['sq_bear'] else 'comp' if par['sq_on'] else '-'}`",
        f"  EXEC   `{'OK' if par['exec_ok'] else 'BLOQ'}`",
        f"{'─'*30}",
        f"SL ref: `{par['last_sl'] if es_long else par['last_sh']}`",
    ]

    tier_label = "SUP/FUEL" if (es_sup or es_fuel) else "STD"
    if AUTO_TRADE:
        if es_sup or es_fuel or STD_AUTOTRADE:
            estado_trade = "abierto" if par["simbolo"] in trades_abiertos else "pendiente"
            lineas.append(f"Auto-trade [{tier_label}]: {estado_trade}{' (DRY)' if DRY_RUN else ''}")
        else:
            lineas.append("Auto-trade: STD_AUTOTRADE=false")
    else:
        lineas.append("Verificar en TradingView 3m + QF×JP")

    return "
".join(lineas)

def construir_resumen(resultados: list, btc_cambio: float, intervalo: int) -> str:
    ahora  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    signo  = "+" if btc_cambio > 0 else ""
    wins   = _estado["wins"]; losses = _estado["losses"]; total = wins + losses
    wr_str = f"{wins}/{total} ({round(wins/total*100)}%)" if total > 0 else "-"

    cb_str = ""
    if time.time() < circuit_breaker_hasta:
        rest   = int((circuit_breaker_hasta - time.time()) / 60)
        cb_str = f"
Circuit breaker: {rest}min restantes"

    skip_str = f"
Ultimo skip: {_estado['ultimo_skip']}" if _estado["ultimo_skip"] else ""
    modo_str = " [DRY RUN]" if DRY_RUN else ""

    sup_l  = [r for r in resultados if r["long_sup"]]
    sup_s  = [r for r in resultados if r["short_sup"]]
    fuel_l = [r for r in resultados if r["long_fuel"]  and not r["long_sup"]]
    fuel_s = [r for r in resultados if r["short_fuel"] and not r["short_sup"]]
    std_l  = [r for r in resultados if r["long_std"]   and not r["long_fuel"]]
    std_s  = [r for r in resultados if r["short_std"]  and not r["short_fuel"]]

    lineas = [
        f"QF×JP v4.2{modo_str} — {ahora}",
        f"BTC {signo}{btc_cambio:.2f}% | proximo scan {intervalo//60}min",
        f"W/L: {wr_str} | PnL dia: ${pnl_acumulado_dia:+.2f} | Racha: {racha_perdidas}{cb_str}{skip_str}",
        f"{'─'*24}",
    ]

    if not resultados:
        lineas.append("Sin senales")
        return "
".join(lineas)

    for lst, etiqueta in [
        (sup_l,  "LONG SUP"),  (sup_s,  "SHORT SUP"),
        (fuel_l, "LONG FUEL"), (fuel_s, "SHORT FUEL"),
    ]:
        if lst:
            lineas.append(f"{etiqueta} ({len(lst)}):")
            for r in lst[:3]:
                sc = r["comp_long"] if "LONG" in etiqueta else r["comp_short"]
                lineas.append(f"  {r['simbolo'].replace('-USDT','')} {sc}/100")

    if std_l or std_s:
        lineas.append(f"STD ({len(std_l)}L/{len(std_s)}S):")
        for r, d in ([(r, "L") for r in std_l[:2]] + [(r, "S") for r in std_s[:2]]):
            sc = r["comp_long"] if d == "L" else r["comp_short"]
            lineas.append(f"  {d} {r['simbolo'].replace('-USDT','')} {sc}/100")

    if trades_abiertos:
        lineas += [f"{'─'*24}", f"Trades abiertos ({len(trades_abiertos)}):"]
        for sym, t in trades_abiertos.items():
            lineas.append(
                f"  {sym.replace('-USDT','')} {t['direccion']} "
                f"SL:{t['sl']} TP:{t.get('tp_desc', t['tp'])}"
            )

    return "
".join(lineas)

# ══════════════════════════════════════════════════════════════════════
# LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════════════════
def ejecutar():
    global pnl_acumulado_dia

    modo = "DRY RUN" if DRY_RUN else ("AUTO TRADE ON" if AUTO_TRADE else "SOLO ALERTAS")
    _estado["modo"] = modo

    log.info(f"QF×JP Scanner v4.2 — {modo}")
    log.info(f"  ${TRADE_USDT}x{LEVERAGE} | SL {SL_PCT}% | TP {TP_PCT}% | Max {MAX_TRADES} trades")
    log.info(f"  STD auto-trade: {'ON' if STD_AUTOTRADE else 'OFF'}")
    log.info(f"  Scores: STD={SC_MIN_STD} FUEL={SC_MIN_FUEL} SUP={SC_MIN_SUP}")

    if BINGX_API_KEY:
        bal = get_balance()
        _estado["balance"] = bal
        log.info(f"  Balance inicial: ${bal:.2f} USDT")

    enviar_telegram(
        f"*QF×JP Scanner v4.2 iniciado*
"
        f"Modo: *{modo}*
"
        f"Config: ${TRADE_USDT} x {LEVERAGE}x | SL {SL_PCT}% | TP {TP_PCT}%
"
        f"STD auto-trade: {'SI' if STD_AUTOTRADE else 'NO'} | Max trades: {MAX_TRADES}
"
        f"CB: {CB_MAX_LOSSES} perd -> {CB_PAUSA_MIN}min
"
        f"Scores: STD>={SC_MIN_STD} FUEL>={SC_MIN_FUEL} SUP>={SC_MIN_SUP}"
    )

    ultima_hora = -1
    ultimo_dia  = -1
    btc_cambio  = 0.0
    intervalo   = INT_NORMAL

    while True:
        ahora = datetime.now(timezone.utc)

        if ahora.day != ultimo_dia:
            if ultimo_dia != -1:
                enviar_telegram(
                    f"*Resumen diario*
"
                    f"PnL: `${pnl_acumulado_dia:+.2f}` USDT
"
                    f"W/L: {_estado['wins']}/{_estado['losses']}"
                )
                pnl_acumulado_dia = 0.0
                _estado["wins"] = _estado["losses"] = 0
            ultimo_dia = ahora.day

        try:
            actualizar_trades()
            resultados, intervalo, btc_cambio = escanear_mercado()

            for par in resultados:
                sym = par["simbolo"]

                es_sup_fuel = par["long_sup"] or par["short_sup"] or                               par["long_fuel"] or par["short_fuel"]
                es_std      = (par["long_std"] and not par["long_fuel"]) or                               (par["short_std"] and not par["short_fuel"])
                accionable  = es_sup_fuel or (es_std and STD_AUTOTRADE)

                if not accionable:
                    continue

                if time.time() - alertas_enviadas.get(sym, 0) < ALERTA_COOLDOWN:
                    pass
                else:
                    msg = construir_alerta(par)
                    if enviar_telegram(msg):
                        alertas_enviadas[sym] = time.time()

                if (AUTO_TRADE or DRY_RUN) and sym not in trades_abiertos:
                    if par["long_sup"] or par["long_fuel"] or (par["long_std"] and STD_AUTOTRADE):
                        trade = abrir_trade(sym, par["precio"], "LONG")
                        if trade:
                            enviar_telegram(
                                f"{'[DRY] ' if DRY_RUN else ''}*LONG ABIERTO*: "
                                f"{sym.replace('-USDT','')}
"
                                f"Entrada: `{trade['entrada']}` "
                                f"SL: `{trade['sl']}` TP: `{trade.get('tp_desc', trade['tp'])}`
"
                                f"Qty: `{trade['qty']}` | ${trade['usdt']}x{LEVERAGE}x"
                            )
                    elif par["short_sup"] or par["short_fuel"] or (par["short_std"] and STD_AUTOTRADE):
                        trade = abrir_trade(sym, par["precio"], "SHORT")
                        if trade:
                            enviar_telegram(
                                f"{'[DRY] ' if DRY_RUN else ''}*SHORT ABIERTO*: "
                                f"{sym.replace('-USDT','')}
"
                                f"Entrada: `{trade['entrada']}` "
                                f"SL: `{trade['sl']}` TP: `{trade.get('tp_desc', trade['tp'])}`
"
                                f"Qty: `{trade['qty']}` | ${trade['usdt']}x{LEVERAGE}x"
                            )

            if ahora.hour != ultima_hora:
                enviar_telegram(construir_resumen(resultados, btc_cambio, intervalo))
                ultima_hora = ahora.hour

        except Exception as e:
            log.error(f"Error en ciclo principal: {e}", exc_info=True)
            enviar_telegram(f"*Error en scanner*
`{str(e)[:200]}`")
            intervalo = INT_NORMAL

        log.info(f"Proximo escaneo en {intervalo}s ({intervalo//60}min)")
        time.sleep(intervalo)


# ══════════════════════════════════════════════════════════════════════
# MARKETSCANNER — adaptador async para main.py v5.6
# Resuelve: ImportError: cannot import name MarketScanner from scanner
# ══════════════════════════════════════════════════════════════════════
class MarketScanner:
    """
    Interfaz async compatible con scanner_loop() de main.py.
    Reutiliza get_tickers() y los filtros de escanear_mercado()
    sin duplicar logica ni modificar el funcionamiento standalone.
    """
    def __init__(self, exchange=None):
        self.exchange = exchange  # BingXClient pasado desde main.py (no usado aqui)

    async def get_tradeable_symbols(self) -> list:
        loop    = _asyncio.get_event_loop()
        tickers = await loop.run_in_executor(None, get_tickers)

        symbols = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            if sym in BLACKLIST:
                continue
            if any(x in sym for x in ("USDC", "BUSD", "TUSD", "DAI", "FDUSD")):
                continue
            try:
                vol24 = float(t.get("quoteVolume", 0))
            except Exception:
                continue
            if vol24 < VOL_MIN_USDT:
                continue
            symbols.append(sym)

        return symbols


if __name__ == "__main__":
    ejecutar()
