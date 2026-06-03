"""
╔══════════════════════════════════════════════════════════════════╗
║         QF×JP Bot v6.1 — AMD FUSION                             ║
║                                                                  ║
║  BASE: v6.0 completo                                             ║
║  + AMD Smart Money (Acum/Manip/Distribución):                    ║
║                                                                  ║
║  ✅ Rango asiático 00:00-08:00 UTC                               ║
║  ✅ Sweep/manipulación wick + cierre                             ║
║  ✅ Filtro volumen institucional                                  ║
║  ✅ MSS post-sweep (stateful por símbolo)                        ║
║  ✅ AMD_REQUIRED=true → bloquea entradas sin confirmación AMD     ║
║  ✅ AMD_BOOST=8 → suma puntos al score cuando AMD activo         ║
║  ✅ Dashboard AMD en alerta Telegram                             ║
║  ✅ Health endpoint incluye amd_signals counter                  ║
╚══════════════════════════════════════════════════════════════════╝
"""
import asyncio, logging, signal as signal_mod, sys, traceback
import os, time, csv, json, math, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import cfg
from engine import QFJPEngine
from bingx_client import BingXClient
from telegram_client import TelegramClient
from risk_manager import RiskManager
from session_filter import SessionFilter
from scanner import MarketScanner
from performance import PerformanceTracker, TradeRecord
from amd_filter import get_amd_filter, AMD_REQUIRED, AMD_BOOST

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("MAIN")

# ══════════════════════════════════════════════════════════════════════
# CONSTANTES DE ENTORNO
# ══════════════════════════════════════════════════════════════════════
MIN_OPERABLE_BALANCE = float(os.getenv("MIN_BALANCE",      "5.0"))
BALANCE_TTL          = int(os.getenv("BALANCE_TTL",        "60"))
DRY_RUN              = os.getenv("DRY_RUN", "false").lower() == "true"
STD_AUTOTRADE        = os.getenv("STD_AUTOTRADE", "true").lower() == "true"
CB_MAX_LOSSES        = int(os.getenv("CB_MAX_LOSSES",      "3"))
CB_PAUSE_MIN         = int(os.getenv("CB_PAUSE_MIN",       "30"))
COOLDOWN_LOSS_MIN    = int(os.getenv("COOLDOWN_LOSS_MIN",  "60"))
TRADE_CSV            = os.getenv("TRADE_CSV", "trades_log.csv")
HEALTH_PORT          = int(os.getenv("PORT", "8080"))

# ══════════════════════════════════════════════════════════════════════
# HEALTH ENDPOINT
# ══════════════════════════════════════════════════════════════════════
_health: dict = {
    "version": "6.1", "modo": "iniciando",
    "balance": 0.0, "trades_abiertos": 0,
    "wins": 0, "losses": 0, "pnl_dia": 0.0,
    "circuit_breaker": False, "racha_perdidas": 0,
    "escaneos": 0, "señales": 0,
    "amd_signals": 0,          # ← NUEVO: señales AMD confirmadas
    "ultimo_scan": "—", "ultimo_skip": "—",
}

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        total = _health["wins"] + _health["losses"]
        wr    = f"{round(_health['wins']/total*100)}%" if total else "—"
        if self.path == "/json":
            body = json.dumps({**_health, "win_rate": wr}, ensure_ascii=False).encode()
            ct   = "application/json"
        else:
            body = (
                f"OK v{_health['version']} modo={_health['modo']} "
                f"bal=${_health['balance']:.2f} trades={_health['trades_abiertos']} "
                f"W/L={_health['wins']}/{_health['losses']} WR={wr} "
                f"pnl=${_health['pnl_dia']:+.2f} cb={'SI' if _health['circuit_breaker'] else 'no'} "
                f"amd={_health['amd_signals']} "
                f"scan={_health['ultimo_scan']} skip={_health['ultimo_skip']}"
            ).encode()
            ct = "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

def _start_health():
    try:
        srv = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
        srv.serve_forever()
    except Exception as e:
        log.warning(f"[health] {e}")

_health_ready = threading.Event()
def _start_health_thread():
    t = threading.Thread(target=_start_health, daemon=True, name="health")
    t.start()
    _health_ready.set()

# ══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════
active_positions: dict            = {}
prev_oi:          dict[str,float] = {}
cooldown_sym:     dict[str,float] = {}
_stop_event:      asyncio.Event   = None

_racha_perdidas:        int   = 0
_circuit_breaker_hasta: float = 0.0
_pnl_dia:               float = 0.0
_dia_actual:            int   = -1

_warn_ts: dict[str,float] = {}

def _can_warn(key: str, cooldown: float = 3600.0) -> bool:
    now = time.monotonic()
    if now - _warn_ts.get(key, 0.0) >= cooldown:
        _warn_ts[key] = now
        return True
    return False

def _skip(razon: str):
    _health["ultimo_skip"] = razon
    log.warning(f"SKIP: {razon}")

# ══════════════════════════════════════════════════════════════════════
# BALANCE CACHE
# ══════════════════════════════════════════════════════════════════════
class BalanceCache:
    def __init__(self, exchange, ttl: int = 60):
        self._exchange = exchange
        self._ttl      = ttl
        self._value    = 0.0
        self._ts       = 0.0
        self._lock     = asyncio.Lock()

    async def get(self, force: bool = False) -> float:
        now = time.monotonic()
        if not force and (now - self._ts) < self._ttl:
            return self._value
        async with self._lock:
            now = time.monotonic()
            if not force and (now - self._ts) < self._ttl:
                return self._value
            try:
                val = await self._exchange.get_balance(force=True)
                if val >= 0:
                    self._value = val
                    self._ts    = time.monotonic()
                    _health["balance"] = val
                    log.info(f"BalanceCache: {val:.4f} USDT")
            except Exception as e:
                log.warning(f"BalanceCache error (último={self._value:.4f}): {e}")
        return self._value

# ══════════════════════════════════════════════════════════════════════
# CSV TRADES LOG
# ══════════════════════════════════════════════════════════════════════
def _log_csv(sym, side, entry, exit_price, pnl_pct, pnl_usdt, ganado,
             tier, conv, trail_active, dry, amd_step=0):
    existe = os.path.exists(TRADE_CSV)
    try:
        with open(TRADE_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not existe:
                w.writerow(["fecha_utc","simbolo","lado","entrada","cierre",
                             "pnl_pct","pnl_usdt","resultado","tier","conv",
                             "trailing","dry_run","amd_step"])
            w.writerow([
                datetime.now(timezone.utc).isoformat(),
                sym, side, entry, exit_price,
                round(pnl_pct, 4), round(pnl_usdt, 4),
                "WIN" if ganado else "LOSS",
                tier, conv, trail_active, dry, amd_step,
            ])
    except Exception as e:
        log.warning(f"CSV error: {e}")

# ══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════
def _cb_activo() -> bool:
    if time.time() < _circuit_breaker_hasta:
        restante = int(_circuit_breaker_hasta - time.time())
        if _can_warn("cb_active", 300):
            log.warning(f"⚡ Circuit breaker activo — {restante}s restantes")
        return True
    if _health["circuit_breaker"] and time.time() >= _circuit_breaker_hasta:
        _health["circuit_breaker"] = False
    return False

async def _registrar_cierre(sym, side, entry, price, pnl_pct,
                             tier, conv, trail_active, usdt_size, tg,
                             amd_step: int = 0):
    global _racha_perdidas, _circuit_breaker_hasta, _pnl_dia

    ganado    = pnl_pct > 0
    pnl_usdt  = usdt_size * pnl_pct / 100
    _pnl_dia += pnl_usdt
    _health["pnl_dia"] = round(_pnl_dia, 2)

    _log_csv(sym, side, entry, price, pnl_pct, pnl_usdt,
             ganado, tier, conv, trail_active, DRY_RUN, amd_step)

    if ganado:
        _racha_perdidas = 0
        _health["wins"] += 1
        emoji = "✅"
    else:
        _racha_perdidas += 1
        _health["losses"] += 1
        _health["racha_perdidas"] = _racha_perdidas
        cooldown_sym[sym] = time.time() + COOLDOWN_LOSS_MIN * 60
        emoji = "❌"

        if _racha_perdidas >= CB_MAX_LOSSES:
            _circuit_breaker_hasta = time.time() + CB_PAUSE_MIN * 60
            _health["circuit_breaker"] = True
            log.warning(f"⚡ Circuit breaker: {_racha_perdidas} pérdidas → pausa {CB_PAUSE_MIN}min")
            try:
                await tg.send_message(
                    f"⚡ *Circuit breaker activado*\n"
                    f"{_racha_perdidas} pérdidas consecutivas\n"
                    f"Pausa: `{CB_PAUSE_MIN} min`"
                )
            except Exception:
                pass

    trail_str  = " (trailing)" if trail_active else ""
    amd_tag    = f" | AMD paso {amd_step}/4" if amd_step > 0 else ""
    resultado  = f"{emoji} {'WIN' if ganado else 'LOSS'} {pnl_pct:+.2f}%{trail_str}"
    log.info(f"Cerrado: {sym} {side} | {resultado} | ${pnl_usdt:+.2f} USDT{amd_tag}")

    try:
        await tg.send_message(
            f"📊 *Trade cerrado*: `{sym}`\n"
            f"Dir: `{side}` | Tier: `{tier}` | Conv: `{conv}/10`\n"
            f"Entrada: `{entry}` → Cierre: `{price:.6f}`\n"
            f"{resultado}\n"
            f"PnL: `${pnl_usdt:+.2f}` | Día: `${_pnl_dia:+.2f}`\n"
            f"AMD: `paso {amd_step}/4`"
        )
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════
# SHUTDOWN LIMPIO
# ══════════════════════════════════════════════════════════════════════
async def _graceful_shutdown(running_tasks: list, clients: list):
    log.info("⏹ Iniciando shutdown limpio...")
    _stop_event.set()
    await asyncio.sleep(5)
    for task in running_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*running_tasks, return_exceptions=True)
    for client in clients:
        for method_name in ("close", "aclose"):
            fn = getattr(client, method_name, None)
            if callable(fn):
                try:
                    result = fn()
                    if asyncio.iscoroutine(result):
                        await result
                    log.info(f"{client.__class__.__name__}.{method_name}() OK")
                except Exception as e:
                    log.warning(f"Error cerrando {client.__class__.__name__}: {e}")
                break
    await asyncio.sleep(0.5)
    log.info("Shutdown completado")

# ══════════════════════════════════════════════════════════════════════
# LOOP POR SÍMBOLO
# ══════════════════════════════════════════════════════════════════════
async def run_symbol(symbol, exchange, tg, risk, session, engine,
                     perf, bal_cache: BalanceCache):
    log.info(f"[{symbol}] task arrancada")
    consecutive_errors = 0

    # AMD filter stateful por símbolo
    amd = get_amd_filter(symbol)

    while not _stop_event.is_set():
        try:
            # ── Guardias previas ────────────────────────────────────
            if not session.is_tradeable():
                await asyncio.sleep(30); continue

            if not perf.is_tradeable(symbol):
                await asyncio.sleep(60); continue

            if time.time() < cooldown_sym.get(symbol, 0):
                restante = int(cooldown_sym[symbol] - time.time())
                if _can_warn(f"cd_{symbol}", 600):
                    log.info(f"[{symbol}] cooldown post-pérdida: {restante}s")
                await asyncio.sleep(min(restante, cfg.LOOP_INTERVAL)); continue

            if _cb_activo():
                await asyncio.sleep(60); continue

            # ── Balance ─────────────────────────────────────────────
            bal = await bal_cache.get()

            if bal < MIN_OPERABLE_BALANCE:
                if _can_warn("low_balance", 3600.0):
                    log.warning(f"Balance {bal:.4f} < mínimo {MIN_OPERABLE_BALANCE} USDT")
                    try:
                        await tg.send_message(
                            f"⚠️ *Balance insuficiente*\n"
                            f"Balance: `{bal:.4f} USDT`\n"
                            f"Mínimo: `{MIN_OPERABLE_BALANCE} USDT`"
                        )
                    except Exception: pass
                await asyncio.sleep(3600); continue

            if not risk.max_daily_loss_ok(bal, cfg.MAX_DAILY_DD_PCT):
                await asyncio.sleep(3600); continue

            risk.update_start_balance(bal)

            if (len(active_positions) >= cfg.MAX_OPEN_POSITIONS
                    and symbol not in active_positions):
                await asyncio.sleep(cfg.LOOP_INTERVAL); continue

            # ── Klines multi-TF ─────────────────────────────────────
            results = await asyncio.gather(
                exchange.get_klines(symbol, "3m",  250),
                exchange.get_klines(symbol, "15m", 100),
                exchange.get_klines(symbol, "1h",  60),
                exchange.get_klines(symbol, "1m",  60),
                return_exceptions=True
            )
            ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m = results

            if isinstance(ohlcv_3m, Exception) or len(ohlcv_3m) < 50:
                await asyncio.sleep(15); continue

            ohlcv_15m = [] if isinstance(ohlcv_15m, Exception) else ohlcv_15m
            ohlcv_1h  = [] if isinstance(ohlcv_1h,  Exception) else ohlcv_1h
            ohlcv_1m  = [] if isinstance(ohlcv_1m,  Exception) else ohlcv_1m

            # ── Contexto de mercado ──────────────────────────────────
            try:
                mctx = await exchange.get_market_context(symbol, cfg.OFI_LEVELS)
            except Exception:
                mctx = {"ofi": 0.0, "funding_rate": 0.0, "open_interest": 0.0}

            mctx["prev_open_interest"] = prev_oi.get(symbol, mctx["open_interest"])
            prev_oi[symbol] = mctx["open_interest"]

            sig    = engine.compute(ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m, mctx)
            ticker = await exchange.get_ticker(symbol)
            price  = ticker["last"]

            _health["ultimo_scan"] = datetime.now(timezone.utc).strftime("%H:%M")
            _health["escaneos"]   += 1

            # ══════════════════════════════════════════════════════
            # AMD — EVALUACIÓN (sobre ohlcv_3m, mayor contexto)
            # ══════════════════════════════════════════════════════
            amd_sig = amd.update(ohlcv_3m)

            # Si AMD_REQUIRED=true y no hay AMD confirmado → skip entrada
            if amd_sig.blocked and symbol not in active_positions:
                _skip(f"{symbol} AMD paso {amd_sig.step}/4 (REQUIRED)")
                await asyncio.sleep(cfg.LOOP_INTERVAL); continue

            # ══════════════════════════════════════════════════════
            # GESTIÓN POSICIÓN ACTIVA
            # ══════════════════════════════════════════════════════
            pos = active_positions.get(symbol)
            if pos:
                atr_pos = pos.get("atr", 0)
                if atr_pos > 0:
                    if pos["side"] == "LONG":
                        if (not pos.get("trail_active")
                                and (price - pos["entry"]) >= atr_pos * cfg.TRAIL_ACTIVATE_ATR):
                            pos["trail_active"] = True
                            pos["trail_sl"]     = price - atr_pos * cfg.TRAIL_ATR_MULT
                        if pos.get("trail_active"):
                            new_t = price - atr_pos * cfg.TRAIL_ATR_MULT
                            if new_t > pos.get("trail_sl", pos["sl"]):
                                pos["trail_sl"] = new_t
                            pos["sl"] = max(pos["sl"], pos["trail_sl"])
                    else:
                        if (not pos.get("trail_active")
                                and (pos["entry"] - price) >= atr_pos * cfg.TRAIL_ACTIVATE_ATR):
                            pos["trail_active"] = True
                            pos["trail_sl"]     = price + atr_pos * cfg.TRAIL_ATR_MULT
                        if pos.get("trail_active"):
                            new_t = price + atr_pos * cfg.TRAIL_ATR_MULT
                            if new_t < pos.get("trail_sl", pos["sl"]):
                                pos["trail_sl"] = new_t
                            pos["sl"] = min(pos["sl"], pos["trail_sl"])

                sl_hit = ((pos["side"] == "LONG"  and price <= pos["sl"]) or
                          (pos["side"] == "SHORT" and price >= pos["sl"]))
                tp_hit = (pos.get("tp") and
                          ((pos["side"] == "LONG"  and price >= pos["tp"]) or
                           (pos["side"] == "SHORT" and price <= pos["tp"])))
                rev    = (sig["direction"] and sig["direction"] != pos["side"]
                          and sig["conviction"] >= 7)

                reason = (("SL" + (" trailing" if pos.get("trail_active") else ""))
                          if sl_hit else "TP" if tp_hit else "Reversal" if rev else None)

                if reason:
                    if cfg.MODE == "LIVE" and not DRY_RUN:
                        await exchange.close_position(symbol, pos["side"])

                    pnl_pct = ((price - pos["entry"]) / pos["entry"] * 100
                               if pos["side"] == "LONG"
                               else (pos["entry"] - price) / pos["entry"] * 100)

                    usdt_size = pos.get("usdt_size", pos["size"] * pos["entry"] / cfg.LEVERAGE)

                    await _registrar_cierre(
                        symbol, pos["side"], pos["entry"], price,
                        pnl_pct, pos["tier"], pos["conv"],
                        pos.get("trail_active", False), usdt_size, tg,
                        amd_step=pos.get("amd_step", 0),
                    )

                    perf.record(TradeRecord(
                        symbol=symbol, side=pos["side"],
                        entry=pos["entry"], exit=price,
                        pnl_pct=pnl_pct, conviction=pos["conv"],
                        tier=pos["tier"]
                    ))

                    del active_positions[symbol]
                    _health["trades_abiertos"] = len(active_positions)
                    await bal_cache.get(force=True)

            # ══════════════════════════════════════════════════════
            # NUEVA ENTRADA
            # ══════════════════════════════════════════════════════
            if symbol not in active_positions and sig["direction"]:
                tier  = sig["tier"]
                conv  = sig["conviction"]
                min_c = (cfg.MIN_CONV_SUP  if tier == "SUP"  else
                         cfg.MIN_CONV_FUEL if tier == "FUEL" else cfg.MIN_CONV_STD)

                # ── Filtro STD_AUTOTRADE ──────────────────────────
                if tier == "STD" and not STD_AUTOTRADE:
                    _skip(f"{symbol} STD ignorado (STD_AUTOTRADE=false)")
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                if sig.get("vol_regime") == "LOW":
                    _skip(f"{symbol} vol LOW")
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                if conv < min_c:
                    _skip(f"{symbol} conv {conv} < min {min_c} para {tier}")
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                # ── AMD: validación de dirección ──────────────────
                # Si AMD está en paso 4, valida que la dirección coincida
                # Si AMD está en pasos 1-3, solo bloquea si AMD_REQUIRED=true
                # y la dirección AMD contradice la señal QF
                amd_conflict = (
                    amd_sig.step == 4
                    and amd_sig.direction is not None
                    and amd_sig.direction != sig["direction"]
                )
                if amd_conflict:
                    _skip(
                        f"{symbol} AMD contradice: AMD={amd_sig.direction} "
                        f"vs QF={sig['direction']}"
                    )
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                # Boost de score cuando AMD completo y alineado
                amd_score_boost = amd_sig.boost if amd_sig.step == 4 else 0

                sl   = sig["sl"]
                tp   = sig.get("tp")
                atr  = sig.get("atr_last", None)
                size = risk.position_size(
                    bal, price, sl,
                    cfg.RISK_PER_TRADE_PCT, cfg.LEVERAGE, atr=atr,
                )

                if size <= 0:
                    _skip(f"{symbol} size=0 (bal={bal:.2f} sl_dist insuficiente)")
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                order_id = "DRY_RUN" if DRY_RUN else "SIGNAL_ONLY"

                if cfg.MODE == "LIVE" and not DRY_RUN:
                    order = await exchange.place_order(
                        symbol, sig["direction"], size, cfg.LEVERAGE, sl, tp,
                        use_maker=cfg.USE_MAKER_ORDERS,
                        maker_timeout=cfg.MAKER_TIMEOUT,
                        maker_offset_pct=cfg.MAKER_OFFSET_PCT,
                    )
                    if not order:
                        _skip(f"{symbol} orden LIVE fallida")
                        await asyncio.sleep(cfg.LOOP_INTERVAL); continue
                    order_id = order.get("orderId", "?")
                    await bal_cache.get(force=True)

                usdt_size = size * price / cfg.LEVERAGE

                active_positions[symbol] = dict(
                    side=sig["direction"], entry=price, sl=sl, tp=tp,
                    size=size, conv=conv, tier=tier,
                    time=datetime.now(timezone.utc),
                    atr=sig.get("atr_last", 0),
                    trail_active=False, trail_sl=None,
                    usdt_size=usdt_size,
                    amd_step=amd_sig.step,          # ← guardamos paso AMD
                )
                _health["trades_abiertos"] = len(active_positions)
                _health["señales"]        += 1
                _health["ultimo_skip"]     = "—"

                if amd_sig.step == 4:
                    _health["amd_signals"] += 1

                # Alerta Telegram enriquecida con AMD
                await _enviar_alerta_entrada(
                    tg, symbol, sig, price, size,
                    order_id, mctx, conv, tier,
                    amd_sig=amd_sig,
                    amd_boost=amd_score_boost,
                )

                log.info(
                    f"[{symbol}] ✅ {sig['direction']} {tier} conv={conv}/10 "
                    f"score={sig['norm_score']:.2f} OFI={sig['ofi']:.2f} "
                    f"AMD={amd_sig.step}/4 boost=+{amd_score_boost} "
                    f"{'[DRY]' if DRY_RUN else ''}"
                )

            consecutive_errors = 0

        except asyncio.CancelledError:
            log.info(f"[{symbol}] task cancelada")
            break
        except Exception as e:
            consecutive_errors += 1
            log.error(f"[{symbol}] error #{consecutive_errors}: {e}")
            if consecutive_errors >= 10:
                try:
                    await tg.send_message(f"⚠️ [{symbol}] {consecutive_errors} errores — pausa 10min")
                except Exception: pass
                await asyncio.sleep(600)
                consecutive_errors = 0
            else:
                await asyncio.sleep(cfg.LOOP_INTERVAL * 2)

        await asyncio.sleep(cfg.LOOP_INTERVAL)


# ══════════════════════════════════════════════════════════════════════
# ALERTA TELEGRAM DE ENTRADA (dashboard AMD integrado)
# ══════════════════════════════════════════════════════════════════════
async def _enviar_alerta_entrada(tg, symbol, sig, price, size,
                                  order_id, mctx, conv, tier,
                                  amd_sig=None, amd_boost: int = 0):
    try:
        es_long  = sig["direction"] == "LONG"
        es_sup   = tier == "SUP"
        es_fuel  = tier == "FUEL"
        emoji    = "🔵" if es_sup else "🟡" if es_fuel else "🟢"
        dir_e    = "🟢" if es_long else "🔴"
        dry_tag  = " [DRY RUN]" if DRY_RUN else ""
        mode_tag = " [SIGNAL]" if cfg.MODE != "LIVE" else ""

        sl_p  = sig.get("sl", 0)
        tp_p  = sig.get("tp", price * (1.05 if es_long else 0.95))
        atr_v = sig.get("atr_last", 0)
        score = sig.get("norm_score", 0)
        ofi   = mctx.get("ofi", 0)
        fr    = mctx.get("funding_rate", 0)
        oi_d  = mctx.get("open_interest", 0) - mctx.get("prev_open_interest", 0)

        decay = sig.get("decay_r", 0) if hasattr(sig, "get") else 0
        db    = max(0, min(8, round(decay / 100 * 8))) if decay else 4
        barra = "█" * db + "░" * (8 - db)

        # ── Bloque AMD ─────────────────────────────────────────────
        amd_lines = ""
        if amd_sig is not None:
            amd_step = amd_sig.step
            step_bar = "■" * amd_step + "□" * (4 - amd_step)
            amd_dir  = amd_sig.direction or "—"

            if amd_sig.step == 4:
                amd_header = f"🎯 *AMD {amd_dir} CONFIRMADO*"
                amd_boost_str = f"+{amd_boost}pts" if amd_boost else ""
            elif amd_sig.step >= 2:
                amd_header = f"⚡ AMD paso {amd_step}/4"
                amd_boost_str = ""
            else:
                amd_header = f"○ AMD paso {amd_step}/4 (sin confirmar)"
                amd_boost_str = ""

            # Detalle de cada paso
            p1 = "✅" if amd_sig.asian_range else "⬜"
            p2 = "✅" if (amd_sig.sweep_low or amd_sig.sweep_high) else "⬜"
            p3 = "✅" if amd_sig.high_vol else "⬜"
            p4 = "✅" if (amd_sig.mss_bull or amd_sig.mss_bear) else "⬜"

            rng_str = ""
            if amd_sig.asian_range:
                r = amd_sig.asian_range
                rng_str = f"`[{r.low:.4f}–{r.high:.4f}]`"

            amd_lines = (
                f"{'─'*28}\n"
                f"{amd_header} {amd_boost_str}\n"
                f"`{step_bar}` {p1}Rng {p2}Swp {p3}Vol {p4}MSS\n"
                f"Rango asiático: {rng_str}\n"
            )

        msg = (
            f"{emoji} *{sig['direction']} {tier}: {symbol}*{dry_tag}{mode_tag}\n"
            f"{'─'*28}\n"
            f"{dir_e} Conv: `{conv}/10` | Score: `{score:.2f}`\n"
            f"💲 Precio: `{price}` | OFI: `{ofi:.2f}`\n"
            f"💸 FR: `{fr*100:.4f}%` | OI Δ: `{oi_d:+.0f}`\n"
            f"🛑 SL: `{sl_p}` | 🎯 TP: `{tp_p}`\n"
            f"📦 Size: `{size}` | ATR: `{atr_v:.6f}`\n"
            f"{'─'*28}\n"
            f"DECAY `{barra}`\n"
            f"{amd_lines}"
            f"OrderID: `{order_id}`"
        )
        await tg.send_message(msg)
    except Exception as e:
        log.warning(f"Alerta entrada error: {e}")


# ══════════════════════════════════════════════════════════════════════
# SCANNER LOOP
# ══════════════════════════════════════════════════════════════════════
async def scanner_loop(exchange, tg, perf, engine, risk, session,
                        bal_cache: BalanceCache):
    scanner = MarketScanner(exchange)
    tasks:  dict[str, asyncio.Task] = {}

    while not _stop_event.is_set():
        try:
            symbols = await scanner.get_tradeable_symbols()
            log.info(f"Scanner: {len(symbols)} pares activos")

            gs = perf.global_stats()
            if gs and gs.get("total_trades", 0) > 0:
                try:
                    await tg.send_message(
                        f"🔍 *Scanner — {len(symbols)} pares*\n"
                        f"WR={gs['win_rate']:.0%} | PF={gs['profit_factor']:.2f} | "
                        f"avg={gs['avg_pnl']:.2f}%\n"
                        f"PnL día: `${_pnl_dia:+.2f}` | "
                        f"W/L: `{_health['wins']}/{_health['losses']}`\n"
                        f"AMD signals: `{_health['amd_signals']}`\n"
                        f"⛔ Suspendidos: {', '.join(gs.get('suspended', [])) or 'ninguno'}"
                    )
                except Exception: pass

            for sym in symbols:
                if sym not in tasks or tasks[sym].done():
                    tasks[sym] = asyncio.create_task(
                        run_symbol(sym, exchange, tg, risk, session,
                                   engine, perf, bal_cache)
                    )

            for sym in list(tasks):
                if sym not in symbols and not tasks[sym].done():
                    tasks[sym].cancel()
                    del tasks[sym]

        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            break
        except Exception as e:
            log.error(f"scanner_loop: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(60)

        await asyncio.sleep(cfg.SCANNER_INTERVAL)


# ══════════════════════════════════════════════════════════════════════
# STATUS LOOP
# ══════════════════════════════════════════════════════════════════════
async def status_loop(tg, bal_cache: BalanceCache):
    global _pnl_dia, _dia_actual

    while not _stop_event.is_set():
        await asyncio.sleep(3600)
        try:
            ahora = datetime.now(timezone.utc)

            if ahora.day != _dia_actual and _dia_actual != -1:
                try:
                    await tg.send_message(
                        f"📅 *Resumen diario*\n"
                        f"PnL: `${_pnl_dia:+.2f}` USDT\n"
                        f"W/L: `{_health['wins']}/{_health['losses']}`\n"
                        f"AMD signals: `{_health['amd_signals']}`\n"
                        f"Trades abiertos: `{len(active_positions)}`"
                    )
                except Exception: pass
                _pnl_dia = 0.0
                _health["wins"] = _health["losses"] = 0
                _health["pnl_dia"] = 0.0
                _health["amd_signals"] = 0

            _dia_actual = ahora.day

            bal = await bal_cache.get(force=True)

            cb_str = ""
            if time.time() < _circuit_breaker_hasta:
                rest   = int((_circuit_breaker_hasta - time.time()) / 60)
                cb_str = f"\n⚡ CB activo: `{rest}min` restantes"

            pos_str = ""
            for sym, p in list(active_positions.items()):
                amd_tag = f" [AMD✓]" if p.get("amd_step", 0) == 4 else ""
                pos_str += f"\n  `{sym}` {p['side']} @ {p['entry']}{amd_tag}"

            await tg.send_message(
                f"⏱ *Status horario*\n"
                f"Balance: `{bal:.2f} USDT`\n"
                f"PnL día: `${_pnl_dia:+.2f}` | W/L: `{_health['wins']}/{_health['losses']}`\n"
                f"Posiciones: `{len(active_positions)}/{cfg.MAX_OPEN_POSITIONS}`{pos_str}{cb_str}\n"
                f"AMD signals hoy: `{_health['amd_signals']}`"
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"status_loop: {e}")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════
async def main():
    global _stop_event, _dia_actual

    _stop_event = asyncio.Event()
    _dia_actual = datetime.now(timezone.utc).day

    _start_health_thread()
    log.info(f"[health] HTTP en 0.0.0.0:{HEALTH_PORT}")

    modo = "DRY RUN" if DRY_RUN else ("LIVE" if cfg.MODE == "LIVE" else "SIGNAL ONLY")
    _health["modo"] = modo

    log.info("═══════════════════════════════════════════")
    log.info("  QF×JP Bot v6.1  |  BingX Futures + AMD")
    log.info(f"  MODE={modo} | MAX_POS={cfg.MAX_OPEN_POSITIONS}")
    log.info(f"  AMD_REQUIRED={'ON' if AMD_REQUIRED else 'OFF'} | AMD_BOOST={AMD_BOOST}pts")
    log.info(f"  MAKER={'ON' if cfg.USE_MAKER_ORDERS else 'OFF'}")
    log.info(f"  STD_AUTOTRADE={'ON' if STD_AUTOTRADE else 'OFF'}")
    log.info(f"  CB: {CB_MAX_LOSSES} pérdidas → pausa {CB_PAUSE_MIN}min")
    log.info(f"  COOLDOWN_LOSS: {COOLDOWN_LOSS_MIN}min por símbolo")
    log.info(f"  MIN_BALANCE={MIN_OPERABLE_BALANCE} USDT | DD_MAX={cfg.MAX_DAILY_DD_PCT}%")
    log.info("═══════════════════════════════════════════")

    tg       = TelegramClient(cfg.TG_TOKEN, cfg.TG_CHAT_ID)
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    risk     = RiskManager()
    session  = SessionFilter()
    engine   = QFJPEngine()
    perf     = PerformanceTracker(cfg.PF_WINDOW, cfg.MIN_PROFIT_FACTOR)

    bal_cache = BalanceCache(exchange, ttl=BALANCE_TTL)

    bal = 0.0
    for attempt in range(5):
        try:
            bal = await bal_cache.get(force=True)
            if bal > 0:
                break
            await asyncio.sleep(5)
        except Exception as e:
            log.warning(f"Balance intento {attempt+1}/5: {e}")
            await asyncio.sleep(10)

    log.info(f"Balance inicial: {bal:.4f} USDT")

    if bal >= MIN_OPERABLE_BALANCE:
        risk.update_start_balance(bal)
    else:
        log.warning(f"Balance {bal:.4f} < mínimo {MIN_OPERABLE_BALANCE} — esperando depósito")

    loop = asyncio.get_event_loop()
    def _stop():
        log.info("Señal de parada recibida")
        _stop_event.set()
    for s in (signal_mod.SIGINT, signal_mod.SIGTERM):
        loop.add_signal_handler(s, _stop)

    maker_fee    = "0.04% maker" if cfg.USE_MAKER_ORDERS else "0.15% market"
    balance_line = (f"`{bal:.2f} USDT`" if bal >= MIN_OPERABLE_BALANCE
                    else f"⚠️ `{bal:.4f} USDT` — deposita fondos")
    try:
        await tg.send_message(
            f"🟢 *QF×JP Bot v6.1 + AMD iniciado*\n"
            f"Modo: *{modo}* | Balance: {balance_line}\n"
            f"Fee: `{maker_fee}` | Leverage: `{cfg.LEVERAGE}×`\n"
            f"AMD: `✅` | Required: `{'✅' if AMD_REQUIRED else '❌'}` | "
            f"Boost: `+{AMD_BOOST}pts`\n"
            f"OFI+FR+OI: `✅` | Multi-TF: `✅` | Trailing SL: `✅`\n"
            f"STD auto: `{'✅' if STD_AUTOTRADE else '❌'}` | "
            f"CSV log: `✅` | CB: `{CB_MAX_LOSSES}→{CB_PAUSE_MIN}min`"
        )
    except Exception as e:
        log.warning(f"Telegram startup: {e}")

    t_scanner = asyncio.create_task(
        scanner_loop(exchange, tg, perf, engine, risk, session, bal_cache)
    )
    t_status = asyncio.create_task(
        status_loop(tg, bal_cache)
    )

    try:
        await asyncio.gather(t_scanner, t_status, return_exceptions=True)
    except Exception as e:
        log.error(f"gather: {e}\n{traceback.format_exc()}")
    finally:
        await _graceful_shutdown(
            running_tasks=[t_scanner, t_status],
            clients=[exchange, tg],
        )
        try:
            await tg.send_message(
                f"🔴 *Bot detenido*\n"
                f"PnL sesión: `${_pnl_dia:+.2f}` USDT\n"
                f"W/L: `{_health['wins']}/{_health['losses']}`\n"
                f"AMD signals: `{_health['amd_signals']}`"
            )
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
