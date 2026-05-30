"""
Risk Manager v5.6 — SL mínimo adaptativo + min_qty + debug logs
Fixes v5.5:
  - min_sl_pct bajado 0.001 → 0.0002 (0.1% era demasiado restrictivo en 3min)
  - Umbral ATR-based opcional: min(pct, 0.3×ATR)
  - Verificación min_qty: evita enviar size=0.0 al exchange sin avisar
  - Logs con valores reales en cada bloqueo para debuggear
"""
import logging
import math

log = logging.getLogger("RiskMgr")


class RiskManager:

    def position_size(
        self,
        balance,
        entry,
        stop_loss,
        risk_pct,
        leverage,
        atr        = None,   # ATR de la vela actual (opcional pero recomendado)
        min_sl_pct = 0.0002, # 0.02% — antes 0.001 (0.1%), demasiado restrictivo
        min_sl_atr = 0.3,    # SL mínimo = 0.3 × ATR cuando se pasa atr
        min_qty    = 0.001,  # cantidad mínima aceptable por el exchange
    ):
        # — Validación básica de parámetros —
        if entry <= 0 or stop_loss <= 0 or balance <= 0:
            log.warning(
                f"Parámetros inválidos: entry={entry} sl={stop_loss} bal={balance}"
            )
            return 0.0

        distance = abs(entry - stop_loss)

        # — Umbral mínimo de distancia al SL —
        # Se toma el mayor entre el porcentaje fijo y el basado en ATR
        # así en mercados volátiles sube automáticamente y en planos baja
        min_dist_pct = entry * min_sl_pct
        min_dist_atr = (atr * min_sl_atr) if (atr and atr > 0) else 0.0
        min_dist     = max(min_dist_pct, min_dist_atr)

        if distance < min_dist:
            log.warning(
                f"SL demasiado cercano — "
                f"dist={distance:.6f}  min={min_dist:.6f}  "
                f"(pct={min_dist_pct:.6f}  atr_based={min_dist_atr:.6f})  "
                f"entry={entry}  sl={stop_loss}"
            )
            return 0.0

        # — Cálculo de tamaño —
        risk_usdt    = balance * (risk_pct / 100)
        raw_size     = risk_usdt / distance
        notional     = raw_size * entry
        max_notional = balance * leverage * 0.80

        if notional > max_notional:
            raw_size = max_notional / entry

        size = math.floor(raw_size * 1000) / 1000

        # — Verificar cantidad mínima del exchange —
        if size < min_qty:
            log.warning(
                f"Tamaño insuficiente — size={size} < min_qty={min_qty}  "
                f"(balance={balance:.2f} USDT  risk={risk_pct}%  dist={distance:.6f})"
            )
            return 0.0

        log.info(
            f"Size OK — {size} unid  "
            f"notional={size * entry:.2f} USDT  "
            f"risk={risk_usdt:.2f} USDT  "
            f"dist={distance:.6f}"
        )
        return size

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
