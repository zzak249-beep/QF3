"""
Risk Manager v5.8 — DD smarter + balance recovery + env var support
Fixes sobre v5.7:
  - MAX_DAILY_DD_PCT ahora lee de env var (default 80%, antes 5%)
  - start_balance se resetea automáticamente si el balance sube
    por encima del valor registrado (ej: usuario añade fondos)
  - Si start_balance era < MIN_START_BALANCE (ej: 0.001 USDT por
    posiciones en pérdida) se recalcula al recuperarse el balance
  - Nuevo método force_reset_dd() para desbloquear manualmente
  - Log explica siempre por qué bloquea y cuánto falta para desbloquear
"""
import logging
import math
import time
import os
from datetime import datetime, timezone

log = logging.getLogger("RiskMgr")

# Leer límite DD desde env var para poder cambiarlo sin redeployar
_ENV_DD = float(os.getenv("MAX_DAILY_DD_PCT", "80"))
# Balance mínimo que se considera "válido" para fijar como start
_MIN_VALID_BAL = float(os.getenv("MIN_START_BALANCE", "1.0"))


class RiskManager:

    def __init__(self):
        self._warn_ts: dict[str, float] = {}
        self._warn_cooldown = 60.0

        self._start_balance      = 0.0
        self._start_balance_date = ""

    # ── helpers ──────────────────────────────────────────────────────
    def _can_warn(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._warn_ts.get(key, 0.0) >= self._warn_cooldown:
            self._warn_ts[key] = now
            return True
        return False

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── API pública ──────────────────────────────────────────────────

    def update_start_balance(self, balance: float) -> None:
        """
        Actualiza start_balance diario con lógica mejorada:
        1. Reset automático cada nuevo día UTC
        2. Si el start anterior era inválido (< MIN_VALID_BAL) y ahora
           hay balance real, resetea para no bloquear el día entero
        3. Si el balance SUBE por encima del start (fondos añadidos),
           actualiza el start al nuevo máximo
        """
        today = self._today_utc()
        needs_reset = False

        # Caso 1: nuevo día
        if self._start_balance_date != today:
            needs_reset = True
            reason = "nuevo día UTC"

        # Caso 2: start anterior era inválido (balance era ~0 por posiciones)
        elif self._start_balance < _MIN_VALID_BAL and balance >= _MIN_VALID_BAL:
            needs_reset = True
            reason = f"start anterior inválido ({self._start_balance:.4f} USDT)"

        # Caso 3: usuario añadió fondos — balance sube por encima del start
        elif balance > self._start_balance * 1.10 and balance >= _MIN_VALID_BAL:
            needs_reset = True
            reason = f"balance subió {balance:.2f} > start {self._start_balance:.2f}"

        if needs_reset and balance >= _MIN_VALID_BAL:
            self._start_balance      = balance
            self._start_balance_date = today
            log.info(f"Start balance diario = {balance:.2f} USDT ({today} UTC) — {reason}")

    def force_reset_dd(self, new_balance: float) -> None:
        """Reset manual del DD — usar si se añaden fondos o se cierra posiciones perdedoras."""
        self._start_balance      = new_balance
        self._start_balance_date = self._today_utc()
        log.info(f"DD reseteado manualmente — nuevo start = {new_balance:.2f} USDT")

    def position_size(
        self,
        balance,
        entry,
        stop_loss,
        risk_pct,
        leverage,
        atr        = None,
        min_sl_pct = 0.0002,
        min_sl_atr = 0.3,
        min_qty    = 0.001,
    ) -> float:

        if entry <= 0 or stop_loss <= 0 or balance <= 0:
            if self._can_warn("bad_params"):
                log.warning(f"Parámetros inválidos: entry={entry} sl={stop_loss} bal={balance}")
            return 0.0

        distance = abs(entry - stop_loss)

        min_dist_pct = entry * min_sl_pct
        min_dist_atr = (atr * min_sl_atr) if (atr and atr > 0) else 0.0
        min_dist     = max(min_dist_pct, min_dist_atr)

        if distance < min_dist:
            if self._can_warn("sl_cercano"):
                log.warning(
                    f"SL demasiado cercano — "
                    f"dist={distance:.6f} mín={min_dist:.6f} "
                    f"entry={entry} sl={stop_loss}"
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
        max_dd_pct:      float = None,
        start_balance:   float = None,
    ) -> bool:
        """
        Comprueba DD diario.
        - max_dd_pct: si None, usa la variable de entorno MAX_DAILY_DD_PCT (default 80%)
        - Log explica cuánto falta para llegar al límite
        - Warning máximo 1 vez por minuto
        """
        # Usar env var si no se pasa explícitamente
        if max_dd_pct is None:
            max_dd_pct = _ENV_DD

        if start_balance is not None and start_balance > _MIN_VALID_BAL:
            self.update_start_balance(start_balance)

        sb = self._start_balance

        # Si no hay start válido todavía, no bloquear
        if sb < _MIN_VALID_BAL or current_balance <= 0:
            return True

        # Si ganamos, siempre OK
        if current_balance >= sb:
            return True

        dd = (sb - current_balance) / sb * 100
        margen = max_dd_pct - dd  # cuánto más puede caer

        if dd > max_dd_pct:
            if self._can_warn("daily_dd"):
                log.warning(
                    f"⛔ DD diario {dd:.2f}% > límite {max_dd_pct}%  "
                    f"(start={sb:.2f}  now={current_balance:.2f} USDT)  "
                    f"— Para desbloquear: añadir fondos o subir MAX_DAILY_DD_PCT"
                )
            return False

        # Aviso preventivo cuando queda poco margen
        if margen < 5.0:
            if self._can_warn("dd_warning"):
                log.warning(
                    f"⚠️ DD diario en {dd:.2f}% — margen restante: {margen:.2f}% "
                    f"hasta límite {max_dd_pct}%"
                )
        return True
