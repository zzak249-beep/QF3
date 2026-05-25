"""
Cliente BingX v2 — soporta get_all_tickers para scanner automático
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
            params["timestamp"] = int(time.time()*1000)
            params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.get(BASE+path, params=params) as r:
            data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"GET {path}: {data}")
        return data.get("data", data)

    async def _post(self, path, params=None):
        params = params or {}
        params["timestamp"] = int(time.time()*1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.post(BASE+path, params=params) as r:
            data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"POST {path}: {data}")
        return data.get("data", data)

    async def get_all_tickers(self) -> list:
        """Devuelve todos los tickers de perpetual futures."""
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
        return {"last": float(t["lastPrice"]), "bid": float(t.get("bidPrice",0)),
                "ask": float(t.get("askPrice",0)), "volume": float(t.get("volume",0))}

    async def get_balance(self):
        data = await self._get("/openApi/swap/v2/user/balance", signed=True)
        for a in data.get("balance", []):
            if a.get("asset") == "USDT":
                return float(a.get("availableMargin", 0))
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

    async def place_order(self, symbol, side, size, leverage, sl_price, tp_price=None):
        await self.set_leverage(symbol, leverage, side)
        await asyncio.sleep(0.2)
        params = {
            "symbol": symbol,
            "side": "BUY" if side=="LONG" else "SELL",
            "positionSide": side,
            "type": "MARKET",
            "quantity": f"{size:.4f}",
            "stopLossPrice": f"{sl_price:.4f}",
        }
        if tp_price:
            params["takeProfitPrice"] = f"{tp_price:.4f}"
        try:
            data = await self._post("/openApi/swap/v2/trade/order", params)
            log.info(f"Order: {symbol} {side} {size} → {data}")
            return data
        except Exception as e:
            log.error(f"place_order {symbol}: {e}")
            return None

    async def close_position(self, symbol, side):
        positions = await self.get_positions(symbol)
        size = 0.0
        for p in positions:
            if p.get("positionSide")==side and float(p.get("positionAmt",0))!=0:
                size = abs(float(p["positionAmt"])); break
        if size == 0: return None
        params = {"symbol": symbol,
                  "side": "SELL" if side=="LONG" else "BUY",
                  "positionSide": side, "type": "MARKET",
                  "quantity": f"{size:.4f}", "reduceOnly": "true"}
        try:
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"close_position {symbol}: {e}"); return None
