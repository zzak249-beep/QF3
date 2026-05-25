"""
Gestión de Riesgo — QF×JP Bot
Kelly fraccionado + límites duros de drawdown.
"""
import logging
import math

log = logging.getLogger("RiskMgr")


class RiskManager:

    def position_size(self, balance: float, entry: float,
                      stop_loss: float, risk_pct: float,
                      leverage: int) -> float:
        """
        Calcula tamaño de posición en contratos (moneda base).

        Fórmula: size = (balance × risk_pct/100) / |entry - sl|
        Limitado por: notional <= balance × leverage × MAX_NOTIONAL_PCT
        """
        if entry <= 0 or stop_loss <= 0:
            return 0.0

        distance = abs(entry - stop_loss)
        if distance < entry * 0.001:   # SL demasiado ajustado (<0.1%)
            log.warning("SL demasiado cercano — omitiendo trade")
            return 0.0

        risk_usdt   = balance * (risk_pct / 100)
        raw_size    = risk_usdt / distance            # contratos en moneda base
        notional    = raw_size * entry
        max_notional = balance * leverage * 0.80      # máx 80% del margen disponible

        if notional > max_notional:
            raw_size = max_notional / entry
            log.info(f"Tamaño recortado por notional máx: {raw_size:.4f}")

        # Redondear a 3 decimales (BingX acepta 3 dp para BTC, 1 para DOGE, etc.)
        size = math.floor(raw_size * 1000) / 1000
        return size

    def max_daily_loss_ok(self, start_balance: float,
                          current_balance: float,
                          max_dd_pct: float) -> bool:
        """Devuelve False si el drawdown diario supera el límite."""
        dd = (start_balance - current_balance) / start_balance * 100
        if dd > max_dd_pct:
            log.warning(f"⛔ Drawdown diario {dd:.2f}% > límite {max_dd_pct}%")
            return False
        return True
