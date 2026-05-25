"""
Cliente BingX v5 — fixes v5.1:
  • r.json(content_type=None)  → evita ContentTypeError si BingX devuelve HTML
  • isinstance(data, dict)     → evita AttributeError 'str' object has no attribute 'get'
  • get_balance() con try/except robusto
  • close() en sesión aiohttp para evitar Unclosed connector
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

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, params: dict) -> str:
        q = urlencode(sorted(params.items()))
        return hmac.new(
            self.secret.encode("utf-8"),
            q.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    # ── _get / _post robustos ────────────────────────────────

    async def _get(self, path, params=None, signed=False):
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.get(BASE + path, params=params) as r:
            # content_type=None evita ContentTypeError cuando BingX devuelve HTML
            data = await r.json(content_type=None)

        # FIX PRINCIPAL: si data no es dict, no se puede llamar .get()
        if not isinstance(data, dict):
            raise RuntimeError(
                f"GET {path}: respuesta inesperada tipo={type(data).__name__} "
                f"val={str(data)[:200]}"
            )
        code = data.get("code", 0)
        if code != 0:
            raise RuntimeError(f"GET {path} code={code}: {data.get('msg', data)}")
        return data.get("data", data)

    async def _post(self, path, params=None):
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.post(BASE + path, params=params) as r:
            data = await r.json(content_type=None)

        if not isinstance(data, dict):
            raise RuntimeError(
                f"POST {path}: respuesta inesperada tipo={type(data).__name__} "
                f"val={str(data)[:200]}"
            )
        code = data.get("code", 0)
        if code != 0:
            raise RuntimeError(f"POST {path} code={code}: {data.get('msg', data)}")
        return data.get("data", data)

    # ── Market Data ─────────────────────────────────────────

    async def get_all_tickers(self) -> list:
        try:
            data = await self._get("/openApi/swap/v2/quote/ticker")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # algunos endpoints envuelven en {"tickers": [...]}
                for key in ("tickers", "data", "result"):
                    if isinstance(data.get(key), list):
                        return data[key]
            return []
        except Exception as e:
            log.error(f"get_all_tickers: {e}")
            return []

    async def get_klines(self, symbol, interval, limit=200):
        try:
            data = await self._get(
                "/openApi/swap/v2/quote/klines",
                {"symbol": symbol, "interval": interval, "limit": limit}
            )
            rows = data if isinstance(data, list) else []
            result = []
            for k in rows:
                try:
                    if isinstance(k, dict):
                        result.append([
                            int(k["time"]),
                            float(k["open"]),
                            float(k["high"]),
                            float(k["low"]),
                            float(k["close"]),
                            float(k["volume"]),
                        ])
                    elif isinstance(k, (list, tuple)) and len(k) >= 6:
                        result.append([int(k[0]), float(k[1]), float(k[2]),
                                       float(k[3]), float(k[4]), float(k[5])])
                except (KeyError, TypeError, ValueError) as ke:
                    log.debug(f"get_klines {symbol} fila ignorada: {ke}")
            return sorted(result, key=lambda x: x[0])
        except Exception as e:
            log.error(f"get_klines {symbol} {interval}: {e}")
            return []

    async def get_ticker(self, symbol):
        try:
            data = await self._get(
                "/openApi/swap/v2/quote/ticker", {"symbol": symbol}
            )
            t = data[0] if isinstance(data, list) else data
            if not isinstance(t, dict):
                raise ValueError(f"ticker item no es dict: {type(t)}")
            return {
                "last"  : float(t.get("lastPrice", 0)),
                "bid"   : float(t.get("bidPrice", 0)),
                "ask"   : float(t.get("askPrice", 0)),
                "volume": float(t.get("volume", 0)),
            }
        except Exception as e:
            log.error(f"get_ticker {symbol}: {e}")
            return {"last": 0.0, "bid": 0.0, "ask": 0.0, "volume": 0.0}

    # ── L13: Order Flow Imbalance ────────────────────────────

    async def get_ofi(self, symbol: str, levels: int = 5) -> float:
        try:
            data = await self._get(
                "/openApi/swap/v2/quote/depth",
                {"symbol": symbol, "limit": levels * 2}
            )
            bids = data.get("bids", []) if isinstance(data, dict) else []
            asks = data.get("asks", []) if isinstance(data, dict) else []
            bid_q = sum(float(b[1]) for b in bids[:levels] if len(b) >= 2)
            ask_q = sum(float(a[1]) for a in asks[:levels] if len(a) >= 2)
            total = bid_q + ask_q
            if total == 0:
                return 0.0
            return (bid_q - ask_q) / total
        except Exception as e:
            log.debug(f"get_ofi {symbol}: {e}")
            return 0.0

    # ── L14: Funding Rate ────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> float:
        try:
            data = await self._get(
                "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol}
            )
            item = data[0] if isinstance(data, list) else data
            if not isinstance(item, dict):
                return 0.0
            # BingX usa "lastFundingRate" o "fundingRate" según versión
            for field in ("lastFundingRate", "fundingRate", "lastFundingRateValue"):
                val = item.get(field)
                if val is not None:
                    return float(val)
            return 0.0
        except Exception as e:
            log.debug(f"get_funding_rate {symbol}: {e}")
            return 0.0

    # ── L15: Open Interest ───────────────────────────────────

    async def get_open_interest(self, symbol: str) -> float:
        try:
            data = await self._get(
                "/openApi/swap/v2/quote/openInterest", {"symbol": symbol}
            )
            item = data[0] if isinstance(data, list) else data
            if not isinstance(item, dict):
                return 0.0
            for field in ("openInterest", "openInterestValue", "openInterestAmt"):
                val = item.get(field)
                if val is not None:
                    return float(val)
            return 0.0
        except Exception as e:
            log.debug(f"get_open_interest {symbol}: {e}")
            return 0.0

    async def get_market_context(self, symbol: str, ofi_levels: int = 5) -> dict:
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

    async def get_balance(self) -> float:
        """Devuelve el availableMargin en USDT. Nunca lanza excepción."""
        try:
            data = await self._get(
                "/openApi/swap/v2/user/balance", signed=True
            )
            # BingX puede devolver {"balance": [...]} o directamente [...]
            if isinstance(data, list):
                balances = data
            elif isinstance(data, dict):
                balances = data.get("balance", [])
                if not balances:
                    # Intentar otras claves
                    for key in ("balances", "assets", "result"):
                        if isinstance(data.get(key), list):
                            balances = data[key]
                            break
            else:
                log.warning(f"get_balance: tipo inesperado {type(data)}: {data}")
                return 0.0

            for a in balances:
                if not isinstance(a, dict):
                    continue
                asset = a.get("asset", a.get("currency", ""))
                if asset == "USDT":
                    for field in ("availableMargin", "available", "free", "balance"):
                        val = a.get(field)
                        if val is not None:
                            return float(val)
            return 0.0
        except Exception as e:
            log.error(f"get_balance error: {e}")
            return 0.0

    async def get_positions(self, symbol=""):
        try:
            p = {"symbol": symbol} if symbol else {}
            data = await self._get(
                "/openApi/swap/v2/user/positions", p, signed=True
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"get_positions {symbol}: {e}")
            return []

    async def set_leverage(self, symbol, leverage, side="LONG"):
        try:
            await self._post(
                "/openApi/swap/v2/trade/leverage",
                {"symbol": symbol, "leverage": leverage, "side": side}
            )
        except Exception as e:
            log.warning(f"set_leverage {symbol}: {e}")

    # ── Orders ───────────────────────────────────────────────

    async def place_order(self, symbol, side, size, leverage, sl_price,
                          tp_price=None, use_maker=True, maker_timeout=30,
                          maker_offset_pct=0.02):
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

        return await self._place_market(
            symbol, bingx_side, side, size, sl_price, tp_price
        )

    async def _place_maker(self, symbol, bingx_side, pos_side, size,
                           sl_price, tp_price, timeout, offset_pct):
        try:
            ticker = await self.get_ticker(symbol)
            if ticker["ask"] == 0 and ticker["bid"] == 0:
                return None

            if bingx_side == "BUY":
                limit_price = round(ticker["ask"] * (1 - offset_pct / 100), 6)
            else:
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
            if not isinstance(data, dict):
                return None
            order_id = (data.get("order", {}) or {}).get("orderId") or data.get("orderId")
            if not order_id:
                return None

            log.info(f"[{symbol}] Maker order {order_id} @ {limit_price}")

            for _ in range(timeout):
                await asyncio.sleep(1)
                status = await self._get_order_status(symbol, order_id)
                if status == "FILLED":
                    log.info(f"[{symbol}] Maker FILLED @ {limit_price}")
                    return data
                if status in ("CANCELLED", "EXPIRED", "REJECTED"):
                    return None

            await self._cancel_order(symbol, order_id)
            return None

        except Exception as e:
            log.warning(f"[{symbol}] _place_maker: {e}")
            return None

    async def _place_market(self, symbol, bingx_side, pos_side, size,
                            sl_price, tp_price):
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
            data = await self._get(
                "/openApi/swap/v2/trade/order",
                {"symbol": symbol, "orderId": order_id},
                signed=True
            )
            order = data[0] if isinstance(data, list) else data
            if isinstance(order, dict):
                return order.get("status", "UNKNOWN")
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    async def _cancel_order(self, symbol: str, order_id: str):
        try:
            await self._post(
                "/openApi/swap/v2/trade/cancelOrder",
                {"symbol": symbol, "orderId": order_id}
            )
            log.info(f"[{symbol}] Orden {order_id} cancelada")
        except Exception as e:
            log.warning(f"_cancel_order {symbol}: {e}")

    async def close_position(self, symbol, side):
        try:
            positions = await self.get_positions(symbol)
            size = 0.0
            for p in positions:
                if (isinstance(p, dict)
                        and p.get("positionSide") == side
                        and float(p.get("positionAmt", 0)) != 0):
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
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"close_position {symbol}: {e}")
            return None
