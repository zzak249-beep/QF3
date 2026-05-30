"""
QF×JP Engine v5.5 — integra Pine Script v3.2 completo
  [M1] Decay adaptativo — OR percentil histórico IC
  [M2] Pesos dinámicos ADX — tendencia/lateral
  [M3] CVD rodante sin deriva (ventana fija)
  [M4] FVG tracking múltiple (ya estaba)
  [M5] Conv-Boost — convicción sube score compuesto
  [M6] Filtro volatilidad mínima ATR
  + L13 OFI / L14 FR / L15 OI (v5)
"""
import numpy as np
import pandas as pd
import warnings
from dataclasses import dataclass
from typing import Optional
from config import cfg

np.seterr(divide='ignore', invalid='ignore')
warnings.filterwarnings('ignore', category=RuntimeWarning)


# ── helpers ─────────────────────────────────────────────────
def _tanh(x): return np.tanh(np.clip(x, -10, 10))
def _ema(s, p):
    a, out = 2/(p+1), np.empty_like(s, dtype=float)
    out[0] = s[0]
    for i in range(1, len(s)): out[i] = a*s[i] + (1-a)*out[i-1]
    return out
def _sma(s, p): return pd.Series(s).rolling(p, min_periods=1).mean().values
def _std(s, p): return pd.Series(s).rolling(p, min_periods=2).std(ddof=0).fillna(0).values
def _high(s,p): return pd.Series(s).rolling(p, min_periods=1).max().values
def _low(s, p): return pd.Series(s).rolling(p, min_periods=1).min().values
def _roll_sum(s, p): return pd.Series(s).rolling(p, min_periods=1).sum().values   # [M3]
def _corr(a, b, p):
    return pd.Series(a).rolling(p, min_periods=max(5,p//2)).corr(pd.Series(b)).fillna(0).values
def _safe(a, b, fill=0.0):
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(np.abs(b) < 1e-12, fill, a / b)
def _atr(h, l, c, p):
    pc = np.roll(c,1); pc[0] = c[0]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    return _ema(tr, p)
def _obv(c, v): return np.cumsum(np.sign(np.diff(c, prepend=c[0])) * v)
def _pivot_h(h, left, right):
    n, out = len(h), np.full(len(h), np.nan)
    for i in range(left, n-right):
        w = h[i-left:i+right+1]
        if h[i] == w.max() and (w==h[i]).sum()==1: out[i]=h[i]
    return out
def _pivot_l(l, left, right):
    n, out = len(l), np.full(len(l), np.nan)
    for i in range(left, n-right):
        w = l[i-left:i+right+1]
        if l[i] == w.min() and (w==l[i]).sum()==1: out[i]=l[i]
    return out
def _linreg(s, p):
    out = np.full(len(s), np.nan)
    for i in range(p-1, len(s)):
        y = s[i-p+1:i+1]; x = np.arange(p)
        c = np.polyfit(x, y, 1); out[i] = np.polyval(c, p-1)
    return out

# ── [M2] ADX ────────────────────────────────────────────────
def _adx(h, l, c, p=14):
    """Devuelve (adx, dmi_plus, dmi_minus)."""
    n = len(c)
    pc = np.roll(c,1); pc[0]=c[0]
    ph = np.roll(h,1); ph[0]=h[0]
    pl = np.roll(l,1); pl[0]=l[0]
    tr  = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    dm_p = np.where((h-ph) > (pl-l), np.maximum(h-ph, 0), 0.0)
    dm_m = np.where((pl-l) > (h-ph), np.maximum(pl-l, 0), 0.0)
    atr_v  = _ema(tr,  p)
    dmi_p  = _safe(_ema(dm_p, p), atr_v) * 100
    dmi_m  = _safe(_ema(dm_m, p), atr_v) * 100
    dx     = _safe(np.abs(dmi_p-dmi_m), dmi_p+dmi_m+1e-12) * 100
    adx_v  = _ema(dx, p)
    return adx_v, dmi_p, dmi_m


@dataclass
class Signal:
    direction    : Optional[str] = None
    tier         : str   = "STD"
    conviction   : int   = 0
    sl           : float = 0.0
    tp           : Optional[float] = None
    atr_last     : float = 0.0
    norm_score   : float = 0.0
    decay_ratio  : float = 0.0
    sig_alive    : bool  = False
    exec_ok      : bool  = False
    htf_bull     : bool  = False
    htf_bear     : bool  = False
    asym_bull    : bool  = False
    asym_bear    : bool  = False
    sell_exhausted : bool = False
    buy_exhausted  : bool = False
    tl_break_long  : bool = False
    tl_break_short : bool = False
    dp_buy       : bool  = False
    dp_sell      : bool  = False
    cvd_rising   : bool  = False
    cvd_bull_div : bool  = False
    cvd_bear_div : bool  = False
    sq_bull      : bool  = False
    sq_bear      : bool  = False
    in_bull_fvg  : bool  = False
    in_bear_fvg  : bool  = False
    in_bull_ob   : bool  = False
    in_bear_ob   : bool  = False
    above_vwap   : bool  = False
    trending     : bool  = False
    vol_regime   : str   = "NORMAL"
    vol_ok_atr   : bool  = True      # [M6]
    adx_val      : float = 0.0       # [M2]
    adx_trend    : bool  = False
    comp_long    : int   = 0         # [M5] score compuesto 0-100
    comp_short   : int   = 0
    ofi          : float = 0.0
    ofi_bull     : bool  = False
    ofi_bear     : bool  = False
    funding_rate : float = 0.0
    fr_bull      : bool  = False
    fr_bear      : bool  = False
    fr_extreme   : bool  = False
    oi_delta     : float = 0.0
    oi_rising    : bool  = False
    htf_1h_bull  : bool  = False
    htf_1h_bear  : bool  = False
    multi_tf_aligned: bool = False


class QFJPEngine:

    def compute(self, ohlcv_3m, ohlcv_15m,
                ohlcv_1h=None, ohlcv_1m=None,
                market_ctx=None) -> dict:
        return self._run(ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m, market_ctx).__dict__

    def _run(self, raw3, raw15, raw1h, raw1m, ctx) -> Signal:
        df3  = self._df(raw3)
        df15 = self._df(raw15)
        o,h,l,c,v = (df3["open"].values, df3["high"].values,
                     df3["low"].values, df3["close"].values,
                     df3["volume"].values)
        n = len(c)
        atr_v = _atr(h, l, c, cfg.ATR_LEN)

        # ── [M2] ADX ────────────────────────────────────────
        adx_v, dmi_p, dmi_m = _adx(h, l, c, cfg.ADX_LEN)
        trend_strong = adx_v >= cfg.ADX_TREND_THR
        trend_up     = (dmi_p > dmi_m) & trend_strong
        adx_factor   = np.minimum(1.0, adx_v / (cfg.ADX_TREND_THR * 2.0))

        # ── [M2] Pesos dinámicos ─────────────────────────────
        w_mom_dyn = cfg.W_MOM * (1 + adx_factor * 0.40)
        w_rev_dyn = np.maximum(cfg.W_REV * 0.30, cfg.W_REV * (1 - adx_factor * 0.50))
        w_tot     = w_mom_dyn + w_rev_dyn + cfg.W_VOL

        # ── L2 Factores ─────────────────────────────────────
        cs = np.roll(c, cfg.MOM_LEN); cs[:cfg.MOM_LEN] = c[:cfg.MOM_LEN]
        f_mom = _safe(
            _safe(c-cs, np.where(cs>1e-12,cs,np.nan)),
            _safe(_std(c,cfg.MOM_LEN), np.where(_sma(c,cfg.MOM_LEN)>1e-12,_sma(c,cfg.MOM_LEN),np.nan))
        )
        basis = _sma(c, cfg.REV_LEN); bs = _std(c, cfg.REV_LEN)
        f_rev = -_safe(c-basis, np.where(bs>1e-12,bs,np.nan))
        obv   = _obv(c,v); om = _ema(obv,cfg.VOL_LEN); os2 = _std(obv,cfg.VOL_LEN)
        f_vol = _safe(obv-om, np.where(os2>1e-12,os2,np.nan))

        raw   = _safe(w_mom_dyn*f_mom + w_rev_dyn*f_rev + cfg.W_VOL*f_vol,
                      np.where(w_tot>1e-12,w_tot,1.0))
        comp  = _ema(np.nan_to_num(raw), cfg.SMO_LEN)
        sc_s  = _std(comp, cfg.DECAY_LEN)
        norm  = np.nan_to_num(_tanh(_safe(comp, np.where(sc_s>1e-12,sc_s,np.nan))))

        # ── [M1] Decay adaptativo ────────────────────────────
        fwd   = _safe(np.diff(c, prepend=c[0]), c)
        ic    = _corr(np.roll(norm,1), fwd, cfg.DECAY_LEN)
        ic_r  = _ema(np.abs(np.nan_to_num(ic)), cfg.SMO_LEN)
        ic_pk = _high(ic_r, cfg.DECAY_LEN)
        decay = np.nan_to_num(_safe(ic_r, np.where(ic_pk>1e-12,ic_pk,np.nan), fill=0.5), nan=0.5)
        # OR percentil (evita bloqueo crónico en 3min)
        win3  = cfg.DECAY_LEN * 3
        ic_adapt = np.array([
            np.percentile(ic_r[max(0,i-win3):i+1], cfg.DECAY_ADAPT_PCT)
            for i in range(n)
        ])
        alive = (decay >= cfg.DECAY_THR) | (ic_r >= ic_adapt)

        # ── L1 Spread ────────────────────────────────────────
        hl_r    = np.where((h-l)<1e-12, 1e-12, h-l)
        spread_e= _sma(np.log(np.where(l>1e-12,h/l,1.0)), cfg.SPL_LEN) * c
        bp_drain= _safe(spread_e, c) * 100
        exec_ok = bp_drain < cfg.BP_THR

        # ── [M6] Filtro volatilidad ATR ─────────────────────
        atr_pct  = _safe(atr_v, c) * 100
        atr_avg20= _sma(atr_pct, 20)
        vol_ok_atr = atr_pct >= (atr_avg20 * cfg.VOL_ATR_THR)

        # ── HTF 15m ──────────────────────────────────────────
        c15 = df15["close"].values
        htf_bull_v = bool(_ema(c15,9)[-1] > _ema(c15,21)[-1])

        # ── L4 Dark Pool ─────────────────────────────────────
        vb      = _sma(v, cfg.DP_BASE)
        dp_buy  = (v > vb*cfg.DP_MULT) & ((h-l)<atr_v*0.6) & (c>o)
        dp_sell = (v > vb*cfg.DP_MULT) & ((h-l)<atr_v*0.6) & (c<o)

        # ── L6 Asimetría ─────────────────────────────────────
        ur = np.where(c>o, h-l, 0.0); dr = np.where(c<o, h-l, 0.0)
        aur= _sma(ur,cfg.ASY_LEN);    adr= _sma(dr,cfg.ASY_LEN)
        rb = _safe(aur, np.where(adr>1e-12,adr,np.nan), fill=1.0)
        rbe= _safe(adr, np.where(aur>1e-12,aur,np.nan), fill=1.0)
        ab = rb  >= cfg.ARR
        abe= rbe >= cfg.ABR

        # ── L7 Trendlines ────────────────────────────────────
        ph_a = _pivot_h(h, cfg.TL_LEFT, cfg.TL_RIGHT)
        pl_a = _pivot_l(l, cfg.PL_LEFT, cfg.PL_RIGHT)
        tl_bl, tl_bs = self._tl_breaks(h,l,c,atr_v,ph_a,pl_a,n)

        # ── L8 Swing ─────────────────────────────────────────
        se, be2, lsl, lsh = self._swing(h,l,c,pl_a,ph_a,n)

        # ── L9 FVG ───────────────────────────────────────────
        _,_,ibfvg,ibervg = self._fvg(h,l,c,atr_v)

        # ── L10 OB ───────────────────────────────────────────
        _,_,ibob,iberob  = self._ob(o,h,l,c,atr_v)

        # ── [M3] CVD rodante (ventana fija, sin deriva) ──────
        bvol = np.where(hl_r>1e-12, ((c-l)/hl_r)*v, v*0.5)
        svol = np.where(hl_r>1e-12, ((h-c)/hl_r)*v, v*0.5)
        delta_bar = bvol - svol
        cvd      = _roll_sum(delta_bar, cfg.CVD_ROLL)   # [M3] rodante
        cvd_e    = _ema(cvd, cfg.CVD_LEN)
        cvdr     = cvd > cvd_e
        dw       = cfg.CVD_DIV
        cvdbd    = np.zeros(n,bool); cvdad = np.zeros(n,bool)
        if n>dw:
            cvdbd[dw:] = (c[dw:]<c[:-dw]) & (cvd[dw:]>cvd[:-dw])
            cvdad[dw:] = (c[dw:]>c[:-dw]) & (cvd[dw:]<cvd[:-dw])

        # ── L12 Squeeze ──────────────────────────────────────
        sqb, sqbe, sqon = self._squeeze(h,l,c,atr_v)

        # ── VWAP ─────────────────────────────────────────────
        cum_v = np.cumsum(v)
        vwap  = _safe(np.cumsum(((h+l+c)/3)*v), np.where(cum_v>1e-12,cum_v,np.nan))
        avwap = c > np.nan_to_num(vwap)

        # ── Vol regime (original) ────────────────────────────
        vol_ratio= _safe(atr_pct, np.where(atr_avg20>1e-12,atr_avg20,np.nan), fill=1.0)
        vol_regime_arr = np.where(vol_ratio<0.6,"LOW",
                         np.where(vol_ratio>2.5,"HIGH","NORMAL"))

        # ── Trend gap ────────────────────────────────────────
        ema9  = _ema(c,9); ema21 = _ema(c,21)
        trend_v = _safe(np.abs(ema9-ema21), c)*100 > 0.15

        # ── 1h / 1m ─────────────────────────────────────────
        htf_1h_bull = htf_1h_bear = tf1m_bull = tf1m_bear = False
        if raw1h and len(raw1h)>=22 and cfg.USE_1H_FILTER:
            c1h = self._df(raw1h)["close"].values
            htf_1h_bull = bool(_ema(c1h,9)[-1]>_ema(c1h,21)[-1])
            htf_1h_bear = not htf_1h_bull
        if raw1m and len(raw1m)>=22:
            c1m = self._df(raw1m)["close"].values
            tf1m_bull = bool(_ema(c1m,9)[-1]>_ema(c1m,21)[-1])
            tf1m_bear = not tf1m_bull

        # ── L13/14/15 ────────────────────────────────────────
        ofi_val   = ctx.get("ofi",0.0) if ctx else 0.0
        fr_val    = ctx.get("funding_rate",0.0) if ctx else 0.0
        oi_cur    = ctx.get("open_interest",0.0) if ctx else 0.0
        oi_prev   = ctx.get("prev_open_interest",0.0) if ctx else 0.0
        oi_delta  = float(_safe(oi_cur-oi_prev, oi_prev if oi_prev>0 else 1.0))
        ofi_bull_w= ofi_val >  cfg.OFI_THR_WEAK
        ofi_bear_w= ofi_val < -cfg.OFI_THR_WEAK
        fr_bull   = fr_val  >  cfg.FR_BULL_THR
        fr_bear   = fr_val  <  cfg.FR_BEAR_THR
        fr_extreme= fr_val  >  cfg.FR_EXTREME_THR
        oi_rising = oi_delta >  cfg.OI_DELTA_THR
        oi_falling= oi_delta < -cfg.OI_DELTA_THR

        # ── Valores finales ──────────────────────────────────
        i = n-1
        ns   = float(norm[i]); dr_v = float(decay[i]); alv = bool(alive[i])
        exok = bool(exec_ok[i]); volatr = bool(vol_ok_atr[i])
        dpb  = bool(dp_buy[i]); dps = bool(dp_sell[i])
        ab_v = bool(ab[i]);     abe_v = bool(abe[i])
        se_v = bool(se[i]);     be_v  = bool(be2[i])
        tlbl = bool(tl_bl[i]); tlbs  = bool(tl_bs[i])
        ibf  = bool(ibfvg[i]); ibef  = bool(ibervg[i])
        ibo  = bool(ibob[i]);  ibeo  = bool(iberob[i])
        cvdr_v  = bool(cvdr[i]); cvdbd_v=bool(cvdbd[i]); cvdad_v=bool(cvdad[i])
        sqb_v   = bool(sqb[i]); sqbe_v =bool(sqbe[i])
        avwap_v = bool(avwap[i])
        trd_v   = bool(trend_v[i]); vol_reg = str(vol_regime_arr[i])
        last_sl = float(lsl[i]) if not np.isnan(lsl[i]) else None
        last_sh = float(lsh[i]) if not np.isnan(lsh[i]) else None
        atr_last= float(atr_v[i])
        adx_now = float(adx_v[i]); adx_str = bool(trend_strong[i])
        multi_tf_long  = htf_bull_v and htf_1h_bull and tf1m_bull
        multi_tf_short = (not htf_bull_v) and htf_1h_bear and tf1m_bear
        h1ok_l = (not cfg.USE_1H_FILTER) or htf_1h_bull
        h1ok_s = (not cfg.USE_1H_FILTER) or htf_1h_bear

        # ══════════════════════════════════════════════════════
        #  SCORE COMPUESTO 0-100 (como Pine v3.2)
        # ══════════════════════════════════════════════════════
        ns_n   = (np.tanh(ns) + 1) / 2
        mom_n  = (np.tanh(float(f_mom[i]) * 2) + 1) / 2
        dec_n  = min(1.0, dr_v)
        cvd_sc = max(0.0, min(1.0, (np.tanh(float(_safe(
            cvd[i]-float(cvd_e[i]),
            max(float(_std(cvd,cfg.CVD_LEN*2)[i]),1e-12)
        ))) + 1) / 2))
        htf_asym_l = (0.5 if htf_bull_v else 0.0) + (0.5 if ab_v else 0.0)
        htf_asym_s = (0.5 if not htf_bull_v else 0.0) + (0.5 if abe_v else 0.0)

        W_SC=0.30; W_CVD=0.25; W_MOM=0.20; W_DEC=0.15; W_HTF=0.10
        cb_l_base = int((W_SC*ns_n + W_CVD*cvd_sc + W_MOM*mom_n + W_DEC*dec_n + W_HTF*htf_asym_l)*100)
        cb_s_base = int((W_SC*(1-ns_n) + W_CVD*(1-cvd_sc) + W_MOM*(1-mom_n) + W_DEC*dec_n + W_HTF*htf_asym_s)*100)

        # ── Conviction 0-10 ──────────────────────────────────
        long_conv = sum([
            ns > 0.10, alv, exok, htf_bull_v,
            ab_v, se_v, tlbl, dpb, cvdr_v,
            (sqb_v or ibf or ibo),
            avwap_v and trd_v,
            ofi_bull_w, fr_bull, oi_rising,
            bool(adx_str and adx_v[i]>0 and dmi_p[i]>dmi_m[i]),  # ADX alcista
            volatr,                                                  # [M6]
        ])
        short_conv = sum([
            ns < -0.10, alv, exok, not htf_bull_v,
            abe_v, be_v, tlbs, dps, not cvdr_v,
            (sqbe_v or ibef or ibeo),
            (not avwap_v) and trd_v,
            ofi_bear_w, fr_bear, oi_rising,
            bool(adx_str and dmi_m[i]>dmi_p[i]),
            volatr,
        ])
        long_conv  = min(long_conv,  10)
        short_conv = min(short_conv, 10)

        # ── [M5] Conv-Boost ──────────────────────────────────
        comp_long  = min(100, cb_l_base + int(long_conv  * 0.5))
        comp_short = min(100, cb_s_base + int(short_conv * 0.5))

        # ── Señales (igual que Pine) ─────────────────────────
        SC_STD=cfg.SC_THR_STD; SC_FUEL=cfg.SC_THR_FUEL; SC_SUP=cfg.SC_THR_SUP
        vol_ok = (vol_reg == "NORMAL") and volatr  # [M6]

        long_base  = (comp_long  >= SC_STD and exok and alv and vol_ok
                      and htf_bull_v and h1ok_l and not fr_extreme)
        long_std   = long_base  and ab_v and se_v
        long_fuel  = long_std   and comp_long >= SC_FUEL and (
                        tlbl or sqb_v or ((ibf or ibo) and cvdr_v))
        long_sup   = long_fuel  and comp_long >= SC_SUP  and (dpb or cvdbd_v)

        short_base = (comp_short >= SC_STD and exok and alv and vol_ok
                      and not htf_bull_v and h1ok_s)
        short_std  = short_base  and abe_v and be_v
        short_fuel = short_std   and comp_short >= SC_FUEL and (
                        tlbs or sqbe_v or ((ibef or ibeo) and not cvdr_v))
        short_sup  = short_fuel  and comp_short >= SC_SUP  and (dps or cvdad_v)

        # ── Multi-TF bonus conviction ─────────────────────────
        if multi_tf_long:  long_conv  = min(10, long_conv  + cfg.MULTI_TF_BONUS)
        if multi_tf_short: short_conv = min(10, short_conv + cfg.MULTI_TF_BONUS)

        direction = tier = None; conviction = 0; sl_p = 0.0; tp_p = None
        if long_sup or long_fuel or long_std:
            direction  = "LONG"
            tier       = "SUP" if long_sup else ("FUEL" if long_fuel else "STD")
            conviction = long_conv
            sl_p = last_sl if last_sl else c[i] - atr_v[i]*2.0
            tp_p = c[i] + (c[i]-sl_p)*cfg.TP_RR
        elif short_sup or short_fuel or short_std:
            direction  = "SHORT"
            tier       = "SUP" if short_sup else ("FUEL" if short_fuel else "STD")
            conviction = short_conv
            sl_p = last_sh if last_sh else c[i] + atr_v[i]*2.0
            tp_p = c[i] - (sl_p-c[i])*cfg.TP_RR

        return Signal(
            direction=direction, tier=tier or "STD", conviction=conviction,
            sl=sl_p, tp=tp_p, atr_last=atr_last,
            norm_score=ns, decay_ratio=dr_v, sig_alive=alv,
            exec_ok=exok, htf_bull=htf_bull_v, htf_bear=not htf_bull_v,
            asym_bull=ab_v, asym_bear=abe_v,
            sell_exhausted=se_v, buy_exhausted=be_v,
            tl_break_long=tlbl, tl_break_short=tlbs,
            dp_buy=dpb, dp_sell=dps,
            cvd_rising=cvdr_v, cvd_bull_div=cvdbd_v, cvd_bear_div=cvdad_v,
            sq_bull=sqb_v, sq_bear=sqbe_v,
            in_bull_fvg=ibf, in_bear_fvg=ibef,
            in_bull_ob=ibo, in_bear_ob=ibeo,
            above_vwap=avwap_v, trending=trd_v,
            vol_regime=vol_reg, vol_ok_atr=volatr,
            adx_val=adx_now, adx_trend=adx_str,
            comp_long=comp_long, comp_short=comp_short,
            ofi=ofi_val, ofi_bull=ofi_bull_w, ofi_bear=ofi_bear_w,
            funding_rate=fr_val, fr_bull=fr_bull, fr_bear=fr_bear, fr_extreme=fr_extreme,
            oi_delta=oi_delta, oi_rising=oi_rising,
            htf_1h_bull=htf_1h_bull, htf_1h_bear=htf_1h_bear,
            multi_tf_aligned=(multi_tf_long if direction=="LONG" else
                              multi_tf_short if direction=="SHORT" else False),
        )

    def _df(self, raw):
        if not raw:
            return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])
        rows = []
        for k in raw:
            if isinstance(k, (list, tuple)) and len(k) >= 6:
                rows.append([k[0],k[1],k[2],k[3],k[4],k[5]])
            elif isinstance(k, dict):
                ts = k.get("timestamp") or k.get("time") or k.get("t") or 0
                rows.append([ts, k.get("open",0), k.get("high",0),
                              k.get("low",0), k.get("close",0), k.get("volume",0)])
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        for col in ["timestamp","open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna().reset_index(drop=True)

    def _tl_breaks(self, h, l, c, atr_v, ph_a, pl_a, n):
        tbl=np.zeros(n,bool); tbs=np.zeros(n,bool)
        phi=np.where(~np.isnan(ph_a))[0]; pli=np.where(~np.isnan(pl_a))[0]
        if len(phi)>=2:
            a,b=phi[-2],phi[-1]
            if ph_a[a]>ph_a[b] and (n-1-a)<=cfg.TL_LOOKBACK:
                sl=(ph_a[b]-ph_a[a])/max(b-a,1)
                for i in range(b+1,n):
                    tn=ph_a[b]+sl*(i-b); tp_=ph_a[b]+sl*(i-1-b)
                    if c[i]>tn+atr_v[i]*cfg.TL_BUF and c[i-1]<=tp_+atr_v[i]*cfg.TL_BUF:
                        tbl[i]=True
        if len(pli)>=2:
            a,b=pli[-2],pli[-1]
            if pl_a[a]<pl_a[b] and (n-1-a)<=cfg.TL_LOOKBACK:
                sl=(pl_a[b]-pl_a[a])/max(b-a,1)
                for i in range(b+1,n):
                    tn=pl_a[b]+sl*(i-b); tp_=pl_a[b]+sl*(i-1-b)
                    if c[i]<tn-atr_v[i]*cfg.TL_BUF and c[i-1]>=tp_-atr_v[i]*cfg.TL_BUF:
                        tbs[i]=True
        return tbl,tbs

    def _swing(self, h, l, c, pl_a, ph_a, n):
        se=np.zeros(n,bool); be=np.zeros(n,bool)
        lsl=np.full(n,np.nan); lsh=np.full(n,np.nan)
        for i in range(cfg.HL_WINDOW,n):
            sv=[pl_a[j] for j in range(max(0,i-cfg.HL_WINDOW),i+1) if not np.isnan(pl_a[j])]
            sh=[ph_a[j] for j in range(max(0,i-cfg.HL_WINDOW),i+1) if not np.isnan(ph_a[j])]
            if sv: lsl[i]=sv[-1]; se[i]=sum(sv[k]>sv[k-1] for k in range(1,len(sv)))>=cfg.HL_COUNT
            if sh: lsh[i]=sh[-1]; be[i]=sum(sh[k]<sh[k-1] for k in range(1,len(sh)))>=cfg.HH_COUNT
        return se,be,lsl,lsh

    def _fvg(self, h, l, c, atr_v):
        n=len(c); bf=np.zeros(n,bool); bef=np.zeros(n,bool)
        ibf=np.zeros(n,bool); ibef=np.zeros(n,bool)
        bt=bn=np.nan; st=sn=np.nan; ba=sa=0
        for i in range(2,n):
            ms=atr_v[i]*cfg.FVG_MIN
            if l[i]>h[i-2] and (l[i]-h[i-2])>ms: bt=l[i];bn=h[i-2];ba=0;bf[i]=True
            else:
                ba+=1
                if ba>cfg.FVG_BARS or (cfg.FVG_MITI and c[i]<bn): bt=bn=np.nan
            if h[i]<l[i-2] and (l[i-2]-h[i])>ms: st=l[i-2];sn=h[i];sa=0;bef[i]=True
            else:
                sa+=1
                if sa>cfg.FVG_BARS or (cfg.FVG_MITI and c[i]>st): st=sn=np.nan
            if not np.isnan(bt) and bn<=c[i]<=bt: ibf[i]=True
            if not np.isnan(st) and sn<=c[i]<=st: ibef[i]=True
        return bf,bef,ibf,ibef

    def _ob(self, o, h, l, c, atr_v):
        n=len(c); bob=np.zeros(n,bool); beo=np.zeros(n,bool)
        ibob=np.zeros(n,bool); ibeo=np.zeros(n,bool)
        bh=bl=np.nan; sh=sl=np.nan; ba=sa=0
        for i in range(2,n):
            imp=atr_v[i]*cfg.OB_IMP
            if (c[i]-o[i])>imp and c[i]>c[i-1] and c[i-1]<o[i-1]:
                bh=o[i-1];bl=c[i-1];ba=0;bob[i]=True
            else:
                ba+=1
                if ba>cfg.OB_BARS or c[i]<bl: bh=bl=np.nan
            if (o[i]-c[i])>imp and c[i]<c[i-1] and c[i-1]>o[i-1]:
                sh=c[i-1];sl=o[i-1];sa=0;beo[i]=True
            else:
                sa+=1
                if sa>cfg.OB_BARS or c[i]>sh: sh=sl=np.nan
            if not np.isnan(bh) and bl<=c[i]<=bh: ibob[i]=True
            if not np.isnan(sh) and sl<=c[i]<=sh: ibeo[i]=True
        return bob,beo,ibob,ibeo

    def _squeeze(self, h, l, c, atr_v):
        n=len(c); p=cfg.SQ_LEN
        bs=_sma(c,p); dv=_std(c,p)
        bbh=bs+cfg.SQ_BBM*dv; bbl=bs-cfg.SQ_BBM*dv
        ke=_ema(c,p); kch=ke+cfg.SQ_KCM*atr_v; kcl=ke-cfg.SQ_KCM*atr_v
        sqon=(bbh<kch)&(bbl>kcl)
        sqfire=~sqon&np.roll(sqon,1); sqfire[0]=False
        hm=_high(h,p); lm=_low(l,p)
        sv=_linreg(c-(hm+lm)/2, p)
        return sqfire&(sv>0), sqfire&(sv<0), sqon
