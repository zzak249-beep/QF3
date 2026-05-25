"""
QF×JP Bot v4.0 — Main loop
Mejoras: scanner automático de pares, tracker de rentabilidad,
filtro profit factor por símbolo, resumen analítico en Telegram.
"""
import asyncio, logging, signal, sys
from datetime import datetime, timezone

from config import cfg
from bot.engine import QFJPEngine
from bot.bingx_client import BingXClient
from bot.telegram_client import TelegramClient
from bot.risk_manager import RiskManager
from bot.session_filter import SessionFilter
from bot.scanner import MarketScanner
from bot.performance import PerformanceTracker, TradeRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("MAIN")

active_positions: dict = {}  # symbol → {side, entry, sl, tp, size, conv, tier, time}


async def run_symbol(symbol, exchange, tg, risk, session, engine, perf, start_bal):
    log.info(f"[{symbol}] Loop iniciado")
    daily_bal = [start_bal]

    while True:
        try:
            # ── Sesión ─────────────────────────────────────
            if not session.is_tradeable():
                await asyncio.sleep(30); continue

            # ── Rentabilidad mínima ─────────────────────────
            if not perf.is_tradeable(symbol):
                await asyncio.sleep(60); continue

            # ── Drawdown diario ─────────────────────────────
            bal = await exchange.get_balance()
            if not risk.max_daily_loss_ok(daily_bal[0], bal, cfg.MAX_DAILY_DD_PCT):
                await tg.send_message(f"⛔ *DD diario alcanzado en {symbol}* — pausado 1h")
                await asyncio.sleep(3600); continue

            # ── Límite posiciones abiertas ──────────────────
            if symbol not in active_positions and len(active_positions) >= cfg.MAX_OPEN_POSITIONS:
                await asyncio.sleep(cfg.LOOP_INTERVAL); continue

            # ── Velas ───────────────────────────────────────
            ohlcv_3m  = await exchange.get_klines(symbol, "3m",  250)
            ohlcv_15m = await exchange.get_klines(symbol, "15m", 100)
            if len(ohlcv_3m) < 100: await asyncio.sleep(5); continue

            # ── Señal ───────────────────────────────────────
            sig = engine.compute(ohlcv_3m, ohlcv_15m)

            # ── Gestión posición activa ─────────────────────
            pos = active_positions.get(symbol)
            if pos:
                ticker = await exchange.get_ticker(symbol)
                price  = ticker["last"]
                sl_hit = ((pos["side"]=="LONG"  and price <= pos["sl"]) or
                          (pos["side"]=="SHORT" and price >= pos["sl"]))
                tp_hit = (pos.get("tp") and
                          ((pos["side"]=="LONG"  and price >= pos["tp"]) or
                           (pos["side"]=="SHORT" and price <= pos["tp"])))

                close_reason = None
                if sl_hit: close_reason = "SL alcanzado"
                elif tp_hit: close_reason = "TP alcanzado"
                elif (sig["direction"] and sig["direction"] != pos["side"]
                      and sig["conviction"] >= 7):
                    close_reason = "Señal contraria"

                if close_reason:
                    if cfg.MODE == "LIVE":
                        await exchange.close_position(symbol, pos["side"])
                    pnl = ((price-pos["entry"])/pos["entry"]*100
                           if pos["side"]=="LONG"
                           else (pos["entry"]-price)/pos["entry"]*100)
                    await tg.send_close(symbol, pos["side"], pos["entry"],
                                        price, pnl, close_reason)
                    # Registrar en tracker
                    perf.record(TradeRecord(
                        symbol=symbol, side=pos["side"],
                        entry=pos["entry"], exit=price,
                        pnl_pct=pnl, conviction=pos["conv"], tier=pos["tier"]
                    ))
                    del active_positions[symbol]

            # ── Nueva entrada ───────────────────────────────
            if symbol not in active_positions and sig["direction"]:
                tier = sig["tier"]; conv = sig["conviction"]
                min_c = (cfg.MIN_CONV_SUP  if tier=="SUP"  else
                         cfg.MIN_CONV_FUEL if tier=="FUEL" else cfg.MIN_CONV_STD)
                # Vol ok filter
                if sig.get("vol_regime") == "LOW":
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue
                if conv < min_c:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                ticker = await exchange.get_ticker(symbol)
                price  = ticker["last"]
                sl     = sig["sl"]; tp = sig.get("tp")
                size   = risk.position_size(bal, price, sl,
                                            cfg.RISK_PER_TRADE_PCT, cfg.LEVERAGE)
                if size <= 0:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                order_id = "SIGNAL_ONLY"
                if cfg.MODE == "LIVE":
                    order = await exchange.place_order(symbol, sig["direction"],
                                                       size, cfg.LEVERAGE, sl, tp)
                    if not order:
                        await asyncio.sleep(cfg.LOOP_INTERVAL); continue
                    order_id = order.get("orderId", "?")

                active_positions[symbol] = dict(
                    side=sig["direction"], entry=price, sl=sl, tp=tp,
                    size=size, conv=conv, tier=tier, time=datetime.utcnow()
                )
                await tg.send_entry(symbol, sig, price, size, order_id)
                log.info(f"[{symbol}] {sig['direction']} {tier} conv={conv}/10 "
                         f"score={sig['norm_score']:.2f} decay={sig['decay_ratio']:.2f}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[{symbol}] {e}", exc_info=True)
            await tg.send_error(f"[{symbol}] {e}")

        await asyncio.sleep(cfg.LOOP_INTERVAL)


async def scanner_loop(exchange, tg, perf, engine, risk, session):
    """Re-escanea pares cada hora y lanza/cancela loops dinámicamente."""
    scanner = MarketScanner(exchange)
    tasks: dict[str, asyncio.Task] = {}

    while True:
        symbols = await scanner.get_tradeable_symbols()
        # Stats globales
        gs = perf.global_stats()
        if gs:
            await tg.send_message(
                f"🔍 *Scanner — {len(symbols)} pares activos*\n"
                f"📊 Stats globales: trades={gs['total_trades']} | "
                f"WR={gs['win_rate']:.0%} | PF={gs['profit_factor']:.2f} | "
                f"avg PnL={gs['avg_pnl']:.2f}%\n"
                f"⛔ Suspendidos: {', '.join(gs['suspended']) or 'ninguno'}"
            )

        bal = await exchange.get_balance()

        # Arrancar tasks nuevas
        for sym in symbols:
            if sym not in tasks or tasks[sym].done():
                t = asyncio.create_task(
                    run_symbol(sym, exchange, tg, risk, session, engine, perf, bal)
                )
                tasks[sym] = t
                log.info(f"Task iniciada: {sym}")

        # Cancelar tasks de pares eliminados del scanner
        for sym in list(tasks.keys()):
            if sym not in symbols and not tasks[sym].done():
                tasks[sym].cancel()
                del tasks[sym]
                log.info(f"Task cancelada: {sym}")

        await asyncio.sleep(cfg.SCANNER_INTERVAL)


async def status_loop(tg, exchange, perf):
    while True:
        await asyncio.sleep(3600)
        try:
            bal = await exchange.get_balance()
            gs  = perf.global_stats()
            await tg.send_status(bal, active_positions, gs)
        except Exception as e:
            log.error(f"status_loop: {e}")


async def main():
    log.info("═══════════════════════════════════")
    log.info("  QF×JP Bot v4.0  |  BingX Futures")
    log.info(f"  SCORE_THR={cfg.SCORE_THR_LONG} | DECAY_THR={cfg.DECAY_THR}")
    log.info(f"  MODE={cfg.MODE} | MAX_POS={cfg.MAX_OPEN_POSITIONS}")
    log.info("═══════════════════════════════════")

    tg       = TelegramClient(cfg.TG_TOKEN, cfg.TG_CHAT_ID)
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    risk     = RiskManager()
    session  = SessionFilter()
    engine   = QFJPEngine()
    perf     = PerformanceTracker(cfg.PF_WINDOW, cfg.MIN_PROFIT_FACTOR)

    bal = await exchange.get_balance()
    mode_str = "🔴 LIVE" if cfg.MODE=="LIVE" else "🟡 SIGNAL ONLY"
    await tg.send_message(
        f"🟢 *QF×JP Bot v4 iniciado*\n"
        f"Modo: {mode_str}\n"
        f"Balance: `{bal:.2f} USDT`\n"
        f"Score umbral: `{cfg.SCORE_THR_LONG*100:.0f}%`\n"
        f"Decay mínimo: `{cfg.DECAY_THR*100:.0f}%` del pico IC\n"
        f"Leverage: `{cfg.LEVERAGE}×` | Riesgo/trade: `{cfg.RISK_PER_TRADE_PCT}%`\n"
        f"Sesiones: `{', '.join(cfg.ALLOWED_SESSIONS)}`"
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in asyncio.all_tasks()])

    await asyncio.gather(
        scanner_loop(exchange, tg, perf, engine, risk, session),
        status_loop(tg, exchange, perf),
        return_exceptions=True
    )
    await tg.send_message("🔴 *Bot detenido*")


if __name__ == "__main__":
    asyncio.run(main())
