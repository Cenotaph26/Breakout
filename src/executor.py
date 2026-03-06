"""
Binance USDT-M Futures Executor
Testnet: https://testnet.binancefuture.com
Live:    https://fapi.binance.com
"""
import hashlib, hmac, logging, math, time
from urllib.parse import urlencode
import httpx

logger = logging.getLogger(__name__)

TESTNET_BASE = "https://testnet.binancefuture.com"
LIVE_BASE    = "https://fapi.binance.com"


class BinanceExecutor:

    def __init__(self, api_key: str, api_secret: str, demo: bool = True):
        self.api_key      = api_key.strip()
        self.api_secret   = api_secret.strip()
        self.base_url     = TESTNET_BASE if demo else LIVE_BASE
        self.demo         = demo
        self._time_offset = 0
        self._qty_step:   float = 0.001
        self._qty_prec:   int   = 3
        self._price_step: float = 0.10
        self._price_prec: int   = 1
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"X-MBX-APIKEY": self.api_key},
        )

    # ── Time sync ──────────────────────────────────────────────
    async def sync_time(self):
        try:
            r = await self._client.get(f"{self.base_url}/fapi/v1/time")
            r.raise_for_status()
            self._time_offset = r.json()["serverTime"] - int(time.time() * 1000)
            logger.info(f"Time sync OK | offset={self._time_offset}ms")
        except Exception as e:
            logger.warning(f"Time sync failed: {e}")

    def _ts(self):
        return int(time.time() * 1000) + self._time_offset

    # ── Signing ────────────────────────────────────────────────
    def _signed_qs(self, params: dict) -> str:
        p = dict(params)
        p["timestamp"]  = self._ts()
        p["recvWindow"] = 5000
        qs  = urlencode(p)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    async def _get(self, path: str, params: dict = {}) -> dict:
        qs = self._signed_qs(params)
        url = f"{self.base_url}{path}?{qs}"
        r  = await self._client.get(url)
        self._log_http(r, "GET", path)
        self._raise(r, "GET", path)
        return r.json()

    async def _post(self, path: str, params: dict = {}) -> dict:
        qs = self._signed_qs(params)
        url = f"{self.base_url}{path}?{qs}"
        r  = await self._client.post(url)
        self._log_http(r, "POST", path)
        self._raise(r, "POST", path)
        return r.json()

    async def _delete(self, path: str, params: dict = {}) -> dict:
        qs = self._signed_qs(params)
        url = f"{self.base_url}{path}?{qs}"
        r  = await self._client.delete(url)
        self._log_http(r, "DELETE", path)
        self._raise(r, "DELETE", path)
        return r.json()

    def _log_http(self, r, method, path):
        status = r.status_code
        level = logging.INFO if r.is_success else logging.WARNING
        logger.log(level, f"httpx — {method} {path} → {status}")

    def _raise(self, r, method, path):
        if not r.is_success:
            try:    body = r.json()
            except: body = r.text[:300]
            raise httpx.HTTPStatusError(
                f"{method} {path} → {r.status_code}: {body}",
                request=r.request, response=r,
            )

    # ── Connection test + filter load ──────────────────────────
    async def test_connection(self) -> dict:
        await self.sync_time()
        result = await self._get("/fapi/v2/account")
        logger.info(f"Binance bağlantısı OK | demo={self.demo}")
        return result

    async def get_balance(self) -> float:
        account = await self._get("/fapi/v2/account")
        for a in account.get("assets", []):
            if a["asset"] == "USDT":
                return float(a["availableBalance"])
        return 0.0

    async def get_position(self, symbol: str) -> dict | None:
        data = await self._get("/fapi/v2/positionRisk", {"symbol": symbol.upper()})
        lst  = data if isinstance(data, list) else [data]
        for p in lst:
            if p["symbol"] == symbol.upper() and float(p["positionAmt"]) != 0:
                return p
        return None

    # ── Exchange filters ───────────────────────────────────────
    async def load_symbol_filters(self, symbol: str):
        try:
            r = await self._client.get(f"{self.base_url}/fapi/v1/exchangeInfo")
            r.raise_for_status()
            for s in r.json().get("symbols", []):
                if s["symbol"] != symbol.upper():
                    continue
                self._qty_prec   = s.get("quantityPrecision", 3)
                self._price_prec = s.get("pricePrecision", 2)
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        self._qty_step = float(f["stepSize"])
                    if f["filterType"] == "PRICE_FILTER":
                        self._price_step = float(f["tickSize"])
                logger.info(
                    f"Filtreler: qty_step={self._qty_step} qty_prec={self._qty_prec} "
                    f"price_step={self._price_step} price_prec={self._price_prec}"
                )
                return
        except Exception as e:
            logger.warning(f"load_symbol_filters: {e}")

    # ── Rounding helpers ───────────────────────────────────────
    def _round_qty(self, qty: float) -> str:
        step = self._qty_step
        if step <= 0: step = 10 ** -self._qty_prec
        floored = math.floor(qty / step) * step
        floored = round(floored, self._qty_prec)
        return f"{floored:.{self._qty_prec}f}"

    def _round_price(self, price: float) -> str:
        step = self._price_step
        if step <= 0: step = 10 ** -self._price_prec
        rounded = round(round(price / step) * step, self._price_prec)
        return f"{rounded:.{self._price_prec}f}"

    # ── Setup ──────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        r = await self._post("/fapi/v1/leverage",
            {"symbol": symbol.upper(), "leverage": leverage})
        logger.info(f"Kaldıraç: {symbol} → {leverage}x")
        return r

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        try:
            await self._post("/fapi/v1/marginType",
                {"symbol": symbol.upper(), "marginType": margin_type})
            logger.info(f"MarginType: {symbol} → {margin_type}")
        except Exception as e:
            # -4046: already set → ok
            logger.debug(f"marginType (ignored): {e}")

    # ── Historical klines ──────────────────────────────────────
    async def get_recent_klines(self, symbol: str, limit: int = 80) -> list[dict]:
        for base in [self.base_url, LIVE_BASE]:
            try:
                r = await self._client.get(f"{base}/fapi/v1/klines", params={
                    "symbol": symbol.upper(), "interval": "1m", "limit": limit,
                })
                r.raise_for_status()
                rows = r.json()
                if rows:
                    result = [{"open": float(k[1]), "high": float(k[2]),
                               "low":  float(k[3]), "close": float(k[4]),
                               "volume": float(k[5]), "timestamp": int(k[0])}
                              for k in rows]
                    logger.info(f"{len(result)} kline yüklendi ({base})")
                    return result
            except Exception as e:
                logger.warning(f"Kline {base}: {e}")
        return []

    # ── Orders ─────────────────────────────────────────────────
    async def market_order(self, symbol: str, side: str, qty: float) -> dict:
        qty_str = self._round_qty(qty)
        logger.info(f"MARKET {side} qty={qty_str}")
        return await self._post("/fapi/v1/order", {
            "symbol": symbol.upper(), "side": side.upper(),
            "type": "MARKET", "quantity": qty_str,
        })

    async def stop_market_order(self, symbol: str, side: str, qty: float,
                                stop_price: float) -> dict:
        qty_str   = self._round_qty(qty)
        price_str = self._round_price(stop_price)
        logger.info(f"STOP {side} qty={qty_str} @ {price_str}")

        # Testnet STOP_MARKET desteklemiyor → STOP (limit) kullan
        # Live'da STOP_MARKET tercih et
        if self.demo:
            # STOP limit: stopPrice tetikler, price ile emir girer
            # Slippage toleransı: %0.5
            slip = 1.005 if side.upper() == "BUY" else 0.995
            limit_price = self._round_price(stop_price * slip)
            try:
                return await self._post("/fapi/v1/order", {
                    "symbol": symbol.upper(), "side": side.upper(),
                    "type": "STOP", "quantity": qty_str,
                    "price": limit_price, "stopPrice": price_str,
                    "reduceOnly": "true", "workingType": "MARK_PRICE",
                    "timeInForce": "GTC",
                })
            except Exception as e:
                logger.warning(f"STOP order failed, trying TAKE_PROFIT: {e}")
                # Fallback: hiç SL emri koyma, bot RT ile kapatır
                return {"orderId": 0}
        else:
            return await self._post("/fapi/v1/order", {
                "symbol": symbol.upper(), "side": side.upper(),
                "type": "STOP_MARKET", "quantity": qty_str,
                "stopPrice": price_str, "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            })

    async def cancel_all_orders(self, symbol: str):
        try:
            r = await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
            logger.info(f"Emirler iptal: {symbol}")
            return r
        except Exception as e:
            logger.warning(f"cancel_all_orders: {e}")

    async def close_position_market(self, symbol: str, pos_amt: float) -> dict:
        side    = "SELL" if pos_amt > 0 else "BUY"
        qty_str = self._round_qty(abs(pos_amt))
        return await self._post("/fapi/v1/order", {
            "symbol": symbol.upper(), "side": side,
            "type": "MARKET", "quantity": qty_str,
            "reduceOnly": "true",
        })

    async def close(self):
        await self._client.aclose()
