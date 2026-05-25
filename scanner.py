"""
Scanner de Mercado — obtiene TODOS los pares USDT de BingX
y filtra por volumen mínimo y liquidez.
"""
import logging
import asyncio
from bot.bingx_client import BingXClient
from config import cfg

log = logging.getLogger("Scanner")


class MarketScanner:
    def __init__(self, exchange: BingXClient):
        self.exchange = exchange
        self._symbols: list[str] = []

    async def get_tradeable_symbols(self) -> list[str]:
        if cfg.SYMBOLS_MODE == "MANUAL":
            log.info(f"Modo MANUAL: {len(cfg.SYMBOLS_MANUAL)} símbolos")
            return cfg.SYMBOLS_MANUAL

        log.info("Escaneando todos los pares USDT de BingX...")
        try:
            all_tickers = await self.exchange.get_all_tickers()
        except Exception as e:
            log.error(f"Scanner error: {e} — usando lista manual")
            return cfg.SYMBOLS_MANUAL

        filtered = []
        for t in all_tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            try:
                vol = float(t.get("quoteVolume", 0))
            except Exception:
                continue
            if vol >= cfg.MIN_VOLUME_USDT:
                filtered.append((sym, vol))

        # Ordenar por volumen descendente, tomar top MAX_SYMBOLS
        filtered.sort(key=lambda x: x[1], reverse=True)
        symbols = [s[0] for s in filtered[:cfg.MAX_SYMBOLS]]

        log.info(f"Scanner: {len(symbols)} pares válidos "
                 f"(vol ≥ {cfg.MIN_VOLUME_USDT/1e6:.0f}M USDT)")
        for s, v in filtered[:cfg.MAX_SYMBOLS]:
            log.info(f"  {s:20s} vol24h={v/1e6:.1f}M")

        self._symbols = symbols
        return symbols

    @property
    def symbols(self) -> list[str]:
        return self._symbols
