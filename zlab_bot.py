"""
KIBITO Z-LAB BOT — ejecuta Z-LAB v2 (5m) en varias monedas de BingX.

MODE=SIGNAL  -> opera en papel: registra entradas/salidas y avisa por Telegram (por defecto)
MODE=LIVE    -> envia ordenes reales (requiere tambien CONFIRM_LIVE=YES)

- Senales: motor zlab_core (identico al backtest validado).
- SL en el exchange (STOP_MARKET closePosition) nada mas entrar; TP y salida por tiempo
  gestionados por el bot al cierre de cada vela de 5m (el objetivo de la reversion se mueve con la VWAP).
- Estado persistente en STATE_FILE (usa un volumen de Railway en /data).
"""
import os
import time
import json
import hmac
import math
import hashlib
import logging
import datetime as dt

import requests

import zlab_core as Z

CODE_VERSION = "zlab-bot 1.1.0"


# ───────────────────────── config ─────────────────────────
def env(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().strip('"').strip("'").strip()
    if raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "si", "on")
    return cast(raw)


MODE          = env("MODE", "SIGNAL").upper()
CONFIRM_LIVE  = env("CONFIRM_LIVE", "NO").upper()
LIVE          = MODE == "LIVE" and CONFIRM_LIVE == "YES"
API_KEY       = env("BINGX_API_KEY", "")
API_SECRET    = env("BINGX_API_SECRET", "")
BASE_URL      = env("BINGX_BASE_URL", "https://open-api.bingx.com")
TG_TOKEN      = env("TELEGRAM_TOKEN", "")
TG_CHAT       = env("TELEGRAM_CHAT_ID", "")
SYMBOLS       = [s.strip().upper() for s in env("SYMBOLS", "JTO-USDT,UAI-USDT,JUP-USDT").split(",") if s.strip()]
AUTO_SYMBOLS  = env("AUTO_SYMBOLS", False, bool)
AUTO_TOP      = env("AUTO_TOP", 6, int)
AUTO_MIN_VOL  = env("AUTO_MIN_VOL_USDT", 2_000_000, float)
AUTO_SCAN_MAX = env("AUTO_SCAN_MAX", 0, int)       # 0 = todas
AUTO_PAGES    = env("AUTO_PAGES", 6, int)           # 6 x 5 dias = 30 dias
AUTO_MIN_TR   = env("AUTO_MIN_TRADES", 12, int)
AUTO_MIN_PF   = env("AUTO_MIN_PF", 1.2, float)
AUTO_EVERY_H  = env("AUTO_EVERY_H", 12, float)
EXCLUDE       = {s.strip().upper() for s in env("EXCLUDE", "BTC,ETH,USDC,NCCOGOLD2USD").split(",") if s.strip()}
RISK_PCT      = env("RISK_PCT", 0.5, float)
MAX_LEV       = env("MAX_NOTIONAL_X", 2.0, float)   # nocional max por operacion / equity
LEVERAGE      = env("LEVERAGE", 5, int)             # apalancamiento del exchange (margen)
MAX_POS       = env("MAX_POSITIONS", 3, int)
MAX_DD_DAY    = env("MAX_DD_DAY_PCT", 2.5, float)   # corte global diario sobre equity
PAPER_EQUITY  = env("PAPER_EQUITY", 10_000, float)
STATE_FILE    = env("STATE_FILE", "/data/zlab_state.json")
HISTORY_BARS  = env("HISTORY_BARS", 1440, int)
REQ_PAUSE_S   = env("REQ_PAUSE_S", 1.1, float)
LOOP_DELAY_S  = env("LOOP_DELAY_S", 8, int)         # segundos tras el cierre de vela

P = dict(Z.P)
P["riskPct"], P["maxLev"] = RISK_PCT, MAX_LEV

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zlab-bot")
S = requests.Session()
S.headers.update({"X-SOURCE-KEY": "BX-AI-SKILL"})


# ───────────────────────── BingX ─────────────────────────
class BingXError(Exception):
    pass


def _check(js):
    if str(js.get("code", 0)) != "0":
        raise BingXError(f"{js.get('code')}: {js.get('msg')}")
    return js.get("data")


def public(path, params=None, retries=3):
    for k in range(retries):
        try:
            r = S.get(BASE_URL + path, params=params, timeout=15)
            r.raise_for_status()
            return _check(r.json())
        except Exception as e:
            log.warning("GET %s %s (%d): %s", path, params, k + 1, e)
            time.sleep(2 + 2 * k)
    return None


def signed(method, path, params=None):
    """Firma BingX: parametros ordenados ASCII, HMAC-SHA256 hex. La MISMA cadena firmada
    se envia (en la URL para GET/DELETE, en el cuerpo form-urlencoded para POST)."""
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    for k, v in params.items():
        if any(ch in str(v) for ch in "&=?#\r\n"):
            raise BingXError(f"valor no permitido en {k}")
    canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sig = hmac.new(API_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    payload = f"{canonical}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    url = BASE_URL + path
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = S.post(url, data=payload, headers=headers, timeout=15)
    elif method == "DELETE":
        r = S.delete(f"{url}?{payload}", headers=headers, timeout=15)
    else:
        r = S.get(f"{url}?{payload}", headers=headers, timeout=15)
    return _check(r.json())


CONTRACTS = {}


def load_contracts():
    data = public("/openApi/swap/v2/quote/contracts") or []
    for c in data:
        CONTRACTS[c["symbol"]] = dict(
            qp=int(c.get("quantityPrecision", 3)), pp=int(c.get("pricePrecision", 4)),
            minq=float(c.get("tradeMinQuantity") or 0), minusdt=float(c.get("tradeMinUSDT") or 0))
    log.info("contratos cargados: %d", len(CONTRACTS))


def rnd_qty(sym, q):
    qp = CONTRACTS.get(sym, {}).get("qp", 3)
    f = 10 ** qp
    return math.floor(q * f) / f


def qty_str(sym, q):
    qp = CONTRACTS.get(sym, {}).get("qp", 3)
    return f"{q:.{qp}f}"


def rnd_px(sym, x):
    return round(x, CONTRACTS.get(sym, {}).get("pp", 6))


def get_bars(sym, limit=HISTORY_BARS, end=None):
    params = {"symbol": sym, "interval": "5m", "limit": min(limit, 1440)}
    if end:
        params["endTime"] = end
    data = public("/openApi/swap/v3/quote/klines", params) or []
    time.sleep(REQ_PAUSE_S)
    bars = sorted((Z.parse_kline(k) for k in data), key=lambda b: b.t)
    now_ms = int(time.time() * 1000)
    return [b for b in bars if b.t + 300_000 <= now_ms]      # solo velas cerradas


KCACHE = {}   # symbol -> {t: Bar} para la seleccion automatica


def get_bars_pages(sym, pages):
    cached = KCACHE.get(sym)
    if cached and max(cached) > int(time.time() * 1000) - 4 * 86_400_000:
        for b in get_bars(sym, 1440):
            cached[b.t] = b
        keep = sorted(cached)[-pages * 1440:]
        KCACHE[sym] = {t: cached[t] for t in keep}
        return [KCACHE[sym][t] for t in keep]
    allb, end = {}, None
    for _ in range(pages):
        chunk = get_bars(sym, 1440, end)
        if not chunk:
            break
        for b in chunk:
            allb[b.t] = b
        end = chunk[0].t - 1
        if len(chunk) < 1400:
            break
    out = sorted(allb.values(), key=lambda b: b.t)
    if out:
        KCACHE[sym] = {b.t: b for b in out}
    return out


def last_price(sym):
    d = public("/openApi/swap/v2/quote/price", {"symbol": sym})
    try:
        return float(d["price"] if isinstance(d, dict) else d[0]["price"])
    except Exception:
        return None


HEDGE = None


def detect_mode():
    global HEDGE
    d = signed("GET", "/openApi/swap/v1/positionSide/dual")
    v = d.get("dualSidePosition") if isinstance(d, dict) else d
    HEDGE = str(v).lower() == "true"
    log.info("modo de posicion: %s", "HEDGE" if HEDGE else "ONE-WAY")


def live_equity():
    d = signed("GET", "/openApi/swap/v3/user/balance")
    for a in d if isinstance(d, list) else [d]:
        if a.get("asset") == "USDT":
            return float(a.get("equity") or a.get("balance"))
    raise BingXError("sin saldo USDT")


def live_position(sym, side):
    d = signed("GET", "/openApi/swap/v2/user/positions", {"symbol": sym}) or []
    want = "LONG" if side == 1 else "SHORT"
    for p in d:
        amt = float(p.get("positionAmt") or 0)
        ps = p.get("positionSide", "BOTH")
        if amt == 0:
            continue
        if ps == want or (ps == "BOTH" and (amt > 0) == (side == 1)):
            return abs(amt), float(p.get("avgPrice") or 0)
    return 0.0, 0.0


def pos_side(side):
    return ("LONG" if side == 1 else "SHORT") if HEDGE else "BOTH"


def set_leverage(sym):
    for s in (["LONG", "SHORT"] if HEDGE else ["BOTH"]):
        try:
            signed("POST", "/openApi/swap/v2/trade/leverage", {"symbol": sym, "side": s, "leverage": LEVERAGE})
        except Exception as e:
            log.warning("leverage %s %s: %s", sym, s, e)


def live_open(sym, side, qty, stop):
    set_leverage(sym)
    o = {"symbol": sym, "side": "BUY" if side == 1 else "SELL", "positionSide": pos_side(side),
         "type": "MARKET", "quantity": qty_str(sym, qty)}
    signed("POST", "/openApi/swap/v2/trade/order", o)
    time.sleep(1.0)
    amt, avg = live_position(sym, side)
    if amt <= 0:
        raise BingXError("la posicion no aparece tras la orden")
    sl = {"symbol": sym, "side": "SELL" if side == 1 else "BUY", "positionSide": pos_side(side),
          "type": "STOP_MARKET", "stopPrice": rnd_px(sym, stop), "quantity": qty_str(sym, amt),
          "closePosition": "true", "workingType": "MARK_PRICE"}
    try:
        signed("POST", "/openApi/swap/v2/trade/order", sl)
    except Exception as e:
        log.error("SL fallo en %s: %s -> cierro la posicion por seguridad", sym, e)
        live_close(sym, side)
        raise
    return amt, avg


def live_close(sym, side):
    amt, _ = live_position(sym, side)
    if amt > 0:
        o = {"symbol": sym, "side": "SELL" if side == 1 else "BUY", "positionSide": pos_side(side),
             "type": "MARKET", "quantity": qty_str(sym, amt)}
        if not HEDGE:
            o["reduceOnly"] = "true"
        signed("POST", "/openApi/swap/v2/trade/order", o)
    try:
        signed("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": sym})
    except Exception as e:
        log.warning("cancelar ordenes %s: %s", sym, e)


# ───────────────────────── Telegram ─────────────────────────
def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log.info("[TG] %s", text.replace("\n", " | "))
        return
    try:
        S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
               data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                     "disable_web_page_preview": "true"}, timeout=15)
    except Exception as e:
        log.warning("telegram: %s", e)


# ───────────────────────── estado ─────────────────────────
def new_state():
    return dict(version=CODE_VERSION, equity=PAPER_EQUITY, positions={}, sym={}, trades=[],
                day=None, day_start_eq=None, day_block=False, symbols=SYMBOLS, auto_ts=0)


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        log.info("estado cargado: %d posiciones, %d trades", len(st.get("positions", {})), len(st.get("trades", [])))
        return st
    except FileNotFoundError:
        return new_state()
    except Exception as e:
        log.error("estado corrupto (%s); empiezo de cero", e)
        return new_state()


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("no se pudo guardar el estado: %s", e)


def symst(st, sym):
    return st["sym"].setdefault(sym, dict(last_sig=0, last_exit=0, trades_day=0, fail_l=0, fail_s=0,
                                          streak=0, pause_until=0, day=None))


# ───────────────────────── logica ─────────────────────────
BAR_MS = 300_000


def fmt(x):
    return f"{x:.6g}"


def record_close(st, sym, pos, exit_px, reason, bar_t):
    side = pos["side"]
    pnl = (exit_px - pos["entry"]) * pos["qty"] * side - (pos["entry"] + exit_px) * pos["qty"] * P["fee"]
    if not LIVE:
        st["equity"] += pnl
    ss = symst(st, sym)
    ss["last_exit"] = bar_t
    if pnl > 0:
        ss["streak"] = 0
    else:
        ss["streak"] += 1
        if ss["streak"] >= P["lossStreak"]:
            ss["pause_until"] = bar_t + P["pauseBars"] * BAR_MS
            ss["streak"] = 0
            tg(f"⏸ <b>{sym}</b> en pausa 12 h tras {P['lossStreak']} pérdidas seguidas")
    if pos["type"] == "fade":
        key = "fail_l" if side == 1 else "fail_s"
        ss[key] = ss[key] + 1 if pnl <= 0 else 0
    r_mult = pnl / pos["risk_usdt"] if pos.get("risk_usdt") else 0
    st["trades"].append(dict(sym=sym, side=side, type=pos["type"], entry=pos["entry"], exit=exit_px,
                             qty=pos["qty"], pnl=pnl, r=r_mult, reason=reason,
                             t_in=pos["t"], t_out=bar_t))
    st["trades"] = st["trades"][-2000:]
    del st["positions"][sym]
    icon = "✅" if pnl > 0 else "❌"
    tg(f"{icon} <b>{'LONG' if side == 1 else 'SHORT'} {sym}</b> cerrado ({reason})\n"
       f"entrada {fmt(pos['entry'])} → salida {fmt(exit_px)}\n"
       f"PnL {pnl:+.2f} USDT ({r_mult:+.2f}R) · {'REAL' if LIVE else 'PAPEL'}")


def manage_position(st, sym, bars, F):
    pos = st["positions"][sym]
    b = bars[-1]
    if b.t < pos["t"]:
        return                                   # la vela de entrada aun no ha cerrado
    i = len(bars) - 1
    side = pos["side"]
    tgt = F.vw[i] if pos["type"] == "fade" else pos["tgt"]
    bars_open = (b.t - pos["t"]) // BAR_MS
    exit_px, reason = None, None
    live_amt = None
    if LIVE:
        live_amt, _ = live_position(sym, side)
        if live_amt <= 0:                         # el SL del exchange ya salto
            exit_px, reason = pos["stop"], "SL"
    if exit_px is None:
        if (side == 1 and b.l <= pos["stop"]) or (side == -1 and b.h >= pos["stop"]):
            exit_px, reason = pos["stop"], "SL"
        elif (side == 1 and b.h >= tgt) or (side == -1 and b.l <= tgt):
            exit_px, reason = tgt, "TP"
        elif bars_open >= P["horizon"] * 2:
            exit_px, reason = b.c, "TIEMPO"
    if exit_px is None:
        return
    if LIVE:
        if live_amt and live_amt > 0:             # sigue abierta en BingX: cerrar a mercado
            live_close(sym, side)
            exit_px = last_price(sym) or exit_px
        else:
            try:
                signed("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": sym})
            except Exception as e:
                log.warning("cancelar ordenes %s: %s", sym, e)
    record_close(st, sym, pos, exit_px, reason, b.t)


def try_entry(st, sym, bars, F):
    i = len(bars) - 1
    b = bars[-1]
    ss = symst(st, sym)
    day = b.t // 86_400_000
    if ss["day"] != day:
        ss.update(day=day, trades_day=0, fail_l=0, fail_s=0)
    s = Z.raw_signal(F, i, ss["fail_l"], ss["fail_s"], P)
    if s is None or (b.t - ss["last_sig"]) // BAR_MS <= P["sigGap"]:
        return
    ss["last_sig"] = b.t
    reasons = []
    if (b.t - ss["last_exit"]) // BAR_MS <= P["cooldown"]:
        reasons.append("cooldown")
    if b.t <= ss["pause_until"]:
        reasons.append("pausa")
    if ss["trades_day"] >= P["maxDay"]:
        reasons.append("máx diario")
    if st["day_block"]:
        reasons.append("corte diario global")
    if len(st["positions"]) >= MAX_POS:
        reasons.append("máx posiciones")
    if not s["valid"]:
        reasons.append("stop/RR")
    side_txt = "LONG" if s["side"] == 1 else "SHORT"
    if reasons:
        log.info("%s señal %s %s descartada: %s", sym, side_txt, s["type"], ", ".join(reasons))
        return

    px = last_price(sym) or b.c
    if (s["side"] == 1 and s["stop"] >= px) or (s["side"] == -1 and s["stop"] <= px):
        log.info("%s precio ya más allá del stop, se descarta", sym)
        return
    dist = abs(px - s["stop"])
    equity = live_equity() if LIVE else st["equity"]
    qty = min(equity * RISK_PCT / 100 / dist, equity * MAX_LEV / px)
    qty = rnd_qty(sym, qty)
    c = CONTRACTS.get(sym, {})
    if qty <= 0 or qty < c.get("minq", 0) or qty * px < max(c.get("minusdt", 0), 2):
        log.info("%s tamaño demasiado pequeño (%s)", sym, qty)
        return
    entry = px
    if LIVE:
        try:
            qty, avg = live_open(sym, s["side"], qty, s["stop"])
            entry = avg or px
        except Exception as e:
            tg(f"⚠️ No se pudo abrir {side_txt} {sym}: {e}")
            return
    risk_usdt = abs(entry - s["stop"]) * qty
    st["positions"][sym] = dict(side=s["side"], type=s["type"], entry=entry, stop=s["stop"], tgt=s["tgt"],
                                qty=qty, t=b.t + BAR_MS, risk_usdt=risk_usdt)
    ss["trades_day"] += 1
    kind = "Reversión a VWAP" if s["type"] == "fade" else "Continuación de tendencia"
    tgt_txt = "VWAP (móvil)" if s["type"] == "fade" else fmt(s["tgt"])
    tg(f"{'🟢' if s['side'] == 1 else '🔴'} <b>{side_txt} {sym}</b> · {kind}\n"
       f"entrada {fmt(entry)} · SL {fmt(s['stop'])} · TP {tgt_txt}\n"
       f"z {s['z']:+.2f}σ · {s['regime']} · R:R {s['rr']:.2f} · riesgo {risk_usdt:.2f} USDT · "
       f"{'REAL' if LIVE else 'PAPEL'}")


def auto_select(st):
    tick = public("/openApi/swap/v2/quote/ticker") or []
    cands = []
    for t in tick if isinstance(tick, list) else [tick]:
        sym = str(t.get("symbol", ""))
        base = sym.split("-")[0]
        if not sym.endswith("-USDT") or base in EXCLUDE or (base.startswith("NC") and base.endswith("USD")):
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except ValueError:
            continue
        if qv >= AUTO_MIN_VOL:
            cands.append((sym, qv))
    cands = sorted(cands, key=lambda x: -x[1])
    if AUTO_SCAN_MAX > 0:
        cands = cands[:AUTO_SCAN_MAX]
    t0 = time.time()
    ranked = []
    for sym, _ in cands:
        try:
            r = Z.backtest(sym, get_bars_pages(sym, AUTO_PAGES), P)
        except Exception as e:
            log.warning("auto %s: %s", sym, e)
            continue
        if r.trades >= AUTO_MIN_TR and r.pf >= AUTO_MIN_PF:
            sc = (min(r.pf, 3) - 1) * math.sqrt(r.trades) - max(r.max_dd_pct - 3, 0) * 0.3
            ranked.append((sc, sym, r))
    ranked.sort(key=lambda x: -x[0])
    chosen = [s for _, s, _ in ranked[:AUTO_TOP]]
    keep = [s for s in st["positions"]]                 # nunca abandonar una posicion abierta
    st["symbols"] = list(dict.fromkeys(chosen + keep)) or SYMBOLS
    st["auto_ts"] = time.time()
    lines = [f"🔄 <b>Selección automática</b> · {len(cands)} monedas analizadas en {(time.time() - t0) / 60:.0f} min",
             f"({AUTO_PAGES * 5} días, ≥{AUTO_MIN_TR} trades, PF ≥ {AUTO_MIN_PF})"]
    for sc, s, r in ranked[:AUTO_TOP]:
        lines.append(f"• {s}: PF {min(r.pf, 99):.2f} · {r.trades} tr · win {r.winrate:.0f}% · DD {r.max_dd_pct:.1f}%")
    if not ranked:
        lines.append("Ninguna moneda cumple. Se mantienen: " + ", ".join(st["symbols"]))
    tg("\n".join(lines))


def daily_report(st, day):
    t0 = day * 86_400_000
    tr = [t for t in st["trades"] if t0 - 86_400_000 <= t["t_out"] < t0]
    allt = st["trades"]
    gp = sum(t["pnl"] for t in allt if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in allt if t["pnl"] <= 0)
    pf = gp / gl if gl > 0 else float("inf") if gp > 0 else 0
    day_pnl = sum(t["pnl"] for t in tr)
    wins = sum(1 for t in allt if t["pnl"] > 0)
    tg(f"📊 <b>Resumen Z-LAB</b> · {'REAL' if LIVE else 'PAPEL'}\n"
       f"Ayer: {len(tr)} trades · PnL {day_pnl:+.2f} USDT\n"
       f"Total: {len(allt)} trades · win {wins / len(allt) * 100 if allt else 0:.0f}% · "
       f"PF {'∞' if pf == float('inf') else f'{pf:.2f}'} · PnL {gp - gl:+.2f} USDT\n"
       f"Equity {'papel ' + format(st['equity'], '.2f') if not LIVE else 'ver BingX'} · "
       f"Monedas: {', '.join(st['symbols'])}")


def cycle(st):
    now_ms = int(time.time() * 1000)
    day = now_ms // 86_400_000
    if st["day"] != day:
        if st["day"] is not None:
            daily_report(st, day)
        eq = live_equity() if LIVE else st["equity"]
        st.update(day=day, day_start_eq=eq, day_block=False)
    if AUTO_SYMBOLS and time.time() - st.get("auto_ts", 0) >= AUTO_EVERY_H * 3600:
        auto_select(st)
        save_state(st)
    syms = list(dict.fromkeys(st.get("symbols") or SYMBOLS))
    for sym in list(st["positions"]):
        if sym not in syms:
            syms.append(sym)
    for sym in syms:
        try:
            bars = get_bars(sym)
            if len(bars) < 600:
                log.warning("%s: pocas velas (%d)", sym, len(bars))
                continue
            F = Z.Features(bars, P)
            if sym in st["positions"]:
                manage_position(st, sym, bars, F)
            if sym not in st["positions"]:
                try_entry(st, sym, bars, F)
        except Exception as e:
            log.exception("%s: %s", sym, e)
        save_state(st)
    eq = live_equity() if LIVE else st["equity"]
    if st["day_start_eq"] and (st["day_start_eq"] - eq) / st["day_start_eq"] * 100 >= MAX_DD_DAY and not st["day_block"]:
        st["day_block"] = True
        tg(f"🛑 Corte diario: pérdida ≥ {MAX_DD_DAY}% · sin nuevas entradas hasta mañana (UTC)")
    save_state(st)


def main():
    log.info("arrancando %s · modo %s · %s", CODE_VERSION, "REAL" if LIVE else "PAPEL", STATE_FILE)
    if MODE == "LIVE" and not LIVE:
        log.warning("MODE=LIVE sin CONFIRM_LIVE=YES -> funciono en PAPEL")
    load_contracts()
    if LIVE:
        if not API_KEY or not API_SECRET:
            raise SystemExit("faltan BINGX_API_KEY / BINGX_API_SECRET para modo REAL")
        detect_mode()
    st = load_state()
    if not AUTO_SYMBOLS:
        st["symbols"] = SYMBOLS
    tg(f"🤖 <b>{CODE_VERSION}</b> iniciado · {'🔴 REAL' if LIVE else '📝 PAPEL'}\n"
       f"Riesgo {RISK_PCT}% · máx {MAX_POS} posiciones · corte diario {MAX_DD_DAY}%\n"
       f"Monedas: {'AUTO' if AUTO_SYMBOLS else ', '.join(SYMBOLS)}")
    while True:
        now = time.time()
        nxt = (math.floor(now / 300) + 1) * 300 + LOOP_DELAY_S
        time.sleep(max(1, nxt - now))
        try:
            cycle(st)
        except Exception as e:
            log.exception("ciclo: %s", e)
            tg(f"⚠️ Error en ciclo: {e}")


if __name__ == "__main__":
    main()
