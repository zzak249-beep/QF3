"""
Cliente BingX v5.6 — añadido close() para shutdown limpio sin ResourceWarning
"""
import asyncio, hashlib, hmac, time, logging
from urllib.parse import urlencode
import aiohttp

log = logging.getLogger("BingX")
BASE = "https://open-api.bingx.com"

_KLINE_FORMAT_LOGGED = set()


class BingXClient:
    def __init__(self, api_key, secret):
        self.api_key    = api_key
        self.secret     = secret
        self._session   = None
        self._bal_cache = 0.0
        self._bal_ts    = 0.0
        self._BAL_TTL   = 120
        self._bal_lock  = asyncio.Lock()

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-BX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self):
        """Cierra la sesión aiohttp. Llamar antes de terminar el proceso."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            log.info("Session cerrada")

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
            data = await r.json(content_type=None)
        if data.get("code") != 0:
            raise RuntimeError(f"GET {path}: {data}")
        return data.get("data", data)

    async def _post(self, path, params=None):
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.post(BASE + path, params=params) as r:
            data = await r.json(content_type=None)
        if data.get("code") != 0:
            raise RuntimeError(f"POST {path}: {data}")
        return data.get("data", data)

    # ── Klines ───────────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        try:
            raw = await self._get("/openApi/swap/v2/quote/klines",
                                  {"symbol": symbol, "interval": interval, "limit": limit})
        except Exception as e:
            log.debug(f"get_klines {symbol}: {e}")
            return []

        if symbol not in _KLINE_FORMAT_LOGGED:
            _KLINE_FORMAT_LOGGED.add(symbol)
            if isinstance(raw, list) and len(raw) > 0:
                log.info(f"[KLINE FORMAT] {symbol}: list[{type(raw[0]).__name__}] "
                         f"first={str(raw[0])[:120]}")
            else:
                log.info(f"[KLINE FORMAT] {symbol}: {type(raw).__name__} = {str(raw)[:120]}")

        return self._parse_klines(raw, symbol)

    def _parse_klines(self, raw, symbol="") -> list:
        result = []
        if not raw:
            return []
        if isinstance(raw, list):
            for k in raw:
                row = self._parse_one_kline(k)
                if row:
                    result.append(row)
            return sorted(result, key=lambda x: x[0]) if result else []
        if isinstance(raw, dict):
            t_key = next((k for k in ["t","time","T","timestamp","ts"] if k in raw), None)
            o_key = next((k for k in ["o","open","O"] if k in raw), None)
            h_key = next((k for k in ["h","high","H"] if k in raw), None)
            l_key = next((k for k in ["l","low","L"] if k in raw), None)
            c_key = next((k for k in ["c","close","C"] if k in raw), None)
            v_key = next((k for k in ["v","volume","V","vol"] if k in raw), None)
            if t_key and o_key:
                ts_arr = raw[t_key]; o_arr = raw[o_key]
                h_arr  = raw.get(h_key, o_arr); l_arr = raw.get(l_key, o_arr)
                c_arr  = raw.get(c_key, o_arr); v_arr = raw.get(v_key, [1]*len(ts_arr))
                for i in range(min(len(ts_arr), len(o_arr))):
                    try:
                        result.append([int(float(ts_arr[i])), float(o_arr[i]),
                                       float(h_arr[i]),  float(l_arr[i]),
                                       float(c_arr[i]),  float(v_arr[i])])
                    except Exception:
                        continue
                return sorted(result, key=lambda x: x[0])
            for v in raw.values():
                if isinstance(v, list) and len(v) > 0:
                    return self._parse_klines(v, symbol)
        log.warning(f"[{symbol}] get_klines: formato desconocido {type(raw)} {str(raw)[:100]}")
        return []

    def _parse_one_kline(self, k) -> list:
        try:
            if isinstance(k, (list, tuple)):
                if len(k) >= 6:
                    return [int(float(k[0])), float(k[1]), float(k[2]),
                            float(k[3]),      float(k[4]), float(k[5])]
                elif len(k) >= 5:
                    return [int(float(k[0])), float(k[1]), float(k[2]),
                            float(k[3]),      float(k[4]), 0.0]
            elif isinstance(k, dict):
                ts = k.get("time") or k.get("t") or k.get("T") or k.get("openTime", 0)
                o  = k.get("open")  or k.get("o") or k.get("O", 0)
                h  = k.get("high")  or k.get("h") or k.get("H", o)
                l  = k.get("low")   or k.get("l") or k.get("L", o)
                c  = k.get("close") or k.get("c") or k.get("C", o)
                v  = k.get("volume") or k.get("v") or k.get("V", 1)
                return [int(float(ts)), float(o), float(h), float(l), float(c), float(v)]
        except Exception as e:
            log.debug(f"_parse_one_kline skip: {e} — k={str(k)[:60]}")
        return None

    # ── Balance ──────────────────────────────────────────────
    async def get_balance(self, force: bool = False) -> float:
        now = time.time()
        if not force and (now - self._bal_ts) < self._BAL_TTL:
            return self._bal_cache
        async with self._bal_lock:
            now = time.time()
            if not force and (now - self._bal_ts) < self._BAL_TTL:
                return self._bal_cache
            try:
                data = await self._get("/openApi/swap/v2/user/balance", signed=True)
                bal  = self._parse_balance(data)
                self._bal_cache = bal
                self._bal_ts    = time.time()
                log.info(f"Balance: {bal:.2f} USDT")
                return bal
            except Exception as e:
                log.error(f"get_balance: {e}")
                return self._bal_cache

    def _parse_balance(self, data) -> float:
        if isinstance(data, dict) and "balance" in data:
            items = data["balance"]
            if isinstance(items, list):
                for a in items:
                    if a.get("asset") == "USDT":
                        return float(a.get("availableMargin", a.get("available", 0)))
            elif isinstance(items, dict):
                return float(items.get("availableMargin", items.get("available", 0)))
        if isinstance(data, list):
            for a in data:
                if isinstance(a, dict) and a.get("asset") == "USDT":
                    return float(a.get("availableMargin", a.get("available", 0)))
        if isinstance(data, dict) and data.get("asset") == "USDT":
            return float(data.get("availableMargin", data.get("available", 0)))
        log.warning(f"Balance formato: {str(data)[:150]}")
        return 0.0

    # ── Tickers ──────────────────────────────────────────────
    async def get_all_tickers(self) -> list:
        try:
            data = await self._get("/openApi/swap/v2/quote/ticker")
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"get_all_tickers: {e}"); return []

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._get("/openApi/swap/v2/quote/ticker", {"symbol": symbol})
        t = data[0] if isinstance(data, list) else data
        return {
            "last"  : float(t.get("lastPrice", 0)),
            "bid"   : float(t.get("bidPrice",  0)),
            "ask"   : float(t.get("askPrice",  0)),
            "volume": float(t.get("volume",    0)),
        }

    # ── Market context ───────────────────────────────────────
    async def get_ofi(self, symbol: str, levels: int = 5) -> float:
        try:
            data  = await self._get("/openApi/swap/v2/quote/depth",
                                    {"symbol": symbol, "limit": levels * 2})
            bids  = data.get("bids", []); asks = data.get("asks", [])
            bid_q = sum(float(b[1]) for b in bids[:levels])
            ask_q = sum(float(a[1]) for a in asks[:levels])
            total = bid_q + ask_q
            return (bid_q - ask_q) / total if total else 0.0
        except Exception: return 0.0

    async def get_funding_rate(self, symbol: str) -> float:
        try:
            data = await self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
            item = data[0] if isinstance(data, list) else data
            return float(item.get("lastFundingRate", 0))
        except Exception: return 0.0

    async def get_open_interest(self, symbol: str) -> float:
        try:
            data = await self._get("/openApi/swap/v2/quote/openInterest", {"symbol": symbol})
            item = data[0] if isinstance(data, list) else data
            return float(item.get("openInterest", 0))
        except Exception: return 0.0

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

    # ── Positions / Orders ───────────────────────────────────
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

    async def place_order(self, symbol, side, size, leverage, sl_price, tp_price=None,
                          use_maker=True, maker_timeout=30, maker_offset_pct=0.02):
        await self.set_leverage(symbol, leverage, side)
        await asyncio.sleep(0.2)
        bingx_side = "BUY" if side == "LONG" else "SELL"
        if use_maker:
            order = await self._place_maker(symbol, bingx_side, side, size,
                                            sl_price, tp_price, maker_timeout, maker_offset_pct)
            if order: return order
        return await self._place_market(symbol, bingx_side, side, size, sl_price, tp_price)

    async def _place_maker(self, symbol, bingx_side, pos_side, size,
                           sl_price, tp_price, timeout, offset_pct):
        try:
            ticker = await self.get_ticker(symbol)
            lp = round(ticker["ask"]*(1-offset_pct/100), 6) if bingx_side=="BUY" \
                 else round(ticker["bid"]*(1+offset_pct/100), 6)
            params = {"symbol": symbol, "side": bingx_side, "positionSide": pos_side,
                      "type": "LIMIT", "price": f"{lp:.6f}",
                      "quantity": f"{size:.4f}", "timeInForce": "PostOnly"}
            if sl_price: params["stopLossPrice"]   = f"{sl_price:.6f}"
            if tp_price: params["takeProfitPrice"] = f"{tp_price:.6f}"
            data     = await self._post("/openApi/swap/v2/trade/order", params)
            order_id = data.get("order", {}).get("orderId") or data.get("orderId")
            if not order_id: return None
            for _ in range(timeout):
                await asyncio.sleep(1)
                st = await self._get_order_status(symbol, order_id)
                if st == "FILLED": return data
                if st in ("CANCELLED","EXPIRED","REJECTED"): return None
            await self._cancel_order(symbol, order_id)
            return None
        except Exception as e:
            log.warning(f"maker {symbol}: {e}"); return None

    async def _place_market(self, symbol, bingx_side, pos_side, size, sl_price, tp_price):
        params = {"symbol": symbol, "side": bingx_side, "positionSide": pos_side,
                  "type": "MARKET", "quantity": f"{size:.4f}"}
        if sl_price: params["stopLossPrice"]   = f"{sl_price:.4f}"
        if tp_price: params["takeProfitPrice"] = f"{tp_price:.4f}"
        try:
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"market {symbol}: {e}"); return None

    async def _get_order_status(self, symbol, order_id):
        try:
            data = await self._get("/openApi/swap/v2/trade/order",
                                   {"symbol": symbol, "orderId": order_id}, signed=True)
            o = data[0] if isinstance(data, list) else data
            return o.get("status", "UNKNOWN")
        except Exception: return "UNKNOWN"

    async def _cancel_order(self, symbol, order_id):
        try:
            await self._post("/openApi/swap/v2/trade/cancelOrder",
                             {"symbol": symbol, "orderId": order_id})
        except Exception as e:
            log.warning(f"cancel {symbol}: {e}")

    async def close_position(self, symbol, side):
        positions = await self.get_positions(symbol)
        size = 0.0
        for p in positions:
            if p.get("positionSide")==side and float(p.get("positionAmt",0))!=0:
                size = abs(float(p["positionAmt"])); break
        if size == 0: return None
        params = {"symbol": symbol, "side": "SELL" if side=="LONG" else "BUY",
                  "positionSide": side, "type": "MARKET",
                  "quantity": f"{size:.4f}", "reduceOnly": "true"}
        try:
            return await self._post("/openApi/swap/v2/trade/order", params)
        except Exception as e:
            log.error(f"close {symbol}: {e}"); return None
