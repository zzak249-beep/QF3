"""
QF×JP Bot v5.0 — Main loop
Mejoras vs v4:
  • Trailing SL dinámico con ATR (activa al 1× ATR de beneficio)
  • Fetch 1h y 1m klines para multi-TF alignment
  • Fetch market_context (OFI + FR + OI) por símbolo
  • OI delta: guarda prev_OI entre loops para calcular delta real
  • Pasa market_context al engine para L13/L14/L15
"""
import asyncio, logging, signal, sys
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

# symbol → {side, entry, sl, tp, size, conv, tier, time, atr, trail_active, trail_sl}
active_positions: dict = {}

# OI anterior por símbolo (para calcular delta entre loops)
prev_oi: dict[str, float] = {}


async def run_symbol(symbol, exchange, tg, risk, session, engine, perf, start_bal):
    log.info(f"[{symbol}] Loop iniciado")
    daily_bal = [start_bal]

    while True:
        try:
            # ── Sesión ──────────────────────────────────────
            if not session.is_tradeable():
                await asyncio.sleep(30); continue

            # ── Profit Factor mínimo ─────────────────────────
            if not perf.is_tradeable(symbol):
                await asyncio.sleep(60); continue

            # ── Drawdown diario ─────────────────────────────
            bal = await exchange.get_balance()
            if not risk.max_daily_loss_ok(daily_bal[0], bal, cfg.MAX_DAILY_DD_PCT):
                await tg.send_message(f"⛔ *DD diario alcanzado en {symbol}* — pausado 1h")
                await asyncio.sleep(3600); continue

            # ── Límite posiciones ───────────────────────────
            if symbol not in active_positions and len(active_positions) >= cfg.MAX_OPEN_POSITIONS:
                await asyncio.sleep(cfg.LOOP_INTERVAL); continue

            # ── Velas multi-TF ──────────────────────────────
            ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m = await asyncio.gather(
                exchange.get_klines(symbol, "3m",  250),
                exchange.get_klines(symbol, "15m", 100),
                exchange.get_klines(symbol, "1h",  60),
                exchange.get_klines(symbol, "1m",  60),
                return_exceptions=True
            )
            # Filtrar excepciones
            if isinstance(ohlcv_3m, Exception) or len(ohlcv_3m) < 100:
                await asyncio.sleep(10); continue
            ohlcv_15m = ohlcv_15m if not isinstance(ohlcv_15m, Exception) else []
            ohlcv_1h  = ohlcv_1h  if not isinstance(ohlcv_1h,  Exception) else []
            ohlcv_1m  = ohlcv_1m  if not isinstance(ohlcv_1m,  Exception) else []

            # ── Market context: OFI + Funding Rate + OI ─────
            mctx = await exchange.get_market_context(symbol, cfg.OFI_LEVELS)
            # Añadir OI anterior para calcular delta
            mctx["prev_open_interest"] = prev_oi.get(symbol, mctx["open_interest"])
            prev_oi[symbol] = mctx["open_interest"]

            # ── Señal ───────────────────────────────────────
            sig = engine.compute(ohlcv_3m, ohlcv_15m, ohlcv_1h, ohlcv_1m, mctx)

            # ── Ticker actual ───────────────────────────────
            ticker = await exchange.get_ticker(symbol)
            price  = ticker["last"]

            # ── Gestión posición activa ─────────────────────
            pos = active_positions.get(symbol)
            if pos:
                atr_pos = pos.get("atr", 0)

                # ── Trailing SL ──────────────────────────────
                if atr_pos > 0:
                    if pos["side"] == "LONG":
                        profit_dist = price - pos["entry"]
                        activate    = atr_pos * cfg.TRAIL_ACTIVATE_ATR

                        if not pos.get("trail_active") and profit_dist >= activate:
                            pos["trail_active"] = True
                            pos["trail_sl"]     = price - atr_pos * cfg.TRAIL_ATR_MULT
                            log.info(f"[{symbol}] Trailing SL activado @ {pos['trail_sl']:.4f}")

                        if pos.get("trail_active"):
                            new_trail = price - atr_pos * cfg.TRAIL_ATR_MULT
                            if new_trail > pos.get("trail_sl", pos["sl"]):
                                pos["trail_sl"] = new_trail
                            # Usar trail_sl como SL efectivo
                            pos["sl"] = max(pos["sl"], pos["trail_sl"])

                    elif pos["side"] == "SHORT":
                        profit_dist = pos["entry"] - price
                        activate    = atr_pos * cfg.TRAIL_ACTIVATE_ATR

                        if not pos.get("trail_active") and profit_dist >= activate:
                            pos["trail_active"] = True
                            pos["trail_sl"]     = price + atr_pos * cfg.TRAIL_ATR_MULT

                        if pos.get("trail_active"):
                            new_trail = price + atr_pos * cfg.TRAIL_ATR_MULT
                            if new_trail < pos.get("trail_sl", pos["sl"]):
                                pos["trail_sl"] = new_trail
                            pos["sl"] = min(pos["sl"], pos["trail_sl"])

                # ── Checkear SL/TP/Reversal ──────────────────
                sl_hit = ((pos["side"]=="LONG"  and price <= pos["sl"]) or
                          (pos["side"]=="SHORT" and price >= pos["sl"]))
                tp_hit = (pos.get("tp") and
                          ((pos["side"]=="LONG"  and price >= pos["tp"]) or
                           (pos["side"]=="SHORT" and price <= pos["tp"])))

                close_reason = None
                if sl_hit: close_reason = "SL alcanzado" + (" (trailing)" if pos.get("trail_active") else "")
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
                                        price, pnl, close_reason,
                                        trail_was_active=pos.get("trail_active", False))
                    perf.record(TradeRecord(
                        symbol=symbol, side=pos["side"],
                        entry=pos["entry"], exit=price,
                        pnl_pct=pnl, conviction=pos["conv"], tier=pos["tier"]
                    ))
                    del active_positions[symbol]

            # ── Nueva entrada ───────────────────────────────
            if symbol not in active_positions and sig["direction"]:
                tier  = sig["tier"]; conv = sig["conviction"]
                min_c = (cfg.MIN_CONV_SUP  if tier=="SUP" else
                         cfg.MIN_CONV_FUEL if tier=="FUEL" else cfg.MIN_CONV_STD)

                if sig.get("vol_regime") == "LOW":
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue
                if conv < min_c:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

                sl   = sig["sl"]; tp = sig.get("tp")
                size = risk.position_size(bal, price, sl,
                                          cfg.RISK_PER_TRADE_PCT, cfg.LEVERAGE)
                if size <= 0:
                    await asyncio.sleep(cfg.LOOP_INTERVAL); continue

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
                    size=size, conv=conv, tier=tier, time=datetime.utcnow(),
                    atr=sig.get("atr_last", 0),
                    trail_active=False, trail_sl=None,
                )
                await tg.send_entry(symbol, sig, price, size, order_id, mctx)
                log.info(f"[{symbol}] {sig['direction']} {tier} conv={conv}/10 "
                         f"score={sig['norm_score']:.2f} decay={sig['decay_ratio']:.2f} "
                         f"OFI={sig['ofi']:.2f} FR={sig['funding_rate']:.4f} "
                         f"OI_delta={sig['oi_delta']:.3%}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[{symbol}] {e}", exc_info=True)
            await tg.send_error(f"[{symbol}] {e}")

        await asyncio.sleep(cfg.LOOP_INTERVAL)


async def scanner_loop(exchange, tg, perf, engine, risk, session):
    scanner = MarketScanner(exchange)
    tasks: dict[str, asyncio.Task] = {}

    while True:
        symbols = await scanner.get_tradeable_symbols()
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
        for sym in symbols:
            if sym not in tasks or tasks[sym].done():
                t = asyncio.create_task(
                    run_symbol(sym, exchange, tg, risk, session, engine, perf, bal)
                )
                tasks[sym] = t
                log.info(f"Task iniciada: {sym}")

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
    log.info("═══════════════════════════════════════")
    log.info("  QF×JP Bot v5.0  |  BingX Futures")
    log.info(f"  SCORE_THR={cfg.SCORE_THR_LONG} | DECAY_THR={cfg.DECAY_THR}")
    log.info(f"  MAKER_ORDERS={'ON' if cfg.USE_MAKER_ORDERS else 'OFF'}")
    log.info(f"  TRAILING_SL: activa a {cfg.TRAIL_ACTIVATE_ATR}×ATR, trail {cfg.TRAIL_ATR_MULT}×ATR")
    log.info(f"  MODE={cfg.MODE} | MAX_POS={cfg.MAX_OPEN_POSITIONS}")
    log.info("═══════════════════════════════════════")

    tg       = TelegramClient(cfg.TG_TOKEN, cfg.TG_CHAT_ID)
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    risk     = RiskManager()
    session  = SessionFilter()
    engine   = QFJPEngine()
    perf     = PerformanceTracker(cfg.PF_WINDOW, cfg.MIN_PROFIT_FACTOR)

    bal = await exchange.get_balance()
    maker_fee = "0.04%" if cfg.USE_MAKER_ORDERS else "0.15%"
    await tg.send_message(
        f"🟢 *QF×JP Bot v5 iniciado*\n"
        f"Modo: {'🔴 LIVE' if cfg.MODE=='LIVE' else '🟡 SIGNAL ONLY'}\n"
        f"Balance: `{bal:.2f} USDT`\n"
        f"Score umbral: `{cfg.SCORE_THR_LONG*100:.0f}%` | "
        f"Decay: `{cfg.DECAY_THR*100:.0f}%`\n"
        f"Fees: `{maker_fee}` ({'Maker limit' if cfg.USE_MAKER_ORDERS else 'Market'})\n"
        f"Trailing SL: `activa @{cfg.TRAIL_ACTIVATE_ATR}×ATR`\n"
        f"Multi-TF: `1m+3m+15m+1h`\n"
        f"OFI/FR/OI: `✅ activos`\n"
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
