"""
Binance USDT-M Futures Executor
Testnet: https://testnet.binancefuture.com
Canlı:   https://fapi.binance.com

Önemli notlar:
- Tüm imzalı isteklerde timestamp + signature query string'de gitmeli
- POST emirleri de parametreleri query string olarak alır (body değil)
- hmac.new() Python 2, Python 3'te hmac.new() çalışır ama doğrusu hmac.new()
- Sunucu saati farkı için /fapi/v1/time ile senkronize edilir
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

TESTNET_BASE = "https://testnet.binancefuture.com"
LIVE_BASE    = "https://fapi.binance.com"


class BinanceExecutor:

    def __init__(self, api_key: str, api_secret: str, demo: bool = True):
        self.api_key    = api_key
        self.api_secret = api_secret.strip()
        self.base_url   = TESTNET_BASE if demo else LIVE_BASE
        self.demo       = demo
        self._time_offset = 0  # ms, sunucu-yerel saat farkı
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"X-MBX-APIKEY": self.api_key.strip()},
        )

    # ── Time sync ─────────────────────────────────────────────────────────────

    async def sync_time(self):
        """Binance sunucu saatiyle senkronize ol — 401 hatalarının çoğunu çözer."""
        try:
            url = f"{self.base_url}/fapi/v1/time"
            r = await self._client.get(url)
            r.raise_for_status()
            server_ms = r.json()["serverTime"]
            local_ms  = int(time.time() * 1000)
            self._time_offset = server_ms - local_ms
            logger.info(f"Saat senkronizasyonu tamamlandı. Offset: {self._time_offset}ms")
        except Exception as e:
            logger.warning(f"Saat senkronizasyonu başarısız: {e}")

    def _ts(self) -> int:
        return int(time.time() * 1000) + self._time_offset

    # ── Signing ───────────────────────────────────────────────────────────────

    def _build_query(self, params: dict) -> str:
        """Parametreleri timestamp ekleyerek query string'e çevir, imzala."""
        params = dict(params)                    # kopya al
        params["timestamp"] = self._ts()
        params["recvWindow"] = 5000
        qs  = urlencode(params)                  # sıralı & deterministic
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{qs}&signature={sig}"

    # ── Generic requests ──────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict | None = None) -> dict:
        qs  = self._build_query(params or {})
        url = f"{self.base_url}{path}?{qs}"
        r   = await self._client.get(url)
        self._check(r, "GET", path)
        return r.json()

    async def _post(self, path: str, params: dict | None = None) -> dict:
        """
        Binance Futures POST emirleri parametreleri QUERY STRING olarak alır,
        request body boş olmalıdır.
        """
        qs  = self._build_query(params or {})
        url = f"{self.base_url}{path}?{qs}"
        r   = await self._client.post(url)       # body yok
        self._check(r, "POST", path)
        return r.json()

    async def _delete(self, path: str, params: dict | None = None) -> dict:
        qs  = self._build_query(params or {})
        url = f"{self.base_url}{path}?{qs}"
        r   = await self._client.delete(url)
        self._check(r, "DELETE", path)
        return r.json()

    def _check(self, r: httpx.Response, method: str, path: str):
        if not r.is_success:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise httpx.HTTPStatusError(
                f"{method} {path} → {r.status_code}: {body}",
                request=r.request,
                response=r,
            )

    # ── Connection test ───────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        """Bağlantıyı ve API key'i doğrula."""
        await self.sync_time()
        result = await self._get("/fapi/v2/account")
        logger.info(f"✅ Binance bağlantısı başarılı | demo={self.demo} | "
                    f"bakiye sayısı={len(result.get('assets',[]))}")
        return result

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """USDT kullanılabilir bakiye."""
        account = await self._get("/fapi/v2/account")
        for a in account.get("assets", []):
            if a["asset"] == "USDT":
                return float(a["availableBalance"])
        return 0.0

    async def get_position(self, symbol: str) -> dict | None:
        data = await self._get("/fapi/v2/positionRisk", {"symbol": symbol.upper()})
        for p in (data if isinstance(data, list) else [data]):
            if p["symbol"] == symbol.upper() and float(p["positionAmt"]) != 0:
                return p
        return None

    # ── Setup ─────────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        result = await self._post("/fapi/v1/leverage", {
            "symbol":   symbol.upper(),
            "leverage": leverage,
        })
        logger.info(f"Kaldıraç: {symbol} → {leverage}x")
        return result

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        try:
            await self._post("/fapi/v1/marginType", {
                "symbol":     symbol.upper(),
                "marginType": margin_type,
            })
        except httpx.HTTPStatusError as e:
            # -4046: zaten bu modda → sorun değil
            if "-4046" in str(e) or "No need to change" in str(e):
                pass
            else:
                logger.warning(f"marginType: {e}")

    # ── Exchange info ─────────────────────────────────────────────────────────

    async def get_symbol_info(self, symbol: str) -> dict | None:
        try:
            url  = f"{self.base_url}/fapi/v1/exchangeInfo"
            r    = await self._client.get(url)
            r.raise_for_status()
            for s in r.json().get("symbols", []):
                if s["symbol"] == symbol.upper():
                    return s
        except Exception as e:
            logger.warning(f"exchangeInfo hatası: {e}")
        return None

    async def get_min_qty(self, symbol: str) -> float:
        info = await self.get_symbol_info(symbol)
        if not info:
            return 0.001
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["minQty"])
        return 0.001

    async def get_qty_precision(self, symbol: str) -> int:
        """Miktar için ondalık basamak sayısı."""
        info = await self.get_symbol_info(symbol)
        if not info:
            return 3
        return info.get("quantityPrecision", 3)

    # ── Orders ────────────────────────────────────────────────────────────────

    def _fmt_qty(self, qty: float, precision: int) -> str:
        return f"{qty:.{precision}f}"

    async def market_order(self, symbol: str, side: str, quantity: float,
                           precision: int = 3) -> dict:
        result = await self._post("/fapi/v1/order", {
            "symbol":   symbol.upper(),
            "side":     side.upper(),   # BUY | SELL
            "type":     "MARKET",
            "quantity": self._fmt_qty(quantity, precision),
        })
        oid = result.get("orderId", "?")
        avg = result.get("avgPrice", "?")
        logger.info(f"✅ MARKET {side.upper()} {quantity} {symbol} | orderId={oid} avgPrice={avg}")
        return result

    async def stop_market_order(self, symbol: str, side: str, quantity: float,
                                stop_price: float, precision: int = 3) -> dict:
        """Stop-loss emri — pozisyon kapatma (reduceOnly)."""
        # Stop fiyatını 2 ondalıkla yuvarla
        result = await self._post("/fapi/v1/order", {
            "symbol":      symbol.upper(),
            "side":        side.upper(),
            "type":        "STOP_MARKET",
            "quantity":    self._fmt_qty(quantity, precision),
            "stopPrice":   f"{stop_price:.2f}",
            "reduceOnly":  "true",
            "workingType": "MARK_PRICE",
        })
        logger.info(f"🛡 STOP {side.upper()} {quantity} @ {stop_price:.2f} | orderId={result.get('orderId')}")
        return result

    async def cancel_all_orders(self, symbol: str):
        try:
            result = await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
            logger.info(f"🗑 Tüm emirler iptal: {symbol}")
            return result
        except Exception as e:
            logger.warning(f"Emir iptal hatası (pozisyon zaten kapanmış olabilir): {e}")

    async def close_position_market(self, symbol: str, position_amt: float,
                                    precision: int = 3) -> dict:
        """Mevcut pozisyonu market fiyatından kapat."""
        side = "SELL" if position_amt > 0 else "BUY"
        qty  = abs(position_amt)
        result = await self._post("/fapi/v1/order", {
            "symbol":     symbol.upper(),
            "side":       side,
            "type":       "MARKET",
            "quantity":   self._fmt_qty(qty, precision),
            "reduceOnly": "true",
        })
        logger.info(f"🔒 Pozisyon kapatıldı: {symbol} {side} {qty:.{precision}f}")
        return result

    async def close(self):
        await self._client.aclose()
