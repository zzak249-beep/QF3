"""
Performance Tracker v1.0 — seguimiento de trades y profit factor por símbolo
Usado por main.py v5.6:
  - PerformanceTracker(pf_window, min_profit_factor)
  - TradeRecord(symbol, side, entry, exit, pnl_pct, conviction, tier)
  - perf.record(trade_record)
  - perf.is_tradeable(symbol) -> bool
  - perf.global_stats()       -> dict
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections import deque
from typing import Optional
import logging

log = logging.getLogger("Perf")


@dataclass
class TradeRecord:
    symbol    : str
    side      : str          # "LONG" | "SHORT"
    entry     : float
    exit      : float
    pnl_pct   : float        # porcentaje sobre capital, positivo = ganancia
    conviction: int          # 0-10
    tier      : str          # "STD" | "FUEL" | "SUP"
    ts        : float = field(default_factory=lambda: __import__('time').time())


class PerformanceTracker:
    """
    Rastrea las últimas `window` operaciones por símbolo y globalmente.
    Suspende símbolos cuyo profit factor caiga por debajo de `min_pf`.
    """

    def __init__(self, window: int = 20, min_pf: float = 0.8):
        self.window = max(window, 5)
        self.min_pf = min_pf
        # Por símbolo: deque de TradeRecord
        self._by_symbol: dict[str, deque] = {}
        # Global
        self._global: deque = deque(maxlen=self.window * 10)
        # Símbolos suspendidos temporalmente
        self._suspended: set = set()

    # ── Registro de trade ────────────────────────────────────────────
    def record(self, trade: TradeRecord) -> None:
        sym = trade.symbol

        if sym not in self._by_symbol:
            self._by_symbol[sym] = deque(maxlen=self.window)

        self._by_symbol[sym].append(trade)
        self._global.append(trade)

        # Evaluar si suspender el símbolo
        pf = self._profit_factor(list(self._by_symbol[sym]))
        n  = len(self._by_symbol[sym])

        if n >= 5 and pf < self.min_pf:
            if sym not in self._suspended:
                self._suspended.add(sym)
                log.warning(f"[{sym}] suspendido — PF={pf:.2f} < {self.min_pf} "
                            f"tras {n} trades")
        else:
            if sym in self._suspended:
                self._suspended.discard(sym)
                log.info(f"[{sym}] reactivado — PF={pf:.2f}")

        log.info(f"[{sym}] trade registrado: {trade.side} {trade.pnl_pct:+.2f}% "
                 f"tier={trade.tier} conv={trade.conviction}/10 | "
                 f"PF_sym={pf:.2f} n={n}")

    # ── ¿Se puede operar este símbolo? ───────────────────────────────
    def is_tradeable(self, symbol: str) -> bool:
        return symbol not in self._suspended

    # ── Estadísticas globales ────────────────────────────────────────
    def global_stats(self) -> Optional[dict]:
        trades = list(self._global)
        if not trades:
            return None

        wins   = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        n      = len(trades)

        gross_win  = sum(t.pnl_pct for t in wins)
        gross_loss = abs(sum(t.pnl_pct for t in losses)) or 1e-9
        pf         = gross_win / gross_loss
        wr         = len(wins) / n if n else 0.0
        avg_pnl    = sum(t.pnl_pct for t in trades) / n if n else 0.0

        # Stats por tier
        by_tier: dict[str, list] = {}
        for t in trades:
            by_tier.setdefault(t.tier, []).append(t.pnl_pct)

        tier_stats = {
            tier: {
                "n":      len(pnls),
                "wr":     sum(1 for p in pnls if p > 0) / len(pnls),
                "avg":    sum(pnls) / len(pnls),
            }
            for tier, pnls in by_tier.items()
        }

        return {
            "total_trades" : n,
            "win_rate"     : wr,
            "profit_factor": round(pf, 3),
            "avg_pnl"      : round(avg_pnl, 3),
            "gross_win"    : round(gross_win, 3),
            "gross_loss"   : round(gross_loss, 3),
            "suspended"    : sorted(self._suspended),
            "by_tier"      : tier_stats,
        }

    # ── Stats por símbolo ────────────────────────────────────────────
    def symbol_stats(self, symbol: str) -> Optional[dict]:
        trades = list(self._by_symbol.get(symbol, []))
        if not trades:
            return None
        wins = [t for t in trades if t.pnl_pct > 0]
        n    = len(trades)
        gw   = sum(t.pnl_pct for t in wins)
        gl   = abs(sum(t.pnl_pct for t in trades if t.pnl_pct <= 0)) or 1e-9
        return {
            "symbol"       : symbol,
            "n"            : n,
            "win_rate"     : len(wins) / n,
            "profit_factor": round(gw / gl, 3),
            "avg_pnl"      : round(sum(t.pnl_pct for t in trades) / n, 3),
            "suspended"    : symbol in self._suspended,
        }

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _profit_factor(trades: list) -> float:
        gw = sum(t.pnl_pct for t in trades if t.pnl_pct > 0)
        gl = abs(sum(t.pnl_pct for t in trades if t.pnl_pct <= 0)) or 1e-9
        return gw / gl
