"""
scanner.py — MarketScanner para QF×JP Bot v6.0
Filtra pares de BingX por volumen, liquidez y blacklist.
Compatible con BingXClient async.
"""
import logging
import aiohttp
from typing import Optional

log = logging.getLogger("Scanner")

_STABLE_FRAGMENTS = ("USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "USDD")
_BINGX_TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"


class MarketScanner:
    """
    Obtiene y filtra los pares negociables de BingX Perpetual Futures.
    Usa get_tickers() del exchange si existe, sino llama a la API directamente.
    """

    def __init__(
        self,
        exchange,
        min_volume_usdt: float = None,
        max_symbols:     int   = None,
        blacklist:       set   = None,
    ):
        self._exchange = exchange

        try:
            from config import cfg
            self._min_vol = min_volume_usdt or cfg.MIN_VOLUME_USDT
            self._max_sym = max_symbols     or cfg.MAX_SYMBOLS
        except Exception:
            self._min_vol = min_volume_usdt or 5_000_000
            self._max_sym = max_symbols     or 40

        import os
        if blacklist is not None:
            self._blacklist = blacklist
        else:
            raw = os.getenv(
                "BLACKLIST",
                "ANIME-USDT,WCT-USDT,TAO-USDT,AAPLX-USDT,"
                "NCSKGOOGL2USD-USDT,NCSKASML2USD-USDT,VINE-USDT"
            )
            self._blacklist = {s.strip().upper() for s in raw.split(",") if s.strip()}

        log.info(
            f"MarketScanner init — min_vol=${self._min_vol/1e6:.1f}M "
            f"max={self._max_sym} blacklist={len(self._blacklist)}"
        )

    async def _fetch_tickers(self) -> list:
        """
        Intenta get_tickers() del exchange; si no existe, llama a BingX directamente.
        """
        # Intentar método del exchange primero
        fn = getattr(self._exchange, "get_tickers", None)
        if callable(fn):
            try:
                result = await fn()
                if result:
                    return result
            except Exception as e:
                log.warning(f"exchange.get_tickers() falló: {e} — usando API directa")

        # Fallback: llamada directa a BingX
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_BINGX_TICKER_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                    return data.get("data", [])
        except Exception as e:
            log.error(f"Fallback BingX tickers falló: {e}")
            return []

    async def get_tradeable_symbols(self) -> list[str]:
        """
        Devuelve lista de símbolos XXXX-USDT ordenados por volumen desc,
        filtrados por volumen mínimo, blacklist y pares estables.
        """
        tickers = await self._fetch_tickers()

        if not tickers:
            log.warning("Sin tickers disponibles")
            return []

        candidatos = []
        for t in tickers:
            sym = t.get("symbol", "")

            if not sym.endswith("-USDT"):
                continue
            if any(frag in sym for frag in _STABLE_FRAGMENTS):
                continue
            if sym.upper() in self._blacklist:
                continue

            # BingX usa "quoteVolume" o "volume"
            try:
                vol = float(t.get("quoteVolume", t.get("volume", 0)))
            except (TypeError, ValueError):
                continue

            if vol < self._min_vol:
                continue

            candidatos.append((sym, vol))

        candidatos.sort(key=lambda x: x[1], reverse=True)
        symbols = [sym for sym, _ in candidatos[: self._max_sym]]

        log.info(
            f"Scanner: {len(tickers)} tickers → "
            f"{len(candidatos)} con vol>${self._min_vol/1e6:.0f}M → "
            f"{len(symbols)} seleccionados (max={self._max_sym})"
        )

        for sym, vol in candidatos[:5]:
            log.info(f"  {sym:25s} vol24h=${vol/1e6:.1f}M")

        return symbols
