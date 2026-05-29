"""
Risk Manager v5.5 — fix drawdown con balance 0
"""
import logging, math
log = logging.getLogger("RiskMgr")

class RiskManager:

    def position_size(self, balance, entry, stop_loss, risk_pct, leverage):
        if entry <= 0 or stop_loss <= 0 or balance <= 0:
            return 0.0
        distance = abs(entry - stop_loss)
        if distance < entry * 0.001:
            log.warning("SL demasiado cercano"); return 0.0
        risk_usdt    = balance * (risk_pct / 100)
        raw_size     = risk_usdt / distance
        notional     = raw_size * entry
        max_notional = balance * leverage * 0.80
        if notional > max_notional:
            raw_size = max_notional / entry
        return math.floor(raw_size * 1000) / 1000

    def max_daily_loss_ok(self, start_balance, current_balance, max_dd_pct):
        # FIX: si balance es 0 o inválido, NO bloquear
        if start_balance <= 0 or current_balance <= 0:
            return True
        # FIX: si current > start (ganancia), OK
        if current_balance >= start_balance:
            return True
        dd = (start_balance - current_balance) / start_balance * 100
        if dd > max_dd_pct:
            log.warning(f"⛔ DD diario {dd:.2f}% > límite {max_dd_pct}%")
            return False
        return True
