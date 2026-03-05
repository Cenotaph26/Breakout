"""
Binance Demo Order Executor
REST API üzerinden gerçek emir gönderir (Demo hesap).

Demo endpoint: https://testnet.binancefuture.com (USDT-M Futures testnet)
   veya        https://dapi.binancefuture.com (bazı dokümanlarda)

Binance Demo (paper trading) için:
  - https://testnet.binancefuture.com → USDT-M Futures Testnet
  - API key & secret → testnet.binancefuture.com üzerinden oluşturulur
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# Demo (testnet) base URL — gerçek para kullanmaz
DEMO_BASE = "https://testnet.binancefuture.com"
LIVE_BASE = "https://fapi.binance.com"


class BinanceExecutor:
    """
    USDT-M Futures için emir gönderici.
    demo=True → testnet.binancefuture.com
    demo=False → fapi.binance.com (gerçek para!)
    """

    def __init__(self, api_key: str, api_secret: str, demo: bool = True):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = DEMO_BASE if demo else LIVE_BASE
        self.demo       = demo
        self._client    = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=10.0,
        )

    def _sign(self, params: dict) -> dict:
        """HMAC-SHA256 imza ekle."""
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        sig = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    async def close(self):
        await self._client.aclose()

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_account(self) -> dict:
        """Hesap bakiyesi ve pozisyon bilgisi."""
        params = self._sign({})
        r = await self._client.get("/fapi/v2/account", params=params)
        r.raise_for_status()
        return r.json()

    async def get_position(self, symbol: str) -> dict | None:
        """Belirli bir sembol için açık pozisyon."""
        params = self._sign({"symbol": symbol.upper()})
        r = await self._client.get("/fapi/v2/positionRisk", params=params)
        r.raise_for_status()
        positions = r.json()
        for p in positions:
            if p["symbol"] == symbol.upper() and float(p["positionAmt"]) != 0:
                return p
        return None

    # ── Leverage & margin ─────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        params = self._sign({"symbol": symbol.upper(), "leverage": leverage})
        r = await self._client.post("/fapi/v1/leverage", params=params)
        r.raise_for_status()
        result = r.json()
        logger.info(f"Kaldıraç ayarlandı: {symbol} → {leverage}x")
        return result

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        """ISOLATED veya CROSSED marjin modu."""
        try:
            params = self._sign({"symbol": symbol.upper(), "marginType": margin_type})
            r = await self._client.post("/fapi/v1/marginType", params=params)
            # -4046 = "No need to change margin type" → kabul edilebilir
            if r.status_code not in (200, 400):
                r.raise_for_status()
        except Exception as e:
            logger.debug(f"Margin type set (may already be set): {e}")

    # ── Orders ────────────────────────────────────────────────────────────────

    async def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """
        Market emri gönder.
        side: 'BUY' | 'SELL'
        quantity: base asset miktarı (örn. 0.001 BTC)
        """
        params = self._sign({
            "symbol":   symbol.upper(),
            "side":     side.upper(),
            "type":     "MARKET",
            "quantity": f"{quantity:.6f}",
        })
        r = await self._client.post("/fapi/v1/order", params=params)
        if not r.is_success:
            logger.error(f"Market order hatası: {r.text}")
            r.raise_for_status()
        result = r.json()
        logger.info(f"✅ Market emir: {side.upper()} {quantity} {symbol} | orderId={result.get('orderId')}")
        return result

    async def stop_market_order(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> dict:
        """
        Stop-Market emri (stop-loss için).
        Pozisyon kapatma emridir: reduceOnly=true.
        """
        params = self._sign({
            "symbol":      symbol.upper(),
            "side":        side.upper(),
            "type":        "STOP_MARKET",
            "quantity":    f"{quantity:.6f}",
            "stopPrice":   f"{stop_price:.2f}",
            "reduceOnly":  "true",
            "workingType": "MARK_PRICE",
        })
        r = await self._client.post("/fapi/v1/order", params=params)
        if not r.is_success:
            logger.error(f"Stop order hatası: {r.text}")
            r.raise_for_status()
        result = r.json()
        logger.info(f"🛡 Stop emir: {side.upper()} {quantity} {symbol} @ {stop_price} | orderId={result.get('orderId')}")
        return result

    async def cancel_all_orders(self, symbol: str) -> dict:
        """Sembole ait tüm açık emirleri iptal et."""
        params = self._sign({"symbol": symbol.upper()})
        r = await self._client.delete("/fapi/v1/allOpenOrders", params=params)
        r.raise_for_status()
        logger.info(f"🗑 Tüm emirler iptal: {symbol}")
        return r.json()

    async def close_position_market(self, symbol: str, position_amt: float) -> dict:
        """
        Mevcut pozisyonu market fiyatından kapat.
        position_amt > 0 → LONG → SELL ile kapat
        position_amt < 0 → SHORT → BUY ile kapat
        """
        side     = "SELL" if position_amt > 0 else "BUY"
        quantity = abs(position_amt)
        params = self._sign({
            "symbol":     symbol.upper(),
            "side":       side,
            "type":       "MARKET",
            "quantity":   f"{quantity:.6f}",
            "reduceOnly": "true",
        })
        r = await self._client.post("/fapi/v1/order", params=params)
        if not r.is_success:
            logger.error(f"Pozisyon kapatma hatası: {r.text}")
            r.raise_for_status()
        result = r.json()
        logger.info(f"🔒 Pozisyon kapatıldı: {symbol} {side} {quantity}")
        return result

    # ── Exchange info ─────────────────────────────────────────────────────────

    async def get_symbol_info(self, symbol: str) -> dict | None:
        """Sembol lot size ve fiyat hassasiyeti."""
        r = await self._client.get("/fapi/v1/exchangeInfo")
        r.raise_for_status()
        for s in r.json().get("symbols", []):
            if s["symbol"] == symbol.upper():
                return s
        return None

    async def get_min_qty(self, symbol: str) -> float:
        """Minimum işlem miktarı (lot size)."""
        info = await self.get_symbol_info(symbol)
        if not info:
            return 0.001
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["minQty"])
        return 0.001
