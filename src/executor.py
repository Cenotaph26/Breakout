"""Binance USDT-M Futures Executor — Multi-symbol."""

import hashlib, hmac, logging, math, time
from urllib.parse import urlencode
import httpx

log = logging.getLogger("executor")
TESTNET = "https://testnet.binancefuture.com"
LIVE = "https://fapi.binance.com"


class Executor:
    def __init__(self, key, secret, demo=True):
        self.key = key.strip(); self.secret = secret.strip()
        self.base = TESTNET if demo else LIVE; self.demo = demo
        self._offset = 0
        self._filters = {}  # symbol -> {qty_step, qty_prec, price_step, price_prec}
        self._http = httpx.AsyncClient(timeout=15, headers={"X-MBX-APIKEY": self.key})

    async def sync_time(self):
        try:
            r = await self._http.get(f"{self.base}/fapi/v1/time"); r.raise_for_status()
            self._offset = r.json()["serverTime"] - int(time.time() * 1000)
        except: pass

    def _sign(self, p):
        p = dict(p); p["timestamp"] = int(time.time()*1000) + self._offset; p["recvWindow"] = 5000
        qs = urlencode(p); sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    async def _get(self, path, params=None):
        r = await self._http.get(f"{self.base}{path}?{self._sign(params or {})}")
        if not r.is_success: raise Exception(f"GET {path} → {r.status_code}: {r.text[:150]}")
        return r.json()

    async def _post(self, path, params=None):
        r = await self._http.post(f"{self.base}{path}?{self._sign(params or {})}")
        if not r.is_success: raise Exception(f"POST {path} → {r.status_code}: {r.text[:150]}")
        return r.json()

    async def _delete(self, path, params=None):
        r = await self._http.delete(f"{self.base}{path}?{self._sign(params or {})}")
        return r.json() if r.is_success else {}

    async def test(self):
        await self.sync_time()
        r = await self._get("/fapi/v2/account")
        log.info(f"Binance OK demo={self.demo}")
        return r

    async def get_balance(self):
        acc = await self._get("/fapi/v2/account")
        for a in acc.get("assets", []):
            if a["asset"] == "USDT": return float(a["availableBalance"])
        return 0.0

    async def get_position(self, sym):
        data = await self._get("/fapi/v2/positionRisk", {"symbol": sym})
        for p in (data if isinstance(data, list) else [data]):
            if p["symbol"] == sym and float(p["positionAmt"]) != 0: return p
        return None

    async def load_all_filters(self):
        """Tüm sembollerin filtrelerini yükle."""
        try:
            r = await self._http.get(f"{self.base}/fapi/v1/exchangeInfo"); r.raise_for_status()
            for s in r.json().get("symbols", []):
                sym = s["symbol"]
                f = {"qty_step": 0.001, "qty_prec": s.get("quantityPrecision", 3),
                     "price_step": 0.01, "price_prec": s.get("pricePrecision", 2)}
                for fl in s.get("filters", []):
                    if fl["filterType"] == "LOT_SIZE": f["qty_step"] = float(fl["stepSize"])
                    if fl["filterType"] == "PRICE_FILTER": f["price_step"] = float(fl["tickSize"])
                self._filters[sym] = f
            log.info(f"{len(self._filters)} sembol filtresi yüklendi")
        except Exception as e:
            log.error(f"Filtre yükleme: {e}")

    async def get_top_symbols(self, top_n=30, min_vol=5_000_000):
        """24h hacme göre en aktif USDT-M perpetual coinleri getir."""
        try:
            r = await self._http.get(f"{self.base}/fapi/v1/ticker/24hr"); r.raise_for_status()
            pairs = []
            for t in r.json():
                sym = t["symbol"]
                if not sym.endswith("USDT"): continue
                vol = float(t.get("quoteVolume", 0))
                if vol >= min_vol:
                    pairs.append((sym, vol))
            pairs.sort(key=lambda x: -x[1])
            result = [p[0] for p in pairs[:top_n]]
            log.info(f"Top {len(result)} coin seçildi (min vol={min_vol:,.0f})")
            return result
        except Exception as e:
            log.error(f"Top symbols: {e}")
            return ["BTCUSDT", "ETHUSDT"]

    def _rqty(self, sym, q):
        f = self._filters.get(sym, {"qty_step": 0.001, "qty_prec": 3})
        s = f["qty_step"]; p = f["qty_prec"]
        v = math.floor(q / s) * s
        return f"{round(v, p):.{p}f}"

    def _rprice(self, sym, price):
        f = self._filters.get(sym, {"price_step": 0.01, "price_prec": 2})
        s = f["price_step"]; p = f["price_prec"]
        v = round(round(price / s) * s, p)
        return f"{v:.{p}f}"

    async def set_leverage(self, sym, lev):
        return await self._post("/fapi/v1/leverage", {"symbol": sym, "leverage": lev})

    async def set_margin_type(self, sym, mt="ISOLATED"):
        try: await self._post("/fapi/v1/marginType", {"symbol": sym, "marginType": mt})
        except: pass

    async def klines(self, sym, limit=80):
        for base in [self.base, LIVE]:
            try:
                r = await self._http.get(f"{base}/fapi/v1/klines", params={"symbol": sym, "interval": "1m", "limit": limit})
                r.raise_for_status()
                return [{"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"v":float(k[5]),"t":int(k[0])} for k in r.json()]
            except: pass
        return []

    async def market_order(self, sym, side, qty):
        qs = self._rqty(sym, qty)
        log.info(f"MARKET {sym} {side} qty={qs}")
        return await self._post("/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":qs})

    async def stop_order(self, sym, side, qty, stop_price):
        qs = self._rqty(sym, qty); ps = self._rprice(sym, stop_price)
        if self.demo:
            slip = 1.005 if side == "BUY" else 0.995
            lp = self._rprice(sym, stop_price * slip)
            try:
                return await self._post("/fapi/v1/order", {
                    "symbol":sym,"side":side,"type":"STOP","quantity":qs,
                    "price":lp,"stopPrice":ps,"reduceOnly":"true",
                    "workingType":"MARK_PRICE","timeInForce":"GTC"})
            except: return {"orderId": 0}
        else:
            return await self._post("/fapi/v1/order", {
                "symbol":sym,"side":side,"type":"STOP_MARKET","quantity":qs,
                "stopPrice":ps,"reduceOnly":"true","workingType":"MARK_PRICE"})

    async def cancel_all_orders(self, sym):
        try: return await self._delete("/fapi/v1/allOpenOrders", {"symbol": sym})
        except: pass

    async def close_position(self, sym, amt):
        side = "SELL" if amt > 0 else "BUY"
        qs = self._rqty(sym, abs(amt))
        return await self._post("/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":qs,"reduceOnly":"true"})

    async def close(self):
        await self._http.aclose()
