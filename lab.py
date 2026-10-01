"""
LABORATORIO de dos ideas sin estudios publicados que conozcamos. Se examinan a la vez.

A · RELOJ DEL FUNDING
    Hipótesis: con funding extremo, parte de los que pagan cierran JUSTO ANTES del cobro
    para no pagarlo y reabren después → presión en contra antes del cobro.
    Señal SIN mirar el futuro: la tasa del cobro ANTERIOR (conocida). Si fue ≥ umbral →
    corto; si ≤ −umbral → largo. Ventanas relativas al cobro T.
    Si la posición sigue abierta en T, cobra/paga ESE funding (se suma al resultado).

B · PRIMA DE BINGX SOBRE BINANCE
    Hipótesis: en subidones de altcoins, el precio de BingX (mucho minorista y copy-trading)
    se estira respecto a Binance y vuelve en minutos.
    Desviación = prima BingX/Binance − su mediana de las 24 h anteriores.
    Si ≥ umbral → corto en BingX; si ≤ −umbral → largo. Entrada en la APERTURA de la vela
    siguiente (5 min tarde: conservador). Solo se opera BingX: si la prima se cierra porque
    se mueve Binance y no BingX, aquí sale perdiendo, como pasaría en real.

Rigor (igual que el examen del P12):
  · 6 variantes PRE-REGISTRADAS por idea; se exige t ≥ 3 en la mejor (corrige por probar 6).
  · t AGRUPADA por instante: varias monedas en el mismo minuto no son pruebas independientes.
  · Placebo: la misma mecánica donde la hipótesis dice que NO debe haber efecto.
  · Entrenamiento/prueba 70/30 cronológico. Costes: 0.16% ida y vuelta (taker + deslizamiento).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

import data5
import wec

A_SYMBOLS = ("BTC,ETH,SOL,XRP,DOGE,BNB,ADA,AVAX,LINK,SUI,LTC,AAVE,NEAR,TRUMP,ENA,WIF,ARB,OP,TIA,INJ,"
             "SEI,APT,FIL,TAO,ZEC,LDO,CRV,ORDI,WLD,ONDO")
B_SYMBOLS = ("SOL,XRP,DOGE,ADA,AVAX,LINK,SUI,LTC,AAVE,NEAR,TRUMP,ENA,WIF,ARB,OP,TIA,INJ,SEI,APT,FIL,"
             "TAO,ZEC,LDO,CRV,ORDI,WLD,ONDO,JUP,STX,DYDX,GALA,SAND,AXS,IMX,FET,KAS,HBAR,ICP,ETC,UNI")

C_SYMBOLS = ("BTC,SOL,DOGE,ADA,AVAX,LINK,SUI,NEAR,TRUMP,ENA,WIF,ARB,OP,TIA,INJ,SEI,APT,FIL,TAO,ZEC,"
             "LDO,CRV,ORDI,WLD,ONDO,JUP,GALA,FET,KAS,PEOPLE,MEW,POPCAT,NOT,BOME,TURBO")

COST_RT = 0.0016
A_VARIANTS = [(thr, a, b, name) for thr in (0.0005, 0.0010)
              for a, b, name in ((-30, 0, "antes (−30→T)"), (-30, 30, "cruza (−30→+30)"), (0, 60, "después (T→+60)"))]
B_VARIANTS = [(thr, mode) for thr in (0.003, 0.005, 0.010) for mode in ("converge", "fijo15")]


# ───────────────────────── estadística común ─────────────────────────
def cluster_t(df: pd.DataFrame, col="net", key="t_entry"):
    """t de la media agrupando por instante de entrada (eventos simultáneos cuentan como uno)."""
    if len(df) < 5:
        return np.nan
    g = df.groupby(key)[col].mean()
    if len(g) < 5 or g.std() == 0:
        return np.nan
    return g.mean() / (g.std(ddof=1) / math.sqrt(len(g)))


def summarize(df: pd.DataFrame, placebo: float) -> dict:
    if df.empty:
        return dict(n=0, clusters=0, win=np.nan, mean=np.nan, t=np.nan, tr=np.nan, te=np.nan, placebo=placebo)
    df = df.sort_values("t_entry")
    k = int(len(df) * 0.7)
    return dict(n=len(df), clusters=df["t_entry"].nunique(), win=(df["net"] > 0).mean() * 100,
                mean=df["net"].mean() * 100, t=cluster_t(df), tr=df["net"].iloc[:k].mean() * 100,
                te=df["net"].iloc[k:].mean() * 100, placebo=placebo)


def verdict(rows: list) -> tuple[str, dict | None]:
    ok = [r for r in rows if r["n"] >= 30 and not np.isnan(r["t"])]
    if not ok:
        return "❌ sin muestra suficiente", None
    best = max(ok, key=lambda r: r["t"])
    if best["t"] >= 3 and best["te"] > 0 and best["mean"] > 0:
        return "✅ APORTA: t ≥ 3, positiva en el tramo de prueba y neta de costes", best
    if best["t"] >= 2 and best["mean"] > 0:
        return "🟡 INDICIO: prometedor pero no concluyente (se pide t ≥ 3 por probar 6 variantes)", best
    return "❌ NO APORTA: no se distingue del azar después de costes", best


def fmt(x, f="{:+.3f}"):
    return "—" if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))) else f.format(x)


def md(rows, head):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"] +
                     ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


# ───────────────────────── A · reloj del funding ─────────────────────────
def study_a(frames5: dict, events: dict):
    trades = {v: [] for v in range(len(A_VARIANTS))}
    placebo = {v: [] for v in range(len(A_VARIANTS))}
    evrows = []
    for sym, ev in events.items():
        df = frames5.get(sym)
        if df is None or len(ev) < 3:
            continue
        op = df["open"]
        ev = ev.sort_values("time").reset_index(drop=True)
        for i in range(1, len(ev)):
            T = ev.at[i, "time"].floor("5min")
            r_prev, r_now = ev.at[i - 1, "rate"], ev.at[i, "rate"]
            d = -1 if r_prev > 0 else 1
            def px(m):
                return op.get(T + pd.Timedelta(minutes=m), np.nan)
            p = {m: px(m) for m in (-60, -30, 0, 30, 60)}
            if any(np.isnan(v) or v <= 0 for v in p.values()):
                continue
            evrows.append(dict(sym=sym, T=T, r_prev=r_prev,
                               w1=d * (p[-30] / p[-60] - 1), w2=d * (p[0] / p[-30] - 1),
                               w3=d * (p[30] / p[0] - 1), w4=d * (p[60] / p[30] - 1)))
            for v, (thr, a, b, _) in enumerate(A_VARIANTS):
                gross = d * (p[b] / p[a] - 1)
                fund = (-d * r_now) if a < 0 < b else 0.0   # solo cobra/paga si está abierta EN T
                rec = dict(sym=sym, t_entry=T + pd.Timedelta(minutes=a), net=gross + fund - COST_RT, fund=fund)
                if abs(r_prev) >= thr:
                    trades[v].append(rec)
                elif abs(r_prev) < 0.0001:        # placebo: funding normal, misma mecánica
                    placebo[v].append(rec)
    rows, table = [], []
    for v, (thr, a, b, name) in enumerate(A_VARIANTS):
        df = pd.DataFrame(trades[v])
        pl = pd.DataFrame(placebo[v])
        s = summarize(df, pl["net"].mean() * 100 if len(pl) else np.nan)
        s["name"] = f"|funding| ≥ {thr*100:.2f}% · {name}"
        rows.append(s)
        table.append([s["name"], s["n"], s["clusters"], fmt(s["win"], "{:.0f}%"), fmt(s["mean"]) + "%",
                      fmt(s["t"], "{:+.2f}"), fmt(s["tr"]) + "%", fmt(s["te"]) + "%", fmt(s["placebo"]) + "%"])
    ev = pd.DataFrame(evrows)
    study_rows = []
    if len(ev):
        bins = [0, 0.0001, 0.0005, 0.001, 1]
        labels = ["< 0.01%", "0.01–0.05%", "0.05–0.10%", "≥ 0.10%"]
        ev["bucket"] = pd.cut(ev["r_prev"].abs(), bins, labels=labels, right=False)
        for lab, g in ev.groupby("bucket", observed=True):
            study_rows.append([lab, len(g)] + [fmt(g[w].mean() * 1e4, "{:+.1f}") for w in ("w1", "w2", "w3", "w4")])
    verd, best = verdict(rows)
    txt = ("## A · Reloj del funding\n\n**" + verd + "**\n\n" +
           md(table, ["Variante", "ops", "instantes", "acierto", "media neta", "t agrupada", "entrenamiento", "prueba", "placebo"]) +
           "\n\n*media neta = por operación, después de 0.16% de costes y sumando el funding cobrado/pagado si cruza T. "
           "placebo = misma mecánica con funding normal (< 0.01%), donde no debería haber efecto.*\n\n"
           "**Estudio de eventos (bruto, sin costes, en pb, a favor de la hipótesis)**\n\n" +
           md(study_rows, ["|funding anterior|", "eventos", "−60→−30", "−30→T", "T→+30", "+30→+60"]) +
           "\n\n*Si la hipótesis es cierta, la columna −30→T crece con el funding. El coste de una operación es 16 pb.*")
    return txt, verd, best, pd.concat([pd.DataFrame(trades[v]).assign(variant=A_VARIANTS[v][3], thr=A_VARIANTS[v][0])
                                       for v in trades if trades[v]], ignore_index=True) if any(trades.values()) else pd.DataFrame()


# ───────────────────────── B · prima BingX / Binance ─────────────────────────
def study_b(bx: dict, bn: dict, seed=0):
    rng = np.random.default_rng(seed)
    trades = {v: [] for v in range(len(B_VARIANTS))}
    placebo = {v: [] for v in range(len(B_VARIANTS))}
    diag = []
    for sym in bx:
        if sym not in bn:
            continue
        j = bx[sym][["open", "close"]].join(bn[sym][["close"]].rename(columns={"close": "bn"}), how="inner")
        if len(j) < 400:
            continue
        prem = j["close"] / j["bn"] - 1
        dev = prem - prem.rolling(288, min_periods=60).median().shift(1)
        o, c, b_, dv = j["open"].to_numpy(), j["close"].to_numpy(), j["bn"].to_numpy(), dev.to_numpy()
        idx = j.index
        diag.append(dict(sym=sym, bars=len(j), med_prem=prem.median() * 1e4, p99=dev.abs().quantile(0.99) * 1e4))
        for v, (thr, mode) in enumerate(B_VARIANTS):
            t, n = 0, len(j)
            while t < n - 7:
                if np.isnan(dv[t]) or abs(dv[t]) < thr:
                    t += 1
                    continue
                d = -1 if dv[t] > 0 else 1
                ent = o[t + 1]
                if mode == "fijo15":
                    k = t + 3
                else:
                    k = t + 6
                    for q in range(t + 1, t + 7):
                        if not np.isnan(dv[q]) and abs(dv[q]) <= thr / 2:
                            k = q
                            break
                ex = c[k]
                gross = d * (ex / ent - 1)
                bx_move = d * (c[k] / c[t] - 1)
                bn_move = -d * (b_[k] / b_[t] - 1)
                trades[v].append(dict(sym=sym, t_entry=idx[t + 1], net=gross - COST_RT, dev=dv[t],
                                      bx_part=bx_move, bn_part=bn_move))
                t = k + 1
            # placebo: mismas entradas en número, en velas SIN desviación y dirección al azar
            calm = np.where(~np.isnan(dv[:-7]) & (np.abs(dv[:-7]) < thr / 3))[0]
            m = sum(1 for x in trades[v] if x["sym"] == sym)
            if len(calm) and m:
                for t0 in rng.choice(calm, size=min(m * 3, len(calm)), replace=False):
                    d = rng.choice([-1, 1])
                    k = t0 + 3
                    placebo[v].append(dict(t_entry=idx[t0 + 1], net=d * (c[k] / o[t0 + 1] - 1) - COST_RT))
    rows, table = [], []
    for v, (thr, mode) in enumerate(B_VARIANTS):
        df = pd.DataFrame(trades[v])
        pl = pd.DataFrame(placebo[v])
        s = summarize(df, pl["net"].mean() * 100 if len(pl) else np.nan)
        s["name"] = f"desviación ≥ {thr*100:.1f}% · " + ("hasta que converge (máx 30 min)" if mode == "converge" else "15 min fijos")
        rows.append(s)
        conv = (f"{df['bx_part'].mean()*1e4:+.1f} / {df['bn_part'].mean()*1e4:+.1f}" if len(df) else "—")
        table.append([s["name"], s["n"], fmt(s["win"], "{:.0f}%"), fmt(s["mean"]) + "%", fmt(s["t"], "{:+.2f}"),
                      fmt(s["tr"]) + "%", fmt(s["te"]) + "%", fmt(s["placebo"]) + "%", conv])
    dg = pd.DataFrame(diag)
    period = ""
    if bx:
        allidx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in bx.values()])))
        period = f"{allidx.min():%Y-%m-%d} → {allidx.max():%Y-%m-%d} ({(allidx.max()-allidx.min()).days} días)"
    verd, best = verdict(rows)
    txt = ("## B · Prima de BingX sobre Binance\n\n" + f"Periodo con datos de BingX: {period} · {len(dg)} monedas con datos en los dos exchanges\n\n**" + verd + "**\n\n" +
           md(table, ["Variante", "ops", "acierto", "media neta", "t agrupada", "entrenamiento", "prueba", "placebo", "cierre BingX / Binance (pb)"]) +
           "\n\n*cierre BingX / Binance = cuánto de la convergencia viene de que BingX vuelva (lo que se cobra) frente a que Binance "
           "se mueva (lo que NO se cobra operando solo BingX). placebo = entradas al azar en momentos sin desviación.*")
    if len(dg):
        txt += (f"\n\nPrima típica BingX/Binance: mediana {dg['med_prem'].median():+.1f} pb · desviación extrema (p99) mediana "
                f"{dg['p99'].median():.0f} pb, máxima {dg['p99'].max():.0f} pb ({dg.loc[dg['p99'].idxmax(),'sym']})")
    return txt, verd, best, pd.concat([pd.DataFrame(trades[v]).assign(variant=f"{B_VARIANTS[v][0]}_{B_VARIANTS[v][1]}")
                                       for v in trades if trades[v]], ignore_index=True) if any(trades.values()) else pd.DataFrame()


def telegram(text, files=()):
    tok = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        for i in range(0, len(text), 3800):
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data=dict(chat_id=chat, text=text[i:i + 3800]), timeout=15)
        for fp in files:
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    requests.post(f"https://api.telegram.org/bot{tok}/sendDocument", data=dict(chat_id=chat), files=dict(document=f), timeout=60)
    except requests.RequestException as e:
        print(f"Telegram: {e}", file=sys.stderr)


def main(argv=None, a_override=None, b_override=None, c_override=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-symbols", default=os.getenv("A_SYMBOLS", A_SYMBOLS))
    ap.add_argument("--b-symbols", default=os.getenv("B_SYMBOLS", B_SYMBOLS))
    ap.add_argument("--a-days", type=int, default=int(os.getenv("A_DAYS", "365")))
    ap.add_argument("--b-days", type=int, default=int(os.getenv("B_DAYS", "60")))
    ap.add_argument("--c-symbols", default=os.getenv("C_SYMBOLS", C_SYMBOLS))
    ap.add_argument("--c-days", type=int, default=int(os.getenv("C_DAYS", "365")))
    ap.add_argument("--only", default=os.getenv("ONLY", "ABC").upper())
    ap.add_argument("--out", default=os.getenv("OUT_DIR", "out"))
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    parts, verds = [], []

    if "A" in a.only:
        if a_override is not None:
            frames5, events = a_override
        else:
            frames5, events = {}, {}
            for s in [x.strip() for x in a.a_symbols.split(",") if x.strip()]:
                try:
                    ev = data5.load_funding_events(s, a.a_days)
                    df, src = data5.load(s, a.a_days, "auto")
                    frames5[s], events[s] = df, ev
                    print(f"  A {s}: {len(df):,} velas · {len(ev)} cobros de funding · {src}", flush=True)
                except Exception as e:
                    print(f"  A {s}: sin datos ({str(e)[:70]})", flush=True)
        txt, v, best, tr = study_a(frames5, events)
        parts.append(txt)
        verds.append("A · Reloj del funding: " + v)
        tr.to_csv(os.path.join(a.out, "a_operaciones.csv"), index=False)

    if "B" in a.only:
        if b_override is not None:
            bx, bn = b_override
        else:
            bx, bn = {}, {}
            for s in [x.strip() for x in a.b_symbols.split(",") if x.strip()]:
                try:
                    dfx, _ = data5.load(s, a.b_days, "bingx")
                    dfn, src = data5.load(s, a.b_days, "auto")
                    bx[s], bn[s] = dfx, dfn
                    print(f"  B {s}: BingX {len(dfx):,} · Binance {len(dfn):,} velas ({src})", flush=True)
                except Exception as e:
                    print(f"  B {s}: sin datos ({str(e)[:70]})", flush=True)
        txt, v, best, tr = study_b(bx, bn)
        parts.append(txt)
        verds.append("B · Prima BingX/Binance: " + v)
        tr.to_csv(os.path.join(a.out, "b_operaciones.csv"), index=False)

    if "C" in a.only:
        if c_override is not None:
            frames_c, btc = c_override
        else:
            frames_c, btc = {}, None
            for s in [x.strip() for x in a.c_symbols.split(",") if x.strip()]:
                try:
                    df, src = data5.load(s, a.c_days, "auto")
                    frames_c[s] = df
                    if s.upper() == "BTC":
                        btc = df
                    print(f"  C {s}: {len(df):,} velas ({src})", flush=True)
                except Exception as e:
                    print(f"  C {s}: sin datos ({str(e)[:70]})", flush=True)
        if btc is None or len(frames_c) < 10:
            v = f"⚠️ INVÁLIDO: faltan datos (BTC {'ok' if btc is not None else 'NO'}, {len(frames_c)} monedas)"
            parts.append("## C · WEC v2\n\n**" + v + "**")
            verds.append("C · WEC mecha: " + v)
        else:
            txt, v, tr = wec.study_c(frames_c, btc)
            parts.append(txt + f"\n\n*Monedas con datos: {len(frames_c)}.*")
            verds.append("C · WEC mecha: " + v)
            tr.to_csv(os.path.join(a.out, "c_operaciones.csv"), index=False)

    report = ("# Laboratorio\n\n## Veredicto\n\n" + "\n".join("• " + x for x in verds) + "\n\n" +
              "\n\n".join(parts) + f"\n\n---\nCostes: {COST_RT*100:.2f}% ida y vuelta · Tiempo: {time.time()-t0:.0f}s\n")
    with open(os.path.join(a.out, "laboratorio.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    telegram("🔬 LABORATORIO\n\n" + "\n".join("• " + x for x in verds),
             [os.path.join(a.out, "laboratorio.md")])
    return dict(report=report, verdicts=verds)


if __name__ == "__main__":
    main()
