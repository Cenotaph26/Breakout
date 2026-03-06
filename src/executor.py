"""Binance USDT-M Futures Executor — Testnet & Live."""

import hashlib, hmac, logging, math, time
from urllib.parse import urlencode
import httpx

log = logging.getLogger("executor")

TESTNET = "https://testnet.binancefuture.com"
LIVE    = "https://fapi.binance.com"


class Executor:
    def __init__(self, key: str, secret: str, demo: bool = True):
        self.key = key.strip()
        self.secret = secret.strip()
        self.base = TESTNET if demo else LIVE
        self.demo = demo
        self._offset = 0
        self._qty_step = 0.001; self._qty_prec = 3
        self._price_step = 0.10; self._price_prec = 2
        self._http = httpx.AsyncClient(timeout=15, headers={"X-MBX-APIKEY": self.key})

    # ── Signing ──────────────────────────────────────────
    async def sync_time(self):
        try:
            r = await self._http.get(f"{self.base}/fapi/v1/time"); r.raise_for_status()
            self._offset = r.json()["serverTime"] - int(time.time() * 1000)
        except Exception as e:
            log.warning(f"Time sync fail: {e}")

    def _sign(self, p: dict) -> str:
        p["timestamp"] = int(time.time()*1000) + self._offset
        p["recvWindow"] = 5000
        qs = urlencode(p)
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    async def _get(self, path, params={}):
        r = await self._http.get(f"{self.base}{path}?{self._sign(dict(params))}")
        self._check(r, path); return r.json()

    async def _post(self, path, params={}):
        r = await self._http.post(f"{self.base}{path}?{self._sign(dict(params))}")
        self._check(r, path); return r.json()

    async def _delete(self, path, params={}):
        r = await self._http.delete(f"{self.base}{path}?{self._sign(dict(params))}")
        self._check(r, path); return r.json()

    def _check(self, r, path):
        if not r.is_success:
            try: body = r.json()
            except: body = r.text[:200]
            log.warning(f"{path} → {r.status_code}: {body}")
            raise httpx.HTTPStatusError(f"{path} → {r.status_code}: {body}", request=r.request, response=r)

    # ── Account ──────────────────────────────────────────
    async def test(self):
        await self.sync_time()
        r = await self._get("/fapi/v2/account")
        log.info(f"Binance OK | demo={self.demo}")
        return r

    async def get_balance(self) -> float:
        acc = await self._get("/fapi/v2/account")
        for a in acc.get("assets", []):
            if a["asset"] == "USDT": return float(a["availableBalance"])
        return 0.0

    async def get_position(self, sym: str):
        data = await self._get("/fapi/v2/positionRisk", {"symbol": sym})
        lst = data if isinstance(data, list) else [data]
        for p in lst:
            if p["symbol"] == sym and float(p["positionAmt"]) != 0:
                return p
        return None

    # ── Filters ──────────────────────────────────────────
    async def load_filters(self, sym: str):
        try:
            r = await self._http.get(f"{self.base}/fapi/v1/exchangeInfo"); r.raise_for_status()
            for s in r.json().get("symbols", []):
                if s["symbol"] != sym: continue
                self._qty_prec = s.get("quantityPrecision", 3)
                self._price_prec = s.get("pricePrecision", 2)
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE": self._qty_step = float(f["stepSize"])
                    if f["filterType"] == "PRICE_FILTER": self._price_step = float(f["tickSize"])
                log.info(f"Filtreler: qty_step={self._qty_step} price_step={self._price_step}")
                return
        except Exception as e:
            log.warning(f"Filtre yükleme: {e}")

    def _rqty(self, q): 
        s = self._qty_step or 10**-self._qty_prec
        return f"{round(math.floor(q/s)*s, self._qty_prec):.{self._qty_prec}f}"
    
    def _rprice(self, p):
        s = self._price_step or 10**-self._price_prec
        return f"{round(round(p/s)*s, self._price_prec):.{self._price_prec}f}"

    # ── Setup ────────────────────────────────────────────
    async def set_leverage(self, sym, lev):
        return await self._post("/fapi/v1/leverage", {"symbol": sym, "leverage": lev})

    async def set_margin_type(self, sym, mt="ISOLATED"):
        try: await self._post("/fapi/v1/marginType", {"symbol": sym, "marginType": mt})
        except: pass  # already set

    # ── Klines ───────────────────────────────────────────
    async def klines(self, sym, limit=80):
        for base in [self.base, LIVE]:
            try:
                r = await self._http.get(f"{base}/fapi/v1/klines", params={"symbol": sym, "interval": "1m", "limit": limit})
                r.raise_for_status()
                return [{"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"v":float(k[5]),"t":int(k[0])} for k in r.json()]
            except Exception as e:
                log.warning(f"Kline {base}: {e}")
        return []

    # ── Orders ───────────────────────────────────────────
    async def market_order(self, sym, side, qty):
        qs = self._rqty(qty)
        log.info(f"MARKET {side} qty={qs}")
        return await self._post("/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":qs})

    async def stop_order(self, sym, side, qty, stop_price):
        """Testnet: STOP limit. Live: STOP_MARKET."""
        qs = self._rqty(qty)
        ps = self._rprice(stop_price)
        if self.demo:
            slip = 1.005 if side == "BUY" else 0.995
            lp = self._rprice(stop_price * slip)
            try:
                return await self._post("/fapi/v1/order", {
                    "symbol":sym,"side":side,"type":"STOP","quantity":qs,
                    "price":lp,"stopPrice":ps,"reduceOnly":"true",
                    "workingType":"MARK_PRICE","timeInForce":"GTC",
                })
            except Exception as e:
                log.warning(f"STOP emri başarısız: {e}")
                return {"orderId": 0}
        else:
            return await self._post("/fapi/v1/order", {
                "symbol":sym,"side":side,"type":"STOP_MARKET","quantity":qs,
                "stopPrice":ps,"reduceOnly":"true","workingType":"MARK_PRICE",
            })

    async def cancel_all_orders(self, sym):
        try: return await self._delete("/fapi/v1/allOpenOrders", {"symbol": sym})
        except: pass

    async def close_position(self, sym, amt):
        side = "SELL" if amt > 0 else "BUY"
        qs = self._rqty(abs(amt))
        return await self._post("/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":qs,"reduceOnly":"true"})

    async def close(self):
        await self._http.aclose()
