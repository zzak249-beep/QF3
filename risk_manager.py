"""
Risk Manager v5.7
Fixes v5.6:
  - Cooldown en warnings: max 1 log por tipo cada 60s (evita spam de miles de líneas)
  - Reset diario automático de start_balance a medianoche UTC
  - __init__ con estado interno limpio
  - SL warning también tiene cooldown
"""
import logging
import math
import time
from datetime import datetime, timezone

log = logging.getLogger("RiskMgr")


class RiskManager:

    def __init__(self):
        # Cooldowns para evitar spam de logs (timestamp del último warning)
        self._warn_ts: dict[str, float] = {}
        self._warn_cooldown = 60.0  # segundos entre warnings del mismo tipo

        # Reset diario de start_balance
        self._start_balance      = 0.0
        self._start_balance_date = ""  # "YYYY-MM-DD" UTC

    # ── helpers ──────────────────────────────────────────────────────────────

    def _can_warn(self, key: str) -> bool:
        """Devuelve True solo si han pasado >= _warn_cooldown segundos desde el último warning de este tipo."""
        now = time.monotonic()
        if now - self._warn_ts.get(key, 0.0) >= self._warn_cooldown:
            self._warn_ts[key] = now
            return True
        return False

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── API pública ──────────────────────────────────────────────────────────

    def update_start_balance(self, balance: float) -> None:
        """
        Llama esto UNA VEZ al arrancar el bot y en cada nuevo día UTC.
        El método se autogestiona: si el día cambió, resetea automáticamente.
        """
        today = self._today_utc()
        if self._start_balance_date != today or self._start_balance <= 0:
            self._start_balance      = balance
            self._start_balance_date = today
            log.info(f"Start balance diario = {balance:.2f} USDT ({today} UTC)")

    def position_size(
        self,
        balance:     float,
        entry:       float,
        stop_loss:   float,
        risk_pct:    float,
        leverage:    float,
        atr:         float = None,
        min_sl_pct:  float = 0.0002,  # 0.02% — v5.5 usaba 0.1%, demasiado restrictivo
        min_sl_atr:  float = 0.3,     # mínimo = 0.3 × ATR si se pasa atr
        min_qty:     float = 0.001,   # cantidad mínima aceptable por el exchange
    ) -> float:

        if entry <= 0 or stop_loss <= 0 or balance <= 0:
            if self._can_warn("bad_params"):
                log.warning(f"Parámetros inválidos: entry={entry} sl={stop_loss} bal={balance}")
            return 0.0

        distance = abs(entry - stop_loss)

        # Umbral mínimo de SL: el mayor entre % fijo y ATR-based
        min_dist_pct = entry * min_sl_pct
        min_dist_atr = (atr * min_sl_atr) if (atr and atr > 0) else 0.0
        min_dist     = max(min_dist_pct, min_dist_atr)

        if distance < min_dist:
            if self._can_warn("sl_cercano"):
                log.warning(
                    f"SL demasiado cercano — "
                    f"dist={distance:.6f}  min={min_dist:.6f}  "
                    f"(pct={min_dist_pct:.6f}  atr_based={min_dist_atr:.6f})  "
                    f"entry={entry}  sl={stop_loss}"
                )
            return 0.0

        risk_usdt    = balance * (risk_pct / 100)
        raw_size     = risk_usdt / distance
        notional     = raw_size * entry
        max_notional = balance * leverage * 0.80

        if notional > max_notional:
            raw_size = max_notional / entry

        size = math.floor(raw_size * 1000) / 1000

        if size < min_qty:
            if self._can_warn("min_qty"):
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

    def max_daily_loss_ok(
        self,
        current_balance: float,
        max_dd_pct:      float,
        start_balance:   float = None,  # opcional: si None usa el interno
    ) -> bool:
        """
        Comprueba si el drawdown diario supera el límite.
        - start_balance se autogestiona (reset diario a medianoche UTC).
        - El warning se emite como máximo 1 vez por minuto para evitar spam.
        """
        # Actualizar start_balance interno si se pasa externamente
        if start_balance is not None and start_balance > 0:
            self.update_start_balance(start_balance)

        sb = self._start_balance

        if sb <= 0 or current_balance <= 0:
            return True
        if current_balance >= sb:
            return True

        dd = (sb - current_balance) / sb * 100

        if dd > max_dd_pct:
            if self._can_warn("daily_dd"):
                log.warning(
                    f"⛔ DD diario {dd:.2f}% > límite {max_dd_pct}%  "
                    f"(start={sb:.2f}  now={current_balance:.2f} USDT)"
                )
            return False

        return True
