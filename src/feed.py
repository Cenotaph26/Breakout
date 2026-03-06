"""Price Feed — Binance WS + Sim."""

import asyncio, json, logging, random, time
import websockets
from src.bot import Candle, Bot

log = logging.getLogger("feed")

TESTNET_WS = "wss://fstream.binancefuture.com/ws"
LIVE_WS    = "wss://fstream.binance.com/ws"


class BinanceFeed:
    def __init__(self, bot: Bot, demo=True):
        self.bot = bot
        self.demo = demo
        self._tasks = []

    def _ws(self): return TESTNET_WS if self.demo else LIVE_WS

    async def start(self):
        self._tasks = [
            asyncio.create_task(self._kline_loop()),
            asyncio.create_task(self._ticker_loop()),
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
            try: await t
            except: pass
        self._tasks = []

    async def _kline_loop(self):
        sym = self.bot.cfg.symbol.lower()
        url = f"{self._ws()}/{sym}@kline_1m"
        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                    log.info("Kline stream bağlandı")
                    async for raw in ws:
                        if not self.bot.running: break
                        try:
                            k = json.loads(raw).get("k", {})
                            # Her tick'te live candle güncelle
                            self.bot.live_candle = {
                                "o": float(k["o"]), "h": float(k["h"]),
                                "l": float(k["l"]), "c": float(k["c"]), "t": int(k["t"]),
                            }
                            self.bot.price = float(k["c"])
                            # Mum kapandıysa strateji tetikle
                            if k.get("x"):
                                c = Candle(o=float(k["o"]),h=float(k["h"]),l=float(k["l"]),c=float(k["c"]),v=float(k["v"]),t=int(k["t"]))
                                self.bot.live_candle = None
                                await self.bot.on_candle(c)
                        except Exception as e:
                            log.debug(f"Kline parse: {e}")
            except asyncio.CancelledError: raise
            except Exception as e:
                log.warning(f"Kline hata: {e}")
                await asyncio.sleep(3)

    async def _ticker_loop(self):
        sym = self.bot.cfg.symbol.lower()
        url = f"{self._ws()}/{sym}@miniTicker"
        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                    log.info("Ticker stream bağlandı")
                    async for raw in ws:
                        if not self.bot.running: break
                        try:
                            d = json.loads(raw)
                            if "c" in d:
                                await self.bot.on_tick(float(d["c"]))
                        except: pass
            except asyncio.CancelledError: raise
            except Exception as e:
                log.warning(f"Ticker hata: {e}")
                await asyncio.sleep(3)


class SimFeed:
    def __init__(self, bot: Bot, interval=2.0):
        self.bot = bot
        self.interval = interval
        self._task = None
        self._p = 67000.0; self._trend = 1; self._tc = 0; self._td = random.randint(6,15)
        self._vol = 0.0018; self._mom = 0.0

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task: self._task.cancel()
        try: await self._task
        except: pass

    async def _run(self):
        while self.bot.running:
            c = self._gen()
            self.bot.price = c.c
            await self.bot.on_candle(c)
            await asyncio.sleep(self.interval)

    def _gen(self) -> Candle:
        op = self._p; bias = 0.56 if self._trend > 0 else 0.44
        self._mom = self._mom * 0.7 + self._trend * 0.0003
        cp = hp = lp = op
        for _ in range(10):
            r = (random.random() - (1-bias)) * self._vol * 2 + self._mom
            cp *= (1+r); hp = max(hp,cp); lp = min(lp,cp)
        wick = abs(cp-op)*random.uniform(0.1,0.4)
        if self._trend > 0: hp = max(hp,cp+wick)
        else: lp = min(lp,cp-wick)
        self._tc += 1
        if self._tc >= self._td:
            self._trend *= -1; self._tc = 0; self._td = random.randint(5,20)
            self._mom = 0; self._vol = random.uniform(.002,.0035)
        else: self._vol = max(.0012, self._vol*.95)
        self._p = cp
        return Candle(o=round(op,2),h=round(max(op,hp),2),l=round(min(op,lp),2),c=round(cp,2),v=round(random.uniform(20,200),2),t=int(time.time()*1000))
