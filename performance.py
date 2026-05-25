"""
Tracker de rentabilidad por símbolo.
Calcula Profit Factor, Win Rate y suspende símbolos no rentables.
"""
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("Performance")


@dataclass
class TradeRecord:
    symbol   : str
    side     : str
    entry    : float
    exit     : float
    pnl_pct  : float
    conviction: int
    tier     : str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_win(self) -> bool:
        return self.pnl_pct > 0


class PerformanceTracker:
    """Ventana deslizante de trades por símbolo."""

    def __init__(self, window: int = 20, min_pf: float = 1.5):
        self.window = window
        self.min_pf = min_pf
        self._trades: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self._suspended: set[str] = set()

    def record(self, trade: TradeRecord):
        self._trades[trade.symbol].append(trade)
        self._evaluate(trade.symbol)

    def _evaluate(self, symbol: str):
        trades = list(self._trades[symbol])
        if len(trades) < 5:   # mínimo 5 trades para evaluar
            return

        gross_profit = sum(t.pnl_pct for t in trades if t.pnl_pct > 0)
        gross_loss   = abs(sum(t.pnl_pct for t in trades if t.pnl_pct < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 99.0
        wr = sum(1 for t in trades if t.is_win) / len(trades)

        if pf < self.min_pf and len(trades) >= 10:
            if symbol not in self._suspended:
                log.warning(f"[{symbol}] SUSPENDIDO — PF={pf:.2f} WR={wr:.0%} "
                            f"(mín PF={self.min_pf})")
                self._suspended.add(symbol)
        elif pf >= self.min_pf and symbol in self._suspended:
            log.info(f"[{symbol}] REACTIVADO — PF={pf:.2f} WR={wr:.0%}")
            self._suspended.discard(symbol)

    def is_tradeable(self, symbol: str) -> bool:
        return symbol not in self._suspended

    def stats(self, symbol: str) -> dict:
        trades = list(self._trades[symbol])
        if not trades:
            return {"trades": 0, "win_rate": 0, "profit_factor": 0,
                    "avg_pnl": 0, "suspended": symbol in self._suspended}
        wins  = [t for t in trades if t.is_win]
        gross_profit = sum(t.pnl_pct for t in wins)
        gross_loss   = abs(sum(t.pnl_pct for t in trades if not t.is_win))
        pf = gross_profit / gross_loss if gross_loss > 0 else 99.0
        return {
            "trades"        : len(trades),
            "win_rate"      : len(wins) / len(trades),
            "profit_factor" : pf,
            "avg_pnl"       : sum(t.pnl_pct for t in trades) / len(trades),
            "suspended"     : symbol in self._suspended,
        }

    def global_stats(self) -> dict:
        all_trades = [t for dq in self._trades.values() for t in dq]
        if not all_trades:
            return {}
        wins = [t for t in all_trades if t.is_win]
        gp   = sum(t.pnl_pct for t in wins)
        gl   = abs(sum(t.pnl_pct for t in all_trades if not t.is_win))
        return {
            "total_trades"  : len(all_trades),
            "win_rate"      : len(wins) / len(all_trades),
            "profit_factor" : gp / gl if gl > 0 else 99.0,
            "avg_pnl"       : sum(t.pnl_pct for t in all_trades) / len(all_trades),
            "suspended"     : list(self._suspended),
        }
