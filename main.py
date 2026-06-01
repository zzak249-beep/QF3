"""
QF×JP Bot v5.6 — 4 mejoras de ventaja estadística en 3min
Sobre v5.5:
  [OFI-ML]  OFI multinivel: cfg.OFI_LEVELS subido a 10 (antes 5)
  [H-FILT]  Filtro horario: altcoins evitan 18-23 UTC (baja liquidez)
  [ALT-PRI] Prioridad altcoins: pares majors requieren +2 conv y umbral
            de score más alto (alpha decayendo en BTC/ETH según research)
  [CAT-LOG] Log de catalizadores: cada señal imprime qué condiciones
            la dispararon (TL / SQ / FVG / OB / DP / CVD / SWP / CHoCH)
"""
import asyncio, logging, signal as signal_mod, sys, traceback, os
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

# ── Estado global ─────────────────────────────────────────────
active_positions : dict             = {}
prev_oi          : dict[str, float] = {}
_stop_event      : asyncio.Event    = None

# ── [ALT-PRI] Pares con alpha decayendo más rápido ───────────
# Research 2026: VPIN en BTC pasó de +82bps/trade (2024) a +12bps (2026)
# Altcoins pequeñas retienen mayor edge porque hay menos competencia algo
MAJOR_PAIRS = {
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT",
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
}
MAJOR_CONV_PENALTY  = 2    # +2 conv mínimo requerido en majors
MAJOR_SCORE_PENALTY = 5    # +5 en umbral de score para majors

# ── [H-FILT] Ventana de baja liquidez UTC (altcoins) ─────────
# Amberdata 2025: liquidez cae 42% entre 11 UTC y 21 UTC.
# 18-23 UTC = transición NY-cierre / antes de Asia → más ruido, menos depth
LOW_LIQ_HOUR_START = 18
LOW_LIQ_HOUR_END   = 23


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
def _is_major(symbol: str) -> bool:
    return symbol in MAJOR_PAIRS


def _log_catalysts(symbol: str, sig: dict) -> str:
    """[CAT-LOG] Construye string de catalizadores activos para logging."""
    d   = sig.get("direction", "")
    lng = d == "LONG"
    cats = []

    if sig.get("tl_break_long"  if lng else "tl_break_short"):  cats.append("TL")
    if sig.get("sq_bull"        if lng else "sq_bear"):          cats.append("SQ")
    if sig.get("in_bull_fvg"    if lng else "in_bear_fvg"):      cats.append("FVG")
    if sig.get("in_bull_ob"     if lng else "in_bear_ob"):       cats.append("OB")
    if sig.get("dp_buy"         if lng else "dp_sell"):          cats.append("DP")
    if lng  and sig.get("cvd_rising"):                           cats.append("CVD↑")
    if not lng and not sig.get("cvd_rising", True):              cats.append("CVD↓")
    if sig.get("liq_bull_sweep" if lng else "liq_bear_sweep"):   cats.append("SWP")
    if sig.get("choch_bull"     if lng else "choch_bear"):       cats.append("CHoCH")
    if sig.get("bos_bull"       if lng else "bos_bear"):         cats.append("BoS")
    if sig.get("cvd_bull_div"   if lng else "cvd_bear_div"):     cats.append("CVD-div")
    if sig.get("rsi_bull_div_hid" if lng else "rsi_bear_div_hid"): cats.append("RSI-hid")

    cat_str = " + ".join(cats) if cats else "base-score"
    log.info(f"[{symbol}] catalizadores: {cat_str} | "
             f"tier={sig.get('tier')} conv={sig.get('conviction')}/10 "
             f"score={sig.get('norm_score', 0):.2f} major={'SÍ' if _is_major(symbol) else 'NO'}")
    return cat_str


def _graceful_shutdown_sync():
    """Señaliza parada limpia."""
    log.info("Señal de parada recibida")
    if _stop_event:
        _stop_event.set()


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
async def run_symbol(symbol, exchange, tg, risk, session, engine, perf):
    log.info(f"[{symbol}] task arrancada {'(MAJOR)' if _is_major(symbol) else '(ALTCOIN)'}")
    consecutive_errors = 0
    is_major = _is_major(symbol)

    while not _stop_event.is_set():
        try:
            # ── Sesión ──────────────────────────────────────
            if not session.is_tradeable():
                await asyncio.sleep(30); continue

            # ── [H-FILT] Filtro horario para altcoins ───────
            # 18-23 UTC: liquidez cae 42% en altcoins → más ruido, menos edge
            utc_hour = datetime.now(timezone.utc).hour
            low_liq  = LOW_LIQ_HOUR_START <= utc_hour < LOW_LIQ_HOUR_END
            if low_liq and not is_major:
                await asyncio.sleep(cfg.LOOP_INTERVAL * 4)
                continue

            # ── PF mínimo ───────────────────────────────────
            if not perf.is_tradeable(symbol):
                await asyncio.sleep(60); continue

            # ── Balance ─────────────────────────────────────
            bal = await exchange.get_balance()
            if bal <= 0:
                await asyncio.sleep(30); continue

            # ── DD diario ───────────────────────────────────
            risk.update_start_balance(bal)
            if not risk.max_daily_loss_ok(bal, cfg.MAX_DAILY_DD_PCT):
                await asyncio.sleep(3600); continue

            # ── Límite posiciones ───────────────────────────
            if symbol not in active_positions and len(active_positions) >= cfg.MAX_OPEN_POSITIONS:
                await asyncio.sleep(cfg.LOOP_INTERVAL); continue

            # ── Velas multi-TF ──────────────────────────────
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

            # ── Market context (OFI multinivel) ─────────────
            try:
                mctx = await exchange.get_market_context(symbol, cfg.OFI_LEVELS)
            except Exception:
                mctx = {"ofi": 0.0, "funding_rate": 0.0, "open_interest": 0.0}

            mctx["prev_open_interest"] = prev_oi.get(symbol, mctx["open_interest"])
            prev_oi[symbol] = mctx["open_interest"]

            # ── Señal ───────────────────────────────────────
            sig = engine.compute(ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m, mctx)

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

            # ── Nueva entrada ────────────────────────────────
            if symbol not in active_positions and sig["direction"]:
                tier = sig["tier"]
                conv = sig["conviction"]

                # [ALT-PRI] Majors necesitan mayor convicción
                # Research: alpha de microestructura en BTC cayó de +82 a +12bps en 2 años
                min_c_base = (cfg.MIN_CONV_SUP  if tier == "SUP"  else
                              cfg.MIN_CONV_FUEL if tier == "FUEL" else cfg.MIN_CONV_STD)
                min_c = min_c_base + (MAJOR_CONV_PENALTY if is_major else 0)

                if sig.get("vol_regime") == "LOW" or conv < min_c:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                # [ALT-PRI] Score umbral más alto para majors
                score_100 = round(sig.get("norm_score", 0) * 100)
                score_thr = cfg.SCORE_THR_LONG + (MAJOR_SCORE_PENALTY if is_major else 0)
                if score_100 < score_thr:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                sl   = sig["sl"]
                tp   = sig.get("tp")
                atr  = sig.get("atr_last", None)
                size = risk.position_size(
                    bal, price, sl,
                    cfg.RISK_PER_TRADE_PCT, cfg.LEVERAGE,
                    atr=atr,
                )
                if size <= 0:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                # [CAT-LOG] Log de catalizadores antes de ejecutar
                cat_str = _log_catalysts(symbol, sig)

                order_id = "SIGNAL_ONLY"
                if cfg.MODE == "LIVE":
                    order = await exchange.place_order(
                        symbol, sig["direction"], size, cfg.LEVERAGE, sl, tp,
                        use_maker=cfg.USE_MAKER_ORDERS,
                        maker_timeout=cfg.MAKER_TIMEOUT,
                        maker_offset_pct=cfg.MAKER_OFFSET_PCT,
                    )
                    if not order:
                        await asyncio.sleep(cfg.LOOP_INTERVAL); continue
                    order_id = order.get("orderId", "?")

                active_positions[symbol] = dict(
                    side=sig["direction"], entry=price, sl=sl, tp=tp,
                    size=size, conv=conv, tier=tier,
                    time=datetime.now(timezone.utc),
                    atr=sig.get("atr_last", 0),
                    trail_active=False, trail_sl=None,
                    catalysts=cat_str,
                )
                await tg.send_entry(symbol, sig, price, size, order_id, mctx)
                log.info(f"[{symbol}] ✅ {sig['direction']} {tier} "
                         f"conv={conv}/10 score={score_100} "
                         f"cats=[{cat_str}]")

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
# [ALT-PRI] Altcoins se procesan antes que majors en el mismo ciclo
# ─────────────────────────────────────────────────────────────
async def scanner_loop(exchange, tg, perf, engine, risk, session):
    scanner = MarketScanner(exchange)
    tasks   : dict[str, asyncio.Task] = {}

    while not _stop_event.is_set():
        try:
            symbols_raw = await scanner.get_tradeable_symbols()

            # [ALT-PRI] Altcoins primero: más edge estadístico en 2026
            symbols = sorted(symbols_raw, key=lambda s: (1 if _is_major(s) else 0, s))
            n_alt   = sum(1 for s in symbols if not _is_major(s))
            n_maj   = len(symbols) - n_alt
            log.info(f"Scanner: {len(symbols)} pares — {n_alt} altcoins + {n_maj} majors")

            gs = perf.global_stats()
            if gs and gs.get("total_trades", 0) > 0:
                await tg.send_message(
                    f"🔍 *Scanner — {len(symbols)} pares* ({n_alt} alt / {n_maj} maj)\n"
                    f"WR={gs['win_rate']:.0%} | PF={gs['profit_factor']:.2f} | "
                    f"avg={gs['avg_pnl']:.2f}%\n"
                    f"⛔ Suspendidos: {', '.join(gs['suspended']) or 'ninguno'}"
                )

            for sym in symbols:
                if sym not in tasks or tasks[sym].done():
                    tasks[sym] = asyncio.create_task(
                        run_symbol(sym, exchange, tg, risk, session, engine, perf)
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
async def status_loop(tg, exchange, perf):
    while not _stop_event.is_set():
        await asyncio.sleep(3600)
        try:
            bal = await exchange.get_balance(force=True)
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
    log.info("  QF×JP Bot v5.6  |  BingX Futures")
    log.info(f"  OFI_LEVELS={cfg.OFI_LEVELS} (multinivel ponderado)")
    log.info(f"  MAJOR_PAIRS={len(MAJOR_PAIRS)} pares con umbral +{MAJOR_SCORE_PENALTY}pts")
    log.info(f"  LOW_LIQ_WINDOW={LOW_LIQ_HOUR_START}-{LOW_LIQ_HOUR_END} UTC (altcoins)")
    log.info(f"  MODE={cfg.MODE} | MAX_POS={cfg.MAX_OPEN_POSITIONS}")
    log.info("═══════════════════════════════════════")

    tg       = TelegramClient(cfg.TG_TOKEN, cfg.TG_CHAT_ID)
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    risk     = RiskManager()
    session  = SessionFilter()
    engine   = QFJPEngine()
    perf     = PerformanceTracker(cfg.PF_WINDOW, cfg.MIN_PROFIT_FACTOR)

    bal = 0.0
    for attempt in range(5):
        try:
            bal = await exchange.get_balance(force=True)
            if bal > 0:
                break
            await asyncio.sleep(5)
        except Exception as e:
            log.warning(f"Balance intento {attempt+1}/5: {e}")
            await asyncio.sleep(10)

    log.info(f"Balance inicial: {bal:.2f} USDT")
    risk.update_start_balance(bal)

    loop = asyncio.get_event_loop()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        loop.add_signal_handler(sig, _graceful_shutdown_sync)

    try:
        maker_fee = "0.04% (maker)" if cfg.USE_MAKER_ORDERS else "0.15% (market)"
        await tg.send_message(
            f"🟢 *QF×JP Bot v5.6 iniciado*\n"
            f"{'🔴 LIVE' if cfg.MODE=='LIVE' else '🟡 SIGNAL ONLY'} | "
            f"Balance: `{bal:.2f} USDT`\n"
            f"Fees: `{maker_fee}` | OFI: `10 niveles` | MultiTF: `✅`\n"
            f"Altcoins: umbral normal | Majors: `+{MAJOR_SCORE_PENALTY}pts score`\n"
            f"Filtro 18-23 UTC: `✅ altcoins` | Leverage: `{cfg.LEVERAGE}×`"
        )
    except Exception as e:
        log.warning(f"Telegram startup: {e}")

    t_scanner = asyncio.create_task(
        scanner_loop(exchange, tg, perf, engine, risk, session)
    )
    t_status = asyncio.create_task(
        status_loop(tg, exchange, perf)
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
