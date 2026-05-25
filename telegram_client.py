"""
Telegram Client v5.1 — fixes:
  • Añadido close() para cerrar sesión aiohttp limpiamente
  • escape_md() para caracteres problemáticos en Markdown
  • parse_mode usa 'HTML' en lugar de 'Markdown' para evitar errores de formato
"""
import aiohttp, logging
from datetime import datetime

log = logging.getLogger("Telegram")
API = "https://api.telegram.org/bot{token}/{method}"


def _esc(text: str) -> str:
    """Escapa caracteres especiales para parse_mode HTML."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramClient:
    def __init__(self, token, chat_id):
        self.token    = token
        self.chat_id  = chat_id
        self._session = None

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_message(self, text, parse_mode="HTML"):
        """Envía mensaje. Si falla con HTML, reintenta sin parse_mode."""
        url  = API.format(token=self.token, method="sendMessage")
        sess = await self._sess()
        for pm in (parse_mode, None):
            try:
                payload = {
                    "chat_id"   : self.chat_id,
                    "text"      : text,
                }
                if pm:
                    payload["parse_mode"] = pm
                async with sess.post(url, json=payload) as r:
                    if r.status == 200:
                        return
                    err = await r.text()
                    log.error(f"TG {r.status} (parse_mode={pm}): {err[:200]}")
                    # Si es error de formato, reintenta sin parse_mode
                    if r.status == 400 and pm:
                        continue
                    return
            except Exception as e:
                log.error(f"TG send_message: {e}")
                return

    async def send_entry(self, symbol, sig, price, size, order_id, mctx=None):
        d    = sig.get("direction", "?")
        tier = sig.get("tier", "STD")
        conv = sig.get("conviction", 0)
        ns   = sig.get("norm_score", 0)
        dr   = sig.get("decay_ratio", 0)
        sl   = sig.get("sl", 0)
        tp   = sig.get("tp")

        t_e  = {"SUP": "⭐", "FUEL": "🔥", "STD": "📍"}.get(tier, "")
        d_e  = "🟢" if d == "LONG" else "🔴"
        bars = "█" * conv + "░" * (10 - conv)
        rr   = (abs((tp - price) / (price - sl))
                if tp and sl and (price - sl) != 0 else 0)
        vol_e = {"LOW": "⚪", "NORMAL": "🟢", "HIGH": "🔴"}.get(
            sig.get("vol_regime", ""), ""
        )

        ofi  = sig.get("ofi", 0.0)
        if ofi > 0.5:    ofi_e = "🟢🟢"
        elif ofi > 0.3:  ofi_e = "🟢"
        elif ofi < -0.5: ofi_e = "🔴🔴"
        elif ofi < -0.3: ofi_e = "🔴"
        else:            ofi_e = "⚪"

        fr  = sig.get("funding_rate", 0.0)
        frp = f"{fr*100:.4f}%"
        if sig.get("fr_extreme"): fr_e = "⚠️ EXTREMO"
        elif sig.get("fr_bull"):  fr_e = "🟢"
        elif sig.get("fr_bear"):  fr_e = "🔴"
        else:                     fr_e = "⚪"

        oid = sig.get("oi_delta", 0.0)
        if sig.get("oi_rising"):    oi_e = f"📈 +{oid:.2%}"
        elif sig.get("oi_falling"): oi_e = f"📉 {oid:.2%}"
        else:                       oi_e = f"➡️ {oid:.2%}"

        multi_e = "✅" if sig.get("multi_tf_aligned") else "—"

        htf15 = "✅" if sig.get("htf_bull" if d == "LONG" else "htf_bear") else "❌"
        htf1h = "✅" if sig.get("htf_1h_bull" if d == "LONG" else "htf_1h_bear") else "❌"
        vwap  = "✅" if (sig.get("above_vwap") if d == "LONG" else not sig.get("above_vwap")) else "❌"
        cvd_  = "✅" if (sig.get("cvd_rising") if d == "LONG" else not sig.get("cvd_rising")) else "❌"
        tl_   = "✅" if sig.get("tl_break_long" if d == "LONG" else "tl_break_short") else "—"
        fvg_  = "✅" if sig.get("in_bull_fvg" if d == "LONG" else "in_bear_fvg") else "—"
        ob_   = "✅" if sig.get("in_bull_ob" if d == "LONG" else "in_bear_ob") else "—"
        sq_   = "✅" if sig.get("sq_bull" if d == "LONG" else "sq_bear") else "—"
        dp_   = "✅" if sig.get("dp_buy" if d == "LONG" else "dp_sell") else "—"

        tp_str = f"{tp:.4f}" if tp else "—"

        msg = (
            f"{d_e} <b>{t_e} {d} [{_esc(tier)}]</b> — <code>{_esc(symbol)}</code>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"💰 Entrada  : <code>{price:.4f}</code>\n"
            f"🛡 Stop-Loss: <code>{sl:.4f}</code>\n"
            f"🎯 Take-Prof: <code>{tp_str}</code>\n"
            f"📐 R/R      : <code>{rr:.2f}x</code>\n"
            f"📦 Size     : <code>{size:.4f}</code>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🧠 Score  : <code>{round(ns*100)}/100</code> (umbral 63)\n"
            f"📉 Decay  : <code>{round(dr*100)}%</code> del pico IC\n"
            f"🏆 Conv   : <code>[{bars}] {conv}/10</code>\n"
            f"📊 Vol    : {vol_e} <code>{sig.get('vol_regime','?')}</code>\n"
            f"📈 Trend  : <code>{'SÍ' if sig.get('trending') else 'NO'}</code>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🔬 <b>Microestructura</b>\n"
            f"  OFI  : {ofi_e} <code>{ofi:+.2f}</code>\n"
            f"  FR   : {fr_e} <code>{frp}</code>\n"
            f"  OI Δ : {oi_e}\n"
            f"  MultiTF: {multi_e}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"  HTF15m {htf15}  HTF1h {htf1h}  VWAP {vwap}\n"
            f"  CVD {cvd_}  TL {tl_}  FVG {fvg_}\n"
            f"  OB {ob_}  SQ {sq_}  DP {dp_}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🆔 <code>{_esc(str(order_id))}</code>  "
            f"⏱ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
        )
        await self.send_message(msg, parse_mode="HTML")

    async def send_close(self, symbol, side, entry, exit_p, pnl_pct, reason,
                         trail_was_active=False):
        e  = "💹" if pnl_pct >= 0 else "💸"
        s  = "+" if pnl_pct >= 0 else ""
        tr = " 🎯 trailing" if trail_was_active else ""
        await self.send_message(
            f"{e} <b>CIERRE {_esc(side)}</b> — <code>{_esc(symbol)}</code>\n"
            f"Entrada: <code>{entry:.4f}</code> → Salida: <code>{exit_p:.4f}</code>\n"
            f"PnL: <code>{s}{pnl_pct:.2f}%</code> | {_esc(reason)}{tr}\n"
            f"⏱ {datetime.utcnow().strftime('%H:%M:%S')} UTC",
            parse_mode="HTML"
        )

    async def send_status(self, balance, positions, global_stats=None):
        pos_lines = ""
        for sym, p in positions.items():
            trail_str = " 🎯" if p.get("trail_active") else ""
            pos_lines += (
                f"  • <code>{_esc(sym)}</code> {p['side']} "
                f"entry=<code>{p['entry']:.4f}</code> "
                f"sl=<code>{p['sl']:.4f}</code> "
                f"conv=<code>{p['conv']}/10</code>{trail_str}\n"
            )
        if not pos_lines:
            pos_lines = "  <i>Sin posiciones</i>\n"

        gs_block = ""
        if global_stats and global_stats.get("total_trades", 0) > 0:
            gs = global_stats
            gs_block = (
                f"━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>Performance global</b>\n"
                f"Trades: <code>{gs['total_trades']}</code> | "
                f"WR: <code>{gs['win_rate']:.0%}</code> | "
                f"PF: <code>{gs['profit_factor']:.2f}</code> | "
                f"avg: <code>{gs['avg_pnl']:.2f}%</code>\n"
                f"⛔ Suspendidos: "
                f"<code>{', '.join(gs.get('suspended', [])) or 'ninguno'}</code>\n"
            )

        bal_str = f"{balance:.2f}" if balance > 0 else "N/A"
        await self.send_message(
            f"📊 <b>Reporte Horario QF×JP v5</b>\n"
            f"💵 Balance: <code>{bal_str} USDT</code>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"<b>Posiciones:</b>\n{pos_lines}"
            f"{gs_block}"
            f"⏱ {datetime.utcnow().strftime('%H:%M UTC')}",
            parse_mode="HTML"
        )

    async def send_error(self, error):
        await self.send_message(
            f"⚠️ <b>Error</b>\n<pre>{_esc(str(error)[:300])}</pre>\n"
            f"⏱ {datetime.utcnow().strftime('%H:%M:%S')} UTC",
            parse_mode="HTML"
        )
