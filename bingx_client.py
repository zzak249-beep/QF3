"""
Cliente BingX v5 — nuevos endpoints:
  • get_orderbook     → Order Flow Imbalance (L13)
  • get_funding_rate  → Funding Rate filter (L14)
  • get_open_interest → Open Interest Delta (L15)
  • place_maker_order → limit post-only con market fallback
"""
import asyncio, hashlib, hmac, time, logging
from urllib.parse import urlencode
import aiohttp

log = logging.getLogger("BingX")
BASE = "https://open-api.bingx.com"


class BingXClient:
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret  = secret
        self._session = None

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    def _sign(self, params):
        q = urlencode(sorted(params.items()))
        return hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()

    async def _get(self, path, params=None, signed=False):
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.get(BASE + path, params=params) as r:
            data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"GET {path}: {data}")
        return data.get("data", data)

    async def _post(self, path, params=None):
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.post(BASE + path, params=params) as r:
            data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"POST {path}: {data}")
        return data.get("data", data)

    # ── Market Data ─────────────────────────────────────────

    async def get_all_tickers(self) -> list:
        data = await self._get("/openApi/swap/v2/quote/ticker")
        if isinstance(data, list): return data
        return []

    async def get_klines(self, symbol, interval, limit=200):
        data = await self._get("/openApi/swap/v2/quote/klines",
                               {"symbol": symbol, "interval": interval, "limit": limit})
        result = []
        for k in (data if isinstance(data, list) else []):
            result.append([int(k["time"]), float(k["open"]), float(k["high"]),
                           float(k["low"]), float(k["close"]), float(k["volume"])])
        return sorted(result, key=lambda x: x[0])

    async def get_ticker(self, symbol):
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        t = data[0] if isinstance(data, list) else data
        return {
            "last"  : float(t["lastPrice"]),
            "bid"   : float(t.get("bidPrice", 0)),
            "ask"   : float(t.get("askPrice", 0)),
            "volume": float(t.get("volume", 0)),
        }

    # ── L13: Order Flow Imbalance ────────────────────────────

    async def get_ofi(self, symbol: str, levels: int = 5) -> float:
        """
        Order Flow Imbalance: (bid_qty - ask_qty) / total.
        Rango -1 a +1. >0 = presión compradora.
        """
        try:
            data = await self._get("/openApi/swap/v2/quote/depth",
                                   {"symbol": symbol, "limit": levels * 2})
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            bid_q = sum(float(b[1]) for b in bids[:levels])
            ask_q = sum(float(a[1]) for a in asks[:levels])
            total = bid_q + ask_q
            if total == 0:
                return 0.0
            return (bid_q - ask_q) / total
        except Exception as e:
            log.debug(f"get_ofi {symbol}: {e}")
            return 0.0

    # ── L14: Funding Rate ────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> float:
        """
        Funding rate actual del perpetuo.
        Positivo = longs pagan (sesgo alcista institucional).
        """
        try:
            data = await self._get("/openApi/swap/v2/quote/premiumIndex",
                                   {"symbol": symbol})
            item = data[0] if isinstance(data, list) else data
            return float(item.get("lastFundingRate", 0))
        except Exception as e:
            log.debug(f"get_funding_rate {symbol}: {e}")
            return 0.0

    # ── L15: Open Interest ───────────────────────────────────

    async def get_open_interest(self, symbol: str) -> float:
        """Open Interest actual en contratos."""
        try:
            data = await self._get("/openApi/swap/v2/quote/openInterest",
                                   {"symbol": symbol})
            item = data[0] if isinstance(data, list) else data
            return float(item.get("openInterest", 0))
        except Exception as e:
            log.debug(f"get_open_interest {symbol}: {e}")
            return 0.0

    async def get_market_context(self, symbol: str, ofi_levels: int = 5) -> dict:
        """
        Fetch OFI + Funding Rate + OI en paralelo (una sola función).
        """
        ofi, fr, oi = await asyncio.gather(
            self.get_ofi(symbol, ofi_levels),
            self.get_funding_rate(symbol),
            self.get_open_interest(symbol),
            return_exceptions=True
        )
        return {
            "ofi"          : ofi if isinstance(ofi, float) else 0.0,
            "funding_rate" : fr  if isinstance(fr,  float) else 0.0,
            "open_interest": oi  if isinstance(oi,  float) else 0.0,
        }

    # ── Account ─────────────────────────────────────────────

    async def get_balance(self):
        try:
            data = await self._get("/openApi/swap/v2/user/balance", signed=True)
            # La API puede devolver dict con "balance", lista directa, o lista anidada
            if isinstance(data, dict):
                items = data.get("balance", [])
            elif isinstance(data, list):
                items = data
            else:
                log.warning(f"get_balance: respuesta inesperada tipo {type(data)}: {data}")
                return 0.0

            for a in items:
                if not isinstance(a, dict):
                    log.debug(f"get_balance: item no-dict ignorado: {a!r}")
                    continue
                if a.get("asset") == "USDT":
                    return float(a.get("availableMargin", 0))
        except Exception as e:
            log.error(f"get_balance: {e}")
        return 0.0

    async def get_positions(self, symbol=""):
        p = {"symbol": symbol} if symbol else {}
        data = await self._get("/openApi/swap/v2/user/positions", p, signed=True)
        return data if isinstance(data, list) else []

    async def set_leverage(self, symbol, leverage, side="LONG"):
        try:
            await self._post("/openApi/swap/v2/trade/leverage",
                             {"symbol": symbol, "leverage": leverage, "side": side})
        except Exception as e:
            log.warning(f"set_leverage {symbol}: {e}")

    # ── Orders ───────────────────────────────────────────────

    async def place_order(self, symbol, side, size, leverage, sl_price, tp_price=None,
                          use_maker=True, maker_timeout=30, maker_offset_pct=0.02):
        """
        Coloca orden. Si use_maker=True intenta LIMIT post-only primero.
        Si no llena en maker_timeout segundos, cancela y cae a MARKET.

        Fees:
          MARKET (taker) = 0.075% × 2 lados = 0.15% round-trip
          LIMIT maker    = 0.02%  × 2 lados = 0.04% round-trip  (−73%)
        """
        await self.set_leverage(symbol, leverage, side)
        await asyncio.sleep(0.2)

        bingx_side = "BUY" if side == "LONG" else "SELL"

        if use_maker:
            order = await self._place_maker(
                symbol, bingx_side, side, size, sl_price, tp_price,
                maker_timeout, maker_offset_pct
            )
            if order:
                return order
            log.info(f"[{symbol}] Maker no llenó — fallback a MARKET")

        # MARKET fallback
        return await self._place_market(symbol, bingx_side, side, size, sl_price, tp_price)

    async def _place_maker(self, symbol, bingx_side, pos_side, size,
                           sl_price, tp_price, timeout, offset_pct):
        """Limit post-only. Devuelve None si no llena en timeout."""
        try:
            ticker = await self.get_ticker(symbol)
            if bingx_side == "BUY":
                # Comprar: limite ligeramente por debajo del ask actual
                limit_price = round(ticker["ask"] * (1 - offset_pct / 100), 6)
            else:
                # Vender: limite ligeramente por encima del bid actual
                limit_price = round(ticker["bid"] * (1 + offset_pct / 100), 6)

            params = {
                "symbol"      : symbol,
                "side"        : bingx_side,
                "positionSide": pos_side,
                "type"        : "LIMIT",
                "price"       : f"{limit_price:.6f}",
                "quantity"    : f"{size:.4f}",
                "timeInForce" : "PostOnly",
            }
            if sl_price:
                params["stopLossPrice"] = f"{sl_price:.6f}"
            if tp_price:
                params["takeProfitPrice"] = f"{tp_price:.6f}"

            data = await self._post("/openApi/swap/v2/trade/order", params)
            order_id = data.get("order", {}).get("orderId") or data.get("orderId")
            if not order_id:
                return None

            log.info(f"[{symbol}] Maker order {order_id} @ {limit_price}")

            # Esperar llenado
            for _ in range(timeout):
                await asyncio.sleep(1)
                status = await self._get_order_status(symbol, order_id)
                if status == "FILLED":
                    log.info(f"[{symbol}] Maker FILLED @ {limit_price}")
                    return data
                if status in ("CANCELLED", "EXPIRED", "REJECTED"):
                    return None

            # Timeout → cancelar
            await self._cancel_order(symbol, order_id)
            return None

        except Exception as e:
            log.warning(f"[{symbol}] _place_maker: {e}")
            return None

    async def _place_market(self, symbol, bingx_side, pos_side, size, sl_price, tp_price):
        params = {
            "symbol"      : symbol,
            "side"        : bingx_side,
            "positionSide": pos_side,
            "type"        : "MARKET",
            "quantity"    : f"{size:.4f}",
        }
        if sl_price:
            params["stopLossPrice"] = f"{sl_price:.4f}"
        if tp_price:
            params["takeProfitPrice"] = f"{tp_price:.4f}"
        try:
            data = await self._post("/openApi/swap/v2/trade/order", params)
            log.info(f"Market order: {symbol} {pos_side} {size} → {data}")
            return data
        except Exception as e:
            log.error(f"place_market {symbol}: {e}")
            return None

    async def _get_order_status(self, symbol: str, order_id: str) -> str:
        try:
            data = await self._get("/openApi/swap/v2/trade/order",
                                   {"symbol": symbol, "orderId": order_id}, signed=True)
            order = data[0] if isinstance(data, list) else data
            return order.get("status", "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    async def _cancel_order(self, symbol: str, order_id: str):
        try:
            await self._post("/openApi/swap/v2/trade/cancelOrder",
                             {"symbol": symbol, "orderId": order_id})
            log.info(f"[{symbol}] Orden {order_id} cancelada")
        except Exception as e:
            log.warning(f"_cancel_order {symbol}: {e}")

    async def close_position(self, symbol, side):
        positions = await self.get_positions(symbol)
        size = 0.0
        for p in positions:
            if p.get("positionSide") == side and float(p.get("positionAmt", 0)) != 0:
                size = abs(float(p["positionAmt"]))
                break
        if size == 0:
            return None
        params = {
            "symbol"      : symbol,
            "side"        : "SELL" if side == "LONG" else "BUY",
            "positionSide": side,
            "type"        : "MARKET",
            "quantity"    : f"{size:.4f}",
            "reduceOnly"  : "true",
        }
        try:
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"close_position {symbol}: {e}")
            return None
