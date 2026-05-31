"""
Telegram Client v5.1 — añadido close() para shutdown limpio sin ResourceWarning
"""
import aiohttp, logging
from datetime import datetime, timezone

log = logging.getLogger("Telegram")
API = "https://api.telegram.org/bot{token}/{method}"


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
        """Cierra la sesión aiohttp. Llamar antes de terminar el proceso."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            log.info("Session cerrada")

    async def send_message(self, text, parse_mode="Markdown"):
        url  = API.format(token=self.token, method="sendMessage")
        sess = await self._sess()
        try:
            async with sess.post(url, json={
                "chat_id"   : self.chat_id,
                "text"      : text,
                "parse_mode": parse_mode,
            }) as r:
                if r.status != 200:
                    log.error(f"TG {r.status}: {await r.text()}")
        except Exception as e:
            log.error(f"TG send: {e}")

    async def send_entry(self, symbol, sig, price, size, order_id, mctx=None):
        d    = sig["direction"]; tier = sig["tier"]; conv = sig["conviction"]
        ns   = sig.get("norm_score",0); dr = sig.get("decay_ratio",0)
        sl   = sig["sl"]; tp = sig.get("tp")
        t_e  = {"SUP":"⭐","FUEL":"🔥","STD":"📍"}.get(tier,"")
        d_e  = "🟢" if d=="LONG" else "🔴"
        bars = "█"*conv + "░"*(10-conv)
        rr   = abs((tp-price)/(price-sl)) if tp and sl and (price-sl)!=0 else 0
        vol_e= {"LOW":"⚪","NORMAL":"🟢","HIGH":"🔴"}.get(sig.get("vol_regime",""),"")

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

        msg = (
            f"{d_e} *{t_e} {d} [{tier}]* — `{symbol}`\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"💰 Entrada  : `{price:.4f}`\n"
            f"🛡 Stop-Loss: `{sl:.4f}`\n"
            f"🎯 Take-Prof: `{f'{tp:.4f}' if tp else '—'}`\n"
            f"📐 R/R      : `{rr:.2f}×`\n"
            f"📦 Size     : `{size:.4f}`\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🧠 Score  : `{round(ns*100)}/100` (umbral 63)\n"
            f"📉 Decay  : `{round(dr*100)}%` del pico IC\n"
            f"🏆 Conv   : `[{bars}] {conv}/10`\n"
            f"📊 Vol    : {vol_e} `{sig.get('vol_regime','?')}`\n"
            f"📈 Trend  : `{'SÍ' if sig.get('trending') else 'NO'}`\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🔬 *Microestructura*\n"
            f"  OFI  : {ofi_e} `{ofi:+.2f}`\n"
            f"  FR   : {fr_e} `{frp}`\n"
            f"  OI Δ : {oi_e}\n"
            f"  MultiTF: {multi_e}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"  HTF15m `{'✅' if sig.get('htf_bull' if d=='LONG' else 'htf_bear') else '❌'}`"
            f"  HTF1h `{'✅' if sig.get('htf_1h_bull' if d=='LONG' else 'htf_1h_bear') else '❌'}`"
            f"  VWAP `{'✅' if (sig.get('above_vwap') if d=='LONG' else not sig.get('above_vwap')) else '❌'}`\n"
            f"  CVD `{'✅' if (sig.get('cvd_rising') if d=='LONG' else not sig.get('cvd_rising')) else '❌'}`"
            f"  TL `{'✅' if sig.get('tl_break_long' if d=='LONG' else 'tl_break_short') else '—'}`"
            f"  FVG `{'✅' if sig.get('in_bull_fvg' if d=='LONG' else 'in_bear_fvg') else '—'}`\n"
            f"  OB `{'✅' if sig.get('in_bull_ob' if d=='LONG' else 'in_bear_ob') else '—'}`"
            f"  SQ `{'✅' if sig.get('sq_bull' if d=='LONG' else 'sq_bear') else '—'}`"
            f"  DP `{'✅' if sig.get('dp_buy' if d=='LONG' else 'dp_sell') else '—'}`\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🆔 `{order_id}`  ⏱ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )
        await self.send_message(msg)

    async def send_close(self, symbol, side, entry, exit_p, pnl_pct, reason,
                         trail_was_active=False):
        e  = "💹" if pnl_pct >= 0 else "💸"
        s  = "+" if pnl_pct >= 0 else ""
        tr = " 🎯 trailing" if trail_was_active else ""
        await self.send_message(
            f"{e} *CIERRE {side}* — `{symbol}`\n"
            f"Entrada: `{entry:.4f}` → Salida: `{exit_p:.4f}`\n"
            f"PnL: `{s}{pnl_pct:.2f}%` | {reason}{tr}\n"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )

    async def send_status(self, balance, positions, global_stats=None):
        pos_lines = ""
        for sym, p in positions.items():
            trail_str = " 🎯" if p.get("trail_active") else ""
            pos_lines += (f"  • `{sym}` {p['side']} entry=`{p['entry']:.4f}` "
                          f"sl=`{p['sl']:.4f}` conv=`{p['conv']}/10`{trail_str}\n")
        if not pos_lines:
            pos_lines = "  _Sin posiciones_\n"

        gs_block = ""
        if global_stats and global_stats.get("total_trades", 0) > 0:
            gs = global_stats
            gs_block = (
                f"━━━━━━━━━━━━━━━━━\n"
                f"📈 *Performance global*\n"
                f"Trades: `{gs['total_trades']}` | WR: `{gs['win_rate']:.0%}` | "
                f"PF: `{gs['profit_factor']:.2f}` | avg: `{gs['avg_pnl']:.2f}%`\n"
                f"⛔ Suspendidos: `{', '.join(gs['suspended']) or 'ninguno'}`\n"
            )
        await self.send_message(
            f"📊 *Reporte Horario QF×JP v5*\n"
            f"💵 Balance: `{balance:.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"*Posiciones:*\n{pos_lines}"
            f"{gs_block}"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

    async def send_error(self, error):
        await self.send_message(
            f"⚠️ *Error*\n```\n{str(error)[:300]}\n```\n"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )
