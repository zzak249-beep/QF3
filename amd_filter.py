"""
╔══════════════════════════════════════════════════════════════════╗
║  AMD FILTER — Smart Money Accumulation/Manipulation/Distribution ║
║                                                                  ║
║  Implementa los 4 pasos AMD en Python sobre OHLCV:              ║
║                                                                  ║
║  PASO 1 — Rango asiático   00:00-08:00 UTC (high/low)           ║
║  PASO 2 — Sweep/manipulación  wick rompe + cierra dentro        ║
║  PASO 3 — Volumen institucional  vol > N × media                ║
║  PASO 4 — MSS post-sweep   cierre rompe estructura              ║
║                                                                  ║
║  Retorna AMDSignal con dirección, fuerza y contexto debug        ║
╚══════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("AMD")

# ── Configuración por env vars ─────────────────────────────────────────
ASIA_START_UTC   = int(os.getenv("AMD_ASIA_START",  "0"))   # hora inicio rango
ASIA_END_UTC     = int(os.getenv("AMD_ASIA_END",    "8"))   # hora fin rango
VOL_MULT         = float(os.getenv("AMD_VOL_MULT",  "1.5")) # multiplicador volumen institucional
VOL_MA_LEN       = int(os.getenv("AMD_VOL_MA_LEN",  "20")) # período media volumen
SWEEP_MIN_PCT    = float(os.getenv("AMD_SWEEP_PCT", "0.05"))# penetración mínima (% del rango)
MSS_BARS         = int(os.getenv("AMD_MSS_BARS",    "3"))   # velas para confirmar MSS
AMD_REQUIRED     = os.getenv("AMD_REQUIRED", "false").lower() == "true"  # True = bloquea sin AMD
AMD_BOOST        = int(os.getenv("AMD_BOOST", "8"))          # puntos de score boost por AMD confirmado


# ══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════
@dataclass
class AsianRange:
    high: float
    low: float
    date: str  # YYYY-MM-DD UTC

    @property
    def size(self) -> float:
        return self.high - self.low

    def __repr__(self):
        return f"AsianRange(H={self.high:.6f} L={self.low:.6f} rng={self.size:.6f} d={self.date})"


@dataclass
class AMDSignal:
    """Resultado del análisis AMD para una barra concreta."""
    direction: Optional[str]       = None   # "LONG" | "SHORT" | None
    step:      int                 = 0      # 0-4: hasta qué paso llegó
    boost:     int                 = 0      # puntos extra de score
    blocked:   bool                = False  # True si AMD_REQUIRED y no hay señal

    # Contexto debug
    asian_range: Optional[AsianRange] = None
    sweep_low:   bool              = False
    sweep_high:  bool              = False
    high_vol:    bool              = False
    mss_bull:    bool              = False
    mss_bear:    bool              = False

    def __repr__(self):
        return (
            f"AMDSignal(dir={self.direction} step={self.step}/4 "
            f"boost=+{self.boost} "
            f"sweep={'L' if self.sweep_low else 'H' if self.sweep_high else '—'} "
            f"vol={'✓' if self.high_vol else '✗'} "
            f"mss={'↑' if self.mss_bull else '↓' if self.mss_bear else '—'})"
        )


# ══════════════════════════════════════════════════════════════════════
# AMD FILTER — clase principal (stateful por símbolo)
# ══════════════════════════════════════════════════════════════════════
class AMDFilter:
    """
    Instanciar una vez por símbolo. Llamar .update(ohlcv) en cada ciclo.

    ohlcv: lista de dicts con keys: timestamp (ms UTC), open, high, low, close, volume
           El último elemento es la vela más reciente (vela actual/cierre).
    """

    def __init__(self, symbol: str = ""):
        self.symbol = symbol
        self._asian_range: Optional[AsianRange] = None
        self._pending_bull_mss: bool  = False
        self._pending_bear_mss: bool  = False
        self._mss_bull_level:  float  = 0.0
        self._mss_bear_level:  float  = 0.0

    # ── helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _bar_hour_utc(ts_ms: int) -> int:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour

    @staticmethod
    def _bar_date_utc(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _in_asia(hour: int) -> bool:
        s, e = ASIA_START_UTC, ASIA_END_UTC
        if s < e:
            return s <= hour < e
        else:  # cruce medianoche
            return hour >= s or hour < e

    # ── rango asiático ─────────────────────────────────────────────────
    def _build_asian_range(self, ohlcv: list[dict]) -> Optional[AsianRange]:
        """
        Reconstruye el rango asiático confirmado.
        Solo usa barras PASADAS (excluye la vela actual = ohlcv[-1])
        para que el rango esté cerrado antes de evaluar el sweep.
        Busca primero en el día de la barra actual; si no encuentra,
        en el día anterior (útil en horarios post-medianoche).
        """
        if not ohlcv:
            return None

        # Excluir la vela actual del rango
        historical = ohlcv[:-1]
        if not historical:
            return None

        last_date = self._bar_date_utc(ohlcv[-1]["timestamp"])

        def _scan(bars, date_filter) -> tuple[Optional[float], Optional[float]]:
            rh = rl = None
            for bar in bars:
                d = self._bar_date_utc(bar["timestamp"])
                h = self._bar_hour_utc(bar["timestamp"])
                if d == date_filter and self._in_asia(h):
                    if rh is None:
                        rh, rl = bar["high"], bar["low"]
                    else:
                        rh = max(rh, bar["high"])
                        rl = min(rl, bar["low"])
            return rh, rl

        # Intentar con el día actual primero
        r_high, r_low = _scan(historical, last_date)

        if r_high is None:
            # Día anterior
            prev_dates = sorted({self._bar_date_utc(b["timestamp"]) for b in historical})
            if len(prev_dates) >= 1:
                prev_date = prev_dates[-1] if prev_dates[-1] != last_date else (
                    prev_dates[-2] if len(prev_dates) >= 2 else None
                )
                if prev_date:
                    r_high, r_low = _scan(historical, prev_date)
                    if r_high is not None:
                        last_date = prev_date

        if r_high is None:
            return None

        return AsianRange(high=r_high, low=r_low, date=last_date)

    # ── análisis principal ─────────────────────────────────────────────
    def update(self, ohlcv: list[dict]) -> AMDSignal:
        """
        Recibe ohlcv completo (ej. 250 velas 3m).
        Retorna AMDSignal con la evaluación actual.
        """
        sig = AMDSignal()

        if len(ohlcv) < max(VOL_MA_LEN + 1, MSS_BARS + 2):
            return sig  # datos insuficientes

        # ── PASO 1: Rango asiático ──────────────────────────────────
        rng = self._build_asian_range(ohlcv)
        if rng is None or rng.size <= 0:
            if AMD_REQUIRED:
                sig.blocked = True
            return sig

        sig.asian_range = rng
        sig.step        = 1
        self._asian_range = rng

        cur  = ohlcv[-1]
        atr_ = self._atr(ohlcv, 10)  # ATR rápido para umbral sweep

        # Penetración mínima: el mayor entre % del rango y 0.01×ATR
        min_sweep_abs = max(rng.size * (SWEEP_MIN_PCT / 100), atr_ * 0.01)

        # ── PASO 2: Sweep / manipulación ───────────────────────────
        # Sweep bajista (bull setup): wick bajo asian_low, cierre dentro
        sweep_low  = (cur["low"]  < rng.low  - min_sweep_abs and
                      cur["close"] > rng.low)
        # Sweep alcista (bear setup): wick sobre asian_high, cierre dentro
        sweep_high = (cur["high"] > rng.high + min_sweep_abs and
                      cur["close"] < rng.high)

        sig.sweep_low  = sweep_low
        sig.sweep_high = sweep_high

        if not sweep_low and not sweep_high:
            # Revisar las últimas MSS_BARS+2 velas por si el sweep ya ocurrió
            sweep_low, sweep_high = self._recent_sweep(ohlcv, rng, min_sweep_abs)
            sig.sweep_low  = sweep_low
            sig.sweep_high = sweep_high
            if not sweep_low and not sweep_high:
                if AMD_REQUIRED:
                    sig.blocked = True
                return sig

        sig.step = 2

        # ── PASO 3: Volumen institucional ──────────────────────────
        vol_ma   = sum(b["volume"] for b in ohlcv[-(VOL_MA_LEN + 1):-1]) / VOL_MA_LEN
        high_vol = cur["volume"] > vol_ma * VOL_MULT

        sig.high_vol = high_vol

        if not high_vol:
            # Revisar si alguna de las últimas velas tuvo el volumen
            high_vol = self._recent_high_vol(ohlcv, vol_ma)
            sig.high_vol = high_vol
            if not high_vol:
                if AMD_REQUIRED:
                    sig.blocked = True
                return sig

        sig.step = 3

        # ── PASO 4: MSS (Market Structure Shift) ───────────────────
        # MSS Bull: cierre rompe el high de las últimas MSS_BARS velas
        # MSS Bear: cierre rompe el low  de las últimas MSS_BARS velas
        recent_highs = [b["high"]  for b in ohlcv[-(MSS_BARS + 2):-1]]
        recent_lows  = [b["low"]   for b in ohlcv[-(MSS_BARS + 2):-1]]
        mss_bull_level = max(recent_highs) if recent_highs else 0
        mss_bear_level = min(recent_lows)  if recent_lows  else float("inf")

        mss_bull = sweep_low  and cur["close"] > mss_bull_level
        mss_bear = sweep_high and cur["close"] < mss_bear_level

        sig.mss_bull = mss_bull
        sig.mss_bear = mss_bear

        if not mss_bull and not mss_bear:
            # Activar pending para próximas velas
            if sweep_low  and not self._pending_bull_mss:
                self._pending_bull_mss  = True
                self._mss_bull_level    = mss_bull_level
                log.debug(f"[{self.symbol}] AMD pending bull MSS > {mss_bull_level:.6f}")
            if sweep_high and not self._pending_bear_mss:
                self._pending_bear_mss  = True
                self._mss_bear_level    = mss_bear_level
                log.debug(f"[{self.symbol}] AMD pending bear MSS < {mss_bear_level:.6f}")

            # Verificar pending de iteraciones anteriores
            if self._pending_bull_mss and cur["close"] > self._mss_bull_level:
                mss_bull = True
                sig.mss_bull = True
                self._pending_bull_mss = False
            elif self._pending_bear_mss and cur["close"] < self._mss_bear_level:
                mss_bear = True
                sig.mss_bear = True
                self._pending_bear_mss = False

        # Invalidar pending si precio re-rompe el rango en sentido contrario
        if self._pending_bull_mss and cur["close"] < rng.low * 0.998:
            self._pending_bull_mss = False
            log.debug(f"[{self.symbol}] AMD bull MSS pending invalidado")
        if self._pending_bear_mss and cur["close"] > rng.high * 1.002:
            self._pending_bear_mss = False
            log.debug(f"[{self.symbol}] AMD bear MSS pending invalidado")

        if not mss_bull and not mss_bear:
            if AMD_REQUIRED:
                sig.blocked = True
            return sig

        # ── AMD COMPLETO ────────────────────────────────────────────
        sig.step      = 4
        sig.direction = "LONG"  if mss_bull else "SHORT"
        sig.boost     = AMD_BOOST

        log.info(
            f"[{self.symbol}] ✅ AMD {sig.direction} | "
            f"rng=[{rng.low:.6f}-{rng.high:.6f}] | {sig}"
        )
        return sig

    # ── utilidades privadas ────────────────────────────────────────────
    def _recent_sweep(self, ohlcv, rng: AsianRange, min_sweep_abs: float,
                       lookback: int = 5) -> tuple[bool, bool]:
        """Busca sweep en las últimas `lookback` velas (no solo la actual)."""
        sweep_low  = False
        sweep_high = False
        for bar in ohlcv[-lookback:]:
            if bar["low"]  < rng.low  - min_sweep_abs and bar["close"] > rng.low:
                sweep_low  = True
            if bar["high"] > rng.high + min_sweep_abs and bar["close"] < rng.high:
                sweep_high = True
        return sweep_low, sweep_high

    def _recent_high_vol(self, ohlcv, vol_ma: float, lookback: int = 5) -> bool:
        """Verifica si alguna vela reciente tuvo volumen institucional."""
        return any(b["volume"] > vol_ma * VOL_MULT for b in ohlcv[-lookback:])

    @staticmethod
    def _atr(ohlcv: list[dict], length: int = 10) -> float:
        if len(ohlcv) < length + 1:
            return 0.0
        trs = []
        for i in range(-length, 0):
            h, l, pc = ohlcv[i]["high"], ohlcv[i]["low"], ohlcv[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0


# ══════════════════════════════════════════════════════════════════════
# REGISTRY — un AMDFilter por símbolo
# ══════════════════════════════════════════════════════════════════════
_registry: dict[str, AMDFilter] = {}

def get_amd_filter(symbol: str) -> AMDFilter:
    """Devuelve (o crea) el AMDFilter asociado al símbolo."""
    if symbol not in _registry:
        _registry[symbol] = AMDFilter(symbol)
    return _registry[symbol]
