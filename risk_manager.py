"""
Risk Manager v6.2
=================
FIX CRÍTICO [C]:
  - max_daily_loss_ok() ya NO recibe max_dd_pct desde main.py
    Antes: main.py llamaba risk.max_daily_loss_ok(bal, cfg.MAX_DAILY_DD_PCT)
           cfg leía MAX_DD_PCT=5.0 → bloqueaba todo
    Ahora: lee siempre desde su propia env var MAX_DAILY_DD_PCT (default 80%)
  - Para cambiar el límite: solo cambiar MAX_DAILY_DD_PCT en Railway

Resto de mejoras heredadas de v5.8:
  - start_balance reset automático al nuevo día UTC
  - Reset si balance sube (usuario añade fondos)
  - Reset si start anterior era inválido
  - force_reset_dd() para desbloquear manualmente
"""
import logging
import math
import time
import os
from datetime import datetime, timezone

log = logging.getLogger("RiskMgr")

# [C] FIX: leer DD SIEMPRE desde env var — nunca desde cfg
_ENV_DD = float(os.getenv("MAX_DAILY_DD_PCT", "80.0"))
_MIN_VALID_BAL = float(os.getenv("MIN_START_BALANCE", "1.0"))


class RiskManager:

    def __init__(self):
        self._warn_ts: dict[str, float] = {}
        self._warn_cooldown = 60.0
        self._start_balance      = 0.0
        self._start_balance_date = ""

    def _can_warn(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._warn_ts.get(key, 0.0) >= self._warn_cooldown:
            self._warn_ts[key] = now
            return True
        return False

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def update_start_balance(self, balance: float) -> None:
        today = self._today_utc()
        needs_reset = False
        reason = ""

        if self._start_balance_date != today:
            needs_reset = True
            reason = "nuevo día UTC"
        elif self._start_balance < _MIN_VALID_BAL and balance >= _MIN_VALID_BAL:
            needs_reset = True
            reason = f"start anterior inválido ({self._start_balance:.4f} USDT)"
        elif balance > self._start_balance * 1.10 and balance >= _MIN_VALID_BAL:
            needs_reset = True
            reason = f"balance subió {balance:.2f} > start {self._start_balance:.2f}"

        if needs_reset and balance >= _MIN_VALID_BAL:
            self._start_balance      = balance
            self._start_balance_date = today
            log.info(f"Start balance = {balance:.2f} USDT ({today} UTC) — {reason}")

    def force_reset_dd(self, new_balance: float) -> None:
        self._start_balance      = new_balance
        self._start_balance_date = self._today_utc()
        log.info(f"DD reseteado — nuevo start = {new_balance:.2f} USDT")

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
                log.warning(f"Params inválidos: entry={entry} sl={stop_loss} bal={balance}")
            return 0.0

        distance = abs(entry - stop_loss)
        min_dist_pct = entry * min_sl_pct
        min_dist_atr = (atr * min_sl_atr) if (atr and atr > 0) else 0.0
        min_dist     = max(min_dist_pct, min_dist_atr)

        if distance < min_dist:
            if self._can_warn("sl_cercano"):
                log.warning(
                    f"SL demasiado cercano — dist={distance:.6f} mín={min_dist:.6f}"
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
                log.warning(f"Tamaño insuficiente — size={size} < min={min_qty}")
            return 0.0

        log.info(
            f"Size OK — {size} | notional={size*entry:.2f} USDT | "
            f"risk={risk_usdt:.2f} USDT | dist={distance:.6f}"
        )
        return size

    def max_daily_loss_ok(
        self,
        current_balance: float,
        max_dd_pct: float = None,          # [C] ignorado — usa env var siempre
        start_balance: float = None,
    ) -> bool:
        """
        [C] FIX: siempre usa MAX_DAILY_DD_PCT de env var.
        El parámetro max_dd_pct se ignora para evitar que cfg.MAX_DAILY_DD_PCT
        (que lee MAX_DD_PCT=5%) sobreescriba el límite real.
        """
        # [C] Siempre desde env var, nunca desde el argumento
        limit = _ENV_DD

        if start_balance is not None and start_balance > _MIN_VALID_BAL:
            self.update_start_balance(start_balance)

        sb = self._start_balance

        if sb < _MIN_VALID_BAL or current_balance <= 0:
            return True

        if current_balance >= sb:
            return True

        dd = (sb - current_balance) / sb * 100
        margen = limit - dd

        if dd > limit:
            if self._can_warn("daily_dd"):
                log.warning(
                    f"⛔ DD diario {dd:.2f}% > límite {limit}%  "
                    f"(start={sb:.2f}  now={current_balance:.2f} USDT)  "
                    f"— Para desbloquear: añadir fondos o subir MAX_DAILY_DD_PCT"
                )
            return False

        if margen < 5.0:
            if self._can_warn("dd_warning"):
                log.warning(
                    f"⚠️ DD diario {dd:.2f}% — margen: {margen:.2f}% hasta {limit}%"
                )
        return True
