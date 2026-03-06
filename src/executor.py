"""
Binance USDT-M Futures Executor
Testnet REST: https://testnet.binancefuture.com
Live REST:    https://fapi.binance.com
"""

import hashlib, hmac, logging, time
from urllib.parse import urlencode
import httpx

logger = logging.getLogger(__name__)

TESTNET_BASE = "https://testnet.binancefuture.com"
LIVE_BASE    = "https://fapi.binance.com"

# Geçmiş kline için gerçek exchange kullan (testnet kline daha az güvenilir)
KLINE_BASE   = "https://fapi.binance.com"


class BinanceExecutor:

    def __init__(self, api_key: str, api_secret: str, demo: bool = True):
        self.api_key      = api_key.strip()
        self.api_secret   = api_secret.strip()
        self.base_url     = TESTNET_BASE if demo else LIVE_BASE
        self.demo         = demo
        self._time_offset = 0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"X-MBX-APIKEY": self.api_key},
        )

    # ── Time sync ─────────────────────────────────────────────
    async def sync_time(self):
        try:
            r = await self._client.get(f"{self.base_url}/fapi/v1/time")
            r.raise_for_status()
            self._time_offset = r.json()["serverTime"] - int(time.time()*1000)
            logger.info(f"Time sync OK | offset={self._time_offset}ms")
        except Exception as e:
            logger.warning(f"Time sync failed: {e}")

    def _ts(self):
        return int(time.time()*1000) + self._time_offset

    # ── Signing ───────────────────────────────────────────────
    def _signed_qs(self, params: dict) -> str:
        p = dict(params)
        p["timestamp"]  = self._ts()
        p["recvWindow"] = 5000
        qs  = urlencode(p)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    async def _get(self, path: str, params: dict = {}) -> dict:
        qs = self._signed_qs(params)
        r  = await self._client.get(f"{self.base_url}{path}?{qs}")
        self._raise(r, "GET", path)
        return r.json()

    async def _post(self, path: str, params: dict = {}) -> dict:
        qs = self._signed_qs(params)
        r  = await self._client.post(f"{self.base_url}{path}?{qs}")
        self._raise(r, "POST", path)
        return r.json()

    async def _delete(self, path: str, params: dict = {}) -> dict:
        qs = self._signed_qs(params)
        r  = await self._client.delete(f"{self.base_url}{path}?{qs}")
        self._raise(r, "DELETE", path)
        return r.json()

    def _raise(self, r, method, path):
        if not r.is_success:
            try:    body = r.json()
            except: body = r.text[:200]
            raise httpx.HTTPStatusError(
                f"{method} {path} → {r.status_code}: {body}",
                request=r.request, response=r,
            )

    # ── Connection test ───────────────────────────────────────
    async def test_connection(self) -> dict:
        await self.sync_time()
        result = await self._get("/fapi/v2/account")
        assets = [a for a in result.get("assets",[]) if float(a.get("walletBalance",0))>0]
        logger.info(f"✅ Binance bağlantısı OK | demo={self.demo} | bakiyeli varlık={len(assets)}")
        return result

    async def get_balance(self) -> float:
        account = await self._get("/fapi/v2/account")
        for a in account.get("assets",[]):
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

    # ── Setup ─────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        r = await self._post("/fapi/v1/leverage", {"symbol": symbol.upper(), "leverage": leverage})
        logger.info(f"Kaldıraç: {symbol} → {leverage}x")
        return r

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        try:
            await self._post("/fapi/v1/marginType", {"symbol": symbol.upper(), "marginType": margin_type})
        except httpx.HTTPStatusError as e:
            if "-4046" not in str(e): logger.debug(f"marginType: {e}")

    # ── Exchange info ─────────────────────────────────────────
    async def get_symbol_info(self, symbol: str) -> dict | None:
        try:
            r = await self._client.get(f"{self.base_url}/fapi/v1/exchangeInfo")
            r.raise_for_status()
            for s in r.json().get("symbols",[]):
                if s["symbol"] == symbol.upper(): return s
        except Exception as e:
            logger.warning(f"exchangeInfo: {e}")
        return None

    async def get_min_qty(self, symbol: str) -> float:
        info = await self.get_symbol_info(symbol)
        if not info: return 0.001
        for f in info.get("filters",[]):
            if f["filterType"] == "LOT_SIZE": return float(f["minQty"])
        return 0.001

    async def get_qty_precision(self, symbol: str) -> int:
        info = await self.get_symbol_info(symbol)
        return info.get("quantityPrecision", 3) if info else 3

    # ── Historical klines (gerçek exchange = daha güvenilir) ──
    async def get_recent_klines(self, symbol: str, limit: int = 80) -> list[dict]:
        """
        Geçmiş mumları çek. Testnet için bile gerçek exchange kullan —
        fiyat yakın, kline verisi her zaman hazır.
        """
        try:
            # Önce testnet'i dene, başarısız olursa gerçek exchange
            for base in [self.base_url, KLINE_BASE]:
                try:
                    r = await self._client.get(
                        f"{base}/fapi/v1/klines",
                        params={"symbol": symbol.upper(), "interval": "1m", "limit": limit},
                    )
                    r.raise_for_status()
                    rows = r.json()
                    if rows:
                        result = [{"open": float(k[1]), "high": float(k[2]),
                                   "low":  float(k[3]), "close": float(k[4]),
                                   "volume": float(k[5]), "timestamp": int(k[0])}
                                  for k in rows]
                        logger.info(f"📊 {len(result)} kline çekildi ({base})")
                        return result
                except Exception as e:
                    logger.warning(f"Kline {base}: {e}")
            return []
        except Exception as e:
            logger.warning(f"get_recent_klines: {e}")
            return []

    # ── Orders ────────────────────────────────────────────────
    def _fmt(self, qty: float, prec: int) -> str:
        return f"{qty:.{prec}f}"

    async def market_order(self, symbol: str, side: str, qty: float, prec: int = 3) -> dict:
        r = await self._post("/fapi/v1/order", {
            "symbol": symbol.upper(), "side": side.upper(),
            "type": "MARKET", "quantity": self._fmt(qty, prec),
        })
        logger.info(f"✅ MARKET {side} {qty} {symbol} | orderId={r.get('orderId')} avgPrice={r.get('avgPrice')}")
        return r

    async def stop_market_order(self, symbol: str, side: str, qty: float,
                                stop_price: float, prec: int = 3) -> dict:
        r = await self._post("/fapi/v1/order", {
            "symbol": symbol.upper(), "side": side.upper(),
            "type": "STOP_MARKET", "quantity": self._fmt(qty, prec),
            "stopPrice": f"{stop_price:.2f}", "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        })
        logger.info(f"🛡 STOP {side} {qty} @ {stop_price:.2f} | orderId={r.get('orderId')}")
        return r

    async def cancel_all_orders(self, symbol: str):
        try:
            r = await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
            logger.info(f"🗑 Emirler iptal: {symbol}")
            return r
        except Exception as e:
            logger.warning(f"cancel orders: {e}")

    async def close_position_market(self, symbol: str, pos_amt: float, prec: int = 3) -> dict:
        side = "SELL" if pos_amt > 0 else "BUY"
        qty  = abs(pos_amt)
        r = await self._post("/fapi/v1/order", {
            "symbol": symbol.upper(), "side": side,
            "type": "MARKET", "quantity": self._fmt(qty, prec),
            "reduceOnly": "true",
        })
        logger.info(f"🔒 Pozisyon kapatıldı: {symbol} {side} {qty:.{prec}f}")
        return r

    async def close(self):
        await self._client.aclose()
