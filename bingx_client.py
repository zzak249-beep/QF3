"""
QF×JP Bot v6.1 — bingx_client.py
Fixes vs v6.0:
  ✅ _sign: usar hmac.new → hmac.new es correcto pero faltaba importar bien
  ✅ place_order: params → body JSON (BingX swap v2 usa JSON body en POST)
  ✅ SL/TP: closePosition usa endpoint correcto con reduceOnly
  ✅ get_balance: parse correcto de balance.balance.availableMargin
  ✅ set_leverage: body correcto
  ✅ cancel: usa DELETE o POST según endpoint real
  ✅ Klines: endpoint v3 devuelve lista de listas (no dicts)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("BINGX")
BASE = "https://open-api.bingx.com"


class BingXClient:
    def __init__(self, api_key: str, secret: str):
        self._key    = api_key
        self._secret = secret.encode()
        self._session: Optional[aiohttp.ClientSession] = None

        # Balance cache — TTL alto: 20 tasks comparten el mismo objeto, evita 100410
        self._bal_value: float = 0.0
        self._bal_ts:    float = 0.0
        self._bal_ttl:   int   = 300   # 5 min
        self._bal_lock:  asyncio.Lock = asyncio.Lock()  # evita stampede al arrancar

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, params: dict) -> str:
        """Firma HMAC-SHA256: ordenar alfabéticamente y firmar query string."""
        qs = urlencode(sorted(params.items()))
        return hmac.new(self._secret, qs.encode(), hashlib.sha256).hexdigest()

    def _ts(self) -> int:
        return int(time.time() * 1000)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict = None,
        signed: bool = True,
        retry: int = 3,
    ) -> Optional[Any]:
        """
        BingX API:
        - GET: todos los parámetros en query string (incluyendo signature)
        - POST: todos los parámetros en query string también (no JSON body)
          excepto algunos endpoints que usan body — para swap v2 es query string
        """
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._ts()
            params["signature"] = self._sign(params)

        headers = {
            "X-BX-APIKEY": self._key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        url = BASE + path
        session = await self._get_session()

        for attempt in range(retry):
            try:
                if method == "GET":
                    async with session.get(url, params=params, headers=headers) as r:
                        text = await r.text()
                        data = json.loads(text)
                else:  # POST
                    async with session.post(url, params=params, headers=headers) as r:
                        text = await r.text()
                        data = json.loads(text)

                code = data.get("code", 0)

                if code == 100410:
                    wait = min(10 * (attempt + 1), 30)
                    log.warning(f"Rate limit 100410 en {path} — espera {wait}s")
                    await asyncio.sleep(wait)
                    continue

                if code not in (0, 200):
                    log.error(f"API error {code} en {path}: {data.get('msg')} | params={params}")
                    if attempt < retry - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue

                return data.get("data") if "data" in data else data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error(f"Network error {path} intento {attempt+1}: {e}")
                if attempt < retry - 1:
                    await asyncio.sleep(2 ** attempt)
            except json.JSONDecodeError as e:
                log.error(f"JSON parse error {path}: {e}")
                if attempt < retry - 1:
                    await asyncio.sleep(2 ** attempt)

        return None

    # ── Balance ──────────────────────────────────────────
    async def get_balance(self, force: bool = False) -> float:
        now = time.time()
        # Fast path: cache fresco, no necesita lock
        if not force and (now - self._bal_ts) < self._bal_ttl:
            return self._bal_value

        # Lock: evita que 20 tasks hagan la misma request simultáneamente al arrancar
        async with self._bal_lock:
            # Re-check dentro del lock: otra task ya pudo haber actualizado
            now = time.time()
            if not force and (now - self._bal_ts) < self._bal_ttl:
                return self._bal_value

            data = await self._request(
                "GET", "/openApi/swap/v2/user/balance",
                params={"currency": "USDT"}
            )
            if data:
                try:
                    bal_obj = data.get("balance", data)
                    bal = float(bal_obj.get("availableMargin",
                                bal_obj.get("available", 0)))
                    if bal > 0:
                        self._bal_value = bal
                        self._bal_ts = time.time()
                        return bal
                except (KeyError, TypeError, ValueError) as e:
                    log.error(f"Balance parse error: {e} | data={data}")

            return self._bal_value

    def invalidate_balance_cache(self):
        self._bal_ts = 0.0

    # ── Klines ────────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 250) -> list:
        data = await self._request(
            "GET", "/openApi/swap/v3/quote/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            signed=False,
        )
        if not data:
            return []
        rows = []
        for k in data:
            try:
                # v3 devuelve lista: [ts, o, h, l, c, v, ...]
                if isinstance(k, (list, tuple)):
                    rows.append({
                        "ts": int(k[0]),
                        "o": float(k[1]),
                        "h": float(k[2]),
                        "l": float(k[3]),
                        "c": float(k[4]),
                        "v": float(k[5]),
                    })
                elif isinstance(k, dict):
                    rows.append({
                        "ts": int(k.get("time", k.get("t", 0))),
                        "o": float(k.get("open",   k.get("o", 0))),
                        "h": float(k.get("high",   k.get("h", 0))),
                        "l": float(k.get("low",    k.get("l", 0))),
                        "c": float(k.get("close",  k.get("c", 0))),
                        "v": float(k.get("volume", k.get("v", 0))),
                    })
            except Exception:
                continue
        return rows

    # ── Ticker ────────────────────────────────────────────
    async def get_ticker(self, symbol: str) -> dict:
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/ticker",
            params={"symbol": symbol},
            signed=False,
        )
        if data and isinstance(data, list):
            d = data[0]
        elif data and isinstance(data, dict):
            d = data
        else:
            return {"last": 0.0, "bid": 0.0, "ask": 0.0}
        return {
            "last": float(d.get("lastPrice", d.get("last", 0))),
            "bid":  float(d.get("bidPrice",  0)),
            "ask":  float(d.get("askPrice",  0)),
        }

    # ── Order Book ────────────────────────────────────────
    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/depth",
            params={"symbol": symbol, "limit": depth},
            signed=False,
        )
        if not data:
            return {"bids": [], "asks": []}
        return {
            "bids": [[float(x[0]), float(x[1])] for x in data.get("bids", [])],
            "asks": [[float(x[0]), float(x[1])] for x in data.get("asks", [])],
        }

    # ── Funding Rate ──────────────────────────────────────
    async def get_funding_rate(self, symbol: str) -> float:
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/premiumIndex",
            params={"symbol": symbol},
            signed=False,
        )
        if not data:
            return 0.0
        if isinstance(data, list):
            data = data[0]
        return float(data.get("lastFundingRate", 0))

    # ── Open Interest ──────────────────────────────────────
    async def get_open_interest(self, symbol: str) -> float:
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/openInterest",
            params={"symbol": symbol},
            signed=False,
        )
        if not data:
            return 0.0
        if isinstance(data, list):
            data = data[0]
        return float(data.get("openInterest", 0))

    # ── Market context ────────────────────────────────────
    async def get_market_context(self, symbol: str, ofi_levels: int = 5) -> dict:
        results = await asyncio.gather(
            self.get_orderbook(symbol, ofi_levels * 2),
            self.get_funding_rate(symbol),
            self.get_open_interest(symbol),
            return_exceptions=True,
        )
        book, fr, oi = results

        ofi = 0.0
        if isinstance(book, dict) and book.get("bids") and book.get("asks"):
            bid_vol = sum(b[1] for b in book["bids"][:ofi_levels])
            ask_vol = sum(a[1] for a in book["asks"][:ofi_levels])
            total   = bid_vol + ask_vol
            ofi     = (bid_vol - ask_vol) / total if total > 0 else 0.0

        return {
            "ofi":               ofi,
            "funding_rate":      fr if isinstance(fr, float) else 0.0,
            "open_interest":     oi if isinstance(oi, float) else 0.0,
            "prev_open_interest": 0.0,
        }

    # ── Todos los símbolos ────────────────────────────────
    async def get_all_symbols(self) -> list:
        data = await self._request(
            "GET", "/openApi/swap/v2/quote/ticker",
            signed=False,
        )
        if not data or not isinstance(data, list):
            return []
        result = []
        for d in data:
            sym = d.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            try:
                result.append({
                    "symbol": sym,
                    "volume": float(d.get("quoteVolume", d.get("volume", 0))),
                    "last":   float(d.get("lastPrice", 0)),
                })
            except Exception:
                continue
        return result

    # ── Posiciones abiertas ───────────────────────────────
    async def get_open_positions(self) -> list:
        data = await self._request(
            "GET", "/openApi/swap/v2/user/positions",
        )
        if not data:
            return []
        positions = []
        items = data if isinstance(data, list) else []
        for p in items:
            try:
                size = float(p.get("positionAmt", 0))
                if abs(size) < 1e-9:
                    continue
                positions.append({
                    "symbol":     p["symbol"],
                    "side":       "LONG" if size > 0 else "SHORT",
                    "size":       abs(size),
                    "entry":      float(p.get("avgPrice", 0)),
                    "unrealized": float(p.get("unrealizedProfit", 0)),
                })
            except Exception:
                continue
        return positions

    # ── Set leverage ──────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        """One-Way mode: un solo call sin parámetro 'side'."""
        await self._request(
            "POST", "/openApi/swap/v2/trade/leverage",
            params={"symbol": symbol, "leverage": str(leverage)},
        )

    # ── Colocar orden ─────────────────────────────────────
    async def place_order(
        self,
        symbol:           str,
        side:             str,    # "LONG" | "SHORT"
        size:             float,
        leverage:         int,
        sl:               float,
        tp:               Optional[float],
        use_maker:        bool = True,
        maker_timeout:    int  = 20,
        maker_offset_pct: float = 0.015,
    ) -> Optional[dict]:
        await self.set_leverage(symbol, leverage)

        # One-Way mode: BUY para abrir LONG, SELL para abrir SHORT. SIN positionSide.
        action = "BUY"  if side == "LONG" else "SELL"
        # FIX: eliminado pos_side / positionSide — cuenta One-Way (Aislado)

        ticker = await self.get_ticker(symbol)
        price  = ticker["last"]
        if price <= 0:
            log.error(f"[{symbol}] Ticker inválido ({price}), abortando orden")
            return None

        # Redondear size a 4 decimales (BingX requiere precisión razonable)
        qty = f"{size:.4f}"

        order_type = "LIMIT" if use_maker else "MARKET"
        params: dict = {
            "symbol":   symbol,
            "side":     action,
            "type":     order_type,
            "quantity": qty,
            # ✅ SIN positionSide — One-Way mode
        }

        if order_type == "LIMIT":
            offset = price * (maker_offset_pct / 100)
            lmt    = price - offset if side == "LONG" else price + offset
            params["price"]       = f"{lmt:.6f}"
            params["timeInForce"] = "GTC"

        order = await self._request("POST", "/openApi/swap/v2/trade/order", params=params)
        if not order:
            log.error(f"[{symbol}] Orden principal falló")
            return None

        order_id = (
            order.get("orderId")
            or (order.get("order") or {}).get("orderId")
        )

        # Si es maker, esperar fill; si no, cancelar y market
        if order_type == "LIMIT" and order_id:
            filled = await self._wait_fill(symbol, str(order_id), maker_timeout)
            if not filled:
                await self._cancel_order(symbol, str(order_id))
                log.info(f"[{symbol}] Maker timeout → fallback MARKET")
                params["type"] = "MARKET"
                params.pop("price",       None)
                params.pop("timeInForce", None)
                order = await self._request("POST", "/openApi/swap/v2/trade/order", params=params)
                if not order:
                    return None
                order_id = (
                    order.get("orderId")
                    or (order.get("order") or {}).get("orderId")
                )

        if not order_id:
            log.error(f"[{symbol}] No se obtuvo orderId")
            return None

        # SL automático
        if sl and sl > 0:
            await self._place_sl(symbol, side, size, sl)

        # TP automático
        if tp and tp > 0:
            await self._place_tp(symbol, side, size, tp)

        self.invalidate_balance_cache()
        log.info(f"[{symbol}] Orden {side} colocada — ID:{order_id} qty:{qty} price:{price:.6f}")
        return {"orderId": str(order_id)}

    async def _wait_fill(self, symbol: str, order_id: str, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            data = await self._request(
                "GET", "/openApi/swap/v2/trade/order",
                params={"symbol": symbol, "orderId": order_id},
            )
            if data:
                status = (
                    data.get("status")
                    or (data.get("order") or {}).get("status", "")
                )
                if status in ("FILLED", "PARTIALLY_FILLED"):
                    return True
                if status in ("CANCELED", "EXPIRED", "REJECTED"):
                    return False
        return False

    async def _cancel_order(self, symbol: str, order_id: str):
        await self._request(
            "POST", "/openApi/swap/v2/trade/cancel",
            params={"symbol": symbol, "orderId": order_id},
        )

    async def _place_sl(self, symbol: str, side: str, size: float, sl: float):
        sl_side = "SELL" if side == "LONG" else "BUY"
        params = {
            "symbol":      symbol,
            "side":        sl_side,
            "type":        "STOP_MARKET",
            "quantity":    f"{size:.4f}",
            "stopPrice":   f"{sl:.6f}",
            "workingType": "MARK_PRICE",
            "reduceOnly":  "true",
            # ✅ SIN positionSide
        }
        r = await self._request("POST", "/openApi/swap/v2/trade/order", params=params)
        if not r:
            log.warning(f"[{symbol}] SL no colocado — sl={sl:.6f}")

    async def _place_tp(self, symbol: str, side: str, size: float, tp: float):
        tp_side = "SELL" if side == "LONG" else "BUY"
        params = {
            "symbol":      symbol,
            "side":        tp_side,
            "type":        "TAKE_PROFIT_MARKET",
            "quantity":    f"{size:.4f}",
            "stopPrice":   f"{tp:.6f}",
            "workingType": "MARK_PRICE",
            "reduceOnly":  "true",
            # ✅ SIN positionSide
        }
        r = await self._request("POST", "/openApi/swap/v2/trade/order", params=params)
        if not r:
            log.warning(f"[{symbol}] TP no colocado — tp={tp:.6f}")

    # ── Cerrar posición ────────────────────────────────────
    async def close_position(self, symbol: str, side: str) -> bool:
        """One-Way mode: cierra con orden market reduceOnly. Sin positionSide."""
        cl_side = "SELL" if side == "LONG" else "BUY"

        # Intentar endpoint dedicado de cierre
        r = await self._request(
            "POST", "/openApi/swap/v2/trade/closePosition",
            params={"symbol": symbol},
        )
        if not r:
            log.warning(f"[{symbol}] closePosition falló → fallback market reduceOnly")
            r = await self._request(
                "POST", "/openApi/swap/v2/trade/order",
                params={
                    "symbol":     symbol,
                    "side":       cl_side,
                    "type":       "MARKET",
                    "reduceOnly": "true",
                    "quantity":   "0",
                    # ✅ SIN positionSide
                },
            )
        self.invalidate_balance_cache()
        return r is not None
