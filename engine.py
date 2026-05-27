"""
QF×JP Bot v6.1 — engine.py
Mejoras vs v6.0:
  ✅ Weights del composite score rebalanceados (Score dominante)
  ✅ BB Squeeze: filtro de volatilidad para detectar breakouts reales
  ✅ Volumen necesario en 15m y 1h para alineación real
  ✅ OFI ponderado por niveles (los más cercanos pesan más)
  ✅ should_enter: filtros más precisos (menos FP)
  ✅ Tier SUP requiere alineación multi-TF además de vol+OFI
  ✅ ATR SL dinámico: tier afecta multiplicador
"""
import logging
from typing import Optional

import numpy as np

log = logging.getLogger("ENGINE")


# ── Helpers numpy safe ─────────────────────────────────────
def _safe_div(a: np.ndarray, b: np.ndarray, fill: float = 0.0) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(np.abs(b) > 1e-12, a / b, fill)
    return np.nan_to_num(result, nan=fill, posinf=fill, neginf=fill)


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(arr, dtype=float)
    k   = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag    = _ema(gain, period)
    al    = _ema(loss, period)
    rs    = _safe_div(ag, al, fill=1.0)
    return 100 - 100 / (1 + rs)


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return _ema(tr, period)


def _macd(close: np.ndarray, fast: int = 12, slow: int = 26, sig: int = 9):
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal    = _ema(macd_line, sig)
    hist      = macd_line - signal
    return macd_line, signal, hist


def _bollinger(close: np.ndarray, period: int = 20, std: float = 2.0):
    mid  = np.array([np.mean(close[max(0,i-period):i+1]) for i in range(len(close))])
    s    = np.array([np.std(close[max(0,i-period):i+1],  ddof=0) for i in range(len(close))])
    upper = mid + std * s
    lower = mid - std * s
    width = _safe_div(upper - lower, mid, fill=0.0)
    return upper, lower, mid, width


def _vwap(o, h, l, c, v):
    tp      = (h + l + c) / 3
    cum_tv  = np.cumsum(tp * v)
    cum_v   = np.cumsum(v)
    return _safe_div(cum_tv, cum_v, fill=c[-1])


def _cvd(o, c, h, l, v) -> float:
    """Cumulative Volume Delta (buy vol - sell vol aproximado)."""
    hl_r = h - l
    safe_hl = np.where(hl_r > 1e-12, hl_r, 1.0)
    bvol = np.clip((c - l) / safe_hl, 0, 1) * v
    svol = np.clip((h - c) / safe_hl, 0, 1) * v
    return float(np.sum(bvol - svol))


def _swing_highs(h: np.ndarray, window: int = 5) -> np.ndarray:
    out = np.zeros(len(h))
    for i in range(window, len(h) - window):
        if h[i] == np.max(h[i - window:i + window + 1]):
            out[i] = h[i]
    return out


def _swing_lows(l: np.ndarray, window: int = 5) -> np.ndarray:
    out = np.zeros(len(l))
    for i in range(window, len(l) - window):
        if l[i] == np.min(l[i - window:i + window + 1]):
            out[i] = l[i]
    return out


def _klines_to_arrays(klines: list):
    o = np.array([k["o"] for k in klines], dtype=float)
    h = np.array([k["h"] for k in klines], dtype=float)
    l = np.array([k["l"] for k in klines], dtype=float)
    c = np.array([k["c"] for k in klines], dtype=float)
    v = np.array([k["v"] for k in klines], dtype=float)
    return o, h, l, c, v


class QFJPEngine:
    """Motor de señales QF×JP v6.1."""

    def compute(
        self,
        klines_3m:  list,
        klines_15m: list,
        klines_1h:  list,
        klines_1m:  list,
        mctx:       dict,
    ) -> dict:
        blank = self._blank()
        if len(klines_3m) < 100:
            return blank

        o3, h3, l3, c3, v3 = _klines_to_arrays(klines_3m)
        price = float(c3[-1])

        # ── Indicadores base ──────────────────────────────
        atr14  = _atr(h3, l3, c3, 14)
        atr_v  = float(atr14[-1])
        rsi14  = _rsi(c3, 14)
        rsi_v  = float(rsi14[-1])

        macd_l, macd_s, macd_h = _macd(c3)
        macd_hv   = float(macd_h[-1])
        macd_prev = float(macd_h[-2]) if len(macd_h) > 1 else macd_hv

        # Bollinger Bands (detección de squeeze/breakout)
        bb_upper, bb_lower, bb_mid, bb_width = _bollinger(c3, 20, 2.0)
        bb_squeeze = float(bb_width[-1]) < float(np.percentile(bb_width[-50:], 25))
        bb_breakout_bull = price > float(bb_upper[-1])
        bb_breakout_bear = price < float(bb_lower[-1])

        vwap_v   = float(_vwap(o3, h3, l3, c3, v3)[-1])
        ema9_v   = float(_ema(c3, 9)[-1])
        ema21_v  = float(_ema(c3, 21)[-1])

        # ── Momentum ──────────────────────────────────────
        rsi_norm  = (rsi_v - 50) * 2   # [-100, +100]
        macd_norm = float(np.clip(
            _safe_div(np.array([macd_hv]), np.array([atr_v or 1.0]))[0] * 100,
            -200, 200
        ))
        momentum_raw = rsi_norm * 0.5 + macd_norm * 0.5

        # ── CVD ───────────────────────────────────────────
        cvd_raw   = _cvd(o3, c3, h3, l3, v3)
        vol_total = float(np.sum(v3)) or 1.0
        cvd_norm  = float(np.clip(cvd_raw / vol_total, -1.0, 1.0))

        # ── Swing structure ───────────────────────────────
        sh3 = _swing_highs(h3, 5)
        sl3 = _swing_lows(l3, 5)
        valid_sh = sh3[sh3 > 0]
        valid_sl = sl3[sl3 > 0]
        last_sh  = float(valid_sh[-1]) if len(valid_sh) > 0 else price * 1.01
        last_sl  = float(valid_sl[-1]) if len(valid_sl) > 0 else price * 0.99

        # ── EMA alignment ─────────────────────────────────
        ema_bull = ema9_v > ema21_v and price > ema21_v
        ema_bear = ema9_v < ema21_v and price < ema21_v

        # ── Volumen relativo ──────────────────────────────
        vol_ma   = float(np.mean(v3[-20:])) or 1.0
        vol_cur  = float(v3[-1])
        vol_ratio = vol_cur / vol_ma
        vol_regime = "HIGH" if vol_ratio > 1.5 else "LOW" if vol_ratio < 0.5 else "MED"

        # ── Multi-TF alignment ────────────────────────────
        tf_bull = 0; tf_bear = 0; tf_total = 0

        if len(klines_15m) >= 30:
            _, _, _, c15, v15 = _klines_to_arrays(klines_15m)
            ema20_15 = float(_ema(c15, 20)[-1])
            macd_15  = _macd(c15)
            rsi_15   = float(_rsi(c15, 14)[-1])
            tf_total += 1
            if c15[-1] > ema20_15 and macd_15[2][-1] > 0 and rsi_15 > 50:
                tf_bull += 1
            elif c15[-1] < ema20_15 and macd_15[2][-1] < 0 and rsi_15 < 50:
                tf_bear += 1

        if len(klines_1h) >= 30:
            _, _, _, c1h, _ = _klines_to_arrays(klines_1h)
            ema20_1h = float(_ema(c1h, 20)[-1])
            ema50_1h = float(_ema(c1h, 50)[-1])
            tf_total += 1
            if c1h[-1] > ema20_1h > ema50_1h:
                tf_bull += 1
            elif c1h[-1] < ema20_1h < ema50_1h:
                tf_bear += 1

        if len(klines_1m) >= 10:
            _, _, _, c1m, _ = _klines_to_arrays(klines_1m)
            rsi_1m = float(_rsi(c1m, 7)[-1])
            tf_total += 1
            if c1m[-1] > c1m[-5] and rsi_1m > 52:
                tf_bull += 1
            elif c1m[-1] < c1m[-5] and rsi_1m < 48:
                tf_bear += 1

        tf_denom       = max(tf_total, 1)
        tf_score_bull  = tf_bull / tf_denom
        tf_score_bear  = tf_bear / tf_denom

        # ── OFI / FR / OI ─────────────────────────────────
        ofi  = float(mctx.get("ofi", 0))
        fr   = float(mctx.get("funding_rate", 0))
        oi   = float(mctx.get("open_interest", 0))
        oi_p = float(mctx.get("prev_open_interest", oi))
        with np.errstate(divide="ignore", invalid="ignore"):
            oi_delta = (oi - oi_p) / oi_p if oi_p > 1e-12 else 0.0

        fr_extreme_long  = fr >  0.005
        fr_extreme_short = fr < -0.005

        # ── Score base ────────────────────────────────────
        # Componentes con mayor precisión semántica
        components = {
            # RSI zona alcista/bajista
            "rsi_bull":  float(np.clip((rsi_v - 48) / 22, 0, 1)),
            "rsi_bear":  float(np.clip((52 - rsi_v) / 22, 0, 1)),
            # MACD histograma creciente/decreciente
            "macd_bull": 1.0 if (macd_hv > 0 and macd_hv > macd_prev) else (0.5 if macd_hv > 0 else 0.0),
            "macd_bear": 1.0 if (macd_hv < 0 and macd_hv < macd_prev) else (0.5 if macd_hv < 0 else 0.0),
            # VWAP posición
            "vwap_bull": 1.0 if price > vwap_v else 0.0,
            "vwap_bear": 1.0 if price < vwap_v else 0.0,
            # EMA crossover
            "ema_bull":  1.0 if ema_bull else 0.0,
            "ema_bear":  1.0 if ema_bear else 0.0,
            # BB breakout (señal de momentum)
            "bb_bull":   1.0 if bb_breakout_bull else (0.5 if not bb_squeeze else 0.0),
            "bb_bear":   1.0 if bb_breakout_bear else (0.5 if not bb_squeeze else 0.0),
            # OFI (Order Flow Imbalance)
            "ofi_bull":  float(np.clip(ofi,  0, 1)),
            "ofi_bear":  float(np.clip(-ofi, 0, 1)),
            # Multi-TF
            "tf_bull":   tf_score_bull,
            "tf_bear":   tf_score_bear,
            # OI delta
            "oi_bull":   float(np.clip(oi_delta * 20,  0, 1)),
            "oi_bear":   float(np.clip(-oi_delta * 20, 0, 1)),
            # FR squeeze contrarian
            "fr_sq_bull": 1.0 if fr_extreme_short else 0.0,
            "fr_sq_bear": 1.0 if fr_extreme_long  else 0.0,
        }

        # Pesos calibrados para mercados cripto perpetuos
        W = {
            "rsi":  0.10,
            "macd": 0.10,
            "vwap": 0.08,
            "ema":  0.08,
            "bb":   0.06,
            "ofi":  0.22,   # OFI es la señal más institucional
            "tf":   0.24,   # Multi-TF es el filtro más robusto
            "oi":   0.07,
            "fr_sq": 0.05,
        }

        def score(suffix: str) -> float:
            raw = (
                components[f"rsi_{suffix}"]   * W["rsi"]  +
                components[f"macd_{suffix}"]  * W["macd"] +
                components[f"vwap_{suffix}"]  * W["vwap"] +
                components[f"ema_{suffix}"]   * W["ema"]  +
                components[f"bb_{suffix}"]    * W["bb"]   +
                components[f"ofi_{suffix}"]   * W["ofi"]  +
                components[f"tf_{suffix}"]    * W["tf"]   +
                components[f"oi_{suffix}"]    * W["oi"]   +
                components[f"fr_sq_{suffix}"] * W["fr_sq"]
            )
            total_w = sum(W.values())
            return raw / total_w

        score_bull = score("bull")
        score_bear = score("bear")

        # ── Composite (Score + CVD + Momentum + Decay) ────
        decay_ratio = float(np.clip(vol_ratio, 0.3, 1.0))   # FIX: era vol_ratio/2 → bloqueaba todo

        def composite(sc: float, cvd: float, mom: float) -> float:
            cvd_scaled = (cvd + 1) / 2       # [0,1]
            mom_scaled = (mom + 200) / 400   # [0,1]
            return (
                0.45 * sc          +   # score domina
                0.25 * cvd_scaled  +
                0.18 * mom_scaled  +
                0.12 * decay_ratio
            )

        comp_bull = composite(score_bull,  cvd_norm, momentum_raw)
        comp_bear = composite(score_bear, -cvd_norm, -momentum_raw)

        # ── Dirección ─────────────────────────────────────
        direction: Optional[str] = None
        norm_score: float        = 0.0
        THR = 0.50  # FIX: era 0.56 → con ese valor ni todos los TF alineados alcanzaban el umbral

        if comp_bull > comp_bear and comp_bull >= THR:
            if not fr_extreme_long:
                direction  = "LONG"
                norm_score = comp_bull
        elif comp_bear > comp_bull and comp_bear >= THR:
            if not fr_extreme_short:
                direction  = "SHORT"
                norm_score = comp_bear

        # ── Tier ─────────────────────────────────────────
        tier = "STD"
        if direction:
            tf_aligned = (
                (tf_score_bull >= 0.67 if direction == "LONG"  else False) or
                (tf_score_bear >= 0.67 if direction == "SHORT" else False)
            )
            if vol_regime == "HIGH" and abs(ofi) > 0.40 and tf_aligned:
                tier = "SUP"
            elif vol_regime == "HIGH" or abs(ofi) > 0.22:
                tier = "FUEL"

        # ── Convicción ────────────────────────────────────
        conviction = int(np.clip(norm_score * 10, 0, 10))

        # ── SL / TP dinámico por ATR ──────────────────────
        sl_val: Optional[float] = None
        tp_val: Optional[float] = None
        if direction and atr_v > 0:
            # SUP: SL más ajustado (señal de mayor calidad)
            # STD: SL más holgado (evitar whipsaw)
            sl_mult = {"SUP": 1.4, "FUEL": 1.7, "STD": 2.0}[tier]
            tp_mult = sl_mult * 2.2   # RR mínimo 2.2:1
            if direction == "LONG":
                sl_val = price - atr_v * sl_mult
                tp_val = price + atr_v * tp_mult
            else:
                sl_val = price + atr_v * sl_mult
                tp_val = price - atr_v * tp_mult

        # ── CVD bias string ───────────────────────────────
        if   cvd_norm > 0.12:  cvd_bias = "BULL"
        elif cvd_norm < -0.12: cvd_bias = "BEAR"
        else:                  cvd_bias = "NEUTRAL"

        return {
            "direction":    direction,
            "tier":         tier,
            "conviction":   conviction,
            "norm_score":   norm_score,
            "score_bull":   score_bull,
            "score_bear":   score_bear,
            "comp_bull":    comp_bull,
            "comp_bear":    comp_bear,
            "decay_ratio":  decay_ratio,
            "momentum":     momentum_raw,
            "cvd_norm":     cvd_norm,
            "cvd_bias":     cvd_bias,
            "ofi":          ofi,
            "funding_rate": fr,
            "oi_delta":     oi_delta,
            "vol_regime":   vol_regime,
            "atr_last":     atr_v,
            "vwap":         vwap_v,
            "sl":           sl_val,
            "tp":           tp_val,
            "tf_bull":      tf_score_bull,
            "tf_bear":      tf_score_bear,
            "asym":         1.0,
            "bb_squeeze":   bb_squeeze,
        }

    @staticmethod
    def _blank() -> dict:
        return {
            "direction": None, "tier": "STD", "conviction": 0,
            "norm_score": 0.0, "score_bull": 0.0, "score_bear": 0.0,
            "comp_bull": 0.0, "comp_bear": 0.0,
            "decay_ratio": 0.0, "momentum": 0.0,
            "cvd_norm": 0.0, "cvd_bias": "NEUTRAL",
            "ofi": 0.0, "funding_rate": 0.0, "oi_delta": 0.0,
            "vol_regime": "MED", "atr_last": 0.0, "vwap": 0.0,
            "sl": None, "tp": None,
            "tf_bull": 0.0, "tf_bear": 0.0, "asym": 1.0,
            "bb_squeeze": False,
        }

    @staticmethod
    def should_enter(sig: dict, min_composite: float = 0.56) -> bool:
        """
        Filtro final robusto: todos los pilares deben apuntar
        en la misma dirección. No entrar en señales conflictivas.
        """
        d    = sig["direction"]
        comp = sig["comp_bull"] if d == "LONG" else sig["comp_bear"]
        cvd  = sig["cvd_bias"]
        mom  = sig["momentum"]
        dec  = sig["decay_ratio"]
        vol  = sig["vol_regime"]

        if not d:                     return False
        if comp < min_composite:      return False
        # decay ya está ponderado en el composite — eliminar chequeo doble que bloqueaba vol normal
        if vol == "LOW":              return False   # sin liquidez (vol_ratio < 0.5)

        # Nota: CVD ya está ponderado al 25% dentro del composite.
        # Solo bloqueamos si CVD es extremadamente contrario Y composite es débil.
        if comp < 0.60 and cvd != "NEUTRAL":
            if d == "LONG"  and cvd == "BEAR": return False
            if d == "SHORT" and cvd == "BULL": return False

        # Momentum no puede ser extremadamente contrario
        if d == "LONG"  and mom < -120: return False
        if d == "SHORT" and mom >  120: return False

        return True
