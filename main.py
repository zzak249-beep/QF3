"""
QF×JP Bot v5.7 — Fix rate limiting en get_balance
Fixes sobre v5.6:
  • Balance centralizado: un solo BalanceCache compartido por todas las tasks
  • get_balance() se llama UNA vez cada BALANCE_TTL segundos (no 29 veces en paralelo)
  • Cooldown de low_balance warnings movido a 1h global (no por símbolo)
  • Si BingX devuelve error de rate limit → usa último balance cacheado
  • MIN_OPERABLE_BALANCE configurable via env var
"""
import asyncio, logging, signal as signal_mod, sys, traceback, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone

from config import cfg
from engine import QFJPEngine
from bingx_client import BingXClient
from telegram_client import TelegramClient
from risk_manager import RiskManager
from session_filter import SessionFilter
from scanner import MarketScanner
from performance import PerformanceTracker, TradeRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("MAIN")

# ── Constantes ────────────────────────────────────────────────
MIN_OPERABLE_BALANCE = float(os.getenv("MIN_BALANCE", "5.0"))  # USDT
BALANCE_TTL          = int(os.getenv("BALANCE_TTL", "60"))     # segundos entre llamadas reales

# ── Estado global ─────────────────────────────────────────────
active_positions : dict             = {}
prev_oi          : dict[str, float] = {}
_stop_event      : asyncio.Event    = None


# ─────────────────────────────────────────────────────────────
#  BALANCE CACHE CENTRALIZADO
#  Todas las tasks leen de aquí — solo una llamada real cada BALANCE_TTL s
# ─────────────────────────────────────────────────────────────
class BalanceCache:
    def __init__(self, exchange, ttl: int = 60):
        self._exchange  = exchange
        self._ttl       = ttl
        self._value     = 0.0
        self._ts        = 0.0
        self._lock      = asyncio.Lock()

    async def get(self, force: bool = False) -> float:
        now = time.monotonic()
        if not force and (now - self._ts) < self._ttl:
            return self._value                          # cache hit — sin llamada API

        async with self._lock:
            # Double-check tras adquirir el lock (otro task puede haber actualizado ya)
            now = time.monotonic()
            if not force and (now - self._ts) < self._ttl:
                return self._value

            try:
                val = await self._exchange.get_balance(force=True)
                if val >= 0:
                    self._value = val
                    self._ts    = time.monotonic()
                    log.info(f"BalanceCache actualizado: {val:.4f} USDT")
            except Exception as e:
                log.warning(f"BalanceCache error (usando último valor {self._value:.4f}): {e}")

        return self._value


# ── Cooldown global para warnings ────────────────────────────
_global_warn_ts: dict[str, float] = {}

def _can_warn_global(key: str, cooldown: float = 3600.0) -> bool:
    now = time.monotonic()
    if now - _global_warn_ts.get(key, 0.0) >= cooldown:
        _global_warn_ts[key] = now
        return True
    return False


# ─────────────────────────────────────────────────────────────
#  SHUTDOWN LIMPIO
# ─────────────────────────────────────────────────────────────
async def _graceful_shutdown(running_tasks: list, clients: list):
    log.info("⏹ Iniciando shutdown limpio...")
    _stop_event.set()
    await asyncio.sleep(5)

    for task in running_tasks:
        if not task.done():
            task.cancel()

    await asyncio.gather(*running_tasks, return_exceptions=True)
    log.info("Tasks canceladas")

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


# ─────────────────────────────────────────────────────────────
#  LOOP POR SÍMBOLO
# ─────────────────────────────────────────────────────────────
async def run_symbol(symbol, exchange, tg, risk, session, engine, perf, bal_cache: BalanceCache):
    log.info(f"[{symbol}] task arrancada")
    consecutive_errors = 0

    while not _stop_event.is_set():
        try:
            if not session.is_tradeable():
                await asyncio.sleep(30)
                continue

            if not perf.is_tradeable(symbol):
                await asyncio.sleep(60)
                continue

            # ── Balance desde caché (una sola llamada real cada BALANCE_TTL s) ──
            bal = await bal_cache.get()

            # ── 1. Balance mínimo operable ───────────────────────────────
            if bal < MIN_OPERABLE_BALANCE:
                if _can_warn_global("low_balance", cooldown=3600.0):
                    log.warning(f"Balance {bal:.4f} USDT < mínimo {MIN_OPERABLE_BALANCE} USDT")
                    try:
                        await tg.send_message(
                            f"⚠️ *Balance insuficiente*\n"
                            f"Balance: `{bal:.4f} USDT`\n"
                            f"Mínimo: `{MIN_OPERABLE_BALANCE} USDT`\n"
                            f"Deposita fondos para reanudar."
                        )
                    except Exception:
                        pass
                await asyncio.sleep(3600)
                continue

            # ── 2. DD check ANTES de update_start_balance ───────────────
            if not risk.max_daily_loss_ok(bal, cfg.MAX_DAILY_DD_PCT):
                await asyncio.sleep(3600)
                continue

            # ── 3. Actualizar start_balance solo con balance sano ────────
            risk.update_start_balance(bal)

            if len(active_positions) >= cfg.MAX_OPEN_POSITIONS and symbol not in active_positions:
                await asyncio.sleep(cfg.LOOP_INTERVAL)
                continue

            results = await asyncio.gather(
                exchange.get_klines(symbol, "3m",  250),
                exchange.get_klines(symbol, "15m", 100),
                exchange.get_klines(symbol, "1h",  60),
                exchange.get_klines(symbol, "1m",  60),
                return_exceptions=True
            )
            ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m = results

            if isinstance(ohlcv_3m, Exception) or len(ohlcv_3m) < 50:
                await asyncio.sleep(15)
                continue

            ohlcv_15m = [] if isinstance(ohlcv_15m, Exception) else ohlcv_15m
            ohlcv_1h  = [] if isinstance(ohlcv_1h,  Exception) else ohlcv_1h
            ohlcv_1m  = [] if isinstance(ohlcv_1m,  Exception) else ohlcv_1m

            try:
                mctx = await exchange.get_market_context(symbol, cfg.OFI_LEVELS)
            except Exception:
                mctx = {"ofi": 0.0, "funding_rate": 0.0, "open_interest": 0.0}

            mctx["prev_open_interest"] = prev_oi.get(symbol, mctx["open_interest"])
            prev_oi[symbol] = mctx["open_interest"]

            sig    = engine.compute(ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m, mctx)
            ticker = await exchange.get_ticker(symbol)
            price  = ticker["last"]

            # ── Gestión posición activa ──────────────────────
            pos = active_positions.get(symbol)
            if pos:
                atr_pos = pos.get("atr", 0)
                if atr_pos > 0:
                    if pos["side"] == "LONG":
                        if not pos.get("trail_active") and (price - pos["entry"]) >= atr_pos * cfg.TRAIL_ACTIVATE_ATR:
                            pos["trail_active"] = True
                            pos["trail_sl"] = price - atr_pos * cfg.TRAIL_ATR_MULT
                        if pos.get("trail_active"):
                            new_t = price - atr_pos * cfg.TRAIL_ATR_MULT
                            if new_t > pos.get("trail_sl", pos["sl"]):
                                pos["trail_sl"] = new_t
                            pos["sl"] = max(pos["sl"], pos["trail_sl"])
                    else:
                        if not pos.get("trail_active") and (pos["entry"] - price) >= atr_pos * cfg.TRAIL_ACTIVATE_ATR:
                            pos["trail_active"] = True
                            pos["trail_sl"] = price + atr_pos * cfg.TRAIL_ATR_MULT
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

                reason = ("SL" + (" trailing" if pos.get("trail_active") else "")) if sl_hit \
                         else "TP" if tp_hit else "Reversal" if rev else None

                if reason:
                    if cfg.MODE == "LIVE":
                        await exchange.close_position(symbol, pos["side"])
                    pnl = ((price - pos["entry"]) / pos["entry"] * 100 if pos["side"] == "LONG"
                           else (pos["entry"] - price) / pos["entry"] * 100)
                    await tg.send_close(symbol, pos["side"], pos["entry"], price, pnl, reason,
                                        trail_was_active=pos.get("trail_active", False))
                    perf.record(TradeRecord(symbol=symbol, side=pos["side"],
                                           entry=pos["entry"], exit=price,
                                           pnl_pct=pnl, conviction=pos["conv"],
                                           tier=pos["tier"]))
                    del active_positions[symbol]
                    # Forzar refresco de balance tras cerrar posición
                    await bal_cache.get(force=True)

            # ── Nueva entrada ────────────────────────────────
            if symbol not in active_positions and sig["direction"]:
                tier  = sig["tier"]
                conv  = sig["conviction"]
                min_c = (cfg.MIN_CONV_SUP  if tier == "SUP"  else
                         cfg.MIN_CONV_FUEL if tier == "FUEL" else cfg.MIN_CONV_STD)

                if sig.get("vol_regime") == "LOW" or conv < min_c:
                    await asyncio.sleep(cfg.LOOP_INTERVAL)
                    continue

                sl   = sig["sl"]
                tp   = sig.get("tp")
                atr  = sig.get("atr_last", None)
                size = risk.position_size(
                    bal, price, sl,
                    cfg.RISK_PER_TRADE_PCT, cfg.LEVERAGE,
                    atr=atr,
                )
                if size <= 0:
                    await asyncio.sleep(cfg.LOOP_INTERVAL)
                    continue

                order_id = "SIGNAL_ONLY"
                if cfg.MODE == "LIVE":
                    order = await exchange.place_order(
                        symbol, sig["direction"], size, cfg.LEVERAGE, sl, tp,
                        use_maker=cfg.USE_MAKER_ORDERS,
                        maker_timeout=cfg.MAKER_TIMEOUT,
                        maker_offset_pct=cfg.MAKER_OFFSET_PCT,
                    )
                    if not order:
                        await asyncio.sleep(cfg.LOOP_INTERVAL)
                        continue
                    order_id = order.get("orderId", "?")
                    # Refresco de balance tras abrir posición
                    await bal_cache.get(force=True)

                active_positions[symbol] = dict(
                    side=sig["direction"], entry=price, sl=sl, tp=tp,
                    size=size, conv=conv, tier=tier,
                    time=datetime.now(timezone.utc),
                    atr=sig.get("atr_last", 0),
                    trail_active=False, trail_sl=None,
                )
                await tg.send_entry(symbol, sig, price, size, order_id, mctx)
                log.info(f"[{symbol}] ✅ {sig['direction']} {tier} conv={conv}/10 "
                         f"score={sig['norm_score']:.2f} OFI={sig['ofi']:.2f}")

            consecutive_errors = 0

        except asyncio.CancelledError:
            log.info(f"[{symbol}] task cancelada limpiamente")
            break
        except Exception as e:
            consecutive_errors += 1
            log.error(f"[{symbol}] error #{consecutive_errors}: {e}")
            if consecutive_errors >= 10:
                await tg.send_error(f"[{symbol}] demasiados errores — task pausada 10min")
                await asyncio.sleep(600)
                consecutive_errors = 0
            else:
                await asyncio.sleep(cfg.LOOP_INTERVAL * 2)

        await asyncio.sleep(cfg.LOOP_INTERVAL)


# ─────────────────────────────────────────────────────────────
#  SCANNER LOOP
# ─────────────────────────────────────────────────────────────
async def scanner_loop(exchange, tg, perf, engine, risk, session, bal_cache: BalanceCache):
    scanner = MarketScanner(exchange)
    tasks   : dict[str, asyncio.Task] = {}

    while not _stop_event.is_set():
        try:
            symbols = await scanner.get_tradeable_symbols()
            log.info(f"Scanner: {len(symbols)} pares activos")

            gs = perf.global_stats()
            if gs and gs.get("total_trades", 0) > 0:
                await tg.send_message(
                    f"🔍 *Scanner — {len(symbols)} pares*\n"
                    f"WR={gs['win_rate']:.0%} | PF={gs['profit_factor']:.2f} | "
                    f"avg={gs['avg_pnl']:.2f}%\n"
                    f"⛔ Suspendidos: {', '.join(gs['suspended']) or 'ninguno'}"
                )

            for sym in symbols:
                if sym not in tasks or tasks[sym].done():
                    tasks[sym] = asyncio.create_task(
                        run_symbol(sym, exchange, tg, risk, session, engine, perf, bal_cache)
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
            log.error(f"scanner_loop error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(60)

        await asyncio.sleep(cfg.SCANNER_INTERVAL)


# ─────────────────────────────────────────────────────────────
#  STATUS LOOP
# ─────────────────────────────────────────────────────────────
async def status_loop(tg, bal_cache: BalanceCache, perf):
    while not _stop_event.is_set():
        await asyncio.sleep(3600)
        try:
            bal = await bal_cache.get(force=True)
            await tg.send_status(bal, active_positions, perf.global_stats())
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"status_loop: {e}")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
async def main():
    global _stop_event
    _stop_event = asyncio.Event()

    log.info("═══════════════════════════════════════")
    log.info("  QF×JP Bot v5.7  |  BingX Futures")
    log.info(f"  SCORE_THR={cfg.SCORE_THR_LONG} | DECAY_THR={cfg.DECAY_THR}")
    log.info(f"  MAKER_ORDERS={'ON' if cfg.USE_MAKER_ORDERS else 'OFF'}")
    log.info(f"  MODE={cfg.MODE} | MAX_POS={cfg.MAX_OPEN_POSITIONS}")
    log.info(f"  MIN_BALANCE={MIN_OPERABLE_BALANCE} USDT | MAX_DD={cfg.MAX_DAILY_DD_PCT}%")
    log.info(f"  BALANCE_TTL={BALANCE_TTL}s (llamadas API reducidas)")
    log.info("═══════════════════════════════════════")

    tg       = TelegramClient(cfg.TG_TOKEN, cfg.TG_CHAT_ID)
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    risk     = RiskManager()
    session  = SessionFilter()
    engine   = QFJPEngine()
    perf     = PerformanceTracker(cfg.PF_WINDOW, cfg.MIN_PROFIT_FACTOR)

    # ── Balance cache centralizado ────────────────────────────
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
        log.warning(
            f"Balance {bal:.4f} USDT < mínimo {MIN_OPERABLE_BALANCE} USDT — "
            f"esperando depósito para inicializar start_balance"
        )

    loop = asyncio.get_event_loop()
    def _stop():
        log.info("Señal de parada recibida")
        _stop_event.set()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    try:
        maker_fee    = "0.04% (maker)" if cfg.USE_MAKER_ORDERS else "0.15% (market)"
        balance_line = (
            f"Balance: `{bal:.2f} USDT`" if bal >= MIN_OPERABLE_BALANCE
            else f"⚠️ Sin fondos: `{bal:.4f} USDT` — deposita ≥ {MIN_OPERABLE_BALANCE} USDT"
        )
        await tg.send_message(
            f"🟢 *QF×JP Bot v5.7 iniciado*\n"
            f"{'🔴 LIVE' if cfg.MODE=='LIVE' else '🟡 SIGNAL ONLY'} | {balance_line}\n"
            f"Fees: `{maker_fee}` | Trailing SL: `✅`\n"
            f"Balance cache: `{BALANCE_TTL}s TTL` | Min: `{MIN_OPERABLE_BALANCE} USDT`\n"
            f"Score: `{cfg.SCORE_THR_LONG*100:.0f}%` | "
            f"Decay: `{cfg.DECAY_THR*100:.0f}%` | "
            f"Leverage: `{cfg.LEVERAGE}×`"
        )
    except Exception as e:
        log.warning(f"Telegram startup: {e}")

    t_scanner = asyncio.create_task(
        scanner_loop(exchange, tg, perf, engine, risk, session, bal_cache)
    )
    t_status = asyncio.create_task(
        status_loop(tg, bal_cache, perf)
    )

    try:
        await asyncio.gather(t_scanner, t_status, return_exceptions=True)
    except Exception as e:
        log.error(f"gather error: {e}\n{traceback.format_exc()}")
    finally:
        await _graceful_shutdown(
            running_tasks=[t_scanner, t_status],
            clients=[exchange, tg],
        )
        try:
            await tg.send_message("🔴 *Bot detenido*")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
