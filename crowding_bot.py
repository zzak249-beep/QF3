"""
crowding_bot.py — bot de SEÑALES del posicionamiento amontonado.

═══════════════════════════════════════════════════════════════════════
v3.0 — POR QUÉ CAMBIA (revisión de 393 ops fade, 18-30 sept 2026)
═══════════════════════════════════════════════════════════════════════
v2.x fade: -0.344 R/op neto, -0.175 R/op BRUTO. 56% de salidas por stop.
La dirección no era el problema principal: desde la entrada, el precio iba
+54 pb a 8 h y +101 pb a 24 h a favor, pero el stop de 1,5 ATR y la salida
a 4 h cortaban justo antes. Cambios:
  - GATILLO=ninguno: entra al cierre de la vela en que se cumple la
    condición (sin esperar vela en contra ni score de cuerpo).
  - SL_ATR 4.0 (stop de catástrofe), TP_R 0 (sin objetivo), sin trail.
  - MAX_BARS 32 (8 h en 15m). ENFRIA_BARRAS 32.
  - MAX_COST_R 0.08 (con SL 4 ATR equivale a ATR >= ~0,8%).
  - Anota r_4h (R a mitad de camino) para comparar horizontes gratis.
  - CSV nuevo (crowding_ops_v3.csv) para no mezclar con v2.
Ojo: en el panel, deduplicando y con 25 pb de coste, la ventaja a 8 h es
~0 (t≈0,5). Esto es un experimento con datos NUEVOS, no una mejora probada.

NO OPERA. NO PIDE CLAVES DE API. Usa solo endpoints públicos de BingX, así
que no puede tocar tu cuenta ni por error. Su único trabajo es generar
señales y MEDIRSE A SÍ MISMO.

═══════════════════════════════════════════════════════════════════════
MEJORAS DE VELOCIDAD Y PRECISIÓN (v2)
═══════════════════════════════════════════════════════════════════════
- premiumIndex de TODOS los símbolos en UNA sola llamada (ahorra ~300 req).
- requests.Session con connection pooling.
- ThreadPoolExecutor (MAX_WORKERS) para OI + klines en paralelo.
- Evaluación y actualización de estado en serie (sin race conditions).
- Rate limit BingX market data: 500 req / 10 s por IP. Con 20 workers
  se mantiene cómodamente por debajo.
- Cadencia típica: de ~9,4 min → ~2-3 min (depende de MAX_SYMBOLS).

═══════════════════════════════════════════════════════════════════════
LO QUE MIDE Y POR QUÉ ASÍ
═══════════════════════════════════════════════════════════════════════
Cada señal abre una operación VIRTUAL con stop y objetivo, y el bot la
sigue hasta que toca uno o se agota el tiempo. Apunta el resultado en R,
con el coste descontado. Sin eso, a los 15 días tendrías una lista de
señales y cero respuestas.

Convención: si en la misma vela el precio toca stop y objetivo, se cuenta
STOP. No sabemos el orden dentro de la vela y equivocarse al revés es
justo lo que infla los backtests.

═══════════════════════════════════════════════════════════════════════
CUÁNTA MUESTRA HACE FALTA (para que no te engañes al leerlo)
═══════════════════════════════════════════════════════════════════════
Con desviación típica de 1R por operación, para distinguir del ruido:

    ventaja 0.50 R/op  ->    31 operaciones
    ventaja 0.30 R/op  ->    87 operaciones
    ventaja 0.20 R/op  ->   196 operaciones
    ventaja 0.10 R/op  ->   784 operaciones

En 15 días, escaneando todos los perpetuos, deberías rondar las 300-500.
Eso solo alcanza para detectar una ventaja GRANDE. Si sale +0.08 R/op,
la respuesta correcta no es "funciona poco": es "no lo sé todavía".

Y ojo con la correlación: en un desplome las alts se mueven juntas, así
que 500 señales del mismo día valen como muchas menos. Por eso el informe
separa el resultado por día y avisa si un solo día domina el total.

═══════════════════════════════════════════════════════════════════════
CALENTAMIENTO
═══════════════════════════════════════════════════════════════════════
Los z-score de basis y open interest se calculan contra la historia que
el propio bot va acumulando; BingX no sirve histórico de OI. Necesita
unos 3 días antes de emitir nada. Durante ese tiempo dirá "calentando" y
es correcto que no haga nada.
"""
from __future__ import annotations

import bisect
import csv
import json
import logging
import math
import os
import signal
import statistics
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import confirm as cf
import panel as pn

VERSION = "3.0"   # ÚNICA fuente de versión: log, Telegram y User-Agent

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,          # stderr hace que Railway pinte todo en rojo como si fueran errores
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("crowding")

_ultimo_ciclo = 0.0
_ultimo_heartbeat = 0.0

BASE = "https://open-api.bingx.com"
UA = {"User-Agent": f"crowding-signal-bot/{VERSION}"}

# Session reutilizable + pool grande (evita "Connection pool is full")
SESSION = requests.Session()
SESSION.headers.update(UA)
_adapter = HTTPAdapter(
    pool_connections=40,
    pool_maxsize=40,
    max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504]),
)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)


def env(k, d):
    v = os.getenv(k)
    if v is None:
        return d
    try:
        if isinstance(d, bool):
            return str(v).strip().lower() in ("1", "true", "yes", "si", "sí", "on")
        if isinstance(d, int):
            return int(float(v))
        if isinstance(d, float):
            return float(v)
    except ValueError:
        log.warning("%s='%s' ilegible, se usa %r", k, v, d)
        return d
    return v


CFG = {
    "TIMEFRAME": env("TIMEFRAME", "15m"),
    "SCAN_SEC": env("SCAN_SEC", 120),          # bajado: el trabajo es mucho más rápido
    # EN HORAS, NO EN MUESTRAS. El bot toma una muestra por ciclo, y la
    # duración del ciclo depende de cuántos símbolos escanee.
    "HIST_HORAS": env("HIST_HORAS", 168.0),   # retención (7 días)
    "MIN_HORAS": env("MIN_HORAS", 30.0),      # antes de emitir
    "MIN_MUESTRAS": env("MIN_MUESTRAS", 200), # y además esta cuenta mínima
    "OI_LOOK_H": env("OI_LOOK_H", 6.0),       # ventana de variación de OI
    "Z_BASIS": env("Z_BASIS", 2.2),
    "Z_OI": env("Z_OI", 1.2),
    "EXT_PCT": env("EXT_PCT", 85.0),
    "ATR_LEN": env("ATR_LEN", 14),
    # v3.0: stop de catástrofe, sin objetivo, salida por tiempo a 8 h
    "SL_ATR": env("SL_ATR", 4.0),
    "TP_R": env("TP_R", 0.0),                 # 0 = sin objetivo
    "MAX_BARS": env("MAX_BARS", 32),
    "MODE": env("MODE", "fade"),
    # ninguno = entra al cierre de la vela con condición | vela = gatillo v2
    "GATILLO": env("GATILLO", "ninguno"),
    "CUERPO_MIN": env("CUERPO_MIN", 0.50),
    "DISP_ATR_MIN": env("DISP_ATR_MIN", 0.60),
    "USE_DECIL": env("USE_DECIL", True),
    "DECIL_SHORT_MIN": env("DECIL_SHORT_MIN", 8),
    "DECIL_LONG_MAX": env("DECIL_LONG_MAX", 1),
    # Funding: momentum tolera sesgo sano; extrema (>0.05%/8h) huele a squeeze
    "FUNDING_MAX_ABS": env("FUNDING_MAX_ABS", 0.05),
    "REQUIRE_FUNDING_ALIGN": env("REQUIRE_FUNDING_ALIGN", True),
    # Anticorprelación: máx señales por ciclo (mismo régimen de mercado)
    "MAX_SENALES_CICLO": env("MAX_SENALES_CICLO", 3),
    # Trail virtual tras 1R a favor
    "TRAIL_AFTER_R": env("TRAIL_AFTER_R", 0.0),   # 0 = sin trail
    "TRAIL_ATR": env("TRAIL_ATR", 1.0),
    # Score mínimo (0–5): z + decil + cuerpo + funding + OI
    "SCORE_MIN": env("SCORE_MIN", 0),
    # MIN_ATR_PCT=1.0 en 15m exige ~200% de volatilidad anualizada: lo pasaban
    # solo las microcaps de lotería, y en este universo NINGUNA. El filtro
    # además DUPLICA a MAX_COST_R con peor criterio: con COST_PCT=0.25 y
    # SL_ATR=1.5, exigir coste<=0.20R ya obliga a ATR>=0.83%. Se deja en 0 y
    # manda el coste, que es el criterio económico.
    "MIN_ATR_PCT": env("MIN_ATR_PCT", 0.0),
    # Techo NUEVO. Sin él entraban símbolos con ATR del 11% por vela de 15m:
    # el stop queda a 17% y el objetivo a 35%, inalcanzable en 16 velas. Ahí
    # el coste en R sale ridículo y parece bueno, pero es el síntoma de que
    # el stop está absurdamente lejos.
    "MAX_ATR_PCT": env("MAX_ATR_PCT", 8.0),
    # ── LA CORRECCIÓN DEL DISPARO ──────────────────────────────────────
    # El bot mira cada ~2 min pero las velas son de 15m: evaluaba la vela EN
    # CURSO unas 7 veces antes de que cerrara, y bastaba con que UNO de esos
    # vistazos pillara un retroceso para disparar el corto aunque la vela
    # acabara VERDE. Simulado con esta misma cerilla:
    #   vela alcista fuerte -> dispara intravela 68,7% · ya cerrada 33,4%
    #   vela neutra         -> dispara intravela 79,4% · ya cerrada 50,8%
    "SOLO_VELA_CERRADA": env("SOLO_VELA_CERRADA", True),
    # No se vende un símbolo que sigue marcando máximos: eso no es la
    # multitud soltando, es la multitud empujando con un retroceso dentro.
    "NO_NUEVO_EXTREMO": env("NO_NUEVO_EXTREMO", 6),
    # Sin enfriamiento, un rally reparte señales durante horas y todas son
    # la misma apuesta contada como si fueran independientes.
    "ENFRIA_BARRAS": env("ENFRIA_BARRAS", 32),
    "COST_PCT": env("COST_PCT", 0.25),
    "MAX_COST_R": env("MAX_COST_R", 0.08),
    "MIN_VOL_24H": env("MIN_VOL_24H", 5_000_000.0),
    "MAX_SYMBOLS": env("MAX_SYMBOLS", 150),
    "STATE": env("STATE", "/data/crowding_state.json"),
    "CSV": env("CSV", "/data/crowding_ops_v3.csv"),
    "R_MITAD_BARRAS": env("R_MITAD_BARRAS", 16),   # barra en la que se anota r_4h
    "TG_TOKEN": env("TG_TOKEN", ""),
    "TG_CHAT": env("TG_CHAT", ""),
    "REPORT_HOUR": env("REPORT_HOUR", 7),
    "TG_SIGNALS": env("TG_SIGNALS", False),
    "TG_CLOSES": env("TG_CLOSES", False),
    # El volumen de Railway no tiene navegador de archivos; sendDocument sí.
    "CSV_CON_INFORME": env("CSV_CON_INFORME", True),
    "CSV_AL_ARRANCAR": env("CSV_AL_ARRANCAR", False),
    "PACING": env("PACING", 0.0),             # 0 = sin sleep extra (el pool controla)
    "MAX_WORKERS": env("MAX_WORKERS", 20),
    # Sin esto el tamaño de la historia lo fija SCAN_SEC: a cadencia 2,2 min
    # y 168 h de retención salen 4.580 puntos por símbolo, unos 80 MB de
    # estado escritos en CADA ciclo. Con 300 s la historia mide lo mismo.
    "MUESTRA_MIN_SEG": env("MUESTRA_MIN_SEG", 300.0),
    "MAX_PUNTOS": env("MAX_PUNTOS", 2200),    # paralelismo seguro bajo el rate limit
    # 120 no alcanzaba para confirm.py: pedía 120 retornos y le llegaban 99,
    # así que conf_regimen salía siempre "pocas velas (99)".
    "KLINES_LIMIT": env("KLINES_LIMIT", 260),
    # Panel transversal: ver panel.py. No decide nada, solo apunta.
    "PANEL_ENABLED": env("PANEL_ENABLED", True),
    "PANEL_CSV": env("PANEL_CSV", "/data/crowding_panel.csv"),
    "PANEL_CADA_SEG": env("PANEL_CADA_SEG", 900.0),
    "PANEL_MIN_SIMBOLOS": env("PANEL_MIN_SIMBOLOS", 30),
    # Régimen: se APUNTA, no decide.
    "CONFIRM_ENABLED": env("CONFIRM_ENABLED", True),
    "CONFIRM_BLOQUEAR": False,
    "CONFIRM_Q": env("CONFIRM_Q", 8),
    "CONFIRM_WIN": env("CONFIRM_WIN", 240),
    "CONFIRM_LAMBDA": env("CONFIRM_LAMBDA", 0.985),
    "CONFIRM_Z": env("CONFIRM_Z", 1.5),
    "CONFIRM_MIN_VELAS": env("CONFIRM_MIN_VELAS", 120),
}

TF_MS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
BAR_SEC = TF_MS.get(str(CFG["TIMEFRAME"]), 900)


# ─────────────────────────────────────────────────────── API pública
def _get(path: str, params: dict | None = None, intentos: int = 3):
    for i in range(intentos):
        try:
            r = SESSION.get(BASE + path, params=params or {}, timeout=12)
            r.raise_for_status()
            j = r.json()
            if str(j.get("code", 0)) not in ("0", "None"):
                log.debug("%s devolvió code=%s", path, j.get("code"))
                return None
            return j.get("data")
        except Exception as e:
            if i == intentos - 1:
                log.debug("%s falló: %s", path, e)
            time.sleep(0.4 * (i + 1) + 0.1 * (i + 1) ** 2)  # backoff + jitter
    return None


def contratos() -> list[str]:
    d = _get("/openApi/swap/v2/quote/contracts")
    if not d:
        return []
    out = []
    for c in d:
        s = c.get("symbol", "")
        if s.endswith("-USDT") and str(c.get("status", 1)) in ("1", "True", "true"):
            out.append(s)
    return out


def volumenes() -> dict:
    d = _get("/openApi/swap/v2/quote/ticker")
    if not d:
        return {}
    out = {}
    for t in d:
        try:
            out[t.get("symbol")] = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            pass
    return out


def premium_todos() -> dict[str, dict]:
    """
    Una sola llamada para TODOS los símbolos.
    Devuelve {symbol: {"mark", "index", "basis", "funding"}}
    """
    d = _get("/openApi/swap/v2/quote/premiumIndex")
    if not d:
        return {}
    if isinstance(d, dict):
        d = [d]
    out = {}
    for item in d:
        try:
            s = item.get("symbol")
            mark = float(item.get("markPrice") or 0)
            index = float(item.get("indexPrice") or 0)
            fr = float(item.get("lastFundingRate") or 0)
            if not s or mark <= 0 or index <= 0:
                continue
            out[s] = {
                "mark": mark,
                "index": index,
                "basis": (mark - index) / index * 100.0,
                "funding": fr * 100.0,
            }
        except (TypeError, ValueError, KeyError):
            continue
    return out


def open_interest(symbol: str):
    d = _get("/openApi/swap/v2/quote/openInterest", {"symbol": symbol})
    if isinstance(d, list):
        d = d[0] if d else None
    if not isinstance(d, dict):
        return None
    try:
        v = float(d.get("openInterest") or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def klines(symbol: str, limit: int | None = None):
    lim = limit or int(CFG["KLINES_LIMIT"])
    d = _get("/openApi/swap/v3/quote/klines",
             {"symbol": symbol, "interval": CFG["TIMEFRAME"], "limit": lim})
    if not d:
        d = _get("/openApi/swap/v2/quote/klines",
                 {"symbol": symbol, "interval": CFG["TIMEFRAME"], "limit": lim})
    if not d:
        return []
    filas = []
    for k in d:
        try:
            if isinstance(k, dict):
                filas.append({"t": int(k["time"]), "o": float(k["open"]), "h": float(k["high"]),
                              "l": float(k["low"]), "c": float(k["close"])})
            else:
                filas.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                              "l": float(k[3]), "c": float(k[4])})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    filas.sort(key=lambda x: x["t"])
    return filas


def _fetch_symbol_raw(symbol: str) -> dict | None:
    """Worker: solo descarga OI + klines. Sin tocar estado compartido."""
    try:
        oi = open_interest(symbol)
        velas = klines(symbol)
        if oi is None or not velas:
            return None
        return {"oi": oi, "velas": velas}
    except Exception:
        log.debug("fetch falló %s", symbol, exc_info=True)
        return None


# ─────────────────────────────────────────────────────── indicadores
def atr(velas, n):
    if len(velas) < n + 1:
        return 0.0
    trs = []
    for i in range(len(velas) - n, len(velas)):
        h, l, cp = velas[i]["h"], velas[i]["l"], velas[i - 1]["c"]
        trs.append(max(h - l, abs(h - cp), abs(l - cp)))
    return sum(trs) / len(trs) if trs else 0.0


def percentil(valores, x):
    if not valores:
        return 50.0
    return sum(1 for v in valores if v <= x) / len(valores) * 100.0


def zscore(hist, x):
    if len(hist) < 30:
        return None
    mu = statistics.fmean(hist)
    sd = statistics.pstdev(hist)
    return (x - mu) / sd if sd > 1e-12 else 0.0


# ─────────────────────────────────────────────────────── estado
@dataclass
class Virtual:
    symbol: str
    lado: str
    abierta_ts: int
    entrada: float
    sl: float
    tp: float
    riesgo: float
    coste_r: float
    basis_z: float
    oi_z: float
    funding: float
    atr_pct: float
    conf_z: float = 0.0
    conf_regimen: str = "sin datos"
    barras: int = 0
    # Excursión máxima a favor y en contra, en R. Contesta gratis si TP_R=2
    # es el objetivo correcto: si el MFE medio de las perdedoras ronda 1.5R,
    # el objetivo está demasiado lejos y lo estás devolviendo.
    mfe: float = 0.0
    mae: float = 0.0
    # R bruto al cierre de la barra R_MITAD_BARRAS (4 h en 15m). Compara
    # horizontes 4 h vs 8 h con las mismas señales, sin otro experimento.
    r_4h: float | None = None


def _podar(h: deque, ahora: float, horas: float):
    """Tira lo más viejo que la ventana de retención."""
    limite = ahora - horas * 3600.0
    while h and h[0][0] < limite:
        h.popleft()


def _valor_hace(h: deque, ahora: float, horas: float):
    """Valor de hace ~N horas: la muestra más cercana a ese instante."""
    if not h:
        return None
    objetivo = ahora - horas * 3600.0
    if h[0][0] > objetivo:
        return None
    mejor = min(h, key=lambda p: abs(p[0] - objetivo))
    return mejor[1]


def _cambios_oi(lo: list, look_h: float) -> list[float]:
    """O(n log n). La versión con deque(lo[:i+1]) costaba 565 ms por símbolo
    con 4000 puntos, o sea ~170 s de CPU por ciclo con 300 símbolos."""
    ts = [p[0] for p in lo]
    look = look_h * 3600.0
    out: list[float] = []
    for i in range(len(lo)):
        objetivo = ts[i] - look
        if ts[0] > objetivo:
            continue
        j = bisect.bisect_left(ts, objetivo, 0, i + 1)
        mejor = min((k for k in (j - 1, j) if 0 <= k <= i),
                    key=lambda k: abs(ts[k] - objetivo), default=None)
        if mejor is None:
            continue
        ref = lo[mejor][1]
        if ref > 0:
            out.append((lo[i][1] - ref) / ref * 100.0)
    return out


def _apuntar(h: deque, ahora: float, valor: float, min_seg: float, tope: int):
    """
    Guarda solo si han pasado min_seg desde la última. El valor ACTUAL no se
    pierde: se usa siempre para el z, se guarde o no. Lo único que se
    controla aquí es cuánta historia se acumula.
    """
    if h and (ahora - h[-1][0]) < min_seg:
        return
    h.append((ahora, valor))
    while len(h) > tope:
        h.popleft()


def _span_horas(h: deque) -> float:
    return (h[-1][0] - h[0][0]) / 3600.0 if len(h) > 1 else 0.0


class Estado:
    def __init__(self, path):
        self.path = path
        self.basis: dict[str, deque] = {}
        self.oi: dict[str, deque] = {}
        self.abiertas: dict[str, Virtual] = {}
        self.ultimo_informe = ""
        self._cargar()

    def _cargar(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            ahora = time.time()
            paso = float(CFG["SCAN_SEC"]) + 60.0
            migradas = 0

            def _cargar(bloque):
                nonlocal migradas
                out = {}
                for k, v in bloque.items():
                    dq = deque()
                    if v and not isinstance(v[0], (list, tuple)):
                        migradas += 1
                        base = ahora - len(v) * paso
                        for i, x in enumerate(v):
                            dq.append((base + i * paso, float(x)))
                    else:
                        for p in v:
                            dq.append((float(p[0]), float(p[1])))
                    out[k] = dq
                return out

            self.basis = _cargar(d.get("basis", {}))
            self.oi = _cargar(d.get("oi", {}))
            if migradas:
                log.warning("Migradas %d series del formato antiguo (sin marca de "
                            "tiempo): los tiempos son estimados", migradas)
            self.abiertas = {k: Virtual(**v) for k, v in d.get("abiertas", {}).items()}
            self.ultimo_informe = d.get("ultimo_informe", "")
            log.info("Estado cargado: %d símbolos con historia, %d virtuales abiertas",
                     len(self.basis), len(self.abiertas))
        except FileNotFoundError:
            log.info("Sin estado previo, empezando de cero")
        except Exception:
            log.exception("Estado ilegible, se empieza de cero")

    def guardar(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "basis": {k: [list(p) for p in v] for k, v in self.basis.items()},
                    "oi": {k: [list(p) for p in v] for k, v in self.oi.items()},
                    "abiertas": {k: asdict(v) for k, v in self.abiertas.items()},
                    "ultimo_informe": self.ultimo_informe,
                }, f)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("No se pudo guardar el estado")


class _Cfg:
    """Puente para que confirm.py lea CFG con getattr, sin config.py."""

    def __getattr__(self, k):
        if k in CFG:
            return CFG[k]
        raise AttributeError(k)


_CFG_OBJ = _Cfg()

COLS = ["cerrada_utc", "symbol", "lado", "abierta_utc", "entrada", "salida",
        "motivo", "r_bruto", "coste_r", "r_neto", "barras", "basis_z",
        "oi_z", "funding", "atr_pct", "conf_z", "conf_regimen", "mfe", "mae",
        "r_4h", "version"]


def anotar(fila: dict):
    try:
        ruta = CFG["CSV"]
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        nuevo = not os.path.exists(ruta) or os.path.getsize(ruta) == 0
        with open(ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
            if nuevo:
                w.writeheader()
            w.writerow(fila)
    except Exception:
        log.exception("No se pudo anotar en el CSV")


def tg(texto: str, tipo: str = "informe"):
    """tipo: 'senal' | 'cierre' | 'informe'. Los dos primeros son opcionales."""
    if tipo == "senal" and not CFG["TG_SIGNALS"]:
        return
    if tipo == "cierre" and not CFG["TG_CLOSES"]:
        return
    if not CFG["TG_TOKEN"] or not CFG["TG_CHAT"]:
        log.info("[telegram] %s", texto.replace("\n", " | ")[:300])
        return
    try:
        requests.post(f"https://api.telegram.org/bot{CFG['TG_TOKEN']}/sendMessage",
                      json={"chat_id": CFG["TG_CHAT"], "text": texto,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)
    except Exception:
        log.exception("Telegram falló")


def enviar_csv(motivo: str = "") -> bool:
    """Manda un CSV por Telegram como archivo. Nunca lanza."""
    for ruta, nombre in ((CFG["CSV"], "crowding_ops.csv"),
                         (CFG.get("PANEL_CSV", "/data/crowding_panel.csv"), "crowding_panel.csv")):
        try:
            if not os.path.exists(ruta) or os.path.getsize(ruta) < 100:
                continue
            if not CFG["TG_TOKEN"] or not CFG["TG_CHAT"]:
                log.warning("Sin Telegram: el CSV está en %s", ruta)
                return False
            tam = os.path.getsize(ruta)
            if tam > 48 * 1024 * 1024:
                log.warning("%s pesa %.0f MB: Telegram no lo admite", nombre, tam / 1e6)
                continue
            with open(ruta, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{CFG['TG_TOKEN']}/sendDocument",
                    data={"chat_id": CFG["TG_CHAT"],
                          "caption": f"{nombre} · {tam/1024:.0f} KB{(' · ' + motivo) if motivo else ''}"[:1000]},
                    files={"document": (nombre, f, "text/csv")}, timeout=120)
            if r.status_code == 200 and r.json().get("ok") is True:
                log.info("%s enviado (%d bytes)", nombre, tam)
            else:
                log.error("Telegram rechazó %s: %s %s", nombre, r.status_code, r.text[:200])
        except Exception:
            log.exception("No se pudo enviar %s", nombre)
    return True


# ─────────────────────────────────────────────────────── lógica
# Amplitud de los símbolos que SÍ se amontonaron. Es lo que permite fijar el
# umbral con datos en vez de con una corazonada.
ATR_AMONT: list[float] = []

# Última barra con señal por símbolo, para el enfriamiento.
ULTIMA_SENAL: dict[str, int] = {}


def muestrear(symbol: str, p: dict | None, oi: float | None, st: Estado):
    """Apunta basis y OI en la historia. Se llama SIEMPRE (también con virtual abierta
    o con el tope de señales alcanzado): si no, la historia del z-score tiene huecos."""
    if p is None or oi is None or oi <= 0:
        return
    ahora = time.time()
    hb = st.basis.setdefault(symbol, deque())
    ho = st.oi.setdefault(symbol, deque())
    _apuntar(hb, ahora, p["basis"], float(CFG["MUESTRA_MIN_SEG"]), int(CFG["MAX_PUNTOS"]))
    _apuntar(ho, ahora, oi, float(CFG["MUESTRA_MIN_SEG"]), int(CFG["MAX_PUNTOS"]))
    horas_ret = float(CFG["HIST_HORAS"])
    _podar(hb, ahora, horas_ret)
    _podar(ho, ahora, horas_ret)


def velas_cerradas(velas: list) -> list:
    if velas and velas[-1]["t"] + BAR_SEC * 1000 > time.time() * 1000:
        return velas[:-1]
    return velas


def evaluar(symbol: str, velas: list, p: dict, oi: float, st: Estado,
            acc: list | None = None):
    """
    Devuelve (senal|None, motivo).
    senal = ('LONG'|'SHORT', datos).
    p = dict de premium (mark/index/basis/funding)
    oi = open interest actual
    """
    # La vela EN CURSO no decide nada. Todo el criterio va sobre cerradas.
    if bool(CFG["SOLO_VELA_CERRADA"]) and velas:
        if velas[-1]["t"] + BAR_SEC * 1000 > time.time() * 1000:
            velas = velas[:-1]

    if len(velas) < 60:
        return None, "pocas velas"
    if p is None:
        return None, "sin premiumIndex"
    if oi is None or oi <= 0:
        return None, "sin open interest"

    ahora = time.time()
    muestrear(symbol, p, oi, st)
    hb = st.basis[symbol]
    ho = st.oi[symbol]

    span = _span_horas(hb)
    min_h = float(CFG["MIN_HORAS"])
    min_n = int(CFG["MIN_MUESTRAS"])
    if span < min_h or len(hb) < min_n:
        return None, f"calentando ({span:.1f}/{min_h:.0f}h, {len(hb)}/{min_n})"

    look_h = float(CFG["OI_LOOK_H"])
    oi_prev = _valor_hace(ho, ahora, look_h)
    if oi_prev is None or oi_prev <= 0:
        return None, "sin ventana de OI"
    oi_chg = (oi - oi_prev) / oi_prev * 100.0

    cambios = _cambios_oi(list(ho), look_h)
    zb = zscore([x[1] for x in hb], p["basis"])
    zo = zscore(cambios, oi_chg)
    if zb is None or zo is None:
        return None, "z no calculable"

    cierres = [v["c"] for v in velas[-100:]]
    px = velas[-1]["c"]
    pr = percentil(cierres, px)
    cierres_reg = [v["c"] for v in velas[-(int(CFG["CONFIRM_WIN"]) + 1):]]

    a = atr(velas, int(CFG["ATR_LEN"]))
    atr_pct = a / px * 100.0 if px > 0 else 0.0
    riesgo = float(CFG["SL_ATR"]) * a
    coste_r = (float(CFG["COST_PCT"]) / 100.0 * px) / riesgo if riesgo > 0 else 99.0

    # Aquí es donde el bot tiraba 299 de cada 300 cálculos. Ahora se apuntan
    # todos: es la misma información, y multiplica por 5.000 la muestra.
    if acc is not None:
        acc.append({"symbol": symbol, "px": px, "basis_z": zb, "oi_z": zo,
                    "pct_precio": pr, "atr_pct": atr_pct,
                    "funding": p["funding"], "coste_r": coste_r})

    largos_amont = zb >= float(CFG["Z_BASIS"]) and zo >= float(CFG["Z_OI"]) and pr >= float(CFG["EXT_PCT"])
    cortos_amont = zb <= -float(CFG["Z_BASIS"]) and zo >= float(CFG["Z_OI"]) and pr <= (100 - float(CFG["EXT_PCT"]))

    if not (largos_amont or cortos_amont):
        return None, f"sin amontonamiento (zb {zb:.2f})"
    # Se apunta el ATR de TODOS los amontonamientos, pasen o no el filtro. Sin
    # esto, calibrar MIN/MAX_ATR_PCT es adivinar: ahora el log dice qué
    # amplitud tienen de verdad los símbolos que se amontonan.
    ATR_AMONT.append(atr_pct)
    if len(ATR_AMONT) > 5000:
        del ATR_AMONT[:2500]

    if atr_pct < float(CFG["MIN_ATR_PCT"]):
        return None, f"sin amplitud ({atr_pct:.2f}%)"
    if atr_pct > float(CFG["MAX_ATR_PCT"]):
        return None, f"amplitud excesiva ({atr_pct:.2f}%)"
    if coste_r > float(CFG["MAX_COST_R"]):
        return None, f"coste {coste_r:.2f}R"

    ult, ant = velas[-1], velas[-2]
    modo = str(CFG.get("MODE", "momentum")).strip().lower()
    fund = float(p.get("funding") or 0.0)

    nb = int(CFG["ENFRIA_BARRAS"])
    if nb > 0 and (ult["t"] - ULTIMA_SENAL.get(symbol, -10**15)) < nb * BAR_SEC * 1000:
        return None, "enfriamiento"

    # Funding extremo: setup de squeeze, no de continuación limpia
    if abs(fund) > float(CFG.get("FUNDING_MAX_ABS", 0.05)):
        return None, f"funding extremo ({fund:+.4f}%)"

    gatillo = str(CFG.get("GATILLO", "ninguno")).strip().lower()
    if modo == "fade" and gatillo == "ninguno":
        # v3.0: la condición basta; entra al cierre de la última vela cerrada.
        lado = "SHORT" if largos_amont else "LONG"
        score = 0
        if abs(zb) >= float(CFG["Z_BASIS"]) + 0.3:
            score += 1
        if abs(zb) >= 2.5:
            score += 1
        if zo >= float(CFG["Z_OI"]) + 0.5:
            score += 1
        if abs(fund) <= 0.02:
            score += 1
        if score < int(CFG.get("SCORE_MIN", 0)):
            return None, f"score {score}<{int(CFG.get('SCORE_MIN', 0))}"
        ULTIMA_SENAL[symbol] = ult["t"]
        reg = cf.calcular(_CFG_OBJ, cierres_reg)
        return (lado, {"px": px, "riesgo": riesgo, "coste_r": coste_r, "zb": zb,
                       "zo": zo, "funding": fund, "atr_pct": atr_pct,
                       "conf_z": reg.z if reg.ok else 0.0,
                       "conf_regimen": reg.etiqueta if reg.ok else reg.motivo,
                       "mode": f"{modo}/{gatillo}", "score": score,
                       "t_senal": ult["t"]}), "señal"

    if modo == "fade":
        ne = int(CFG["NO_NUEVO_EXTREMO"])
        if ne > 0 and len(velas) > ne + 1:
            maxN = max(v["h"] for v in velas[-ne - 1:-1])
            minN = min(v["l"] for v in velas[-ne - 1:-1])
            if largos_amont and ult["h"] >= maxN:
                return None, "sigue haciendo máximos"
            if cortos_amont and ult["l"] <= minN:
                return None, "sigue haciendo mínimos"

    rng = ult["h"] - ult["l"]
    body = abs(ult["c"] - ult["o"])
    cuerpo_min = float(CFG.get("CUERPO_MIN", 0.5))
    if rng <= 0 or (body / rng) < cuerpo_min:
        return None, "cuerpo débil"
    disp_min = float(CFG.get("DISP_ATR_MIN", 0.6)) * a if a > 0 else 0.0

    lado = None
    if modo == "momentum":
        # A FAVOR del crowding (IC del panel: basis alto → retornos relativos altos)
        if largos_amont and ult["c"] > ant["h"] and ult["c"] > ult["o"]:
            if (ult["c"] - ult["o"]) < disp_min:
                return None, "desplazamiento débil"
            if bool(CFG.get("REQUIRE_FUNDING_ALIGN", True)) and fund < -0.01:
                return None, "funding no alineado (LONG)"
            lado = "LONG"
        elif cortos_amont and ult["c"] < ant["l"] and ult["c"] < ult["o"]:
            if (ult["o"] - ult["c"]) < disp_min:
                return None, "desplazamiento débil"
            if bool(CFG.get("REQUIRE_FUNDING_ALIGN", True)) and fund > 0.01:
                return None, "funding no alineado (SHORT)"
            lado = "SHORT"
        if lado is None:
            return None, "esperando vela a favor"
    else:
        if largos_amont and ult["c"] < ant["l"] and ult["c"] < ult["o"]:
            if (ult["o"] - ult["c"]) < disp_min:
                return None, "desplazamiento débil"
            lado = "SHORT"
        elif cortos_amont and ult["c"] > ant["h"] and ult["c"] > ult["o"]:
            if (ult["c"] - ult["o"]) < disp_min:
                return None, "desplazamiento débil"
            lado = "LONG"
        if lado is None:
            return None, "esperando vela en contra"

    # Score de calidad 0–5 (solo dispara si >= SCORE_MIN)
    score = 0
    if abs(zb) >= float(CFG["Z_BASIS"]) + 0.3:
        score += 1
    if abs(zb) >= 2.5:
        score += 1
    if zo >= float(CFG["Z_OI"]) + 0.5:
        score += 1
    if (body / rng) >= 0.60:
        score += 1
    if abs(fund) <= 0.02:
        score += 1
    if score < int(CFG.get("SCORE_MIN", 3)):
        return None, f"score {score}<{int(CFG.get('SCORE_MIN', 3))}"

    ULTIMA_SENAL[symbol] = ult["t"]
    reg = cf.calcular(_CFG_OBJ, cierres_reg)
    return (lado, {"px": px, "riesgo": riesgo, "coste_r": coste_r, "zb": zb,
                   "zo": zo, "funding": fund, "atr_pct": atr_pct,
                   "conf_z": reg.z if reg.ok else 0.0,
                   "conf_regimen": reg.etiqueta if reg.ok else reg.motivo,
                   "mode": modo, "score": score, "t_senal": ult["t"]}), "señal"


def seguir_virtuales(st: Estado, symbol: str, velas):
    """Cierra las virtuales que tocaron stop, objetivo o se quedaron sin tiempo."""
    v = st.abiertas.get(symbol)
    if v is None:
        return
    # Solo velas CERRADAS posteriores a la vela de la señal. Antes entraba la vela en
    # curso: el trail se calculaba con un cierre provisional y la salida por tiempo
    # usaba un precio que aún no existía.
    nuevas = [k for k in velas_cerradas(velas) if k["t"] > v.abierta_ts]
    if not nuevas:
        return
    # Tras un parón/redeploy pueden llegar más velas que MAX_BARS: se sale en la N.
    nuevas = nuevas[: int(CFG["MAX_BARS"])]
    v.barras = len(nuevas)
    largo = v.lado == "LONG"
    salida = motivo = None
    trail_after = float(CFG.get("TRAIL_AFTER_R", 1.0))
    trail_atr_m = float(CFG.get("TRAIL_ATR", 1.0))
    n_mitad = int(CFG.get("R_MITAD_BARRAS", 16))
    for idx, k in enumerate(nuevas, start=1):
        favor = (k["h"] - v.entrada) if largo else (v.entrada - k["l"])
        contra = (v.entrada - k["l"]) if largo else (k["h"] - v.entrada)
        v.mfe = max(v.mfe, favor / v.riesgo)
        v.mae = max(v.mae, contra / v.riesgo)
        # 1º la salida con el stop VIGENTE; el trail calculado con esta vela solo
        # vale desde la siguiente (antes se movía con el cierre y se comprobaba con
        # el mínimo de la MISMA vela: salidas que en real no habrían pasado).
        toca_sl = (k["l"] <= v.sl) if largo else (k["h"] >= v.sl)
        # tp <= 0 = sin objetivo (v3.0, TP_R=0)
        toca_tp = v.tp > 0 and ((k["h"] >= v.tp) if largo else (k["l"] <= v.tp))
        if toca_sl:
            salida, motivo = v.sl, "stop"
            break
        if toca_tp:
            salida, motivo = v.tp, "objetivo"
            break
        if idx == n_mitad and v.r_4h is None and v.riesgo > 0:
            v.r_4h = ((k["c"] - v.entrada) if largo else (v.entrada - k["c"])) / v.riesgo
        # Trail: tras +1R mueve SL a favor (breakeven+)
        if trail_after > 0 and v.mfe >= trail_after and v.riesgo > 0:
            atr_aprox = v.riesgo / float(CFG["SL_ATR"]) if float(CFG["SL_ATR"]) > 0 else v.riesgo
            if largo:
                nuevo_sl = k["c"] - atr_aprox * trail_atr_m
                if nuevo_sl > v.sl:
                    v.sl = nuevo_sl
            else:
                nuevo_sl = k["c"] + atr_aprox * trail_atr_m
                if nuevo_sl < v.sl:
                    v.sl = nuevo_sl
    if salida is None and v.barras >= int(CFG["MAX_BARS"]):
        salida, motivo = nuevas[-1]["c"], "tiempo"
    if salida is None:
        return

    bruto = ((salida - v.entrada) if largo else (v.entrada - salida)) / v.riesgo
    neto = bruto - v.coste_r
    anotar({
        "cerrada_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": v.symbol, "lado": v.lado,
        "abierta_utc": datetime.fromtimestamp(v.abierta_ts / 1000, timezone.utc).isoformat(timespec="seconds"),
        "entrada": v.entrada, "salida": salida, "motivo": motivo,
        "r_bruto": round(bruto, 4), "coste_r": round(v.coste_r, 4),
        "r_neto": round(neto, 4), "barras": v.barras,
        "basis_z": round(v.basis_z, 3), "oi_z": round(v.oi_z, 3),
        "funding": round(v.funding, 5), "atr_pct": round(v.atr_pct, 3),
        "conf_z": round(v.conf_z, 3), "conf_regimen": v.conf_regimen,
        "mfe": round(v.mfe, 3), "mae": round(v.mae, 3),
        "r_4h": "" if v.r_4h is None else round(v.r_4h, 4),
        "version": VERSION,
    })
    icono = "✅" if neto > 0 else "🔴"
    tg(f"{icono} <b>{v.symbol.split('-')[0]}</b> {v.lado} virtual cerrada por {motivo}: "
       f"<b>{neto:+.2f} R</b> en {v.barras} velas", "cierre")
    st.abiertas.pop(symbol, None)


def informe(st: Estado):
    try:
        with open(CFG["CSV"], encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
    except Exception:
        filas = []
    if not filas:
        return (f"📊 <b>Crowding · informe</b>\nSin operaciones cerradas todavía.\n"
                f"Historia acumulada en {len(st.basis)} símbolos.\n"
                f"<i>Necesita ~200 muestras por símbolo antes de emitir.</i>")
    rs = [float(x["r_neto"]) for x in filas]
    n = len(rs)
    media = statistics.fmean(rs)
    sd = statistics.pstdev(rs) if n > 1 else 1.0
    t = media * math.sqrt(n) / sd if sd > 1e-9 else 0.0
    gan = sum(1 for r in rs if r > 0)
    por_dia: dict[str, float] = {}
    for x in filas:
        d = x["cerrada_utc"][:10]
        por_dia[d] = por_dia.get(d, 0.0) + float(x["r_neto"])
    peor = max(por_dia.items(), key=lambda kv: abs(kv[1])) if por_dia else ("-", 0.0)
    dom = abs(peor[1]) / max(abs(sum(rs)), 1e-9) * 100 if rs else 0

    if n < 31:
        lectura = f"muestra {n}: no alcanza ni para detectar 0.50 R/op"
    elif n < 87:
        lectura = f"muestra {n}: solo detectaría una ventaja ≥0.50 R/op"
    elif n < 196:
        lectura = f"muestra {n}: solo detectaría una ventaja ≥0.30 R/op"
    else:
        lectura = f"muestra {n}: detecta ventajas ≥0.20 R/op"

    r4 = []
    for x in filas:
        try:
            if x.get("r_4h") not in (None, ""):
                r4.append(float(x["r_4h"]) - float(x["coste_r"]))
        except ValueError:
            pass
    L = [f"📊 <b>Crowding v{VERSION} · informe</b>",
         f"<b>{n}</b> operaciones · {gan / n * 100:.0f}% ganadoras",
         f"<b>{media:+.3f} R/op</b> · total {sum(rs):+.1f} R · t={t:.2f}",
         f"<i>{lectura}</i>",
         f"Días con operaciones: {len(por_dia)}"]
    if r4:
        L.append(f"Si hubiera salido a {int(CFG['R_MITAD_BARRAS'])} velas: "
                 f"{statistics.fmean(r4):+.3f} R/op neto (n={len(r4)})")
    if dom > 40:
        L.append(f"⚠️ El día {peor[0]} pesa el {dom:.0f}% del total: son señales "
                 f"correlacionadas, no {n} independientes")
    if abs(t) < 2:
        L.append("Sin significación: <b>esto todavía no dice nada</b>")
    por_reg: dict[str, list] = {}
    for x in filas:
        por_reg.setdefault(x.get("conf_regimen") or "?", []).append(float(x["r_neto"]))
    if len(por_reg) > 1:
        L.append("Por régimen:")
        for k, v in sorted(por_reg.items(), key=lambda kv: -len(kv[1])):
            aviso = " ⚠" if len(v) < 31 else ""
            L.append(f"  {k}: {statistics.fmean(v):+.3f} R/op (n={len(v)}){aviso}")
    por_lado: dict[str, list] = {}
    for x in filas:
        por_lado.setdefault(x.get("lado") or "?", []).append(float(x["r_neto"]))
    if len(por_lado) > 1:
        L.append("Por lado:")
        for k, v in sorted(por_lado.items(), key=lambda kv: -len(kv[1])):
            L.append(f"  {k}: {statistics.fmean(v):+.3f} R/op (n={len(v)})"
                     + (" ⚠" if len(v) < 31 else ""))
    elif por_lado:
        L.append(f"<i>Solo hay {next(iter(por_lado))}: el otro lado del filtro "
                 f"no ha disparado ni una vez. Revisa Z_BASIS y EXT_PCT.</i>")
    por_atr: dict[str, list] = {}
    for x in filas:
        try:
            a = float(x.get("atr_pct") or 0)
        except ValueError:
            continue
        cubo = "ATR <2%" if a < 2 else "ATR 2-5%" if a < 5 else "ATR >5%"
        por_atr.setdefault(cubo, []).append(float(x["r_neto"]))
    if len(por_atr) > 1:
        L.append("Por amplitud:")
        for k, v in sorted(por_atr.items()):
            L.append(f"  {k}: {statistics.fmean(v):+.3f} R/op (n={len(v)})"
                     + (" ⚠" if len(v) < 31 else ""))
    L.append(f"Virtuales abiertas: {len(st.abiertas)}")
    if not CFG["TG_SIGNALS"]:
        L.append("<i>Avisos por señal apagados (TG_SIGNALS). Todo está en el CSV.</i>")
    return "\n".join(L)


# Embudo acumulado desde el arranque. El recuento de un ciclo suelto no dice
# dónde se corta el sistema: aquí se ve que el 100% de los amontonamientos
# moría en el filtro de amplitud, cosa que el log recortado a 3 razones
# escondía.
EMBUDO: dict[str, int] = {}


def ciclo(st: Estado, simbolos: list[str]):
    señales = motivos = 0
    razones: dict[str, int] = {}

    # 1) premiumIndex de TODOS de una sola vez
    premiums = premium_todos()
    if not premiums:
        log.warning("No se pudo obtener premiumIndex global")
        return

    # 2) Descarga paralela de OI + klines
    raw: dict[str, dict] = {}
    workers = max(1, int(CFG["MAX_WORKERS"]))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_symbol_raw, sym): sym for sym in simbolos}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                data = fut.result()
                if data:
                    raw[sym] = data
            except Exception:
                log.debug("Error en worker %s", sym, exc_info=True)

    # 3) Evaluación secuencial (estado compartido seguro)
    acc: list | None = [] if pn.toca(_CFG_OBJ) else None
    for sym in simbolos:
        try:
            data = raw.get(sym)
            if not data:
                continue
            velas = data["velas"]
            oi = data["oi"]
            p = premiums.get(sym)

            seguir_virtuales(st, sym, velas)
            if sym in st.abiertas:
                muestrear(sym, p, oi, st)       # la historia no se corta mientras hay virtual
                continue

            sig, motivo = evaluar(sym, velas, p, oi, st, acc)
            if sig is not None and señales >= int(CFG.get("MAX_SENALES_CICLO", 3)):
                ULTIMA_SENAL.pop(sym, None)     # no abierta: que no cuente para el enfriamiento
                sig, motivo = None, "tope ciclo"
            clave = motivo.split("(")[0].strip()
            razones[clave] = razones.get(clave, 0) + 1
            motivos += 1
            if sig is None:
                continue

            lado, d = sig
            entrada = d["px"]
            sl = entrada - d["riesgo"] if lado == "LONG" else entrada + d["riesgo"]
            tp_r = float(CFG["TP_R"])
            if tp_r > 0:
                tp = entrada + tp_r * d["riesgo"] if lado == "LONG" else entrada - tp_r * d["riesgo"]
                if tp <= 0:
                    tp = 0.0
            else:
                tp = 0.0                        # sin objetivo
            st.abiertas[sym] = Virtual(
                symbol=sym, lado=lado, abierta_ts=d["t_senal"], entrada=entrada,
                sl=sl, tp=tp, riesgo=d["riesgo"], coste_r=d["coste_r"],
                basis_z=d["zb"], oi_z=d["zo"], funding=d["funding"], atr_pct=d["atr_pct"],
                conf_z=d["conf_z"], conf_regimen=d["conf_regimen"])
            señales += 1
            flecha = "🟢" if lado == "LONG" else "🔴"
            tg(f"{flecha} <b>{sym.split('-')[0]}</b> {lado} · {d.get('mode','?')} · score {d.get('score',0)}\n"
               f"Entrada <code>{entrada:.8g}</code> · SL <code>{sl:.8g}</code> · TP <code>{(f'{tp:.8g}' if tp > 0 else 'no')}</code> · salida {int(CFG['MAX_BARS'])} velas\n"
               f"basis z {d['zb']:+.2f} · OI z {d['zo']:+.2f} · funding {d['funding']:+.4f}%\n"
               f"ATR {d['atr_pct']:.2f}% · coste {d['coste_r']:.2f} R\n"
               f"<i>Solo señal virtual · v{VERSION}</i>", "senal")
        except Exception:
            log.exception("Fallo evaluando %s", sym)

    if acc:
        escritas = pn.registrar(_CFG_OBJ, acc)
        if escritas:
            log.info("Panel: %d filas apuntadas (%s)", escritas, CFG["PANEL_CSV"])

    for k, v in razones.items():
        EMBUDO[k] = EMBUDO.get(k, 0) + v
    top = sorted(razones.items(), key=lambda kv: -kv[1])[:4]
    global _ultimo_ciclo, _ultimo_heartbeat
    ahora = time.time()
    cad = (ahora - _ultimo_ciclo) / 60.0 if _ultimo_ciclo else 0.0
    _ultimo_ciclo = ahora

    # Progreso de calentamiento
    min_h = float(CFG["MIN_HORAS"])
    min_n = int(CFG["MIN_MUESTRAS"])
    listos = calentando = 0
    sum_h = sum_n = 0.0
    for sym in simbolos:
        hb = st.basis.get(sym)
        if not hb or len(hb) < 2:
            calentando += 1
            continue
        span = (hb[-1][0] - hb[0][0]) / 3600.0
        n = len(hb)
        sum_h += span
        sum_n += n
        if span >= min_h and n >= min_n:
            listos += 1
        else:
            calentando += 1
    total = max(listos + calentando, 1)
    media_h = sum_h / total
    media_n = sum_n / total

    if señales > 0 or calentando == 0:
        log.info("Ciclo: %d símbolos · %d señales · cadencia %.1f min | workers=%d | listos %d/%d · %s",
                 motivos, señales, cad, workers, listos, total,
                 " · ".join(f"{k}: {v}" for k, v in top))
    else:
        log.info("Ciclo: %d símbolos · 0 señales · cadencia %.1f min | calentando %d/%d "
                 "(media %.1f h, %.0f muestras) | %s",
                 motivos, cad, calentando, total, media_h, media_n,
                 " · ".join(f"{k}: {v}" for k, v in top[:3]))
    log.info("Embudo acumulado | %s",
             " · ".join(f"{k}: {v}" for k, v in sorted(EMBUDO.items(), key=lambda kv: -kv[1])))
    if len(ATR_AMONT) >= 20:
        xs = sorted(ATR_AMONT)
        q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
        log.info("Amplitud de los amontonamientos (n=%d): p10 %.2f%% · mediana %.2f%% · "
                 "p90 %.2f%% | filtro actual %.2f–%.2f%%",
                 len(xs), q(0.10), q(0.50), q(0.90),
                 float(CFG["MIN_ATR_PCT"]), float(CFG["MAX_ATR_PCT"]))

    # Heartbeat cada ~60 min
    if ahora - _ultimo_heartbeat >= 3600:
        _ultimo_heartbeat = ahora
        msg = (f"💓 <b>Crowding heartbeat</b>\n"
               f"Universo {len(simbolos)} · listos {listos}/{total}\n"
               f"Calentando: media {media_h:.1f} h / {media_n:.0f} muestras\n"
               f"Virtuales abiertas: {len(st.abiertas)}\n"
               f"Cadencia: {cad:.1f} min")
        tg(msg, "informe")
        log.info("Heartbeat: listos %d/%d · media %.1f h · %d virtuales",
                 listos, total, media_h, len(st.abiertas))

    st.guardar()


def main():
    log.info("Crowding bot v%s — SOLO SEÑALES · MODE=%s · GATILLO=%s · SL %.1f ATR · TP_R %.1f · "
             "MAX_BARS %d · score≥%s | %s workers=%s",
             VERSION, CFG.get("MODE"), CFG.get("GATILLO"), float(CFG["SL_ATR"]), float(CFG["TP_R"]),
             int(CFG["MAX_BARS"]), CFG.get("SCORE_MIN"), CFG["TIMEFRAME"], CFG["MAX_WORKERS"])
    st = Estado(CFG["STATE"])

    # Railway manda SIGTERM al redesplegar: se guarda el estado antes de salir.
    def _salir(signum, _frame):
        log.info("Señal %s recibida: guardando estado y saliendo", signum)
        st.guardar()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _salir)

    dir_estado = os.path.dirname(CFG["STATE"]) or "."
    # Railway define RAILWAY_VOLUME_MOUNT_PATH cuando el servicio tiene volumen; os.path.ismount
    # puede dar falso negativo con su forma de montar, así que vale cualquiera de las dos.
    vol_rw = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").rstrip("/")
    en_volumen_rw = bool(vol_rw) and os.path.abspath(dir_estado).startswith(os.path.abspath(vol_rw))
    sin_volumen = not (os.path.ismount(dir_estado) or en_volumen_rw)
    log.info("Volumen: RAILWAY_VOLUME_MOUNT_PATH=%s · estado en %s · %s", vol_rw or "(no definido)",
             dir_estado, "PERSISTENTE" if not sin_volumen else "EFÍMERO")
    if vol_rw and not en_volumen_rw:
        log.warning("Hay volumen en %s pero el estado se escribe en %s: cambia STATE/CSV/PANEL_CSV "
                    "a esa ruta", vol_rw, dir_estado)
    if sin_volumen:
        log.warning("%s NO es un volumen montado: el estado y los CSV se BORRAN en cada "
                    "redeploy y el calentamiento de 30 h vuelve a cero", dir_estado)
    syms = contratos()
    vols = volumenes()
    if vols:
        syms = [s for s in syms if vols.get(s, 0) >= float(CFG["MIN_VOL_24H"])]
        syms.sort(key=lambda s: vols.get(s, 0), reverse=True)
    syms = syms[: int(CFG["MAX_SYMBOLS"])]
    log.info("Universo: %d símbolos", len(syms))
    estado_txt = (f"Estado cargado: {len(st.basis)} símbolos con historia"
                  if st.basis else "Estado vacío: empieza el calentamiento")
    aviso_vol = (f"⚠️ <b>{dir_estado} no es un volumen</b>: cada redeploy borra datos y calentamiento\n"
                 if sin_volumen else "")
    tg(f"🤖 <b>Crowding bot v{VERSION} arrancado</b>\n{len(syms)} símbolos · {CFG['TIMEFRAME']}\n"
       f"{estado_txt}\n{aviso_vol}"
       f"Workers: {CFG['MAX_WORKERS']} · SCAN_SEC: {CFG['SCAN_SEC']}\n"
       f"Avisos: señal {'ON' if CFG['TG_SIGNALS'] else 'off'} · "
       f"cierre {'ON' if CFG['TG_CLOSES'] else 'off'} · informe diario a las "
       f"{CFG['REPORT_HOUR']}:00 UTC\n"
       f"<i>Sin claves de API: solo lee endpoints públicos. No puede operar.</i>\n"
       f"<i>Calentamiento: 30 h + 200 muestras por símbolo (~30-35 h de reloj).</i>")

    if CFG["CSV_AL_ARRANCAR"]:
        enviar_csv("pedido al arrancar")

    ultimo_universo = time.time()
    while True:
        try:
            if time.time() - ultimo_universo > 6 * 3600:
                v2 = volumenes()
                if v2:
                    s2 = [s for s in contratos() if v2.get(s, 0) >= float(CFG["MIN_VOL_24H"])]
                    s2.sort(key=lambda s: v2.get(s, 0), reverse=True)
                    if s2:
                        syms = s2[: int(CFG["MAX_SYMBOLS"])]
                ultimo_universo = time.time()

            ciclo(st, syms)

            hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if (datetime.now(timezone.utc).hour == int(CFG["REPORT_HOUR"])
                    and st.ultimo_informe != hoy):
                st.ultimo_informe = hoy
                st.guardar()
                tg(informe(st))
                if CFG["CSV_CON_INFORME"]:
                    enviar_csv("informe diario")
        except Exception:
            log.exception("Fallo en el ciclo")
        time.sleep(int(CFG["SCAN_SEC"]))


if __name__ == "__main__":
    main()
