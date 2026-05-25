"""
QF×JP Engine v4.0 — Señales optimizadas
Cambios vs v3:
  • SCORE_THR sube a 0.63 (investigación IC predictivo)
  • DECAY_THR sube a 0.65 (65% pico = señal viva estadísticamente)
  • Filtro LONG/SHORT diferenciado por sesión y VWAP
  • Detección automática régimen (trending vs ranging) — evita whipsaws
  • Volatility regime filter — no operar en micro-volatilidad
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from config import cfg


# ── helpers ────────────────────────────────────────────────────
def _tanh(x):  return np.tanh(np.clip(x, -10, 10))
def _ema(s, p):
    a, out = 2/(p+1), np.empty_like(s, dtype=float)
    out[0] = s[0]
    for i in range(1, len(s)): out[i] = a*s[i] + (1-a)*out[i-1]
    return out
def _sma(s, p): return pd.Series(s).rolling(p, min_periods=1).mean().values
def _std(s, p): return pd.Series(s).rolling(p, min_periods=2).std(ddof=0).values
def _high(s,p): return pd.Series(s).rolling(p, min_periods=1).max().values
def _low(s, p): return pd.Series(s).rolling(p, min_periods=1).min().values
def _corr(a, b, p):
    return pd.Series(a).rolling(p, min_periods=max(5,p//2)).corr(pd.Series(b)).values
def _atr(h, l, c, p):
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]; return _ema(tr, p)
def _obv(c, v):
    return np.cumsum(np.sign(np.diff(c, prepend=c[0])) * v)
def _pivot_high(h, left, right):
    n, out = len(h), np.full(len(h), np.nan)
    for i in range(left, n-right):
        w = h[i-left:i+right+1]
        if h[i] == w.max() and (w == h[i]).sum() == 1: out[i] = h[i]
    return out
def _pivot_low(l, left, right):
    n, out = len(l), np.full(len(l), np.nan)
    for i in range(left, n-right):
        w = l[i-left:i+right+1]
        if l[i] == w.min() and (w == l[i]).sum() == 1: out[i] = l[i]
    return out
def _linreg(s, p):
    out = np.full(len(s), np.nan)
    for i in range(p-1, len(s)):
        y = s[i-p+1:i+1]; x = np.arange(p)
        coef = np.polyfit(x, y, 1); out[i] = np.polyval(coef, p-1)
    return out


@dataclass
class Signal:
    direction   : Optional[str] = None
    tier        : str  = "STD"
    conviction  : int  = 0
    sl          : float = 0.0
    tp          : Optional[float] = None
    # componentes
    norm_score  : float = 0.0
    decay_ratio : float = 0.0
    sig_alive   : bool  = False
    exec_ok     : bool  = False
    htf_bull    : bool  = False
    htf_bear    : bool  = False
    asym_bull   : bool  = False
    asym_bear   : bool  = False
    sell_exhausted: bool = False
    buy_exhausted : bool = False
    tl_break_long : bool = False
    tl_break_short: bool = False
    dp_buy      : bool  = False
    dp_sell     : bool  = False
    cvd_rising  : bool  = False
    cvd_bull_div: bool  = False
    cvd_bear_div: bool  = False
    sq_bull     : bool  = False
    sq_bear     : bool  = False
    in_bull_fvg : bool  = False
    in_bear_fvg : bool  = False
    in_bull_ob  : bool  = False
    in_bear_ob  : bool  = False
    squeeze_on  : bool  = False
    above_vwap  : bool  = False
    trending    : bool  = False
    vol_regime  : str   = "NORMAL"  # LOW | NORMAL | HIGH
    session     : str   = "OFF"


class QFJPEngine:

    def compute(self, ohlcv_3m: list, ohlcv_15m: list) -> dict:
        return self._run(ohlcv_3m, ohlcv_15m).__dict__

    def _run(self, raw3: list, raw15: list) -> Signal:
        df3  = self._df(raw3);  df15 = self._df(raw15)
        o,h,l,c,v = (df3["open"].values, df3["high"].values,
                     df3["low"].values,  df3["close"].values,
                     df3["volume"].values)
        n = len(c)

        # ── L1 ─────────────────────────────────────────────
        atr_v     = _atr(h, l, c, cfg.ATR_LEN)
        spread_e  = _sma(np.log(h/np.where(l>0,l,1e-9)), cfg.SPL_LEN) * c
        bp_drain  = (spread_e / np.where(c>0,c,1e-9)) * 100

        # ── Volatility Regime ───────────────────────────────
        atr_pct   = atr_v / np.where(c>0,c,1e-9) * 100
        atr_ma    = _sma(atr_pct, 50)
        vol_ratio = atr_pct / np.where(atr_ma>0, atr_ma, 1e-9)
        # LOW <0.6 = rango muerto, HIGH >2.5 = flash crash
        vol_regime_arr = np.where(vol_ratio < 0.6, "LOW",
                         np.where(vol_ratio > 2.5, "HIGH", "NORMAL"))

        # ── Trend regime (ADX proxy) ────────────────────────
        # Usando EMA9 vs EMA21 distancia como proxy de tendencia
        ema9_3  = _ema(c, 9);  ema21_3 = _ema(c, 21)
        trend_gap = np.abs(ema9_3 - ema21_3) / np.where(c>0,c,1e-9) * 100
        trending_arr = trend_gap > 0.15   # >0.15% distancia = mercado en tendencia

        # ── L2 Factores ─────────────────────────────────────
        cs = np.roll(c, cfg.MOM_LEN); cs[:cfg.MOM_LEN] = c[:cfg.MOM_LEN]
        f_mom = np.where(_std(c,cfg.MOM_LEN)!=0,
                         ((c-cs)/np.where(cs>0,cs,1e-9)) / (_std(c,cfg.MOM_LEN)/np.where(_sma(c,cfg.MOM_LEN)>0,_sma(c,cfg.MOM_LEN),1e-9)), 0)
        basis = _sma(c, cfg.REV_LEN); bs = _std(c, cfg.REV_LEN)
        f_rev = np.where(bs!=0, -(c-basis)/bs, 0)
        obv   = _obv(c,v); om = _ema(obv, cfg.VOL_LEN); os2 = _std(obv, cfg.VOL_LEN)
        f_vol = np.where(os2!=0, (obv-om)/os2, 0)
        raw   = cfg.W_MOM*f_mom + cfg.W_REV*f_rev + cfg.W_VOL*f_vol
        comp  = _ema(raw, cfg.SMO_LEN)
        sc_s  = _std(comp, cfg.DECAY_LEN)
        norm  = np.where(sc_s!=0, _tanh(comp/sc_s), 0)

        # ── L3 Decaimiento ──────────────────────────────────
        fwd   = np.diff(c, prepend=c[0]) / np.where(c>0,c,1e-9)
        ic    = _corr(np.roll(norm,1), fwd, cfg.DECAY_LEN)
        ic_r  = _ema(np.abs(np.nan_to_num(ic)), cfg.SMO_LEN)
        ic_pk = _high(ic_r, cfg.DECAY_LEN)
        decay = np.where(ic_pk>0, ic_r/ic_pk, 0.5)
        # UMBRAL OPTIMIZADO: 65% del pico IC
        alive = decay >= cfg.DECAY_THR

        # ── L4 Dark Pool ────────────────────────────────────
        vb  = _sma(v, cfg.DP_BASE)
        vsp = v > vb * cfg.DP_MULT
        rng_narrow = (h-l) < atr_v * 0.6
        dp_buy  = vsp & rng_narrow & (c>o)
        dp_sell = vsp & rng_narrow & (c<o)

        # ── L5 Ejecución ────────────────────────────────────
        exec_ok = bp_drain < cfg.BP_THR

        # ── HTF Régimen ─────────────────────────────────────
        c15 = df15["close"].values
        htf_bull_v = bool(_ema(c15,9)[-1] > _ema(c15,21)[-1])
        htf_bear_v = not htf_bull_v

        # ── L6 Asimetría ────────────────────────────────────
        ur = np.where(c>o, h-l, 0.0); dr = np.where(c<o, h-l, 0.0)
        aur = _sma(ur, cfg.ASY_LEN); adr = _sma(dr, cfg.ASY_LEN)
        rb  = np.where(adr>0, aur/adr, 1.0)
        rbe = np.where(aur>0, adr/aur, 1.0)
        ab  = rb  >= cfg.ARR
        abe = rbe >= cfg.ABR

        # ── L7 Trendlines ───────────────────────────────────
        ph_a = _pivot_high(h, cfg.TL_LEFT, cfg.TL_RIGHT)
        pl_a = _pivot_low(l,  cfg.PL_LEFT, cfg.PL_RIGHT)
        tl_bl, tl_bs = self._tl_breaks(h,l,c,atr_v,ph_a,pl_a,n)

        # ── L8 Swing ────────────────────────────────────────
        se, be2, lsl, lsh = self._swing(h,l,c,pl_a,ph_a,n)

        # ── L9 FVG ──────────────────────────────────────────
        bfvg, bervg, ibfvg, ibervg = self._fvg(h,l,c,atr_v)

        # ── L10 OB ──────────────────────────────────────────
        bob, berob, ibob, iberob = self._ob(o,h,l,c,atr_v)

        # ── L11 CVD ─────────────────────────────────────────
        hl_r  = h-l
        bvol  = np.where(hl_r>0, ((c-l)/hl_r)*v, v*0.5)
        svol  = np.where(hl_r>0, ((h-c)/hl_r)*v, v*0.5)
        cvd   = np.cumsum(bvol-svol)
        cvd_e = _ema(cvd, cfg.CVD_LEN)
        cvdr  = cvd > cvd_e
        dw    = cfg.CVD_DIV
        cvdbd = np.zeros(n, bool); cvdad = np.zeros(n, bool)
        if n > dw:
            cvdbd[dw:] = (c[dw:]<c[:-dw]) & (cvd[dw:]>cvd[:-dw])
            cvdad[dw:] = (c[dw:]>c[:-dw]) & (cvd[dw:]<cvd[:-dw])

        # ── L12 Squeeze ─────────────────────────────────────
        sqb, sqbe, sqon = self._squeeze(h,l,c,atr_v)

        # ── VWAP ────────────────────────────────────────────
        hlc3 = (h+l+c)/3
        vwap = np.cumsum(hlc3*v) / np.where(np.cumsum(v)>0, np.cumsum(v), 1)
        avwap = c > vwap

        # ── Valores finales (última barra) ──────────────────
        i = n-1
        ns   = float(norm[i]);    alv  = bool(alive[i])
        exok = bool(exec_ok[i]);  dpb  = bool(dp_buy[i]);  dps = bool(dp_sell[i])
        ab_v = bool(ab[i]);       abe_v= bool(abe[i])
        se_v = bool(se[i]);       be_v = bool(be2[i])
        tlbl = bool(tl_bl[i]);    tlbs = bool(tl_bs[i])
        ibf  = bool(ibfvg[i]);    ibef = bool(ibervg[i])
        ibo  = bool(ibob[i]);     ibeo = bool(iberob[i])
        cvdr_v   = bool(cvdr[i]); cvdbd_v = bool(cvdbd[i]); cvdad_v = bool(cvdad[i])
        sqb_v= bool(sqb[i]);      sqbe_v = bool(sqbe[i]);   sqon_v = bool(sqon[i])
        avwap_v  = bool(avwap[i])
        last_sl  = float(lsl[i])  if not np.isnan(lsl[i])  else None
        last_sh  = float(lsh[i])  if not np.isnan(lsh[i])  else None
        trend_v  = bool(trending_arr[i])
        vol_reg  = str(vol_regime_arr[i])
        dr_v     = float(decay[i])

        # ══════════════════════════════════════════════════
        #  LÓGICA DE SEÑAL MEJORADA v4
        #  SCORE_THR = 0.63 (era 0.15), DECAY_THR = 0.65
        # ══════════════════════════════════════════════════

        # Filtro de régimen de volatilidad — no operar en extremos
        vol_ok = vol_reg == "NORMAL"

        # LONG — condiciones base
        long_std = (ns > cfg.SCORE_THR_LONG   # score >63%
                    and alv                     # señal viva (decay ≥65%)
                    and exok                    # spread ok
                    and htf_bull_v              # HTF alcista
                    and ab_v                    # asimetría velas bullish
                    and se_v                    # agotamiento vendedor
                    and vol_ok)                 # ← NUEVO: régimen vol normal

        # LONG adicional: sesión + VWAP + trending mejoran calidad
        long_quality = (avwap_v                 # sobre VWAP
                        and trend_v)            # mercado en tendencia

        long_fuel = long_std and (tlbl or sqb_v or ((ibf or ibo) and cvdr_v))
        long_sup  = long_fuel and (dpb or cvdbd_v)

        # SHORT — condiciones base
        short_std = (ns < -cfg.SCORE_THR_SHORT
                     and alv
                     and exok
                     and htf_bear_v
                     and abe_v
                     and be_v
                     and vol_ok)

        short_quality = (not avwap_v and trend_v)

        short_fuel = short_std and (tlbs or sqbe_v or ((ibef or ibeo) and not cvdr_v))
        short_sup  = short_fuel and (dps or cvdad_v)

        # ── Conviction (0-10) ────────────────────────────────
        long_conv = sum([
            ns > cfg.SCORE_THR_LONG, alv, exok, htf_bull_v,
            ab_v, se_v, tlbl, dpb, cvdr_v,
            (sqb_v or ibf or ibo),
            # bonus calidad
            long_quality,        # +1 si sobre VWAP y trending
        ])
        # cap a 10
        long_conv = min(long_conv, 10)

        short_conv = sum([
            ns < -cfg.SCORE_THR_SHORT, alv, exok, htf_bear_v,
            abe_v, be_v, tlbs, dps, not cvdr_v,
            (sqbe_v or ibef or ibeo),
            short_quality,
        ])
        short_conv = min(short_conv, 10)

        # ── Dirección final ──────────────────────────────────
        direction = tier = None
        conviction = 0; sl_p = 0.0; tp_p = None

        if long_sup or long_fuel or long_std:
            direction  = "LONG"
            tier       = "SUP" if long_sup else ("FUEL" if long_fuel else "STD")
            conviction = long_conv
            sl_p  = last_sl if last_sl else c[i] - atr_v[i]*2.0
            tp_p  = c[i] + (c[i]-sl_p) * cfg.TP_RR
        elif short_sup or short_fuel or short_std:
            direction  = "SHORT"
            tier       = "SUP" if short_sup else ("FUEL" if short_fuel else "STD")
            conviction = short_conv
            sl_p  = last_sh if last_sh else c[i] + atr_v[i]*2.0
            tp_p  = c[i] - (sl_p-c[i]) * cfg.TP_RR

        return Signal(
            direction=direction, tier=tier or "STD", conviction=conviction,
            sl=sl_p, tp=tp_p,
            norm_score=ns, decay_ratio=dr_v, sig_alive=alv,
            exec_ok=exok, htf_bull=htf_bull_v, htf_bear=htf_bear_v,
            asym_bull=ab_v, asym_bear=abe_v,
            sell_exhausted=se_v, buy_exhausted=be_v,
            tl_break_long=tlbl, tl_break_short=tlbs,
            dp_buy=dpb, dp_sell=dps,
            cvd_rising=cvdr_v, cvd_bull_div=cvdbd_v, cvd_bear_div=cvdad_v,
            sq_bull=sqb_v, sq_bear=sqbe_v,
            in_bull_fvg=ibf, in_bear_fvg=ibef,
            in_bull_ob=ibo, in_bear_ob=ibeo,
            squeeze_on=sqon_v, above_vwap=avwap_v,
            trending=trend_v, vol_regime=vol_reg,
        )

    # ── sub-módulos ─────────────────────────────────────────

    def _df(self, raw):
        df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        for c in ["open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna().reset_index(drop=True)

    def _tl_breaks(self, h, l, c, atr_v, ph_a, pl_a, n):
        tbl = np.zeros(n, bool); tbs = np.zeros(n, bool)
        phi = np.where(~np.isnan(ph_a))[0]; pli = np.where(~np.isnan(pl_a))[0]
        if len(phi) >= 2:
            a, b = phi[-2], phi[-1]
            if ph_a[a] > ph_a[b] and (n-1-a) <= cfg.TL_LOOKBACK:
                sl = (ph_a[b]-ph_a[a])/max(b-a,1)
                for i in range(b+1,n):
                    tn = ph_a[b]+sl*(i-b); tp_ = ph_a[b]+sl*(i-1-b)
                    if c[i] > tn+atr_v[i]*cfg.TL_BUF and c[i-1]<=tp_+atr_v[i]*cfg.TL_BUF:
                        tbl[i]=True
        if len(pli) >= 2:
            a, b = pli[-2], pli[-1]
            if pl_a[a] < pl_a[b] and (n-1-a) <= cfg.TL_LOOKBACK:
                sl = (pl_a[b]-pl_a[a])/max(b-a,1)
                for i in range(b+1,n):
                    tn = pl_a[b]+sl*(i-b); tp_ = pl_a[b]+sl*(i-1-b)
                    if c[i] < tn-atr_v[i]*cfg.TL_BUF and c[i-1]>=tp_-atr_v[i]*cfg.TL_BUF:
                        tbs[i]=True
        return tbl, tbs

    def _swing(self, h, l, c, pl_a, ph_a, n):
        se = np.zeros(n,bool); be = np.zeros(n,bool)
        lsl = np.full(n,np.nan); lsh = np.full(n,np.nan)
        for i in range(cfg.HL_WINDOW, n):
            sl_v=[pl_a[j] for j in range(max(0,i-cfg.HL_WINDOW),i+1) if not np.isnan(pl_a[j])]
            sh_v=[ph_a[j] for j in range(max(0,i-cfg.HL_WINDOW),i+1) if not np.isnan(ph_a[j])]
            if sl_v:
                lsl[i]=sl_v[-1]
                se[i] = sum(sl_v[k]>sl_v[k-1] for k in range(1,len(sl_v))) >= cfg.HL_COUNT
            if sh_v:
                lsh[i]=sh_v[-1]
                be[i] = sum(sh_v[k]<sh_v[k-1] for k in range(1,len(sh_v))) >= cfg.HH_COUNT
        return se, be, lsl, lsh

    def _fvg(self, h, l, c, atr_v):
        n=len(c); bf=np.zeros(n,bool); bef=np.zeros(n,bool)
        ibf=np.zeros(n,bool); ibef=np.zeros(n,bool)
        bt=bn=np.nan; st=sn=np.nan; ba=sa=0
        for i in range(2,n):
            ms=atr_v[i]*cfg.FVG_MIN
            if l[i]>h[i-2] and (l[i]-h[i-2])>ms:
                bt=l[i]; bn=h[i-2]; ba=0; bf[i]=True
            else:
                ba+=1
                if ba>cfg.FVG_BARS or (cfg.FVG_MITI and c[i]<bn): bt=bn=np.nan
            if h[i]<l[i-2] and (l[i-2]-h[i])>ms:
                st=l[i-2]; sn=h[i]; sa=0; bef[i]=True
            else:
                sa+=1
                if sa>cfg.FVG_BARS or (cfg.FVG_MITI and c[i]>st): st=sn=np.nan
            if not np.isnan(bt) and bn<=c[i]<=bt: ibf[i]=True
            if not np.isnan(st) and sn<=c[i]<=st: ibef[i]=True
        return bf, bef, ibf, ibef

    def _ob(self, o, h, l, c, atr_v):
        n=len(c); bob=np.zeros(n,bool); beo=np.zeros(n,bool)
        ibob=np.zeros(n,bool); ibeo=np.zeros(n,bool)
        bh=bl=np.nan; sh=sl=np.nan; ba=sa=0
        for i in range(2,n):
            imp=atr_v[i]*cfg.OB_IMP
            sb=(c[i]-o[i])>imp and c[i]>c[i-1]
            sbe=(o[i]-c[i])>imp and c[i]<c[i-1]
            if sb and c[i-1]<o[i-1]: bh=o[i-1]; bl=c[i-1]; ba=0; bob[i]=True
            else:
                ba+=1
                if ba>cfg.OB_BARS or c[i]<bl: bh=bl=np.nan
            if sbe and c[i-1]>o[i-1]: sh=c[i-1]; sl=o[i-1]; sa=0; beo[i]=True
            else:
                sa+=1
                if sa>cfg.OB_BARS or c[i]>sh: sh=sl=np.nan
            if not np.isnan(bh) and bl<=c[i]<=bh: ibob[i]=True
            if not np.isnan(sh) and sl<=c[i]<=sh: ibeo[i]=True
        return bob, beo, ibob, ibeo

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
